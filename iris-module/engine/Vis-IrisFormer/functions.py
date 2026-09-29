# -*- coding: utf-8 -*-
"""
Functions for MAE training
RenMin 20211209
"""

import torch
import torch.nn.functional as F
import numpy as np
import math
import random
from einops import rearrange
import random
import pdb

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    #pdb.set_trace()
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred    = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].contiguous().view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def get_mask_index_list(ih, iw, mask_scale, num_patch):
    # 2D coord
    coord2D = []
    for i in range(mask_scale[0]):
        for j in range(mask_scale[1]):
            coord2D.append([ih*mask_scale[0]+i, iw*mask_scale[1]+j])
            
    # 1D coord
    coord1D = []
    N = len(coord2D)
    for i in range(N):
        c2d = coord2D[i]
        c1d = c2d[0]*num_patch + c2d[1]
        coord1D.append(c1d)
    return coord1D, coord2D


def MaskSampling(token_length, mask_ratio):
    token_index = [x for x in range(1, token_length+1)]
    random.shuffle(token_index)
    num_remain = int(token_length*(1.-mask_ratio))
    N = int(token_length//num_remain)
    samplings = []
    for i in range(N):
        remain_list = [0] + token_index[i*num_remain:(i+1)*num_remain]
        mask_list = token_index[:i*num_remain] + token_index[(i+1)*num_remain:]
        samplings.append([remain_list, mask_list])
    if token_length%num_remain > 0:
        remain_list = [0] + token_index[N*num_remain:]
        mask_list = token_index[:N*num_remain]
        samplings.append([remain_list, mask_list])
    return samplings

            

def PatchWiseMSE(img_orig, img_recon, patch_size, remain_list, dis_type='cos'):
    tokens_orig = rearrange(img_orig, 'c (h p1) (w p2) -> (h w) (p1 p2 c)', p1=patch_size, p2=patch_size)
    tokens_recon = rearrange(img_recon, 'c (h p1) (w p2) -> (h w) (p1 p2 c)', p1=patch_size, p2=patch_size)
    
    if dis_type == 'mse':
        # MSE
        dis = ((tokens_orig - tokens_recon)**2).mean(1)
    elif dis_type =='cos':
        #pdb.set_trace()
        # Cosine Similarity
        tokens_orig_norm = F.normalize(tokens_orig, dim=1)
        tokens_recon_norm = F.normalize(tokens_recon.float(), dim=1)
        sim = (tokens_orig_norm * tokens_recon_norm).sum(1)
        dis = 1. - sim
    
    dis[remain_list] = -100
    return dis



# Gaussian kernel of patch
def Gaussian2D(X, Y, mu, sigmma):
    return torch.exp(-0.5*((X-mu[0])**2/sigmma[0] + (Y-mu[1])**2/sigmma[1]))

def GausKernelPatch(num_patch, std=3):
    filt_map = torch.zeros(num_patch**2, num_patch**2)
    xy = [k for k in range(num_patch)]
    X = torch.tensor(xy).unsqueeze(1).expand(num_patch, num_patch)
    Y = torch.tensor(xy).unsqueeze(0).expand(num_patch, num_patch)
    for i in range(num_patch):
        for j in range(num_patch):
            mu = [i, j]
            sigmma = [std, std]
            Z = Gaussian2D(X, Y, mu, sigmma)
            filt_map[i*num_patch+j, :] = Z.contiguous().view(-1)
    return filt_map


# get random maskes
def RandomMask(num_patch, mask_ratio, mask_scale):
    index_H = num_patch // mask_scale[0]
    index_W = num_patch // mask_scale[1]
    
    index_list = [ind for ind in range(index_H*index_W)]
    random.shuffle(index_list)
    mask_num = int(mask_ratio*index_H*index_W)
    mask_index_list = index_list[:mask_num]
    
    coord1D = []
    coord2D = []
    for mi in mask_index_list:
        ih = mi//index_H
        iw = mi%index_W
        c1d, c2d = get_mask_index_list(ih, iw, mask_scale, num_patch)
        coord1D = coord1D + c1d
        coord2D = coord2D + c2d
    return coord1D, coord2D
        

def map_distance(map, pooling, distance='cosine'):

    if pooling == 'mean' or pooling=='cls':
        if distance=='cosine':
            dists = map.mm(map.t())
        elif distance=='hamming':
            map_bi = (map > map.mean()).float().type(torch.uint8)
            dists = torch.sum(map_bi.unsqueeze(1)^map_bi, dim=-1)   # hamming distance

    elif pooling=='map':
        if distance=='cosine':
            map_view0 = map.view(map.size(0), -1, map.size(-1)).unsqueeze(0)
            dists = torch.zeros(map.size(0), map.size(0))
            for i in range(map.size(0)):
                    map_i = map[i, :].view(1, -1, map.size(-1)).unsqueeze(1)
                    dists[i,...] = torch.sum(torch.mul(map_i, map_view0), dim=3).mean(2).squeeze()
        elif distance=='hamming':
            map_bi = ((map-map.mean()).abs()>0.1).float().view(map.size(0), -1).type(torch.uint8)
            dists = torch.zeros(map.size(0), map.size(0))
            for i in range(map.size(0)):
                    dists[i,...] = torch.sum(map_bi[i,:].view(1,1,-1)^map_bi, dim=-1)
    
    return dists
            


class WarmupLr():
    def __init__(self, initial) -> None:
        pass


def lr_lambda(epoch, scheduler, start_epoch, end_epoch, warmup_epoch):
    
    if epoch+start_epoch < warmup_epoch:
        lr_lambda = (epoch+start_epoch+1)/warmup_epoch

    elif scheduler=='cos':
        lr_lambda = 0.5*(math.cos((epoch+start_epoch-warmup_epoch)/(end_epoch-warmup_epoch)*math.pi)+1)   

    elif scheduler=='step':
        lr_lambda = 0.01+0.09*(epoch<end_epoch*0.5)+0.9*(epoch<end_epoch*0.2)
    
    elif scheduler=='cos_step':
        if epoch<end_epoch*0.4:
            lr_lambda = 0.5*(math.cos((epoch+start_epoch-warmup_epoch)/(end_epoch-warmup_epoch)*math.pi)+1)
        else:
            lr_lambda = 0.5*(math.cos((epoch+start_epoch-warmup_epoch)/(end_epoch-warmup_epoch)*math.pi)+1)*0.2

    return lr_lambda
    
    
    
    