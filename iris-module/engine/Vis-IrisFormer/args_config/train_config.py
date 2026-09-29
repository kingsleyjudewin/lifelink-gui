#!/usr/bin/env python3
#-*- coding:utf-8 -*-

import argparse
import os.path as osp

cp_dir   = './checkpoint/'

def cls_args():

    parser = argparse.ArgumentParser(description='Iris Causal Transformer')

    # -- env
    parser.add_argument('--use_gpu', type=bool, default=True)
    parser.add_argument('--gpu_ids', type=list, default=[0])
    parser.add_argument('--num_workers', type=int,  default=4)  # TODO
    parser.add_argument('--run_name', type=str, default='test_run')
    parser.add_argument('--wandb', action='store_true')
    parser.add_argument('--test_while_train', action='store_true')

    # -- model
    parser.add_argument('--drop_ratio', type=float, default=0.4)          # TODO
    parser.add_argument('--used_as',    type=str,   default='baseline', choices=['baseline', 'UE', 'backbone'])   
    parser.add_argument('--input_size', type=tuple, default=(64,512))
    parser.add_argument('--patch_size', type=tuple, default=(16,16))
    parser.add_argument('--mask_ratio', type=float, default=0.0)
    parser.add_argument('--in_feats',   type=int,   default=768)
    parser.add_argument('--ft_pool', type=str, default='map', choices=['cls', 'mean', 'map'])
        ## 'cls'= use a classification token; 'mean' = use average pooling; 'map'= keep the sequencial output feature
    parser.add_argument('--position_embedding', type=str, default='rope2d', choices=['none', 'cosine', 'learnable', 'window', 'polar', 'rope1d', 'rope2d'])
        ## 'window' is the position embedding used in Swin Transformer, where we set the window size to the whole image 
        ## 'polar' is a modified version of 'window' by considering the rotation invariance of normalized iris images. This should perform a little bit better than using 'window', yet it is not the best choice.
        ## 'rope1d' regards an input image as a 1D sequence with legth HW, while 'rope2d' calculates position bias with both the x and y index of each image patch.

    # -- loss
    parser.add_argument('--triplet_margin', type=float, default=0.5)      
    parser.add_argument('--triplet_step', type=bool, default=True)  # wether to train with an easy-to-hard manner

    # -- information bottleneck (not used in the proposed method)
    parser.add_argument('--bottleneck', action='store_true')   # wether to use information bottleneck
    parser.add_argument('--bottleneck_feats', type=int, default=768)
    parser.add_argument('--kl_lambda', type=float, default=0.0001)     # weight for kl loss when using information bottleneck
    
    # -- dataset
    parser.add_argument('--batch_size',  type=int,   default=90)
    parser.add_argument('--people_per_batch_train', type=int, default=45)
    parser.add_argument('--images_per_person_train', type=int, default=40)
    parser.add_argument('--triplet_alpha', type=float, default=0.2)
    parser.add_argument('--shift_possibility', type=float, default=0)   # possibility to shift the image as augmentation
    parser.add_argument('--shift_pixel', type=int, default=0)       # maximum number of pixels for shifting
    parser.add_argument('--sample_pairs_number', type=int, default=10000) # sample pairs for testing

    # -- optimizer
    parser.add_argument('--start_epoch', type=int,   default=0)
    parser.add_argument('--end_epoch',   type=int,   default=200)
    parser.add_argument('--warmup', type=str, default='cos', choices=['cos', 'exp', 'step', 'cos_step'])
    parser.add_argument('--warmup_epoch',  type=int,   default=16)
    parser.add_argument('--early_stop', type=int, default=-1)
    parser.add_argument('--lr_backbone', type=float, default=0.0001)    

    parser.add_argument('--gamma',       type=float, default=0.1)      # FIXED
    parser.add_argument('--weight_decay',type=float, default=0.05)     # FIXED
    parser.add_argument('--resume',      type=str,   default='')       # checkpoint

    # -- save or print
    parser.add_argument('--is_debug', action='store_true')   # TODO
    parser.add_argument('--print_freq',type=int,   default=10) 
    parser.add_argument('--save_freq', type=int,   default=5)  # TODO
    
    args = parser.parse_args()

    return args
