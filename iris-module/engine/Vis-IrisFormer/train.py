# -*- coding: utf-8 -*-

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0'
import sys
import logging
import shutil
import torch
import torchvision
import  math
import time
import random
import numpy as np
from sklearn.metrics import roc_curve, det_curve

import torchvision.transforms as transforms
from torch.cuda.amp import autocast as autocast
import torch.nn.functional as F
from torch.cuda.amp import GradScaler

from model.Transformers.VIT.mae import MAEVisionTransformers as MAE
from model.UGCL_loss import MapTripletMarginLoss
from triplet_data import DataGenerator
from batch_data import CSVDataset
import functions as Fun

from args_config import train_config
from data_config import ND0405In_config, ThsIn_config, MobIn_config

import pdb
import wandb

def get_tpr_at_fpr(tpr, fpr, thr):
    idx = np.argwhere(fpr > thr)
    return tpr[idx[0]][0]

def get_eer(tpr, fpr):
    for i, fpr_point in enumerate(fpr):
        if (tpr[i] >= 1 - fpr_point):
            idx = i
            break
    if (tpr[idx] == tpr[idx+1]):
        return 1 - tpr[idx]
    else:
        return fpr[idx]
    
def get_contributing_params(y, top_level=True):
    nf = y.grad_fn.next_functions if top_level else y.next_functions
    for f, _ in nf:
        try:
            yield f.variable
        except AttributeError:
            pass  # node has no tensor
        if f is not None:
            yield from get_contributing_params(f, top_level=False)    


