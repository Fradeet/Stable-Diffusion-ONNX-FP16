# Copyright 2022 Dirk Moerenhout. All rights reserved.
#
# This program is free software: you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not,
# see <https://www.gnu.org/licenses/>.
#
# *****
# NOTE this was originally derived from:
# https://github.com/huggingface/diffusers/blob/main/scripts/convert_stable_diffusion_checkpoint_to_onnx.py
#
# Original file released under Apache License, Version 2.0
# *****
#
# Version history
# v1.2 First fully working version converting unet to fp16
# v2.0 Refactored + enabled conversion to fp16 for Text Encoder
# v2.1 Support for safetensors
# v2.2 Reduce visible warnings
# v3.0 You can now provide an alternative VAE
# v3.1 Align with diffusers 0.12.0
# v4.0 Support ckpt conversion (--> renamed to conv_sd_to_onnx.py)
# v5.0 Use ONNX Runtime Transformers for model optimisation
# v6.0 Support ControlNet
# v6.1 Support for diffusers 0.15.0
# v7.0 Support for diffusers 0.16.0 and torch 2.1

import warnings
import argparse
import os
import shutil
from pathlib import Path
import json
import tempfile
from typing import Union, Optional, Tuple

import torch
from torch.onnx import export
import safetensors

import onnx
from onnxruntime.transformers.float16 import convert_float_to_float16
from diffusers.models import AutoencoderKL
from diffusers import (
    OnnxRuntimeModel,
    OnnxStableDiffusionPipeline,
    StableDiffusionPipeline,
    ControlNetModel,
    UNet2DConditionModel
)
from diffusers.models.unet_2d_condition import UNet2DConditionOutput
from diffusers.pipelines.stable_diffusion.convert_from_ckpt import download_from_original_stable_diffusion_ckpt

# To improve future development and testing, warnings should be limited to what is somewhat useful
# Truncation warnings are expected as part of FP16 conversion and should not be shown
warnings.filterwarnings('ignore','.*will be truncated.*')
# We are ignoring prim::Constant type related warnings
warnings.filterwarnings('ignore','.*The shape inference of prim::Constant type is missing.*')

# ONNX Runtime Transformers offers ONNX model optimisation
# It does not directly support DirectML but we can use a custom class
# Based on onnx_model_unet.py in ONNX Runtime Transformers
from onnx import ModelProto
from onnxruntime.transformers.onnx_model_unet import UnetOnnxModel

class UnetOnnxModelDML(UnetOnnxModel):
    def __init__(self, model: ModelProto, num_heads: int = 0, hidden_size: int = 0):
        """Initialize UNet ONNX Model.

        Args:
            model (ModelProto): the ONNX model
            num_heads (int, optional): number of attention heads. Defaults to 0 (detect the parameter automatically).
            hidden_size (int, optional): hidden dimension. Defaults to 0 (detect the parameter automatically).
        """
        assert (num_heads == 0 and hidden_size == 0) or (num_heads > 0 and hidden_size % num_heads == 0)

        super().__init__(model, num_heads=num_heads, hidden_size=hidden_size)

    def optimize(self, enable_shape_inference=False):
        if not enable_shape_inference:
            self.disable_shape_inference()
        self.fuse_layer_norm()
        self.preprocess()
        self.postprocess()

# We need a wrapper for UNet2DConditionModel as we need to pass tuples
# We can't properly export tuples of Tensors with ONNX

