#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.

import os
import argparse
import random
import warnings
from loguru import logger

import torch
import torch.backends.cudnn as cudnn

from netspresso.compressor import ModelCompressor, Task, Framework, CompressionMethod, RecommendationMethod

from yolox.core import launch
from yolox.exp import Exp, check_exp_value, get_exp
from yolox.utils import configure_module, configure_nccl, configure_omp, get_num_devices, replace_module
from yolox.models.network_blocks import SiLU

def make_parser():
    parser = argparse.ArgumentParser("YOLOX train parser")

    """
        Common arguments
    """
    parser.add_argument(
        "-f",
        "--exp_file",
        default=None,
        type=str,
        help="experiment description file",
    )
    parser.add_argument("-expn", "--experiment-name", type=str, default=None)
    parser.add_argument("-n", "--name", type=str, default=None, help="model name")
    parser.add_argument(
        "opts",
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )

    """
        Compression arguments
    """
    parser.add_argument(
        "--compression_method",
        type=str,
        choices=["PR_L2", "PR_GM", "PR_NN", "PR_ID", "FD_TK", "FD_CP", "FD_SVD"],
        default="PR_L2"
    )
    parser.add_argument(
        "--recommendation_method",
        type=str,
        choices=["slamp", "vbmf"],
        default="slamp"
    )
    parser.add_argument(
        "--compression_ratio",
        type=float,
        default=0.5
    )
    parser.add_argument(
        "-w",
        "--weight_path",
        type=str
    )
    parser.add_argument(
        "-m",
        "--np_email",
        help="NetsPresso login e-mail",
        type=str,
    )
    parser.add_argument(
        "-p",
        "--np_password",
        help="NetsPresso login password",
        type=str,
    )

    """
        Fine-tuning arguments
    """
    parser.add_argument(
        "--dist-backend", default="nccl", type=str, help="distributed backend"
    )
    parser.add_argument(
        "--dist-url",
        default=None,
        type=str,
        help="url used to set up distributed training",
    )
    parser.add_argument("-b", "--batch-size", type=int, default=64, help="batch size")
    parser.add_argument(
        "-d", "--devices", default=None, type=int, help="device for training"
    )
    parser.add_argument(
        "--resume", default=False, action="store_true", help="resume training"
    )
    parser.add_argument("-c", "--ckpt", default=None, type=str, help="checkpoint file")
    parser.add_argument(
        "-e",
        "--start_epoch",
        default=None,
        type=int,
        help="resume training start epoch",
    )
    parser.add_argument(
        "--num_machines", default=1, type=int, help="num of node for training"
    )
    parser.add_argument(
        "--machine_rank", default=0, type=int, help="node rank for multi-node training"
    )
    parser.add_argument(
        "--fp16",
        dest="fp16",
        default=False,
        action="store_true",
        help="Adopting mixed precision training.",
    )
    parser.add_argument(
        "--cache",
        type=str,
        nargs="?",
        const="ram",
        help="Caching imgs to ram/disk for fast training.",
    )
    parser.add_argument(
        "-o",
        "--occupy",
        dest="occupy",
        default=False,
        action="store_true",
        help="occupy GPU memory first for training.",
    )
    parser.add_argument(
        "-l",
        "--logger",
        type=str,
        help="Logger to be used for metrics. \
        Implemented loggers include `tensorboard` and `wandb`.",
        default="tensorboard"
    )

    """
        Export arguments
    """
    parser.add_argument(
        "--input", default="images", type=str, help="input node name of onnx model"
    )
    parser.add_argument(
        "--output", default="output", type=str, help="output node name of onnx model"
    )
    parser.add_argument(
        "--opset", default=11, type=int, help="onnx opset version"
    )
    parser.add_argument("--export_batch_size", type=int, default=1, help="batch size")
    parser.add_argument(
        "--dynamic", action="store_true", help="whether the input shape should be dynamic or not"
    )
    parser.add_argument("--no-onnxsim", action="store_true", help="use onnxsim or not")
    parser.add_argument(
        "--decode_in_inference",
        action="store_true",
        help="decode in inference or not"
    )

    return parser


@logger.catch
def main(exp: Exp, args):
    if exp.seed is not None:
        random.seed(exp.seed)
        torch.manual_seed(exp.seed)
        cudnn.deterministic = True
        warnings.warn(
            "You have chosen to seed training. This will turn on the CUDNN deterministic setting, "
            "which can slow down your training considerably! You may see unexpected behavior "
            "when restarting from checkpoints."
        )

    # set environment variables for distributed training
    configure_nccl()
    configure_omp()
    cudnn.benchmark = True

    trainer = exp.get_trainer(args)
    trainer.train()


