# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings

import bitsandbytes as bnb
import torch

from .layer import LoraLayer,transpose, is_bnb_4bit_available, is_bnb_available


if is_bnb_available():

    class Linear8bitLt(bnb.nn.Linear8bitLt, LoraLayer):
        # Lora implemented in a dense layer
        def __init__(
            self,
            adapter_name,
            in_features,
            out_features,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.0,
            **kwargs,
        ) -> None:
            bnb.nn.Linear8bitLt.__init__(
                self,
                in_features,
                out_features,
                bias=kwargs.get("bias", True),
                has_fp16_weights=kwargs.get("has_fp16_weights", True),
                memory_efficient_backward=kwargs.get("memory_efficient_backward", False),
                threshold=kwargs.get("threshold", 0.0),
                index=kwargs.get("index", None),
            )
            LoraLayer.__init__(self, in_features=in_features, out_features=out_features)

            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
            init_lora_weights = kwargs.pop("init_lora_weights", True)
            self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
            self.set_adapter(adapter_name)

        def merge(self):
            if self.merged:
                warnings.warn(
                    f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                    f"You are now additionally merging {','.join(self.active_adapters)}."
                )
            for active_adapter in self.active_adapters:
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Merge lora module to 8-bit linear may get different generations due to rounding errors."
                )
                lora_data = self.get_delta_weight(active_adapter)

                if self.state.SCB is None:
                    self.state.SCB = self.weight.SCB
                # Dequantize the result of identity matrix and int8 weight because bitsandbytes does not support int8
                # dequantization directly
                im = torch.eye(self.weight.data.shape[-1]).contiguous().half().to(self.weight.device)
                im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
                im, Sim = bnb.functional.transform(im, "col32")

                if self.state.CxB is None:
                    self.state.CxB, self.state.SB = bnb.functional.transform(
                        self.weight.data, to_order=self.state.formatB
                    )
                out32, Sout32 = bnb.functional.igemmlt(im, self.state.CxB, Sim, self.state.SB)
                output = bnb.functional.mm_dequant(out32, Sout32, SCim, self.state.SCB, bias=None).t()
                w_data = output.to(lora_data.dtype).to(lora_data.device) + lora_data
                self.weight = bnb.nn.Int8Params(
                    w_data.to("cpu"), requires_grad=False, has_fp16_weights=self.weight.has_fp16_weights
                ).to(self.weight.device)
                self.state.reset_grads()
                self.merged_adapters.append(active_adapter)

        def unmerge(self):
            if not self.merged:
                warnings.warn("Already unmerged. Nothing to do.")
                return
            while len(self.merged_adapters) > 0:
                active_adapter = self.merged_adapters.pop()
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Unmerge lora module to 8-bit linear may get different generations due to rounding errors."
                )
                lora_data = self.get_delta_weight(active_adapter)

                if self.state.SCB is None:
                    self.state.SCB = self.weight.SCB
                im = torch.eye(self.weight.data.shape[-1]).contiguous().half().to(self.weight.device)
                im, imt, SCim, SCimt, coo_tensorim = bnb.functional.double_quant(im)
                im, Sim = bnb.functional.transform(im, "col32")

                if self.state.CxB is None:
                    self.state.CxB, self.state.SB = bnb.functional.transform(
                        self.weight.data, to_order=self.state.formatB
                    )
                out32, Sout32 = bnb.functional.igemmlt(im, self.state.CxB, Sim, self.state.SB)
                output = bnb.functional.mm_dequant(out32, Sout32, SCim, self.state.SCB, bias=None).t()
                w_data = output.to(lora_data.dtype).to(lora_data.device) - lora_data
                self.weight = bnb.nn.Int8Params(
                    w_data.to("cpu"), requires_grad=False, has_fp16_weights=self.weight.has_fp16_weights
                ).to(self.weight.device)
                self.state.reset_grads()

        def get_delta_weight(self, adapter):
            return (
                transpose(
                    self.lora_B[adapter].weight @ self.lora_A[adapter].weight,
                    False,
                )
                * self.scaling[adapter]
            )

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            if self.disable_adapters:
                if self.merged:
                    self.unmerge()
                result = super().forward(x)
            elif self.merged:
                result = super().forward(x)
            else:
                result = super().forward(x)
                for active_adapter in self.active_adapters:
                    if active_adapter not in self.lora_A.keys():
                        continue
                    lora_A = self.lora_A[active_adapter]
                    lora_B = self.lora_B[active_adapter]
                    dropout = self.lora_dropout[active_adapter]
                    scaling = self.scaling[active_adapter]

                    requires_conversion = not torch.is_autocast_enabled()
                    if requires_conversion:
                        expected_dtype = result.dtype
                        compute_dtype = lora_A.weight.dtype
                        if x.dtype != compute_dtype:
                            x = x.to(compute_dtype)
                    output = lora_B(lora_A(dropout(x)))
                    if requires_conversion:
                        output = output.to(expected_dtype)
                    output = output * scaling
                    result += output

            return result


