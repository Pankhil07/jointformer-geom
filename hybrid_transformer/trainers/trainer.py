import math
import torch
from hybrid_transformer.configs.trainers.trainer import TrainerConfig

import os
import time
import pickle
from contextlib import nullcontext

import numpy as np
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group


class Trainer:

    def __init__(self, config: TrainerConfig, model: torch.nn, train_dataset, eval_dataset, tokenizer):

        # out dir
        self.out_dir = config.out_dir

        # eval
        self.eval_interval = config.eval_interval
        self.log_interval = config.log_interval
        self.eval_iters = config.eval_iters
        self.eval_only = config.eval_only  # if True, script exits right after the first eval

        # load / save
        self.always_save_checkpoint = config.always_save_checkpoint  # if True, always save a checkpoint after each eval
        self.init_from = config.init_from  # 'scratch' or 'resume' or 'gpt2*'

        # adamw optimizer
        self.learning_rate = config.learning_rate  # max learning rate
        self.max_iters = config.max_iters  # total number of training iterations
        self.weight_decay = config.weight_decay
        self.beta1 = config.beta1
        self.beta2 = config.beta2
        self.grad_clip = config.grad_clip  # clip gradients at this value, or disable if == 0.0

        # learning rate decay settings
        self.decay_lr = config.decay_lr  # whether to decay the learning rate
        self.warmup_iters = config.warmup_iters  # how many steps to warm up for
        self.lr_decay_iters = config.lr_decay_iters  # should be ~= max_iters per Chinchilla
        self.min_lr = config.min_lr  # minimum learning rate, should be ~= learning_rate/10 per Chinchilla

        # DDP settings
        self.ddp_enabled = config.ddp_enabled
        self.ddp_backend = config.ddp_backend  # 'nccl', 'gloo', etc.

        # runtime
        self.gradient_accumulation_steps = config.gradient_accumulation_steps  # used to simulate larger batch sizes
        self.batch_size = config.batch_size  # if gradient_accumulation_steps > 1, this is the micro-batch size
        self.device = config.device  # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
        self.dtype = config.dtype  # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
        self.compile = config.compile  # use PyTorch 2.0 to compile the model to be faster

        # data
        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset
        self.tokenizer = tokenizer

        # model
        self.model = model

        self._ddp()
        self._post_init()
        self._post_init_model()
        self._init_ckpt()

        # optimizer
        self.optimizer = model.configure_optimizers(
            self.weight_decay, self.learning_rate, (self.beta1, self.beta2), self.device)

        # scaler
        self.scaler = torch.cuda.amp.GradScaler(enabled=(self.dtype == 'float16'))

    def _ddp(self) -> None:

        self.ddp = int(os.environ.get('RANK', -1)) != -1
        if self.ddp and self.ddp_enabled:
            print("DDP enabled!")
            init_process_group(backend=self.ddp_backend)
            self.ddp_rank = int(os.environ['RANK'])
            self.ddp_local_rank = int(os.environ['LOCAL_RANK'])
            self.ddp_world_size = int(os.environ['WORLD_SIZE'])
            self.device = f'cuda:{self.ddp_local_rank}'
            torch.cuda.set_device(self.device)
            self.master_process = self.ddp_rank == 0  # this process will do logging, checkpointing etc.
            self.seed_offset = self.ddp_rank  # each process gets a different seed
            # world_size number of processes will be training simultaneously, so we can scale
            # down the desired gradient accumulation iterations per process proportionally
            assert self.gradient_accumulation_steps % self.ddp_world_size == 0
            self.gradient_accumulation_steps //= self.ddp_world_size
            self.model = DDP(self.model, device_ids=[self.ddp_local_rank])
        else:
            # if not ddp, we are running on a single gpu, and one process
            print("Running on a single device!")
            self.master_process = True
            self.seed_offset = 0
            self.ddp_world_size = 1
        tokens_per_iter = self.gradient_accumulation_steps * self.ddp_world_size * self.batch_size * self.model.max_seq_len
        print(f"tokens per iteration will be: {tokens_per_iter:,}")

    def _post_init(self):
        if self.master_process:
            os.makedirs(self.out_dir, exist_ok=True)
        torch.manual_seed(1337 + self.seed_offset)
        torch.backends.cuda.matmul.allow_tf32 = True  # allow tf32 on matmul
        torch.backends.cudnn.allow_tf32 = True  # allow tf32 on cudnn
        self.device_type = 'cuda' if 'cuda' in self.device else 'cpu'  # for later use in torch.autocast
        # note: float16 data type will automatically use a GradScaler
        self.ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[self.dtype]
        self.ctx = nullcontext() if self.device_type == 'cpu' else torch.amp.autocast(device_type=self.device_type, dtype=self.ptdtype)
        print(f"Using {self.device_type} device")

    def _post_init_model(self):
        # compile the model
        self.model.to(self.device)
        if self.compile:
            print("compiling the model... (takes a ~minute)")
            self.unoptimized_model = self.model
            model = torch.compile(self.model)  # requires PyTorch 2.0

        # wrap model into DDP container
        if self.ddp and self.ddp_enabled:
            self.model = DDP(self.model, device_ids=[self.ddp_local_rank])

    def _init_ckpt(self):
        self.ckpt = {
            'iter_num': 0,
            'best_val_loss': 1e9,
        }

    # learning rate decay scheduler (cosine with warmup)
    def _get_lr(self, it: int) -> float:
        # 1) linear warmup for warmup_iters steps
        if it < self.warmup_iters:
            return self.learning_rate * it / self.warmup_iters
        # 2) if it > lr_decay_iters, return min learning rate
        if it > self.lr_decay_iters:
            return self.min_lr
        # 3) in between, use cosine decay down to min learning rate
        decay_ratio = (it - self.warmup_iters) / (self.lr_decay_iters - self.warmup_iters)
        assert 0 <= decay_ratio <= 1
        coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
        return self.min_lr + coeff * (self.learning_rate - self.min_lr)

    def set_lr(self, iter_num: int) -> None:
        lr = self._get_lr(iter_num) if self.decay_lr else self.learning_rate
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = lr

    def get_batch(self, split, task):
        inputs = None
        if split == 'train':
            inputs = self.tokenizer.get_inputs(
                dataset=self.train_dataset, task=task, batch_size=self.batch_size, device=self.device)
        if split == 'val':
            inputs = self.tokenizer.get_inputs(
                dataset=self.eval_dataset, task=task, batch_size=self.batch_size, device=self.device)
        return inputs

    @torch.no_grad()
    def estimate_loss(self):
        out = {}
        self.model.eval()
        for split in ['train', 'val']:
            losses = torch.zeros(self.eval_iters)
            for k in range(self.eval_iters):
                inputs = self.get_batch(split, 'lm')
                with self.ctx:
                    outputs = self.model(
                        input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'], labels=inputs['labels'],
                        target=inputs['target'], eos_mask=inputs['eos_mask'])
                losses[k] = outputs['unsupervised_loss'].item()
            out[split] = losses.mean()

            valid = []
            for k in range(self.eval_iters):
                idx = torch.ones(size=(self.batch_size, 1), device=self.device) * self.tokenizer.generate_token_id
                idx = idx.long()
                samples = self.model.generate(idx=idx, max_new_tokens=self.tokenizer.max_molecule_length)
                valid.extend(self.tokenizer.is_valid_smiles(samples))
            out['valid'] = sum(valid) / len(valid)
        self.model.train()
        return out

    @classmethod
    def from_config(cls, config: TrainerConfig, model: torch.nn.Module) -> 'Trainer':
        return cls(config, model)
