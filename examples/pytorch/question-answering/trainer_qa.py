# coding=utf-8
# Copyright 2020 The HuggingFace Team All rights reserved.
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
"""
A subclass of `Trainer` specific to Question-Answering tasks
"""
import torch
import sys
if "--use_ipex" in sys.argv:
    import intel_extension_for_pytorch as ipex
else:
    from torch.utils import mkldnn as mkldnn_utils

from transformers import Trainer, is_torch_tpu_available
from transformers.trainer_utils import PredictionOutput


if is_torch_tpu_available():
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met


class QuestionAnsweringTrainer(Trainer):
    def __init__(self, *args, eval_examples=None, post_process_function=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.eval_examples = eval_examples
        self.post_process_function = post_process_function

    def evaluate(self, profile= False, use_ipex=None, bf16=None, jit_mode=None, max_seq_length=None, eval_dataset=None, eval_examples=None, ignore_keys=None, metric_key_prefix: str = "eval"):
        eval_dataset = self.eval_dataset if eval_dataset is None else eval_dataset
        eval_dataloader = self.get_eval_dataloader(eval_dataset)
        eval_examples = self.eval_examples if eval_examples is None else eval_examples

        # Temporarily disable metric computation, we will do it in the loop here.
        compute_metrics = self.compute_metrics
        self.compute_metrics = None
        eval_loop = self.prediction_loop if self.args.use_legacy_prediction_loop else self.evaluation_loop
        self.model.eval()
        if jit_mode:
            jit_inputs=()
            for _,batch in enumerate(eval_dataloader):
                for _,label in enumerate(batch):
                    if (batch[label].dim()) >=4:
                        dumpy_tensor = torch.ones((batch[label].shape), dtype=torch.long).to(memory_format=torch.channels_last)
                    else:
                        dumpy_tensor = torch.ones((batch[label].shape), dtype=torch.long)
                    L1=list(jit_inputs)
                    L1.append(dumpy_tensor)
                    jit_inputs=tuple(L1)
                break
            # if use_ipex:
            #     from intel_extension_for_pytorch.quantization import prepare, convert
            #     from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
            #     qconfig = QConfig(activation=MinMaxObserver.with_args(qscheme=torch.per_tensor_affine, dtype=torch.quint8), weight=PerChannelMinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_channel_symmetric))
            #     self.model = prepare(self.model, qconfig, example_inputs=jit_inputs, inplace=False)
            #     self.model.load_qconf_summary(qconf_summary = "./configure.json")
            #     if bf16:
            #         # self.model = ipex.optimize(self.model.to(memory_format=torch.channels_last), dtype=torch.bfloat16, level="O1")
            #         with torch.cpu.amp.autocast(), torch.no_grad():
            #             self.model = convert(self.model)
            #             self.model = torch.jit.trace(self.model, jit_inputs, strict=False)
            #         self.model = torch.jit.freeze(self.model)
            #     else:
            #         # self.model = ipex.optimize(self.model.to(memory_format=torch.channels_last), dtype=torch.float32, level="O1")
            #         with torch.no_grad():
            #             self.model = torch.jit.trace(self.model, jit_inputs, strict=False)
            #         self.model = torch.jit.freeze(self.model)
            # else:
            #     if bf16:
            #         with torch.cpu.amp.autocast(), torch.no_grad():
            #             self.model = torch.jit.trace(self.model.to(memory_format=torch.channels_last), jit_inputs, strict=False)
            #         self.model = torch.jit.freeze(self.model)
            #         with torch.no_grad():
            #             for _,batch in enumerate(eval_dataloader):
            #                 for _,label in enumerate(batch):
            #                     if batch[label].dim() >=4:
            #                         batch[label]=batch[label].to(memory_format=torch.channels_last)
            #     else:
            #         with torch.no_grad():
            #             self.model = torch.jit.trace(self.model.to(memory_format=torch.channels_last), jit_inputs, strict=False)
            #         self.model = torch.jit.freeze(self.model)
            #         with torch.no_grad():
            #             for _,batch in enumerate(eval_dataloader):
            #                 for _,label in enumerate(batch):
            #                     if batch[label].dim() >=4:
            #                         batch[label]=batch[label].to(memory_format=torch.channels_last)
            import intel_extension_for_pytorch as ipex_
            from intel_extension_for_pytorch.quantization import prepare
            from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
            qconfig = QConfig(activation=MinMaxObserver.with_args(qscheme=torch.per_tensor_affine, dtype=torch.quint8), weight=PerChannelMinMaxObserver.with_args(dtype=torch.qint8, qscheme=torch.per_channel_symmetric))
            ipex_.nn.utils._model_convert.replace_dropout_with_identity(self.model)
            self.model = prepare(self.model, qconfig, example_inputs=jit_inputs, inplace=False)
            output = eval_loop(
                eval_dataloader,
                description="Evaluation",
                # No point gathering the predictions if there are no metrics, otherwise we defer to
                # self.args.prediction_loss_only
                prediction_loss_only=True if compute_metrics is None else None,
                ignore_keys=ignore_keys,
            )
            self.model.save_qconf_summary(qconf_summary = "./configure.json")
            exit()
        else:
            if use_ipex:
                if bf16:
                    self.model = ipex.optimize(self.model.to(memory_format=torch.channels_last), dtype=torch.bfloat16, level="O1")
                else:
                    self.model = ipex.optimize(self.model.to(memory_format=torch.channels_last), dtype=torch.float32, level="O1")
            else:
                if bf16:
                    for _,batch in enumerate(eval_dataloader):
                        for _,label in enumerate(batch):
                            batch[label]=batch[label].to(torch.bfloat16)
                    self.model = mkldnn_utils.to_mkldnn(self.model, dtype=torch.bfloat16)
                else:
                    self.model = mkldnn_utils.to_mkldnn(self.model)

        with torch.autograd.profiler.profile(
            enabled=profile,
            use_cuda=False,
            record_shapes=False,
            with_flops=False,
        ) as prof:
            if bf16:
                if use_ipex:
                    with torch.cpu.amp.autocast(), torch.no_grad():
                        for _,batch in enumerate(eval_dataloader):
                            for _,label in enumerate(batch):
                                if batch[label].dim() >=4:
                                    batch[label]=batch[label].to(memory_format=torch.channels_last)
                    if jit_mode:
                        try:
                            output = eval_loop(
                                eval_dataloader,
                                description="Evaluation",
                                # No point gathering the predictions if there are no metrics, otherwise we defer to
                                # self.args.prediction_loss_only
                                prediction_loss_only=True if compute_metrics is None else None,
                                ignore_keys=ignore_keys,
                            )
                        finally:
                            self.compute_metrics = compute_metrics
                    else:
                        with torch.cpu.amp.autocast():
                            try:
                                output = eval_loop(
                                    eval_dataloader,
                                    description="Evaluation",
                                    # No point gathering the predictions if there are no metrics, otherwise we defer to
                                    # self.args.prediction_loss_only
                                    prediction_loss_only=True if compute_metrics is None else None,
                                    ignore_keys=ignore_keys,
                                )
                            finally:
                                self.compute_metrics = compute_metrics
                else:
                    if jit_mode:
                        try:
                            output = eval_loop(
                                eval_dataloader,
                                description="Evaluation",
                                # No point gathering the predictions if there are no metrics, otherwise we defer to
                                # self.args.prediction_loss_only
                                prediction_loss_only=True if compute_metrics is None else None,
                                ignore_keys=ignore_keys,
                            )
                        finally:
                            self.compute_metrics = compute_metrics
                    else:
                        with torch.cpu.amp.autocast():
                            try:
                                output = eval_loop(
                                    eval_dataloader,
                                    description="Evaluation",
                                    # No point gathering the predictions if there are no metrics, otherwise we defer to
                                    # self.args.prediction_loss_only
                                    prediction_loss_only=True if compute_metrics is None else None,
                                    ignore_keys=ignore_keys,
                                )
                            finally:
                                self.compute_metrics = compute_metrics
            else:
                if use_ipex:
                    with torch.no_grad():
                        for _,batch in enumerate(eval_dataloader):
                            for _,label in enumerate(batch):
                                if batch[label].dim() >=4:
                                    batch[label]=batch[label].to(memory_format=torch.channels_last)
                    try:
                        output = eval_loop(
                            eval_dataloader,
                            description="Evaluation",
                            # No point gathering the predictions if there are no metrics, otherwise we defer to
                            # self.args.prediction_loss_only
                            prediction_loss_only=True if compute_metrics is None else None,
                            ignore_keys=ignore_keys,
                        )
                    finally:
                        self.compute_metrics = compute_metrics
                else:
                    try:
                        output = eval_loop(
                            eval_dataloader,
                            description="Evaluation",
                            # No point gathering the predictions if there are no metrics, otherwise we defer to
                            # self.args.prediction_loss_only
                            prediction_loss_only=True if compute_metrics is None else None,
                            ignore_keys=ignore_keys,
                        )
                    finally:
                        self.compute_metrics = compute_metrics
        if profile:
            print(prof.key_averages().table(sort_by="self_cpu_time_total"))

        if self.post_process_function is not None and self.compute_metrics is not None:
            eval_preds = self.post_process_function(eval_examples, eval_dataset, output.predictions)
            metrics = self.compute_metrics(eval_preds)

            # Prefix all keys with metric_key_prefix + '_'
            for key in list(metrics.keys()):
                if not key.startswith(f"{metric_key_prefix}_"):
                    metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

            self.log(metrics)
        else:
            metrics = {}

        if self.args.tpu_metrics_debug or self.args.debug:
            # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
            xm.master_print(met.metrics_report())

        self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, metrics)
        return metrics

    def predict(self, predict_dataset, predict_examples, ignore_keys=None, metric_key_prefix: str = "test"):
        predict_dataloader = self.get_test_dataloader(predict_dataset)

        # Temporarily disable metric computation, we will do it in the loop here.
        compute_metrics = self.compute_metrics
        self.compute_metrics = None
        eval_loop = self.prediction_loop if self.args.use_legacy_prediction_loop else self.evaluation_loop
        try:
            output = eval_loop(
                predict_dataloader,
                description="Prediction",
                # No point gathering the predictions if there are no metrics, otherwise we defer to
                # self.args.prediction_loss_only
                prediction_loss_only=True if compute_metrics is None else None,
                ignore_keys=ignore_keys,
            )
        finally:
            self.compute_metrics = compute_metrics

        if self.post_process_function is None or self.compute_metrics is None:
            return output

        predictions = self.post_process_function(predict_examples, predict_dataset, output.predictions, "predict")
        metrics = self.compute_metrics(predictions)

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return PredictionOutput(predictions=predictions.predictions, label_ids=