if is_bnb_4bit_available():

    class Linear4bit(bnb.nn.Linear4bit, LoraLayer):
        # Lora implemented in a dense layer
        def __init__(
            self,
            adapter_name,
            in_features,
            out_features,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.0,
            **kwargs,
        ) -> None:
            bnb.nn.Linear4bit.__init__(
                self,
                in_features,
                out_features,
                bias=kwargs.get("bias", True),
                compute_dtype=kwargs.get("compute_dtype", torch.float32),
                compress_statistics=kwargs.get("compress_statistics", True),
                quant_type=kwargs.get("quant_type", "nf4"),
            )
            LoraLayer.__init__(self, in_features=in_features, out_features=out_features)

            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False

            init_lora_weights = kwargs.pop("init_lora_weights", True)
            self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
            self.set_adapter(adapter_name)

        def merge(self):
            if self.merged:
                warnings.warn(
                    f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                    f"You are now additionally merging {','.join(self.active_adapters)}."
                )
            for active_adapter in self.active_adapters:
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Merge lora module to 4-bit linear may get different generations due to rounding errors."
                )
                # Refer to https://gist.github.com/ChrisHayduk/1a53463331f52dca205e55982baf9930
                kwargs = self.weight.__dict__
                lora_data = self.get_delta_weight(active_adapter)
                w_data = bnb.functional.dequantize_4bit(self.weight.data, self.weight.quant_state) + lora_data
                self.weight = bnb.nn.Params4bit(w_data.to("cpu"), requires_grad=False, **kwargs).to(self.weight.device)
                self.merged_adapters.append(active_adapter)

        def unmerge(self):
            if not self.merged:
                warnings.warn("Already unmerged. Nothing to do.")
                return
            while len(self.merged_adapters) > 0:
                active_adapter = self.merged_adapters.pop()
                if active_adapter not in self.lora_A.keys():
                    continue
                warnings.warn(
                    "Unmerge lora module to 4-bit linear may get different generations due to rounding errors."
                )
                kwargs = self.weight.__dict__
                lora_data = self.get_delta_weight(active_adapter)
                w_data = bnb.functional.dequantize_4bit(self.weight.data, self.weight.quant_state) - lora_data
                self.weight = bnb.nn.Params4bit(w_data.to("cpu"), requires_grad=False, **kwargs).to(self.weight.device)

        def get_delta_weight(self, adapter):
            return (
                transpose(
                    self.lora_B[adapter].weight @ self.lora_A[adapter].weight,
                    False,
                )
                * self.scaling[adapter]
            )

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            if self.disable_adapters:
                if self.merged:
                    self.unmerge()
                result = super().forward(x)
            elif self.merged:
                result = super().forward(x)
            else:
                result = super().forward(x)
                # As per Tim Dettmers, for 4bit, we need to defensively clone here.
                # The reason is that in some cases, an error can occur that backprop
                # does not work on a manipulated view. This issue may be solved with
                # newer PyTorch versions but this would need extensive testing to be
                # sure.
                result = result.clone()

                for active_adapter in self.active_adapters:
                    if active_adapter not in self.lora_A.keys():
                        continue
                    lora_A = self.lora_A[active_adapter]
                    lora_B = self.lora_B[active_adapter]
                    dropout = self.lora_dropout[active_adapter]
                    scaling = self.scaling[active_adapter]

                    requires_conversion = not torch.is_autocast_enabled()
                    if requires_conversion:
                        expected_dtype = result.dtype
                        x = x.to(lora_A.weight.dtype)

                    output = lora_B(lora_A(dropout(x)))
                    if requires_conversion:
                        output = output.to(expected_dtype)
                    output = output * scaling
                    result += output

            return result