if __name__ == "__main__":
    configure_module()
    args = make_parser().parse_args()

    """ 
        Convert YOLOX model to fx 
    """
    logger.info("yolox to fx graph start.")

    exp = get_exp(args.exp_file, args.name)
    check_exp_value(exp)
    exp.merge(args.opts)
    if not args.experiment_name:
        args.experiment_name = exp.exp_name

    model = exp.get_model(netspresso=True)

    # load the model state dict
    ckpt = torch.load(args.weight_path, map_location="cpu")

    model.train()
    if "model" in ckpt:
        ckpt = ckpt["model"]
    model.load_state_dict(ckpt, strict=False)

    logger.info("loading checkpoint done.")
    
    _graph = torch.fx.Tracer().trace(model)
    traced_model = torch.fx.GraphModule(model, _graph)
    torch.save(traced_model, './' + exp.exp_name + '_fx.pt')
    logger.info(f"generated model to compress model {exp.exp_name + '_fx.pt'}")
    
    head = exp.get_head()
    torch.save(head, './' + exp.exp_name + '_head.pt')
    logger.info(f"generated model to model's head {exp.exp_name + '_head.pt'}")

    logger.info("yolox to fx graph end.")

    """ 
        Model compression - recommendation compression 
    """
    logger.info("Compression step start.")
    
    compressor = ModelCompressor(email=args.np_email, password=args.np_password)

    UPLOAD_MODEL_NAME = exp.exp_name
    TASK = Task.OBJECT_DETECTION
    FRAMEWORK = Framework.PYTORCH
    UPLOAD_MODEL_PATH = exp.exp_name + '_fx.pt'
    INPUT_SHAPES = [{"batch": 1, "channel": 3, "dimension": exp.input_size}]
    model = compressor.upload_model(
        model_name=UPLOAD_MODEL_NAME,
        task=TASK,
        framework=FRAMEWORK,
        file_path=UPLOAD_MODEL_PATH,
        input_shapes=INPUT_SHAPES,
    )

    COMPRESSION_METHOD = args.compression_method
    RECOMMENDATION_METHOD = args.recommendation_method
    RECOMMENDATION_RATIO = args.compression_ratio
    COMPRESSED_MODEL_NAME = f'{UPLOAD_MODEL_NAME}_{COMPRESSION_METHOD}_{RECOMMENDATION_RATIO}'
    OUTPUT_PATH = COMPRESSED_MODEL_NAME + '.pt'
    compressed_model = compressor.recommendation_compression(
        model_id=model.model_id,
        model_name=COMPRESSED_MODEL_NAME,
        compression_method=COMPRESSION_METHOD,
        recommendation_method=RECOMMENDATION_METHOD,
        recommendation_ratio=RECOMMENDATION_RATIO,
        output_path=OUTPUT_PATH,
    )

    logger.info("Compression step end.") 
    
    """ 
        Retrain YOLOX model 
    """
    logger.info("Fine-tuning step start.")
    compressed_path = OUTPUT_PATH
    head_path = exp.exp_name + '_head.pt'
    
    exp = get_exp(args.exp_file, args.name + '-netspresso')
    check_exp_value(exp)
    exp.merge(args.opts)
    exp.basic_lr_per_img *= 0.1

    exp.compressed_model = compressed_path
    exp.head = head_path
    model = exp.get_model()
    model.train()

    num_gpu = get_num_devices() if args.devices is None else args.devices
    assert num_gpu <= get_num_devices()

    if args.cache is not None:
        exp.dataset = exp.get_dataset(cache=True, cache_type=args.cache)

    dist_url = "auto" if args.dist_url is None else args.dist_url
    launch(
        main,
        num_gpu,
        args.num_machines,
        args.machine_rank,
        backend=args.dist_backend,
        dist_url=dist_url,
        args=(exp, args),
    )

    logger.info("Fine-tuning step end.")

    """ 
        Export YOLOX model to onnx
    """
    logger.info("Export model to onnx format step start.")
    # init model
    exp = get_exp(args.exp_file, args.name + '-netspresso')
    check_exp_value(exp)
    exp.merge(args.opts)

    exp.compressed_model = compressed_path
    exp.head = head_path
    model = exp.get_model()
    
    # load state dict
    ckpt_file = os.path.join(exp.output_dir, args.experiment_name, 'best_ckpt.pth')
    if not os.path.isfile(ckpt_file):
        ckpt_file = os.path.join(exp.output_dir, args.experiment_name, 'latest_ckpt.pth')
    ckpt = torch.load(ckpt_file, map_location="cpu")

    if "model" in ckpt:
        ckpt = ckpt["model"]
    model.load_state_dict(ckpt)

    model.eval()
    model = replace_module(model, torch.nn.SiLU, SiLU)
    model.head.decode_in_inference = args.decode_in_inference

    logger.info("loading checkpoint done.")
    dummy_input = torch.randn(args.export_batch_size, 3, exp.test_size[0], exp.test_size[1])

    torch.onnx._export(
        model,
        dummy_input,
        COMPRESSED_MODEL_NAME + '.onnx',
        input_names=[args.input],
        output_names=[args.output],
        dynamic_axes={args.input: {0: 'batch'},
                      args.output: {0: 'batch'}} if args.dynamic else None,
        opset_version=args.opset,
    )
    logger.info("generated onnx model named {}".format(COMPRESSED_MODEL_NAME + '.onnx'))

    if not args.no_onnxsim:
        import onnx
        from onnxsim import simplify

        # use onnx-simplifier to reduce reduent model.
        onnx_model = onnx.load(COMPRESSED_MODEL_NAME + '.onnx')
        model_simp, check = simplify(onnx_model)
        assert check, "Simplified ONNX model could not be validated"
        onnx.save(model_simp, COMPRESSED_MODEL_NAME + '.onnx')
        logger.info("generated simplified onnx model named {}".format(COMPRESSED_MODEL_NAME + '.onnx'))

    logger.info("Export model to onnx format step end.")
