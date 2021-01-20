"""
    Train the NN-basedmask generator.
"""

import argparse
import glob
import logging
import os
import random
from pathlib import Path
from pdb import set_trace

import coloredlogs
import enlighten
import matplotlib.pyplot as plt
import seaborn as sns
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as T
from PIL import Image
from torch.utils.data import ConcatDataset, DataLoader, Dataset
from torchvision import io

from dnn.fasterrcnn_resnet50 import FasterRCNN_ResNet50_FPN
from maskgen.fcn_16_single_channel import FCN
from utils.bbox_utils import center_size
from utils.loss_utils import cross_entropy as get_loss
from utils.mask_utils import *
from utils.results_utils import read_ground_truth_mask, read_results
from utils.video_utils import get_qp_from_name, read_videos, write_video

sns.set()


def main(args):

    # initialization for distributed training
    # dist.init_process_group(backend='nccl')
    # torch.cuda.set_device(args.local_rank)

    # initialize logger
    logger = logging.getLogger("train")
    logger.addHandler(logging.FileHandler(args.log))
    torch.set_default_tensor_type(torch.FloatTensor)

    # there will be only one dataset
    assert len(args.inputs) == 1

    # read videos
    videos, _, _ = read_videos(args.inputs, logger, sort=True, dataloader=False)
    bws = [0, 1]

    # construct training set and cross validation set
    training_set = ConcatDataset(videos)
    training_set, cross_validation_set = torch.utils.data.random_split(
        training_set,
        [int(0.8 * len(training_set)), int(0.2 * len(training_set))],
        generator=torch.Generator().manual_seed(100),
    )
    # training_sampler = torch.utils.data.DistributedSampler(training_set)
    training_loader = torch.utils.data.DataLoader(
        training_set, batch_size=args.batch_size, shuffle=True, num_workers=2
    )
    cross_validation_loader = torch.utils.data.DataLoader(
        cross_validation_set, batch_size=args.batch_size, num_workers=2
    )

    # construct the application
    application = FasterRCNN_ResNet50_FPN()

    # construct the mask generator
    mask_generator = FCN()
    if os.path.exists(args.path):
        logger.info(f"Load the model from %s", args.path)
        mask_generator.load(args.path)
    mask_generator.cuda()
    mask_generator.train()
    # mask_generator = torch.nn.parallel.DistributedDataParallel(mask_generator, device_ids=[args.local_rank])

    optimizer = torch.optim.Adam(mask_generator.parameters(), lr=args.learning_rate)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, "min")

    # load ground truth results
    ground_truth_results = read_results(args.ground_truth, application.name, logger)
    ground_truth_results = [
        application.filter_results(ground_truth_results[fid], args.confidence_threshold)
        for fid in ground_truth_results
    ]
    ground_truth_boxes = [center_size(result[2]) for result in ground_truth_results]
    mask_slice_shape = [1, 1, 720 // args.tile_size, 1280 // args.tile_size]
    mask_slice = torch.ones(mask_slice_shape)
    ground_truth_mask = [
        generate_mask_from_regions(mask_slice.clone(), box, 0, args.tile_size)
        for box in ground_truth_boxes
    ]

    mean_cross_validation_loss_before = 100

    for iteration in range(args.num_iterations):

        """
            Training
        """

        progress_bar = enlighten.get_manager().counter(
            total=len(training_set),
            desc=f"Iteration {iteration} on training set",
            unit="frames",
        )

        training_losses = []

        for idx, data in enumerate(training_loader):

            progress_bar.update(incr=len(data["fid"]))

            # inference
            hq_image = data["image"].cuda()
            mask_slice = mask_generator(hq_image)
            fids = [fid.item() for fid in data["fid"]]

            # calculate loss
            target = torch.cat([ground_truth_mask[fid].long().cuda() for fid in fids])
            loss = get_loss(mask_slice, target, 1)
            loss.backward()

            # optimization and logging
            mask_slice_temp = mask_slice.softmax(dim=1)[:, 1:2, :, :]
            logger.info(
                "Min: %f, Mean: %f, Std: %f, Training loss: %f",
                mask_slice_temp.min().item(),
                mask_slice_temp.mean().item(),
                mask_slice_temp.std().item(),
                loss.item(),
            )
            training_losses.append(loss.item())
            optimizer.step()
            optimizer.zero_grad()

            if idx % 500 == 0:
                # save the model
                mask_generator.save(args.path)

        mean_training_loss = torch.tensor(training_losses).mean()
        logger.info("Average training loss: %.3f", mean_training_loss.item())

        """
            Cross validation
        """

        progress_bar = enlighten.get_manager().counter(
            total=len(cross_validation_set),
            desc=f"Iteration {iteration} on cross validation set",
            unit="frames",
        )

        cross_validation_losses = []

        for idx, data in enumerate(cross_validation_loader):

            progress_bar.update(incr=len(data["fid"]))

            # extract data from dataloader
            hq_image = data["image"].cuda()
            fids = data["fid"]
            fids = [fid.item() for fid in fids]

            # inference
            with torch.no_grad():
                mask_slice = mask_generator(hq_image)

                target = torch.cat(
                    [ground_truth_mask[fid].long().cuda() for fid in fids]
                )
                loss = get_loss(mask_slice, target, 1)

            # optimization and logging
            logger.info(f"Cross validation loss: {loss.item()}")
            cross_validation_losses.append(loss.item())

        mean_cross_validation_loss = torch.tensor(cross_validation_losses).mean().item()
        logger.info("Average cross validation loss: %.3f", mean_cross_validation_loss)

        if mean_cross_validation_loss < mean_cross_validation_loss_before:
            mask_generator.save(args.path + ".best")
        mean_cross_validation_loss_before = min(
            mean_cross_validation_loss_before, mean_cross_validation_loss
        )

        # check if we need to reduce learning rate.
        scheduler.step(mean_cross_validation_loss)


if __name__ == "__main__":

    # set the format of the logger
    coloredlogs.install(
        fmt="%(asctime)s [%(levelname)s] %(name)s:%(funcName)s[%(lineno)s] -- %(message)s",
        level="INFO",
    )

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-i",
        "--inputs",
        nargs="+",
        help="The video file name. The largest video file will be the ground truth.",
        required=True,
    )
    # parser.add_argument('-s', '--source', type=str, help='The original video source.', required=True)
    # parser.add_argument('-g', '--ground_truth', type=str,
    #                     help='The ground truth videos.', required=True)
    parser.add_argument(
        "-p",
        "--path",
        type=str,
        help="The path to store the generator parameters.",
        required=True,
    )
    parser.add_argument(
        "--log", type=str, help="The logging file.", required=True,
    )
    parser.add_argument(
        "-g", "--ground_truth", type=str, help="The ground truth file.", required=True
    )
    # parser.add_argument('-o', '--output', type=str,
    #                     help='The output name.', required=True)
    parser.add_argument(
        "--confidence_threshold",
        type=float,
        help="The confidence score threshold for calculating accuracy.",
        default=0.3,
    )
    parser.add_argument(
        "--iou_threshold",
        type=float,
        help="The IoU threshold for calculating accuracy in object detection.",
        default=0.5,
    )
    parser.add_argument(
        "--num_iterations",
        type=int,
        help="Number of iterations for optimizing the mask.",
        default=500,
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        help="Number of iterations for optimizing the mask.",
        default=2,
    )
    parser.add_argument(
        "--tile_size", type=int, help="The tile size of the mask.", default=8
    )
    parser.add_argument(
        "--learning_rate", type=float, help="The learning rate.", default=1e-4
    )
    parser.add_argument(
        "--gamma", type=float, help="The gamma parameter for focal loss.", default=2
    )
    parser.add_argument(
        "--local_rank", default=-1, type=int, help="The GPU id for distributed training"
    )

    args = parser.parse_args()

    main(args)