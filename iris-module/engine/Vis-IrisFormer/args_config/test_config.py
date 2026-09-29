#!/usr/bin/env python3
#-*- coding:utf-8 -*-

import argparse
import os.path as osp

cp_dir   = './checkpoint/'

def cls_args():

    parser = argparse.ArgumentParser(description='PyTorch for softmax-baseline')

    # -- env
    parser.add_argument('--use_gpu', type=bool, default=True)
    parser.add_argument('--gpu_ids', type=list, default=[0])
    parser.add_argument('--workers', type=int,  default=4)  # TODO
    parser.add_argument('--sample_pairs_number', type=int, default=10000)
    parser.add_argument('--batch_size',  type=int,   default=256)

    # -- model
    parser.add_argument('--input_size', type=tuple, default=(64,512))
    parser.add_argument('--patch_size', type=tuple, default=(16,16))
    parser.add_argument('--mask_ratio', type=float, default=0.0)
    parser.add_argument('--in_feats',   type=int,   default=256)
    parser.add_argument('--ft_pool', type=str, default='mean', choices=['mean', 'cls', 'map'])
    parser.add_argument('--position_embedding', type=str, default='cosine', choices=['none', 'cosine', 'learnable', 'window', 'polar', 'rope1d', 'rope2d'])

    # -- information bottleneck (not used in the proposed method)
    parser.add_argument('--bottleneck', action='store_true')   # wether to use information bottleneck
    parser.add_argument('--bottleneck_feats', type=int, default=768)

    # -- save result
    parser.add_argument('--save_report', action='store_true')

    args = parser.parse_args()

    return args
