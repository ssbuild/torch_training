# -*- coding: utf-8 -*-
import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)),'../..'))
import random
import torch
import logging
from torch.nn import CrossEntropyLoss
from pytorch_lightning import LightningDataModule, Trainer, seed_everything
from transformers import AdamW,get_linear_schedule_with_warmup
from deep_training.model.nlp.models.transformer import TransformerForMaskLM
from data_loader import MLM_DataHelper as DataHelper
from deep_training.data_helper.data_args_func import load_tokenizer_and_config_with_args, make_all_dataset_with_args, \
    load_all_dataset_with_args
from transformers import HfArgumentParser
from deep_training.data_helper.training_args import ModelArguments, TrainingArguments, DataArguments,MlmDataArguments


class MyTransformer(TransformerForMaskLM):
    def __init__(self,*args,**kwargs):
        super(MyTransformer, self).__init__(*args,**kwargs)
        self.loss_fct = CrossEntropyLoss(reduction='none',ignore_index=self.config.pad_token_id)

    def _compute_loss(self,y_trues,y_preds,weight):
        y_preds = torch.transpose(y_preds, 1, 2)
        loss = self.loss_fct(y_preds,y_trues)
        loss = loss * weight
        loss = torch.sum(loss, dtype=torch.float) / (torch.sum(weight, dtype=torch.float) + 1e-8)
        return loss

    def training_step(self, batch, batch_idx):
        weight = batch.pop('weight')
        labels = batch.pop('labels')
        outputs = self(**batch)
        logits = outputs[0]
        loss = self._compute_loss(labels,logits,weight)
        self.log('batch_idx',batch_idx,prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        weight = batch.pop('weight')
        labels = batch.pop('labels')
        outputs = self(**batch)
        # val_loss, logits = outputs[:2]
        logits = outputs[0]
        val_loss = self._compute_loss(labels, logits, weight)
        return {"losses": val_loss, "logits": logits, "labels": labels}

    def test_step(self, batch, batch_idx):
        weight = batch.pop('weight')
        if 'labels' in batch:
            batch.pop('labels')
        x, y = batch
        out = self(x)
        return out

if __name__== '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments, MlmDataArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        model_args, training_args, data_args, mlm_data_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, training_args, data_args, mlm_data_args = parser.parse_args_into_dataclasses()

    dataHelper = DataHelper(data_args.data_backend)
    tokenizer, config, label2id, id2label = load_tokenizer_and_config_with_args(dataHelper, model_args, training_args,data_args)
    rng = random.Random(training_args.seed)
    save_fn_args = (tokenizer, data_args.max_seq_length,
                    rng, mlm_data_args.do_whole_word_mask, mlm_data_args.max_predictions_per_seq,
                    mlm_data_args.masked_lm_prob)


    N = training_args.dupe_factor
    train_files,eval_files,test_files = [],[],[]
    for i in range(N):
        intermediate_name = data_args.intermediate_name + '_{}'.format(i)
        logging.info('make data {}...'.format(intermediate_name))
        train_file, eval_file, test_file = make_all_dataset_with_args(dataHelper, save_fn_args, data_args,intermediate_name=intermediate_name)
        train_files.append(train_file)
        eval_files.append(eval_file)
        test_files.append(test_file)

    print(train_files, eval_files, test_files)
    dm = load_all_dataset_with_args(dataHelper, training_args, train_files, eval_files, test_files)
    dm.setup("fit")
    model = MyTransformer(config=config,model_args=model_args,training_args=training_args)
    trainer = Trainer(
        # callbacks=[progress_bar],
        max_epochs=training_args.max_epochs,
        max_steps=training_args.max_steps,
        accelerator="gpu",
        devices=data_args.devices,  # limiting got iPython runs
        enable_progress_bar=True,
        default_root_dir=data_args.output_dir,
        gradient_clip_val=training_args.max_grad_norm,
        accumulate_grad_batches = training_args.gradient_accumulation_steps
    )

    if data_args.do_train:
        trainer.fit(model, datamodule=dm)

    if data_args.do_eval:
        trainer.validate(model, datamodule=dm)

    if data_args.do_test:
        trainer.test(model, datamodule=dm)