#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
import pdb
import torch
import random
from tqdm import tqdm
import numpy as np
import torch.nn.functional as F
import torchvision.transforms as transforms

from data_config import ND0405In_config, ThsIn_config, MobIn_config
from args_config import test_config
from model.Transformers.VIT.mae import MAEVisionTransformers as MAE
from batch_data import CSVDataset, TestDataset

import pandas as pd
from sklearn.metrics import roc_curve

from pyeer.eer_info import get_eer_stats
from pyeer.report import generate_eer_report, export_error_rates

torch.backends.cudnn.bencmark = True


def get_tpr_at_fpr(tpr, fpr, thr):
    idx = np.argwhere(fpr > thr)
    return tpr[idx[0]][0]

def get_eer(tpr, fpr):
    for i, fpr_point in enumerate(fpr):
        if (tpr[i] >= 1 - fpr_point):
            print('last:', tpr[i-1], 1-fpr[i-1], fpr[i-1])
            print('current:', tpr[i], 1-fpr[i], fpr[i])
            print('next:', tpr[i+1], 1-fpr[i+1], fpr[i+1])
            print('now:', i)
            idx = i
            break
    if (tpr[idx] == tpr[idx+1]):
        return 1 - tpr[idx]
    else:
        return fpr[idx]

class Tester(object):

    def __init__(self, args, config, check_path, save_path):
        self.args    = args
        self.use_gpu = args.use_gpu and torch.cuda.is_available()
        self.config = config
        self.check_path = check_path
        self.save_path = save_path

        os.makedirs(self.save_path, exist_ok=True)

        self.transform_uni = transforms.Compose([
            transforms.Resize(size=[64,512]),
            transforms.ToTensor(),
            ])
    
    def _data_loader(self):

        str = '-' * 16
        print('%sDataset Loading%s' % (str, str))

        self.args.input_size = (64, 512)

        ROOT_DIR, CSV_FILE  = self.config.test_loaderGet()

        test_dataset = CSVDataset(csv_file=CSV_FILE, root_dir=ROOT_DIR, 
                                  transform=self.transform_uni)

        self.test_loader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=self.args.batch_size,
            num_workers=self.args.workers,
            pin_memory=True,
            drop_last=False
        )

        print('Data loading was finished ...')
    
    def _data_loader_equal(self):
        print('Loading datasets from {} and formulating pairs from...'.format(self.config.data_name))
        test_txt = self.config._test_list
        img_list = pd.read_csv(test_txt, header=0, names=['iris_img_path','class_index'])
        # img_list = img_list[img_list['class_index'] < 50]

        ava_idx = img_list['class_index'].value_counts().index
        classes = ava_idx.shape[0] # img_list['label'].value_counts().shape[0]
        print('{} classes in total.'.format(classes))

        imgs_path = img_list['iris_img_path'].to_list()
        imgs_class = img_list['class_index'].to_list()

        genuine_pairs = []
        impostor_pairs = []

        for i in range(len(imgs_path)):
            if i == len(imgs_path) - 1:
                break
            img1_path = imgs_path[i]
            img1_class = imgs_class[i]

            for j in range(i+1, len(imgs_path)):
                img2_path = imgs_path[j]
                img2_class = imgs_class[j]

                if img1_class == img2_class:
                    genuine_pairs.append([img1_path, img2_path])
                else:
                    impostor_pairs.append([img1_path, img2_path])
        print('{} genuine pairs, {} impostor pairs.'.format(len(genuine_pairs), len(impostor_pairs)))
        
        # Sample the same number of genuine and impostor pairs
        sample_number = min(len(genuine_pairs), self.args.sample_pairs_number)
        genuine_pairs = random.sample(genuine_pairs, sample_number)
        impostor_pairs = random.sample(impostor_pairs, sample_number)
        print('Selecting {} Genuine Pairs AND {} Impostor Pairs'.format(sample_number, sample_number))

        #pdb.set_trace
        genuine_dataset = TestDataset(genuine_pairs, self.config._root_path, transform=self.transform_uni)
        self.genuine_loader = torch.utils.data.DataLoader(
                    genuine_dataset,
                    batch_size=self.args.batch_size,
                    num_workers=self.args.workers,
                    pin_memory=True,
                    drop_last=False)
        impostor_dataset = TestDataset(impostor_pairs, self.config._root_path, transform=self.transform_uni)
        self.impostor_loader = torch.utils.data.DataLoader(
                    impostor_dataset,
                    batch_size=self.args.batch_size,
                    num_workers=self.args.workers,
                    pin_memory=True,
                    drop_last=False)


    def _model_loader(self):

        str = '-' * 16
        print('%sModel Loading%s' % (str, str))

        self.model = MAE(
            img_size = self.args.input_size,
            patch_size = self.args.patch_size,  
            encoder_dim = 768,
            encoder_depth = 12,
            encoder_heads = 12,
            decoder_dim = 512,
            decoder_depth = 8,
            decoder_heads = 16, 
            mask_ratio = self.args.mask_ratio,
            num_classes = self.config._num_class,
            ft_pool = self.args.ft_pool,
            pos_embed = self.args.position_embedding,
            bottleneck = self.args.bottleneck,
            bottleneck_dim = self.args.bottleneck_feats
        )
        
        if self.check_path is None:
            raise FileNotFoundError('Not given any pre-trained model path.')
        
        ckpt_dict = torch.load(self.check_path, map_location="cpu")
        ckpt_dict_filtered = {k:v for k, v in ckpt_dict['mae_model'].items() if 'ft' not in k}
        self.model.load_state_dict(ckpt_dict_filtered, strict=False)

        print('current SOTA model is from epoch:', ckpt_dict['epoch'], 'with EER=', ckpt_dict['sota_acc'])

        if self.use_gpu and len(self.args.gpu_ids) > 1:
            self.model = torch.nn.DataParallel(self.model, device_ids=self.args.gpu_ids)
            print('Parallel mode was going ...')
        elif self.use_gpu:
            self.model.cuda()
            print('Single-gpu mode was going ...')
        else:
            print('CPU mode was going ...')
        
        print('Model loading was finished ...')

    def _report_settings(self):
        ''' Report the settings '''

        str = '-' * 16
        print('%sTesting Setting%s' % (str, str))
        print("- Dataset   : {}".format(self.config.data_name))
        print("- Protocol  : {}".format(self.config.test_type))
        print("- Checkpoint: {}".format(self.check_path))
        print("- Out path  : {}".format(self.save_path))
        print('-' * 52)         
    
    def _test_model(self, dataset):

        str = '-' * 16
        print('%sTest Running%s' % (str, str))

        self.model.eval()
        whole_distance = 1
        
        with torch.no_grad():
            for img1, img2 in tqdm(dataset):           
                img1.requires_grad = False
                img2.requires_grad = False

                if self.use_gpu:
                    img1, img2 = img1.cuda(), img2.cuda()

                norm_embedding1,_,_,_,_ = self.model.Encoder.autoencoder(img1, train=False, mask_index=None)
                if self.args.ft_pool == 'mean':
                    data_feature1 = norm_embedding1.mean(1).unsqueeze(-2)
                elif self.args.ft_pool == 'cls':
                    data_feature1 = norm_embedding1[:,0].unsqueeze(-2)
                elif self.args.ft_pool == 'map':
                    data_feature1 = norm_embedding1

                norm_embedding2,_,_,_,_ = self.model.Encoder.autoencoder(img2, train=False, mask_index=None)
                if self.args.ft_pool == 'mean':
                    data_feature2 = norm_embedding2.mean(1).unsqueeze(-2)
                elif self.args.ft_pool == 'cls':
                    data_feature2 = norm_embedding2[:,0].unsqueeze(-2)
                elif self.args.ft_pool == 'map':
                    data_feature2 = norm_embedding2
                    
                distance = torch.mean(F.cosine_similarity(data_feature1, data_feature2, dim=-1), dim=-1)
                      
                if torch.is_tensor(whole_distance):
                    whole_distance = torch.cat((whole_distance, distance),0)           
                else:
                    whole_distance = distance
        
        return whole_distance.cpu()
        
    
    def test_runner(self):

        self._report_settings()

        self._data_loader_equal()

        self._model_loader()

        distance_genuine = self._test_model(self.genuine_loader)
        label_genuine = torch.ones_like(distance_genuine, dtype=torch.int)
        distance_impostor = self._test_model(self.impostor_loader)
        label_impostor = torch.zeros_like(distance_impostor, dtype=torch.int)

        distance = torch.cat((distance_genuine, distance_impostor), dim=0)
        label = torch.cat((label_genuine, label_impostor), dim=0)
        fpr, tpr, _ = roc_curve(label, distance, pos_label=1)
        eer = get_eer(tpr,fpr)
        prec1 = 1.-get_tpr_at_fpr(tpr, fpr, 1e-1)
        prec2 = 1.-get_tpr_at_fpr(tpr, fpr, 1e-3)
        prec3 = 1.-get_tpr_at_fpr(tpr, fpr, 1e-5)
        print('total fpr list length:', len(fpr))
        print('RESULTS: EER {:.4f} | R@A1e-1 {:.4f} | R@A1e-3 {:.4f} | R@A1e-5 {:.4f}\n'.format(eer,prec1,prec2,prec3))

        if self.args.save_report:
            stats = get_eer_stats(distance_genuine.tolist(), distance_impostor.tolist(), ds_scores=False)
            generate_eer_report([stats], [self.config.data_name], self.save_path+'/'+self.config.data_name+'_report.csv')
            export_error_rates(stats.fmr, stats.fnmr, self.save_path+'/'+self.config.data_name+'_det.csv' )





if __name__ == "__main__":
     
    input_args = test_config()

    config_test = ND0405In_config()
    #config_test = ThsIn_config()
    #config_test = MobIn_config()
    
    run_name = 'ND0405_Within_rope2d0.75_lr1_step_pshift14'
    checkpoint_path = './checkpoint/'+run_name+'/sota.pth'
    mat_path = './checkpoint/'+run_name+'/eval/'

    tester = Tester(input_args, config_test, checkpoint_path, mat_path)
    tester.test_runner()