class UNet2DConditionModel_Cnet(UNet2DConditionModel):
    def forward(
        self,
        sample: torch.FloatTensor,
        timestep: Union[torch.Tensor, float, int],
        encoder_hidden_states: torch.Tensor,
        down_block_add_res00: Optional[torch.Tensor] = None,
        down_block_add_res01: Optional[torch.Tensor] = None,
        down_block_add_res02: Optional[torch.Tensor] = None,
        down_block_add_res03: Optional[torch.Tensor] = None,
        down_block_add_res04: Optional[torch.Tensor] = None,
        down_block_add_res05: Optional[torch.Tensor] = None,
        down_block_add_res06: Optional[torch.Tensor] = None,
        down_block_add_res07: Optional[torch.Tensor] = None,
        down_block_add_res08: Optional[torch.Tensor] = None,
        down_block_add_res09: Optional[torch.Tensor] = None,
        down_block_add_res10: Optional[torch.Tensor] = None,
        down_block_add_res11: Optional[torch.Tensor] = None,
        mid_block_additional_residual: Optional[torch.Tensor] = None,
        return_dict: bool = False,
    ) -> Union[UNet2DConditionOutput, Tuple]:
        down_block_add_res = (
            down_block_add_res00, down_block_add_res01, down_block_add_res02,
            down_block_add_res03, down_block_add_res04, down_block_add_res05,
            down_block_add_res06, down_block_add_res07, down_block_add_res08,
            down_block_add_res09, down_block_add_res10, down_block_add_res11)
        return super().forward(
            sample = sample,
            timestep = timestep,
            encoder_hidden_states = encoder_hidden_states,
            down_block_additional_residuals = down_block_add_res,
            mid_block_additional_residual = mid_block_additional_residual,
            return_dict = return_dict
        )

