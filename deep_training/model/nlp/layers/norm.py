# @Time    : 2022/11/12 21:54
# @Author  : tk
# @FileName: norm.py
import torch
from torch import nn


class LayerNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-12, conditional_size=None, weight=True, bias=True, norm_mode='normal',
                 **kwargs):
        """layernorm 层，这里自行实现
           条件layernorm来自于苏剑林的想法，详情：https://spaces.ac.cn/archives/7124
        """
        super(LayerNorm, self).__init__()

        if weight:
            self.weight = nn.Parameter(torch.ones(hidden_size))
        if bias:
            self.bias = nn.Parameter(torch.zeros(hidden_size))
        self.norm_mode = norm_mode

        self.eps = eps
        self.conditional_size = conditional_size
        if conditional_size:
            self.dense1 = nn.Linear(conditional_size, hidden_size, bias=False)
            self.dense1.weight.data.uniform_(0, 0)
            self.dense2 = nn.Linear(conditional_size, hidden_size, bias=False)
            self.dense2.weight.data.uniform_(0, 0)

    def forward(self, x):
        inputs = x[0]

        if self.norm_mode == 'rmsnorm':
            variance = inputs.to(torch.float32).pow(2).mean(-1, keepdim=True)
            o = inputs * torch.rsqrt(variance + self.eps)
        else:
            u = inputs.mean(-1, keepdim=True)
            s = (inputs - u).pow(2).mean(-1, keepdim=True)
            o = (inputs - u) / torch.sqrt(s + self.eps)

        if not hasattr(self, 'weight'):
            self.weight = 1
        if not hasattr(self, 'bias'):
            self.bias = 0

        if self.conditional_size:
            cond = x[1]
            for _ in range(len(inputs.shape) - len(cond.shape)):
                cond = cond.unsqueeze(dim=1)
            return (self.weight + self.dense1(cond)) * o + (self.bias + self.dense2(cond))
        else:
            return self.weight * o + self.bias

