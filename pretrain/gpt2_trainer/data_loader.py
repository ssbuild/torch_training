# -*- coding: utf-8 -*-
# @Time    : 2022/11/4 13:31
# -*- coding: utf-8 -*-
# @Time    : 2022/11/4 13:31
import os
import json
import typing
import numpy as np
import torch
from asmodels.dataHelper import DataHelper
from transformers import BertTokenizer

class Gpt2_DataHelper(DataHelper):
    # 切分词
    def on_data_process(self, data_index: int, data: typing.Any, user_data: tuple):
        tokenizer: BertTokenizer
        tokenizer,max_seq_length = user_data
        x = data
        if isinstance(x, tuple):
            o = tokenizer(text=x[0], text_pair=x[1], max_length=max_seq_length, truncation=True,
                          add_special_tokens=True)
        else:
            o = tokenizer(x, max_length=max_seq_length, truncation=True, add_special_tokens=True, )

        input_ids = np.asarray(o['input_ids'], dtype=np.int64)
        attention_mask = np.asarray(o['attention_mask'], dtype=np.int64)
        token_type_ids = np.asarray(o['token_type_ids'], dtype=np.int64)

        seqlen = np.asarray(len(input_ids), dtype=np.int64)
        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            input_ids = np.pad(input_ids, (0, pad_len), 'constant', constant_values=(0, 0))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))
            token_type_ids = np.pad(token_type_ids, (0, pad_len), 'constant', constant_values=(0, 0))
        d = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': token_type_ids,
            'labels': input_ids,
            'seqlen': seqlen
        }
        return d


    # 读取文件
    @staticmethod
    def read_data_from_file(filename):
        D = []
        with open(filename, mode='r', encoding='utf-8') as f:
            string = f.read()
            jds = json.loads(string)
            for i,jd in enumerate(jds):
                D.append((jd['content'], jd['title']))

                if i > 1000:
                    break
        return D


    @staticmethod
    def collect_fn(batch):
        o = {}
        for i, b in enumerate(batch):
            if i == 0:
                for k in b:
                    o[k] = [torch.tensor(b[k])]
            else:
                for k in b:
                    o[k].append(torch.tensor(b[k]))
        for k in o:
            o[k] = torch.stack(o[k])

        seqlen = o.pop('seqlen')
        max_len = torch.max(seqlen)

        o['input_ids'] = o['input_ids'][:, :max_len]
        o['attention_mask'] = o['attention_mask'][:, :max_len]
        if 'token_type_ids' in o:
            o['token_type_ids'] = o['token_type_ids'][:, :max_len]
        o['labels'] = o['labels'][:, :max_len]
        return o