def onnx_export(
    model,
    model_args: tuple,
    output_path: Path,
    ordered_input_names,
    output_names,
    dynamic_axes,
    opset,
):
    '''export a PyTorch model as an ONNX model'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export(
        model,
        model_args,
        f=output_path.as_posix(),
        input_names=ordered_input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        do_constant_folding=True,
        opset_version=opset,
    )

@torch.no_grad()
def convert_to_fp16(
    model_path
):
    '''Converts an ONNX model on disk to FP16'''
    model_dir=os.path.dirname(model_path)
    # Breaking down in steps due to Windows bug in convert_float_to_float16_model_path
    onnx.shape_inference.infer_shapes_path(model_path)
    fp16_model = onnx.load(model_path)
    fp16_model = convert_float_to_float16(
        fp16_model, keep_io_types=True, disable_shape_infer=True
    )
    # clean up existing tensor files
    shutil.rmtree(model_dir)
    os.mkdir(model_dir)
    # save FP16 model
    onnx.save(fp16_model, model_path)

@torch.no_grad()
def convert_models(pipeline: StableDiffusionPipeline,
                                        output_path: str,
                                        opset: int,
                                        fp16: bool,
                                        notune: bool,
                                        controlnet_path: str,
                                        attention_slicing: str):
    '''Converts the individual models in a path (UNET, VAE ...) to ONNX'''

    output_path = Path(output_path)

    # TEXT ENCODER
    num_tokens = pipeline.text_encoder.config.max_position_embeddings
    text_hidden_size = pipeline.text_encoder.config.hidden_size
    text_input = pipeline.tokenizer(
        "A sample prompt",
        padding="max_length",
        max_length=pipeline.tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    textenc_path=output_path / "text_encoder" / "model.onnx"
    onnx_export(
        pipeline.text_encoder,
        # casting to torch.int32 https://github.com/huggingface/transformers/pull/18515/files
        model_args=(text_input.input_ids.to(device=device, dtype=torch.int32)),
        output_path=textenc_path,
        ordered_input_names=["input_ids"],
        output_names=["last_hidden_state", "pooler_output"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
        },
        opset=opset,
    )
    if fp16:
        textenc_model_path = str(textenc_path.absolute().as_posix())
        convert_to_fp16(textenc_model_path)

    # UNET
    unet_in_channels = pipeline.unet.config.in_channels
    unet_sample_size = pipeline.unet.config.sample_size
    unet_path = output_path / "unet" / "model.onnx"
    if controlnet_path:
        # reload UNET to get an ONNX exportable version with ControlNet support
        with tempfile.TemporaryDirectory() as tmpdirname:
            pl.unet.save_pretrained(tmpdirname)
            controlnet_unet=UNet2DConditionModel_Cnet.from_pretrained(tmpdirname,
                low_cpu_mem_usage=False)

        if attention_slicing:
            pl.enable_attention_slicing(attention_slicing)
            controlnet_unet.set_attention_slice(attention_slicing)

        onnx_export(
            controlnet_unet,
            model_args=(
                torch.randn(2, unet_in_channels, unet_sample_size,
                    unet_sample_size).to(device=device, dtype=dtype),
                torch.randn(2).to(device=device, dtype=dtype),
                torch.randn(2, num_tokens, text_hidden_size).to(device=device, dtype=dtype),
                torch.randn(2, 320, unet_sample_size, unet_sample_size).to(device=device, dtype=dtype),
                torch.randn(2, 320, unet_sample_size, unet_sample_size).to(device=device, dtype=dtype),
                torch.randn(2, 320, unet_sample_size, unet_sample_size).to(device=device, dtype=dtype),
                torch.randn(2, 320, unet_sample_size//2,unet_sample_size//2).to(device=device, dtype=dtype),
                torch.randn(2, 640, unet_sample_size//2,unet_sample_size//2).to(device=device, dtype=dtype),
                torch.randn(2, 640, unet_sample_size//2,unet_sample_size//2).to(device=device, dtype=dtype),
                torch.randn(2, 640, unet_sample_size//4,unet_sample_size//4).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//4,unet_sample_size//4).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//4,unet_sample_size//4).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//8,unet_sample_size//8).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//8,unet_sample_size//8).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//8,unet_sample_size//8).to(device=device, dtype=dtype),
                torch.randn(2, 1280, unet_sample_size//8,unet_sample_size//8).to(device=device, dtype=dtype),
                False,
            ),
            output_path=unet_path,
            ordered_input_names=[
                "sample",
                "timestep",
                "encoder_hidden_states",
                "down_block_0",
                "down_block_1",
                "down_block_2",
                "down_block_3",
                "down_block_4",
                "down_block_5",
                "down_block_6",
                "down_block_7",
                "down_block_8",
                "down_block_9",
                "down_block_10",
                "down_block_11",
                "mid_block_additional_residual",
                "return_dict"
            ],
            output_names=["out_sample"],  # has to be different from "sample" for correct tracing
            dynamic_axes={
                "sample": {0: "batch", 1: "channels", 2: "height", 3: "width"},
                "timestep": {0: "batch"},
                "encoder_hidden_states": {0: "batch", 1: "sequence"},
                "down_block_0": {0: "batch", 2: "height", 3: "width"},
                "down_block_1": {0: "batch", 2: "height", 3: "width"},
                "down_block_2": {0: "batch", 2: "height", 3: "width"},
                "down_block_3": {0: "batch", 2: "height2", 3: "width2"},
                "down_block_4": {0: "batch", 2: "height2", 3: "width2"},
                "down_block_5": {0: "batch", 2: "height2", 3: "width2"},
                "down_block_6": {0: "batch", 2: "height4", 3: "width4"},
                "down_block_7": {0: "batch", 2: "height4", 3: "width4"},
                "down_block_8": {0: "batch", 2: "height4", 3: "width4"},
                "down_block_9": {0: "batch", 2: "height8", 3: "width8"},
                "down_block_10": {0: "batch", 2: "height8", 3: "width8"},
                "down_block_11": {0: "batch", 2: "height8", 3: "width8"},
                "mid_block_additional_residual": {0: "batch", 2: "height8", 3: "width8"},
            },
            opset=opset,
        )

        controlnet = ControlNetModel.from_pretrained(args.controlnet_path, low_cpu_mem_usage=False)
        if attention_slicing:
            controlnet.set_attention_slice(attention_slicing)
        cnet_path = output_path / "controlnet" / "model.onnx"
        onnx_export(
            controlnet,
            model_args=(
                torch.randn(2, 4, 64, 64).to(device=device, dtype=dtype),
                torch.randn(2).to(device=device, dtype=dtype),
                torch.randn(2, 77, 768).to(device=device, dtype=dtype),
                torch.randn(2, 3, 512,512).to(device=device, dtype=dtype),
            ),
            output_path=cnet_path,
            ordered_input_names=["sample", "timestep", "encoder_hidden_states", "controlnet_cond"],
            output_names=["down_block_res_samples", "mid_block_res_sample"],
            dynamic_axes={
                "sample": {0: "batch", 1: "channels", 2: "height", 3: "width"},
                "timestep": {0: "batch"},
                "encoder_hidden_states": {0: "batch", 1: "sequence"},
                "controlnet_cond": {0: "batch", 2: "height", 3: "width"}
            },
            opset=opset,
        )

        if fp16:
            cnet_path_model_path = str(cnet_path.absolute().as_posix())
            convert_to_fp16(cnet_path_model_path)

    else:
        onnx_export(
            pipeline.unet,
            model_args=(
                torch.randn(2, unet_in_channels, unet_sample_size,
                    unet_sample_size).to(device=device, dtype=dtype),
                torch.randn(2).to(device=device, dtype=dtype),
                torch.randn(2, num_tokens, text_hidden_size).to(device=device, dtype=dtype),
                False,
            ),
            output_path=unet_path,
            ordered_input_names=["sample", "timestep", "encoder_hidden_states", "return_dict"],
            output_names=["out_sample"],  # has to be different from "sample" for correct tracing
            dynamic_axes={
                "sample": {0: "batch", 1: "channels", 2: "height", 3: "width"},
                "timestep": {0: "batch"},
                "encoder_hidden_states": {0: "batch", 1: "sequence"},
            },
            opset=opset,
        )
    del pipeline.unet

    unet_model_path = str(unet_path.absolute().as_posix())
    unet_dir = os.path.dirname(unet_model_path)
    unet = onnx.load(unet_model_path)
    # clean up existing tensor files
    shutil.rmtree(unet_dir)
    os.mkdir(unet_dir)

    optimizer = UnetOnnxModelDML(unet, 0, 0)
    if not notune:
        optimizer.optimize()
        optimizer.topological_sort()

    # collate external tensor files into one
    onnx.save_model(
        optimizer.model,
        unet_model_path,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location="weights.pb",
        convert_attribute=False,
    )
    if fp16:
        convert_to_fp16(unet_model_path)
    del unet, optimizer

    # VAE ENCODER
    vae_encoder = pipeline.vae
    vae_in_channels = vae_encoder.config.in_channels
    vae_sample_size = vae_encoder.config.sample_size
    # need to get the raw tensor output (sample) from the encoder
    vae_encoder.forward = lambda sample, return_dict: vae_encoder.encode(sample,
        return_dict)[0].sample()
    onnx_export(
        vae_encoder,
        model_args=(
            torch.randn(1, vae_in_channels, vae_sample_size,
                vae_sample_size).to(device=device, dtype=dtype),
            False,
        ),
        output_path=output_path / "vae_encoder" / "model.onnx",
        ordered_input_names=["sample", "return_dict"],
        output_names=["latent_sample"],
        dynamic_axes={
            "sample": {0: "batch", 1: "channels", 2: "height", 3: "width"},
        },
        opset=opset,
    )

    # VAE DECODER
    vae_decoder = pipeline.vae
    vae_latent_channels = vae_decoder.config.latent_channels
    vae_out_channels = vae_decoder.config.out_channels
    # forward only through the decoder part
    vae_decoder.forward = vae_encoder.decode
    onnx_export(
        vae_decoder,
        model_args=(
            torch.randn(1, vae_latent_channels, unet_sample_size,
                unet_sample_size).to(device=device, dtype=dtype),
            False,
        ),
        output_path=output_path / "vae_decoder" / "model.onnx",
        ordered_input_names=["latent_sample", "return_dict"],
        output_names=["sample"],
        dynamic_axes={
            "latent_sample": {0: "batch", 1: "channels", 2: "height", 3: "width"},
        },
        opset=opset,
    )
    del pipeline.vae

    # SAFETY CHECKER
    # NOTE:
    # Safety checker is excluded because it is a resource hog and you'd be turning it off anyway
    # I'm not a legal expert but IMHO you are still bound by the model's license after conversion
    # Check the license of the model you are converting and abide by it

    safety_checker = None
    feature_extractor = None

    onnx_pipeline = OnnxStableDiffusionPipeline(
        vae_encoder=OnnxRuntimeModel.from_pretrained(output_path / "vae_encoder",
            low_cpu_mem_usage=False),
        vae_decoder=OnnxRuntimeModel.from_pretrained(output_path / "vae_decoder",
            low_cpu_mem_usage=False),
        text_encoder=OnnxRuntimeModel.from_pretrained(output_path / "text_encoder",
            low_cpu_mem_usage=False),
        tokenizer=pipeline.tokenizer,
        unet=OnnxRuntimeModel.from_pretrained(output_path / "unet",low_cpu_mem_usage=False),
        scheduler=pipeline.scheduler,
        safety_checker=safety_checker,
        feature_extractor=feature_extractor,
        requires_safety_checker=safety_checker is not None,
    )

    onnx_pipeline.save_pretrained(output_path)

    if controlnet_path:
        confname=f"{output_path}/model_index.json"
        with open(confname, 'r', encoding="utf-8") as f:
            modelconf = json.load(f)
            modelconf['controlnet'] = ("diffusers","OnnxRuntimeModel")
        with open(confname, 'w', encoding="utf-8") as f:
            json.dump(modelconf, f, indent=1)

    print("ONNX pipeline saved to", output_path)

    del pipeline
    del onnx_pipeline
    _ = OnnxStableDiffusionPipeline.from_pretrained(output_path,
        provider="DmlExecutionProvider",
        low_cpu_mem_usage=False)
    print("ONNX pipeline is loadable")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--model_path",
        type=str,
        required=True,
        help=(
            "Path to the `diffusers` checkpoint to convert (either local directory or on the Hub). "
            "Or the path to a local checkpoint saved in .ckpt or .safetensors."
        )
    )

    parser.add_argument(
        "--output_path",
        type=str,
        required=True,
        help="Path to the output model."
    )

    parser.add_argument(
        "--vae_path",
        default="",
        type=str,
        help=(
            "Path to alternate VAE `diffusers` checkpoint (either local or on the Hub). "
        )
    )

    parser.add_argument(
        "--controlnet_path",
        default="",
        type=str,
        help=(
            "Path to controlnet model to import and convert (either local or on the Hub). "
            "Setting this results in an SD model intended to be used with a specific ControlNet"
        )
    )

    parser.add_argument(
        "--opset",
        default=15,
        type=int,
        help="The version of the ONNX operator set to use.",
    )

    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Export Text Encoder and UNET in mixed `float16` mode"
    )

    parser.add_argument(
        "--notune",
        action="store_true",
        help="Turn off tuning UNET with ONNX Runtime Transformers"
    )

    parser.add_argument(
        "--attention-slicing",
        choices={"auto","max"},
        type=str,
        help=(
            "Attention slicing reduces VRAM needed, off by default. Set to auto or max. "
            "WARNING: max implies --notune"
        )
    )

    parser.add_argument(
        "--clip-skip",
        choices={2,3,4},
        type=int,
        help="Add permanent clip skip to ONNX model."
    )

    parser.add_argument(
        "--diffusers-output",
        type=str,
        help="Directory to dump a pre-conversion copy in diffusers format in."
    )

    parser.add_argument(
        "--ckpt-original-config-file",
        default=None,
        type=str,
        help="The YAML config file corresponding to the original architecture."
    )

    parser.add_argument(
        "--ckpt-image-size",
        default=None,
        type=int,
        help="The image size that the model was trained on. Typically 512 or 768"
    )

    parser.add_argument(
        "--ckpt-prediction_type",
        default=None,
        type=str,
        help=(
            "Prediction type the model was trained on. "
           "'epsilon' for SD v1.X and SD v2 Base, 'v-prediction' for SD v2"
       )
    )

    parser.add_argument(
        "--ckpt-pipeline_type",
        default=None,
        type=str,
        help="The pipeline type. If `None` pipeline will be automatically inferred."
    )

    parser.add_argument(
        "--ckpt-extract-ema",
        action="store_true",
        help=(
            "Only relevant for checkpoints that have both EMA and non-EMA weights. "
            "If set enables extraction of EMA weights (Default is non-EMA). "
            "EMA weights usually yield higher quality images for inference. "
            "Non-EMA weights are usually better to continue fine-tuning."
        )
    )

    parser.add_argument(
        "--ckpt-num-in-channels",
        default=None,
        type=int,
        help=(
            "The number of input channels. "
            "If `None` number of input channels will be automatically inferred."
        )
    )

    parser.add_argument(
        "--ckpt-upcast-attention",
        action="store_true",
        help=(
            "Whether the attention computation should always be upcasted. "
            "Necessary when running SD 2.1"
        )
    )

    args = parser.parse_args()

    dtype=torch.float32
    device = "cpu"
    if args.model_path.endswith(".ckpt") or args.model_path.endswith(".safetensors"):
        pl = download_from_original_stable_diffusion_ckpt(
            checkpoint_path=args.model_path,
            original_config_file=args.ckpt_original_config_file,
            image_size=args.ckpt_image_size,
            prediction_type=args.ckpt_prediction_type,
            model_type=args.ckpt_pipeline_type,
            extract_ema=args.ckpt_extract_ema,
            scheduler_type="pndm",
            num_in_channels=args.ckpt_num_in_channels,
            upcast_attention=args.ckpt_upcast_attention,
            from_safetensors=args.model_path.endswith(".safetensors")
        )
    else:
        pl = StableDiffusionPipeline.from_pretrained(args.model_path,
            torch_dtype=dtype,low_cpu_mem_usage=False).to(device)

    if args.vae_path:
        with tempfile.TemporaryDirectory() as tmpdirname:
            pl.save_pretrained(tmpdirname)
            if args.vae_path.endswith('/vae'):
                vae = AutoencoderKL.from_pretrained(args.vae_path[:-4],subfolder='vae',
                    low_cpu_mem_usage=False)
            else:
                vae = AutoencoderKL.from_pretrained(args.vae_path,low_cpu_mem_usage=False)
            pl = StableDiffusionPipeline.from_pretrained(tmpdirname,
                torch_dtype=dtype, vae=vae,low_cpu_mem_usage=False).to(device)

    if args.clip_skip:
        with tempfile.TemporaryDirectory() as tmpdirname:
            pl.save_pretrained(tmpdirname)
            confname=f"{tmpdirname}/text_encoder/config.json"
            with open(confname, 'r', encoding="utf-8") as f:
                clipconf = json.load(f)
                clipconf['num_hidden_layers'] = clipconf['num_hidden_layers']-args.clip_skip+1
            with open(confname, 'w', encoding="utf-8") as f:
                json.dump(clipconf, f, indent=1)
            pl = StableDiffusionPipeline.from_pretrained(tmpdirname,
                torch_dtype=dtype,low_cpu_mem_usage=False).to(device)

    blocktune=False
    if args.attention_slicing:
        if args.attention_slicing == "max":
            blocktune=True
            print ("WARNING: attention_slicing max implies --notune")
        pl.enable_attention_slicing(args.attention_slicing)

    if args.diffusers_output:
        pl.save_pretrained(args.diffusers_output)

    convert_models(pl, args.output_path,
                                    args.opset,
                                    args.fp16,
                                    args.notune or blocktune,
                                    args.controlnet_path,
                                    args.attention_slicing)
    