class Trainer(object):

    def __init__(self, args, config, ckpt_path=None):
        self.args    = args
        self.use_gpu = args.use_gpu and torch.cuda.is_available()
        self.parallel = args.use_gpu and len(self.args.gpu_ids)>1
        self.config = config
        self.ckpt_path = ckpt_path
        self.result  = dict()

        logger = logging.getLogger()
        logger.setLevel(logging.INFO) 
        rq = time.strftime('%Y%m%d%H%M', time.localtime(time.time()))
        save_name = '_'+self.args.run_name
        log_path = os.getcwd() + '/checkpoint/'+self.config.data_name+'_'+self.config.test_type+save_name+'/'
        log_path = log_path.replace(' ','')
        os.makedirs(log_path, exist_ok=True)
        log_name = log_path + self.config.data_name + '_'+self.config.test_type + rq + '.log'
        logfile = log_name
        logger.handlers.clear()
        fh = logging.FileHandler(logfile, mode='w')
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        self.logger = logger
        
        if self.args.wandb:
            wandb.init(project='IrisFormer', name=self.args.run_name, reinit=True, config={'log name':rq})

        self.args.classnum = self.config.num_classGet()
        save_to = './checkpoint/'+self.config.data_name+'_'+self.config.test_type+save_name
        self.args.save_to = save_to.replace(' ','')
        os.makedirs(self.args.save_to, exist_ok=True)
    
    def _print_log_print(self, print_words):        
        print(print_words)
        self.logger.info(print_words)

    def _report_settings(self):
        ''' Report the settings '''

        str = '-' * 16
        self._print_log_print('%sEnvironment Versions%s' % (str, str))
        self._print_log_print("- Python    : {}".format(sys.version.strip().split('|')[0]))
        self._print_log_print("- PyTorch   : {}".format(torch.__version__))
        self._print_log_print("- TorchVison: {}".format(torchvision.__version__))
        self._print_log_print("- USE_GPU   : {}".format(self.use_gpu))
        self._print_log_print("- IS_DEBUG  : {}".format(self.args.is_debug))
        self._print_log_print('%sTraining Setting%s' % (str, str))
        self._print_log_print("- Dataset   : {}".format(self.config.data_name))
        self._print_log_print("- Protocol  : {}".format(self.config.test_type))
        self._print_log_print("- DUL using : {}".format(self.args.used_as))
        self._print_log_print("- Class num : {}".format(self.args.classnum))
        self._print_log_print('%sOutput Setting%s' % (str, str))        
        self._print_log_print("- Model path: {}".format(self.args.save_to)) 
        self._print_log_print('-' * 52)

    def _eval(self, feats, label):
        pdb.set_trace()
        feats = feats/feats.norm(dim=-1, keepdim=True)
        gallery_onehot = F.one_hot(label, num_classes=-1).float()

        sim_mat = Fun.map_distance(feats, pooling=self.args.ft_pool)

        sig_mat = torch.mm(gallery_onehot, gallery_onehot.t())
        scores = sim_mat.contiguous().view(-1)
        signals = sig_mat.contiguous().view(-1)
        
        ind_keep = 1. - torch.eye(feats.size(0))
        ind_keep = ind_keep.contiguous().view(-1)
        scores = scores[ind_keep>0]
        signals = signals[ind_keep>0]
        score_matrix = scores.reshape((-1, ))
        label_matrix = signals.reshape((-1, ))
        
        fpr, tpr, _ = roc_curve(label_matrix.cpu(), score_matrix.cpu(), pos_label=1)

        out_inf = {}
        out_inf['eer'] = get_eer(tpr,fpr)
        out_inf['RatAe-1'] = 1.-get_tpr_at_fpr(tpr, fpr, 1e-1)
        out_inf['RatAe-3'] = 1.-get_tpr_at_fpr(tpr, fpr, 1e-3)
        out_inf['RatAe-5'] = 1.-get_tpr_at_fpr(tpr, fpr, 1e-5)
        
        return out_inf

    def _model_loader(self):

        # ============ define model ============ #
        self.mae_model = MAE(
            img_size = self.args.input_size,
            patch_size = self.args.patch_size,  
            encoder_dim = 768,
            encoder_depth = 12,
            encoder_heads = 12,
            decoder_dim = 512,
            decoder_depth = 8,
            decoder_heads = 16, 
            mask_ratio = self.args.mask_ratio,
            num_classes = self.args.classnum,
            pos_embed = self.args.position_embedding,
            ft_pool = self.args.ft_pool,
            bottleneck = self.args.bottleneck,
            bottleneck_dim = self.args.bottleneck_feats
        )
        
        self.loss_fun = MapTripletMarginLoss(pooling=self.args.ft_pool, reduction='mean')

        if self.ckpt_path is not None:
           assert os.path.isfile(self.ckpt_path)
           try: ckpt = torch.load(self.ckpt_path, map_location="cpu")['state_dict']
           except: ckpt = torch.load(self.ckpt_path, map_location="cpu")['mae_model']
           model_dict = self.mae_model.state_dict()
           ckpt = {k:v for k,v in ckpt.items() if k in model_dict and 'Encoder' in k}
           if not self.args.patch_size[0]==16:
               print('patch size:', self.args.patch_size[0])
               ckpt.pop('Encoder.patch_embed.proj.bias')
               ckpt.pop('Encoder.patch_embed.proj.weight')

           model_dict.update(ckpt)
           self.mae_model.load_state_dict(model_dict)

        
        # GPU settings
        if self.use_gpu:
            self.mae_model.cuda()
            self.loss_fun.cuda()

        if self.parallel:
            self.mae_model = torch.nn.DataParallel(self.mae_model,device_ids=self.args.gpu_ids)
            self._print_log_print('Parallel mode was going ...')
        elif self.use_gpu:
            self._print_log_print('Single-gpu mode was going ...')
        else:
            self._print_log_print('CPU mode was going ...')

        
        # ================== optimizer ================== #
        # Params of MAE model
        params = []
        for name, value in self.mae_model.named_parameters():
            params += [{'params':value, 'lr': self.args.lr_backbone}]

        self.optimizer = torch.optim.AdamW(params, weight_decay=self.args.weight_decay)
        
        # schedueler
        if self.args.warmup=='cos':
            warmup_lambda = lambda epoch: (epoch+self.args.start_epoch+1)/self.args.warmup_epoch if epoch+self.args.start_epoch<self.args.warmup_epoch else 0.5*(math.cos((epoch+self.args.start_epoch-self.args.warmup_epoch)/(self.args.end_epoch-self.args.warmup_epoch)*math.pi)+1)
        elif self.args.warmup=='exp':
            warmup_lambda = lambda epoch: (epoch+self.args.start_epoch+1)/self.args.warmup_epoch if epoch+self.args.start_epoch<self.args.warmup_epoch else 0.9**(epoch+self.args.start_epoch-self.args.warmup_epoch)
        elif self.args.warmup=='step':
            warmup_lambda = lambda epoch: (epoch+self.args.start_epoch+1)/self.args.warmup_epoch if epoch+self.args.start_epoch<self.args.warmup_epoch else 0.01+0.09*(epoch<self.args.end_epoch*0.5)+0.9*(epoch<self.args.end_epoch*0.2)
        elif self.args.warmup=='cos_step':
            warmup_lambda = lambda epoch:(epoch+self.args.start_epoch+1)/self.args.warmup_epoch if epoch+self.args.start_epoch<self.args.warmup_epoch else 0.5*(math.cos((epoch+self.args.start_epoch-self.args.warmup_epoch)*(0.2+0.8*(epoch<self.args.end_epoch*0.4))/(self.args.end_epoch-self.args.warmup_epoch)*math.pi)+1)

        self.schedueler = torch.optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=warmup_lambda)
        self.scaler = GradScaler()

        self._print_log_print('Model loading was finished ...')

    def _data_loader(self):

        self.args.input_size = (64, 512)
        
        # ============ gat data =============== #
        transform_uni = transforms.Compose([
            transforms.Resize(size=[64,512]),
            transforms.ToTensor()])

        self.train_loader = DataGenerator(root_dir=self.config._root_path,
                                      txt=self.config._train_list, 
                                      batch_size=self.args.batch_size, 
                                      people_per_batch=self.args.people_per_batch_train,
                                      imgs_per_person=self.args.images_per_person_train,
                                      patch_size=self.args.patch_size,
                                      d=self.args.shift_pixel,
                                      p=self.args.shift_possibility,
                                      )
                              
        val_dataset = CSVDataset(csv_file=self.config._val_list, root_dir=self.config._root_path, transform=transform_uni)
        self.val_loader = torch.utils.data.DataLoader(
            val_dataset,
            batch_size=self.args.batch_size,
            num_workers=self.args.num_workers,
            pin_memory=True,
            drop_last=False)
        
        if self.args.test_while_train:
            print('using test while training')
            test_dataset = CSVDataset(csv_file=self.config._test_list, root_dir=self.config._root_path, transform=transform_uni)
            self.test_loader = torch.utils.data.DataLoader(
                test_dataset,
                batch_size=self.args.batch_size,
                num_workers=self.args.num_workers,
                pin_memory=True,
                drop_last=False)


        self._print_log_print('Data loading was finished ...')
    
    def _train_one_epoch(self, epoch = 0):

        self.mae_model.train()
        
        if self.args.triplet_step:
            if epoch<self.args.end_epoch*0.2:
                triplet_alpha = self.args.triplet_alpha*0.5
            elif epoch<self.args.end_epoch*0.5:
                triplet_alpha = self.args.triplet_alpha*0.75
            else:
                triplet_alpha = self.args.triplet_alpha
        else:
            triplet_alpha = self.args.triplet_alpha
        
        self.train_loader.reset(self.mae_model, pool=self.args.ft_pool, in_feats=self.args.in_feats, cuda=self.args.use_gpu, alpha=triplet_alpha)
        train_g = self.train_loader.gen()
        
        loss_recorder = []

        for idx in range(self.train_loader.batches):

            triplet_batch = next(train_g)
            img_ps = triplet_batch['ps']
            img_as = triplet_batch['as']
            img_ns = triplet_batch['ns']
            
            if self.use_gpu:
                img_ps, img_as, img_ns = img_ps.cuda(), img_as.cuda(), img_ns.cuda()
            
            # forward
            mask_index = None
            if self.args.mask_ratio>0:
                token_index = [tidx for tidx in range(self.mae_model.num_patch[0]*self.mae_model.num_patch[1])]
                mask_index = random.sample(token_index, int(len(token_index)*self.args.mask_ratio))
                
            with autocast():
                embedding_as,_,_, mu_as, logvar_as = self.mae_model.Encoder.autoencoder(img_as, train=True, mask_index=mask_index)
                embedding_ps,_,_, mu_ps, logvar_ps = self.mae_model.Encoder.autoencoder(img_ps, train=True, mask_index=mask_index)
                embedding_ns,_,_, mu_ns, logvar_ns = self.mae_model.Encoder.autoencoder(img_ns, train=True, mask_index=mask_index)

                tri_loss = self.loss_fun(embedding_as, embedding_ps, embedding_ns, margin=triplet_alpha)
                
                if logvar_as is not None:
                    kl_loss_ps = -(1 + logvar_ps  - logvar_ps.exp()) / 2
                    kl_loss_as = -(1 + logvar_as  - logvar_as.exp()) / 2
                    kl_loss_ns = -(1 + logvar_ns  - logvar_ns.exp()) / 2
                    kl_loss = kl_loss_ps.sum(dim=-1).mean()+kl_loss_as.sum(dim=-1).mean()+kl_loss_ns.sum(dim=-1).mean()
                else: kl_loss = 0
                
                loss = tri_loss + self.args.kl_lambda * kl_loss
            
            # backward
            self.optimizer.zero_grad()
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            
            loss_recorder.append(loss.item())
            if (idx + 1) % self.args.print_freq == 0:
                current_lr = self.optimizer.state_dict()['param_groups'][0]['lr']
                self._print_log_print('epoch : %2d|%2d, iter : %3d|%3d, lr : %.5f,  loss : %.4f' % \
                        (epoch, self.args.end_epoch, idx+1, self.train_loader.batches, current_lr, np.mean(loss_recorder)))
                
                if self.args.wandb:
                    wandb.log({'train/loss': np.mean(loss_recorder)})

        train_loss = np.mean(loss_recorder)
        print('train_loss : %.4f' % train_loss)

        return train_loss
      
    def _test_model(self, epoch, dataset, test='val'):

        self.mae_model.eval()
        whole_feature = 1        
        
        with torch.no_grad():
            for data, label in dataset:           
                data.requires_grad = False
                label.requires_grad = False

                if self.use_gpu:
                    data = data.cuda()
                    label = label.cuda()

                norm_embedding,_,_,_,_ = self.mae_model.Encoder.autoencoder(data, train=False, mask_index=None)

                if self.args.ft_pool == 'mean':
                    data_feature = norm_embedding.mean(1)
                elif self.args.ft_pool == 'cls':
                    data_feature = norm_embedding[:,0]
                elif self.args.ft_pool == 'map':
                    data_feature = norm_embedding

                if torch.is_tensor(whole_feature):
                    whole_feature = torch.cat((whole_feature, data_feature),0)
                    whole_label = torch.cat((whole_label,label),0)            
                else:
                    whole_feature = data_feature
                    whole_label = label

        eval = self._eval(whole_feature, whole_label)
        self._print_log_print('EPOCH-{} {}: EER {:.4f} | R@A1e-1 {:.4f} | R@A1e-3 {:.4f} | R@A1e-5 {:.4f}\n'
                                .format(epoch, test, eval['eer'], eval['RatAe-1'], eval['RatAe-3'], eval['RatAe-5']))
        if self.args.wandb:
            wandb.log({test+'/eer':eval['eer'], test+'/RatAe-1':eval['RatAe-1'], 
                       test+'/RatAe-3':eval['RatAe-3'], test+'/RatAe-5':eval['RatAe-5']})
        return eval
    
    def _save_weights(self, testinfo = {}):
        ''' save the weights during the process of training '''
        
        if not os.path.exists(self.args.save_to):
            os.mkdir(self.args.save_to)
            
        sota_flag = self.result['sota_eer'] > testinfo['eer']

        save_name = '%s/epoch_%02d-eer_%.4f.pth' % \
                         (self.args.save_to, self.result['epoch'], testinfo['eer'])
        if sota_flag:
            save_name = '%s/sota.pth' % self.args.save_to
            self.result['sota_eer']   = testinfo['eer']
            self._print_log_print('%s Yahoo, SOTA model was updated %s' % ('*'*16, '*'*16))
            
            torch.save({
                'epoch'   : self.result['epoch'], 
                'mae_model': self.mae_model.state_dict(),
                'sota_acc': testinfo['eer']}, save_name)

            normal_name = '%s/epoch_%02d-eer_%.4f.pth' % \
                         (self.args.save_to, self.result['epoch'], testinfo['eer'])
            shutil.copy(save_name, normal_name)
        
    def _dul_training(self):
        
        self.result['sota_eer']   = 1
        for epoch in range(self.args.start_epoch, self.args.end_epoch + 1):

            start_time = time.time()
            self.result['epoch'] = epoch
            self._train_one_epoch(epoch)
            self.schedueler.step()
            eval_info = self._test_model(epoch, self.val_loader, test='val')
            if self.args.test_while_train:
                eval_info = self._test_model(epoch, self.test_loader, test='test')
            end_time = time.time()
            self._print_log_print('Single epoch cost time : %.2f mins' % ((end_time - start_time)/60))
            self._save_weights(eval_info)
            
            if self.args.is_debug:
                break

            if epoch==self.args.early_stop:
                break
    
    def train_runner(self):

        self._report_settings()

        self._data_loader()

        self._model_loader()

        self._dul_training()

        if self.args.wandb:
            wandb.finish()
    

# ============================================ #
# =================== main =================== #
# ============================================ #
    
if __name__ == '__main__':
    
    input_args = train_config()

    # dataset configerations
    config = ND0405In_config()
    #config = ThsIn_config()
    #config = MobIn_config()
    mae_ckpt = './model/vit-mae_losses_0.201.pth'

    trainer = Trainer(input_args, config, mae_ckpt)
    trainer.train_runner()




    
        
        
        
        

