# -*- coding: utf-8 -*-
# @Time:  10:42
# @Author: tk
# @File：lora_wrapper
import os
from contextlib import contextmanager
import torch
from transformers.utils import PushToHubMixin
from nlp.layers.lora.lora_v2.utils import _set_trainable, _set_adapter
from nlp.models.lora.lora_v2.adalora_model import AdaLoraModel
from nlp.models.lora.lora_v2.configuration import LoraArgumentsV2, WEIGHTS_NAME, LoraArguments, AdaLoraArguments
from nlp.models.lora.lora_v2.lora_model import LoraModel
from nlp.models.lora.lora_v2.save_and_load import get_peft_model_state_dict, set_peft_model_state_dict


PEFT_TYPE_TO_MODEL_MAPPING = {
    "lora": LoraModel,
    "adalora": AdaLoraModel,
}

PEFT_TYPE_TO_CONFIG_MAPPING = {
    "lora": LoraArguments,
    "adalora": AdaLoraArguments,
}

class LoraModelV2(PushToHubMixin, torch.nn.Module):
    """
    Base model encompassing various Peft methods.

    Args:
        model ([`~transformers.PreTrainedModel`]): The base transformer model used for Peft.
        lora_config_v2 ([`LoraV2Arguments`]): The configuration of the Peft model.


    **Attributes**:
        - **base_model** ([`~transformers.PreTrainedModel`]) -- The base transformer model used for Peft.
        - **peft_config** ([`LoraV2Arguments`]) -- The configuration of the Peft model.
        - **modules_to_save** (`list` of `str`) -- The list of sub-module names to save when
        saving the model.
        - **prompt_encoder** ([`PromptEncoder`]) -- The prompt encoder used for Peft if
        using [`PromptLearningConfig`].
        - **prompt_tokens** (`torch.Tensor`) -- The virtual prompt tokens used for Peft if
        using [`PromptLearningConfig`].
        - **transformer_backbone_name** (`str`) -- The name of the transformer
        backbone in the base model if using [`PromptLearningConfig`].
        - **word_embeddings** (`torch.nn.Embedding`) -- The word embeddings of the transformer backbone
        in the base model if using [`PromptLearningConfig`].
    """

    def __init__(self, model, lora_config_v2: LoraArgumentsV2, adapter_name="default"):
        super().__init__()
        self.base_model = model
        self.config = self.base_model.config
        self.modules_to_save = None
        self.lora_config_v2 = {}
        self.active_adapter = adapter_name
        self.lora_type = lora_config_v2.lora_type
        self.base_model_torch_dtype = getattr(model, "dtype", None)
        self.lora_config_v2[adapter_name] = lora_config_v2
        self.base_model = PEFT_TYPE_TO_MODEL_MAPPING[lora_config_v2.lora_type](
            self.base_model, self.lora_config_v2, adapter_name
        )

    def save_pretrained(self, save_directory, **kwargs):
        r"""
        This function saves the adapter model and the adapter configuration files to a directory, so that it can be
        reloaded using the [`LoraModel.from_pretrained`] class method, and also used by the [`LoraModel.push_to_hub`]
        method.

        Args:
            save_directory (`str`):
                Directory where the adapter model and configuration files will be saved (will be created if it does not
                exist).
            kwargs (additional keyword arguments, *optional*):
                Additional keyword arguments passed along to the `push_to_hub` method.
        """
        if os.path.isfile(save_directory):
            raise ValueError(f"Provided path ({save_directory}) should be a directory, not a file")
        os.makedirs(save_directory, exist_ok=True)

        for adapter_name, peft_config in self.lora_config_v2.items():
            # save only the trainable weights
            output_state_dict = get_peft_model_state_dict(
                self, state_dict=kwargs.get("state_dict", None), adapter_name=adapter_name
            )
            output_dir = os.path.join(save_directory, adapter_name) if adapter_name != "default" else save_directory
            os.makedirs(output_dir, exist_ok=True)
            torch.save(output_state_dict, os.path.join(output_dir, WEIGHTS_NAME))

            # save the config and change the inference mode to `True`
            if peft_config.base_model_name_or_path is None:
                peft_config.base_model_name_or_path = (
                    self.base_model.model.__dict__.get("name_or_path", None)
                )
            inference_mode = peft_config.inference_mode
            peft_config.inference_mode = True
            peft_config.save_pretrained(output_dir)
            peft_config.inference_mode = inference_mode

    @classmethod
    def from_pretrained(cls, model, model_id, adapter_name="default", is_trainable=False, **kwargs):
        r"""
        Instantiate a [`LoraModel`] from a pretrained Lora configuration and weights.

        Args:
            model ([`~transformers.PreTrainedModel`]):
                The model to be adapted. The model should be initialized with the
                [`~transformers.PreTrainedModel.from_pretrained`] method from the 🤗 Transformers library.
            model_id (`str` or `os.PathLike`):
                The name of the Lora configuration to use. Can be either:
                    - A string, the `model id` of a Lora configuration hosted inside a model repo on the Hugging Face
                      Hub.
                    - A path to a directory containing a Lora configuration file saved using the `save_pretrained`
                      method (`./my_lora_config_directory/`).
        """

        # load the config
        config = PEFT_TYPE_TO_CONFIG_MAPPING[
            LoraArgumentsV2.from_pretrained(model_id, subfolder=kwargs.get("subfolder", None)).peft_type
        ].from_pretrained(model_id, subfolder=kwargs.get("subfolder", None))

        # if (getattr(model, "hf_device_map", None) is not None) and len(
        #     set(model.hf_device_map.values()).intersection({"cpu", "disk"})
        # ) > 0:
        #     remove_hook_from_submodules(model)


        config.inference_mode = not is_trainable
        model = cls(model, config, adapter_name)
        model.load_adapter(model_id, adapter_name, **kwargs)
        return model



    def print_trainable_parameters(self):
        """
        Prints the number of trainable parameters in the model.
        """
        trainable_params = 0
        all_param = 0
        for _, param in self.named_parameters():
            num_params = param.numel()
            # if using DS Zero 3 and the weights are initialized empty
            if num_params == 0 and hasattr(param, "ds_numel"):
                num_params = param.ds_numel

            all_param += num_params
            if param.requires_grad:
                trainable_params += num_params
        print(
            f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}"
        )

    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.base_model, name)

    def forward(self, *args, **kwargs):
        """
        Forward pass of the model.
        """
        return self.get_base_model()(*args, **kwargs)

    @contextmanager
    def disable_adapter(self):
        """
        Disables the adapter module.
        """

        self.base_model.disable_adapter_layers()
        yield
        self.base_model.enable_adapter_layers()

    def get_base_model(self):
        """
        Returns the base model.
        """
        return self.base_model.model

    def add_adapter(self, adapter_name, peft_config):
        if peft_config.lora_type != self.lora_type:
            raise ValueError(
                f"Cannot combine adapters with different peft types. "
                f"Found {self.lora_type} and {peft_config.lora_type}."
            )
        self.lora_config_v2[adapter_name] = peft_config

        self.base_model.add_adapter(adapter_name, peft_config)
        if getattr(peft_config, "modules_to_save", None) is not None:
            if self.modules_to_save is None:
                self.modules_to_save = set(peft_config.modules_to_save)
            else:
                self.modules_to_save = self.modules_to_save.update(peft_config.modules_to_save)
            _set_trainable(self, adapter_name)

    def load_adapter(self, model_id, adapter_name, is_trainable=False, **kwargs):
        if adapter_name not in self.lora_config_v2:
            # load the config
            peft_config = PEFT_TYPE_TO_CONFIG_MAPPING[
                LoraArgumentsV2.from_pretrained(model_id, subfolder=kwargs.get("subfolder", None)).lora_type
            ].from_pretrained(model_id, subfolder=kwargs.get("subfolder", None))

            peft_config.inference_mode = not is_trainable
            self.add_adapter(adapter_name, peft_config)

        # load weights if any
        path = os.path.join(model_id, kwargs["subfolder"]) if kwargs.get("subfolder", None) is not None else model_id

        if os.path.exists(os.path.join(path, WEIGHTS_NAME)):
            filename = os.path.join(path, WEIGHTS_NAME)
        else:
            raise ValueError(
                f"Can't find weights for {model_id} in {model_id} or in the Hugging Face Hub. "
                f"Please check that the file {WEIGHTS_NAME} is present at {model_id}."
            )

        adapters_weights = torch.load(
            filename, map_location=torch.device("cuda" if torch.cuda.is_available() else "cpu")
        )
        # load the weights into the model
        set_peft_model_state_dict(self, adapters_weights, adapter_name=adapter_name)



    def set_adapter(self, adapter_name):
        """
        Sets the active adapter.
        """
        if adapter_name not in self.lora_config_v2:
            raise ValueError(f"Adapter {adapter_name} not found.")
        self.active_adapter = adapter_name
        self.base_model.set_adapter(adapter_name)
        _set_adapter(self, adapter_name)

    @property
    def active_peft_config(self):
        return self.lora_config_v2[self.active_adapter]