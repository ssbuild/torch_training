# -*- coding: utf-8 -*-
# @Author  : ssbuild
# @Time    : 2023/5/29 15:32
import torch
from torch import nn
from deep_training.models.models.transformer import TransformerForSeq2SeqLM

from ..auto.base_wapper import BaseModelWrapper
from ...utils.transformer_utils import hf_decorator
from ...weight.modelweighter import *
import logging
logger = logging.getLogger(__name__)

__all__ = [
    'RewardT5Model',
    'RewardTransformer'
]

class RewardT5Model(TransformerForSeq2SeqLM):
    def __init__(self, *args, **kwargs):
        super(RewardT5Model, self).__init__(*args, **kwargs)
        self.score = nn.Linear(self.config.hidden_size, self.config.num_labels)

    def enable_input_require_grads(self):
        # setattr(self.model, 'model_parallel', True)
        # setattr(self.model, 'is_parallelizable', True)
        # self.model.gradient_checkpointing_enable()
        self.model.enable_input_require_grads()

    def forward_value(self,**batch):
        back_module = self.model.lm_head
        self.model.lm_head = self.score
        state = self(**batch)[0]
        value = state
        self.model.lm_head = back_module
        return value.squeeze(-1)


    def forward_loss(self,chosen_ids: torch.Tensor, chosen_values: torch.Tensor,
                     rejected_ids: torch.Tensor, rejected_values: torch.Tensor):
        chosen_mean_scores = []
        rejected_mean_scores = []
        loss = 0.

        for i in range(chosen_ids.size(0)):
            chosen_id = chosen_ids[i]
            rejected_id = rejected_ids[i]
            chosen_value = chosen_values[i]
            rejected_value = rejected_values[i]

            # Check if there is any padding otherwise take length of sequence
            c_inds = (chosen_id == self.config.eos_token_id).nonzero()
            c_ind = c_inds[0].item() if len(c_inds) > 0 else chosen_id.shape[0]
            r_inds = (rejected_id == self.config.eos_token_id).nonzero()
            r_ind = r_inds[0].item() if len(r_inds) > 0 else rejected_id.shape[0]
            end_ind = max(c_ind, r_ind)

            # Retrieve first index where trajectories diverge
            divergence_ind = (chosen_id != rejected_id).nonzero()[0]
            assert divergence_ind > 0

            # Index into the correct rewards
            c_truncated_reward = chosen_value[divergence_ind:end_ind]
            r_truncated_reward = rejected_value[divergence_ind:end_ind]

            # Append the last rewards to the list of end scores
            chosen_mean_scores.append(c_truncated_reward[-1])
            rejected_mean_scores.append(r_truncated_reward[-1])

            # Compute loss based on truncated rewards (ignore padding)
            loss += -torch.log(torch.sigmoid(c_truncated_reward - r_truncated_reward)).mean()

        loss = loss / chosen_ids.size(0)
        chosen_mean_scores = torch.stack(chosen_mean_scores)
        rejected_mean_scores = torch.stack(rejected_mean_scores)
        return loss,chosen_mean_scores,rejected_mean_scores

    def forward_score(self,input_ids,values):
        bs = values.size(0)
        seq_len = input_ids.shape[1]
        chosen_mean_scores = [
        ]  # we use this name for consistency with the original forwad function
        for i in range(bs):
            input_id = input_ids[i]
            value = values[i]
            c_inds = (input_id == self.config.pad_token_id).nonzero()
            # here we only use the answer part of the sequence so we do not need to care about the padding at the beginning
            c_ind = c_inds[0].item() if len(c_inds) > 0 else seq_len
            chosen_mean_scores.append(value[c_ind - 1])
        return values,torch.stack(chosen_mean_scores)

    def forward_returns(self, **inputs):
        input_ids = inputs['decoder_input_ids']
        rewards = self.forward_value(**inputs)
        ends = torch.argmax((input_ids == self.config.eos_token_id).float(), dim=1).view(-1, 1)
        returns = torch.gather(rewards, 1, ends).squeeze(-1)
        return returns

    def compute_loss(self, *args, return_value_only=False, **batch) -> tuple:
        input_a, input_b = {}, {}
        for k, v in batch.items():
            i, k = (input_b, k[:-1]) if k.endswith('2') else (input_a, k)
            i[k] = v

        value_a = self.forward_value(**input_a)
        if len(input_b) > 0:
            value_b = self.forward_value(**input_b)
            loss, chosen_mean_scores, rejected_mean_scores = self.forward_loss(input_a["decoder_input_ids"], value_a,
                                                                               input_b["decoder_input_ids"], value_b)
            loss_dict = {
                "loss": loss,
                "chosen_mean_scores": chosen_mean_scores.mean(),
                "rejected_mean_scores": rejected_mean_scores.mean()
            }
            if self.training:
                return (loss_dict,)
            return (loss, value_a, value_b)

        if return_value_only:
            return (value_a,)
        scores = self.forward_score(batch["decoder_input_ids"], value_a)
        return (value_a, scores)




class RewardTransformer(RewardT5Model,ModelWeightMixin,BaseModelWrapper, with_pl=True):
    @hf_decorator
    def __init__(self, *args,new_num_tokens=None, **kwargs):
        lora_args: LoraConfig = kwargs.pop('lora_args', None)
        super(RewardTransformer, self).__init__(*args, **kwargs)
        self.lora_args = lora_args


        self.resize_token_embs(new_num_tokens, getattr(self, "pad_to_multiple_of", 128))
        self.inject_model()

    def get_model_lr(self, model=None, lr=None):
        # for n, p in self.named_parameters():
        #     print(n, p.requires_grad)
        lr = lr if lr is not None else self.config.task_specific_params['learning_rate']
        if self.lora_args is not None and self.lora_args.enable:
            return [(self.backbone, lr)]
        elif self.prompt_args and self.prompt_args.enable:
            return [(self.backbone, lr)]
        return super(RewardTransformer, self).get_model_lr(model, lr)

    def get_llm_model(self) -> PreTrainedModel:
        if self.lora_args is not None and self.lora_args.enable:
            return self.backbone.model.model
        elif self.prompt_args is not None and self.prompt_args.enable:
            # PromptModel 方法覆盖原来方法
            return self.backbone.model
        return self.backbone.model


    def forward_returns(self,*args,**kwargs):
        if self.lora_args is not None and self.lora_args.enable:
            model = self.backbone.model
        else:
            model = self.backbone
        return model.forward_returns(*args,**kwargs)