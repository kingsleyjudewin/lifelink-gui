#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
import torch.nn.functional as F

import pdb

class ClsLoss(nn.Module):
    ''' Classic loss function for face recognition '''

    def __init__(self, args):

        super(ClsLoss, self).__init__()
        self.args     = args


    def forward(self, predy, target, weight = None, mu = None, logvar = None):

        loss = None
        sum_weight = 0.
        if self.args.loss_mode == 'focal_loss':
            logp = F.cross_entropy(predy, target, reduction='none')
            prob = torch.exp(-logp)
            loss = ((1-prob) ** self.args.loss_power * logp).mean()

        elif self.args.loss_mode == 'hardmining':
            batch_size = predy.shape[0]
            logp      = F.cross_entropy(predy, target, reduction='none')
            inv_index = torch.argsort(-logp) # from big to small
            num_hard  = int(self.args.hard_ratio * batch_size)
            hard_idx  = inv_index[:num_hard]
            loss      = torch.sum(F.cross_entropy(predy[hard_idx], target[hard_idx]))

        elif self.args.loss_mode == 'triplet':
            n = predy.size(0)	# batch_size
            
            # Compute pairwise distance, replace by the official when merged
            dist = torch.pow(predy, 2).sum(dim=1, keepdim=True).expand(n, n)
            dist = dist + dist.t()
            dist.addmm_(1, -2, predy, predy.t())
            dist = dist.clamp(min=1e-12).sqrt()  # for numerical stability
            
            # For each anchor, find the hardest positive and negative
            mask = target.expand(n, n).eq(target.expand(n, n).t())
            dist_ap, dist_an = [], []
            for i in range(n):
                dist_ap.append(dist[i][mask[i]].max().unsqueeze(0))
                dist_an.append(dist[i][mask[i] == 0].min().unsqueeze(0))
            dist_ap = torch.cat(dist_ap)
            dist_an = torch.cat(dist_an)
            
            # Compute ranking hinge loss
            y = torch.ones_like(dist_an)
            loss = F.margin_ranking_loss(dist_an, dist_ap, y, margin=self.args.triplet_margin, reduction='mean')
        
        else: # navie-softmax
            loss_list = F.cross_entropy(predy, target, reduction="none")
            if (weight is not None):
                loss_list = loss_list * weight
            loss = torch.mean(loss_list)

        if (mu is not None) and (logvar is not None):
            kl_loss = -(1 + logvar - mu.pow(2) - logvar.exp()) / 2
            kl_loss = kl_loss.sum(dim=1).mean()
            loss    = loss + self.args.kl_lambda * kl_loss
            
        return loss

class MapTripletMarginLoss(nn.Module):
    def __init__(self, pooling='none', reduction='mean'):
        super(MapTripletMarginLoss, self).__init__()
        self.pooling = pooling
        self.reduction = reduction

    def forward(self, anchor, positive, negative, margin):
        #pdb.set_trace()
        if self.pooling=='cls':
            anchor = anchor[:, 0]
            positive = positive[:, 0]
            negative = negative[:, 0]
            loss = F.triplet_margin_loss(anchor, positive, negative, margin=margin, reduction=self.reduction)
        
        elif self.pooling=='mean':
            anchor = anchor.mean(1)
            positive = positive.mean(1)
            negative = negative.mean(1)
            loss = F.triplet_margin_loss(anchor, positive, negative, margin=margin, reduction=self.reduction)
        
        elif self.pooling=='map':
            #pdb.set_trace()
            dis_ap = torch.mean(torch.sqrt(torch.square(anchor-positive).sum(-1)), dim=-1)
            dis_an = torch.mean(torch.sqrt(torch.square(anchor-negative).sum(-1)), dim=-1)
            #loss = torch.max(dis_ap-dis_an+self.margin, 0)[0]
            target = torch.ones_like(dis_an) * -1.
            loss = F.margin_ranking_loss(dis_ap, dis_an, target, margin=margin, reduction=self.reduction)
            #if self.reduction=='mean':
            #    loss = loss.mean()

        return loss
        