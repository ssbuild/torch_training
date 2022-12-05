# -*- coding: utf-8 -*-
# @Time    : 2022/12/5 9:27
import numpy as np
import math
import torch
from torch import nn
from .transformer import TransformerModel
from ..layers.handshakingkernel import HandshakingKernel
from ..losses.loss_tplinker import TplinkerLoss
__all__ = [
    'TransformerForTplinker'
]

def extract_spoes(outputs):
    ents: np.ndarray
    heads: np.ndarray
    tails: np.ndarray
    for ents, heads, tails in zip(outputs[0].argmax(-1),outputs[1].argmax(-1),outputs[2].argmax(-1)):
        for e in ents.nonzero():
            ...

        for h in heads.nonzero():
            ...

        for t in tails.nonzero():
            ...


class TransformerForTplinker(TransformerModel):
    def __init__(self,  *args, **kwargs):
        tplinker_args = kwargs.get('tplinker_args',None)
        shaking_type = tplinker_args.shaking_type if tplinker_args else None
        inner_enc_type = tplinker_args.inner_enc_type if tplinker_args else None
        super(TransformerForTplinker, self).__init__(*args, **kwargs)
        self.dropout = nn.Dropout(self.config.hidden_dropout_prob)
        self.handshakingkernel = HandshakingKernel(self.config.hidden_size,shaking_type,inner_enc_type)

        self.ent_fc = nn.Linear(self.config.hidden_size, 2)
        self.head_rel_fc_list = [nn.Linear(self.config.hidden_size, 3) for _ in range(self.config.num_labels)]
        self.tail_rel_fc_list = [nn.Linear(self.config.hidden_size, 3) for _ in range(self.config.num_labels)]

        for ind, fc in enumerate(self.head_rel_fc_list):
            self.register_parameter("weight_4_head_rel{}".format(ind), fc.weight)
            self.register_parameter("bias_4_head_rel{}".format(ind), fc.bias)
        for ind, fc in enumerate(self.tail_rel_fc_list):
            self.register_parameter("weight_4_tail_rel{}".format(ind), fc.weight)
            self.register_parameter("bias_4_tail_rel{}".format(ind), fc.bias)
        self.loss_fn = TplinkerLoss()

    def get_model_lr(self):
        return super(TransformerForTplinker, self).get_model_lr() + [
            (self.handshakingkernel, self.config.task_specific_params['learning_rate_for_task']),
            (self.ent_fc, self.config.task_specific_params['learning_rate_for_task']),
            (layer, self.config.task_specific_params['learning_rate_for_task'] for layer in self.head_rel_fc_list),
            (layer, self.config.task_specific_params['learning_rate_for_task'] for layer in self.tail_rel_fc_list),
        ]

    def compute_loss(self, batch):
        entity_labels: torch.Tensor = batch.pop('entity_labels', None)
        head_labels: torch.Tensor = batch.pop('head_labels', None)
        tail_labels: torch.Tensor = batch.pop('tail_labels', None)
        attention_mask = batch['attention_mask']
        outputs = self(**batch)
        logits = outputs[0]
        if self.model.training:
            logits = self.dropout(logits)
        shaking_hiddens = self.handshakingkernel(logits, attention_mask)
        shaking_hiddens4ent = shaking_hiddens
        shaking_hiddens4rel = shaking_hiddens

        # add distance embeddings if it is set
        if self.dist_emb_size != -1:
            # set self.dist_embbedings
            hidden_size = shaking_hiddens.size()[-1]
            if self.dist_embbedings is None:
                dist_emb = torch.zeros([self.dist_emb_size, hidden_size]).to(shaking_hiddens.device)
                for d in range(self.dist_emb_size):
                    for i in range(hidden_size):
                        if i % 2 == 0:
                            dist_emb[d][i] = math.sin(d / 10000 ** (i / hidden_size))
                        else:
                            dist_emb[d][i] = math.cos(d / 10000 ** ((i - 1) / hidden_size))
                seq_len = attention_mask.size()[1]
                dist_embbeding_segs = []
                for after_num in range(seq_len, 0, -1):
                    dist_embbeding_segs.append(dist_emb[:after_num, :])
                self.dist_embbedings = torch.cat(dist_embbeding_segs, dim=0)

            if self.ent_add_dist:
                shaking_hiddens4ent = shaking_hiddens + self.dist_embbedings[None, :, :].repeat(
                    shaking_hiddens.size()[0], 1, 1)
            if self.rel_add_dist:
                shaking_hiddens4rel = shaking_hiddens + self.dist_embbedings[None, :, :].repeat(
                    shaking_hiddens.size()[0], 1, 1)

        #         if self.dist_emb_size != -1 and self.ent_add_dist:
        #             shaking_hiddens4ent = shaking_hiddens + self.dist_embbedings[None,:,:].repeat(shaking_hiddens.size()[0], 1, 1)
        #         else:
        #             shaking_hiddens4ent = shaking_hiddens
        #         if self.dist_emb_size != -1 and self.rel_add_dist:
        #             shaking_hiddens4rel = shaking_hiddens + self.dist_embbedings[None,:,:].repeat(shaking_hiddens.size()[0], 1, 1)
        #         else:
        #             shaking_hiddens4rel = shaking_hiddens

        # b,s*(s+1)/2,3
        ent_shaking_outputs = self.ent_fc(shaking_hiddens4ent)

        head_rel_shaking_outputs_list = []
        for fc in self.head_rel_fc_list:
            head_rel_shaking_outputs_list.append(fc(shaking_hiddens4rel))

        tail_rel_shaking_outputs_list = []
        for fc in self.tail_rel_fc_list:
            tail_rel_shaking_outputs_list.append(fc(shaking_hiddens4rel))

        # b,t, s*(s+1)/2,3
        head_rel_shaking_outputs = torch.stack(head_rel_shaking_outputs_list, dim=1)
        # b,t, s*(s+1)/2,3
        tail_rel_shaking_outputs = torch.stack(tail_rel_shaking_outputs_list, dim=1)

        if entity_labels is not None:
            loss1 = self.loss_fn(ent_shaking_outputs, entity_labels)
            loss2 = self.loss_fn(head_rel_shaking_outputs, head_labels)
            loss3 = self.loss_fn(tail_rel_shaking_outputs, tail_labels)
            loss = (loss1 + loss2 + loss3) / 3
            loss_dict = {'loss': loss,
                         'loss_entities': loss1,
                         'loss_head': loss2,
                         'loss_tail': loss3}
            outputs = (loss_dict, ent_shaking_outputs, head_rel_shaking_outputs, tail_rel_shaking_outputs,
                       entity_labels, head_labels, tail_labels)
        else:
            outputs = (ent_shaking_outputs, head_rel_shaking_outputs, tail_rel_shaking_outputs)
        return outputs
