import torch
from torch.nn import functional as F
from torchvision import transforms
from PIL import Image

import pandas as pd
import numpy as np
import time
import os
import copy
import pdb


# pre-process
transform_uni = transforms.Compose([
        transforms.Resize(size=[64,512]),
        transforms.Grayscale(1),
        transforms.ToTensor(),
        ])

    
class ImageClass():
    "Stores the paths to images for a given class"

    def __init__(self, name, image_paths, root_dir):
        self.name = name
        self.image_paths = [os.path.join(root_dir, i) for i in image_paths]

    def __str__(self):
        return self.name + ', ' + str(len(self.image_paths)) + ' images'

    def __len__(self):
        return len(self.image_paths)

class DataGenerator():
    def __init__(
            self,
            root_dir,
            txt,
            batch_size,
            people_per_batch,
            imgs_per_person,
            patch_size=(16,16),
            d=14, # max number of pixels for shifting
            p=0.5,
    ):
        self.root_dir = root_dir
        self.img_list = pd.read_csv(txt, header=0, names=['iris_img_path','class_index'])
        self.classes = self.img_list['class_index'].value_counts().to_dict()
        self.batch_size = batch_size
        self.batches = None
        self.people_per_batch = people_per_batch
        self.images_per_person = imgs_per_person

        self.patch_size = patch_size
        self.shift_d = d
        self.shift_p = p

        self.dataset = self.get_dataset()
        self.triplets = None
        self.anchor_labels = None

    def get_dataset(self):
        dataset = []
        norf_classes = len(self.classes)

        classes_list = list(self.classes.keys())

        print('-'*50)

        for i in range(norf_classes):
            class_name = classes_list[i]
            img_paths = self.img_list[self.img_list['class_index'] == class_name]['iris_img_path'].tolist()
            dataset.append(ImageClass(class_name, img_paths, self.root_dir))

        print('{} classes in total.'.format(len(dataset)))
        print('-'*50)

        return dataset

    def sample_people(self, dataset, people_per_batch, images_per_person):
        nrof_images = people_per_batch * images_per_person

        # Sample classes from the dataset
        nrof_classes = len(dataset)
        class_indices = np.arange(nrof_classes)
        np.random.shuffle(class_indices)

        i = 0
        image_paths = []
        image_labels = []
        num_per_class = []
        sampled_class_indices = []
        # Sample images from these classes until we have enough
        while len(image_paths) < nrof_images:
            class_index = class_indices[i]
            nrof_images_in_class = len(dataset[class_index])
            image_indices = np.arange(nrof_images_in_class)
            np.random.shuffle(image_indices)
            nrof_images_from_class = min(nrof_images_in_class, images_per_person, nrof_images - len(image_paths))
            idx = image_indices[0: nrof_images_from_class]
            image_paths_for_class = [dataset[class_index].image_paths[j] for j in idx]
            image_labels += [dataset[class_index].name for j in idx]
            sampled_class_indices += [class_index] * nrof_images_from_class
            image_paths += image_paths_for_class
            num_per_class.append(nrof_images_from_class)
            i += 1
        
        print('{} samples in total, Number of persons: {}, Samples per class: {}'.format(len(image_paths), len(num_per_class), num_per_class))

        return image_paths, image_labels, num_per_class

    def select_triplets(self, embeddings, nrof_images_per_class, image_paths, people_per_batch, map_size=(4,32), alpha=0.2):
        """ Select the triplets for training
        """
        trip_idx = 0
        emb_start_idx = 0
        num_trips = 0
        triplets = []

        embeddings_map = None

        for i in range(people_per_batch):
            nrof_images = int(nrof_images_per_class[i])
            for j in range(1, nrof_images):
                a_idx = emb_start_idx + j - 1
                neg_dists_sqr = self.distance(embeddings, embeddings_map, map_size, a_idx) # calculate distance
                pos_dists_sqr = copy.deepcopy(neg_dists_sqr[emb_start_idx:emb_start_idx + nrof_images])
                neg_dists_sqr[emb_start_idx:emb_start_idx + nrof_images] = np.NaN
                
                for pair in range(j, nrof_images):  # For every possible positive pair.
                    p_idx = emb_start_idx + pair
                    all_neg = np.where(neg_dists_sqr - pos_dists_sqr[pair] < alpha)[0]
                    nrof_random_negs = all_neg.shape[0]
                    if nrof_random_negs > 0:
                        
                        rnd_idx = np.random.randint(nrof_random_negs)
                        n_idx = all_neg[rnd_idx]
                        # # select the hardest negative sample, largely increase searching time
                        #n_idx = all_neg[0]
                        #s_neg_dist_sqr = neg_dists_sqr[n_idx]
                        #for idx in range(nrof_random_negs):
                        #    if neg_dists_sqr[all_neg[idx]] < s_neg_dist_sqr:
                        #        s_neg_dist_sqr = neg_dists_sqr[all_neg[idx]]
                        #        n_idx = all_neg[idx]
                        
                        triplets.append([image_paths[a_idx], image_paths[p_idx], image_paths[n_idx]])
                        trip_idx += 1

                    num_trips += 1

            emb_start_idx += nrof_images

        np.random.shuffle(triplets)

        return triplets, num_trips, len(triplets)

    def reset(self, mae_model, pool='mean', in_feats=768, cuda=False, alpha=0.2):
        t_s = time.perf_counter()

        image_paths, image_labels, num_per_class = self.sample_people(self.dataset, self.people_per_batch, self.images_per_person)
        nrof_examples = self.people_per_batch * self.images_per_person
        nrof_batches = int(np.floor(nrof_examples / self.batch_size))

        if pool=='map':
            patch_num = mae_model.num_patch[0]*mae_model.num_patch[1]+mae_model.Encoder.num_tokens
            embeddings = torch.zeros(nrof_batches * self.batch_size, patch_num, in_feats)
        else:
            embeddings = torch.zeros(nrof_batches * self.batch_size, 1, in_feats)
        labels = torch.zeros(nrof_batches * self.batch_size)
        for i in range(nrof_batches):
            imgs = torch.zeros(size=(self.batch_size, 3, 64, 512))
            for j in range(self.batch_size):
                img = self.shift_transform(image_paths[i * self.batch_size + j], p=0)
                imgs[j, :, :, :] = img
                labels[i * self.batch_size+j] = image_labels[i * self.batch_size + j]

            if cuda: imgs=imgs.cuda()
            with torch.no_grad():
                #pdb.set_trace()
                feat, _, _, _, _ = mae_model.Encoder.autoencoder(imgs, train=False, mask_index=None)
                if pool=='mean': feat = feat.mean(1).unsqueeze(1)
                elif pool=='cls': feat = feat[:,0].unsqueeze(1)
            embeddings[i*self.batch_size:(i+1)*self.batch_size, ...] = feat

        embeddings = F.normalize(embeddings)

        self.triplets, nrof_random_negs, nrof_triplets = self.select_triplets(
            embeddings, num_per_class, image_paths, self.people_per_batch, mae_model.num_patch, alpha)
        self.batches = int(np.floor(nrof_triplets / self.batch_size))
        #self.anchor_labels = labels

        t_e = time.perf_counter()
        print('INFO: Batch data generated, time cost={:.4f}s'.format(t_e - t_s))

    def shift_transform(self, img_file, p):
        # input: PIL.Image
        # output: shifted image, shift up to about d pixels
        # p: probability of performing this shift
        img = Image.open(img_file).convert('RGB')
        img = transform_uni(img)
        
        if torch.rand(1)<p:
            shift = np.random.randint(1, self.shift_d+1, size=1)[0]
            if torch.rand(1)>0.5: 
                left, right = img[:, :, :shift], img[:, :, shift:]
            else: 
                left, right = img[:, :, :-shift], img[:, :, -shift:]              
            shift_img = torch.cat((right, left), dim=2)
        
        else: shift_img = img

        return shift_img

    def distance(self, embeddings, embeddings_map, map_size, a_idx):
        if embeddings.size(1)==1:
            dis = torch.sum(torch.square(embeddings[a_idx] - embeddings), dim=-1).squeeze()
        else:
            
            if map_size[0]>map_size[1]:
                print('rotated mode not support now')
            else:
                if embeddings_map is not None:
                    map_dis = torch.sum(torch.square(embeddings_map[a_idx] - embeddings_map), dim=-1)
                    map_dis_min, _ = torch.min(map_dis, 1)
                    dis = map_dis_min.view(map_dis_min.size(0), -1).mean(-1)
                else:
                    map_dis =  torch.sum(torch.square(embeddings[a_idx] - embeddings), dim=-1)
                    dis = map_dis.view(map_dis.size(0), -1).mean(-1)
        return dis

    def gen(self):
        for i in range(self.batches):
            pos_sample = torch.zeros(self.batch_size, 3, 64, 512)
            anc_sample = torch.zeros(self.batch_size, 3, 64, 512)
            neg_sample = torch.zeros(self.batch_size, 3, 64, 512)


            for j in range(self.batch_size):
                ps_img_file = self.triplets[i*self.batch_size+j][0]
                as_img_file = self.triplets[i*self.batch_size+j][1]
                ns_img_file = self.triplets[i*self.batch_size+j][2]

                #pos_sample[j, ...] = transform_uni(Image.open(ps_img_file).convert('RGB')) 
                #anc_sample[j, ...] = transform_uni(Image.open(as_img_file).convert('RGB')) 
                #neg_sample[j, ...] = transform_uni(Image.open(ns_img_file).convert('RGB'))
                pos_sample[j, ...] = self.shift_transform(ps_img_file, p=self.shift_p)
                anc_sample[j, ...] = self.shift_transform(as_img_file, p=self.shift_p)
                neg_sample[j, ...] = self.shift_transform(ns_img_file, p=self.shift_p)

                #label[j] = self.anchor_labels[i*self.batch_size+j]

                # pos_mask_sample[j, ...] = transform_uni(
                #     Image.open(ps_img_file.replace('_img.png', '_mask.png')))
                # anc_mask_sample[j, ...] = transform_uni(
                #     Image.open(as_img_file.replace('_img.png', '_mask.png')))
                # neg_mask_sample[j, ...] = transform_uni(
                #     Image.open(ns_img_file.replace('_img.png', '_mask.png')))

            triplet_data = {
                'ps': pos_sample,
                # 'ps_mask': torch.ceil(pos_mask_sample[:,0,:,:]),
                'as': anc_sample,
                # 'as_mask': torch.ceil(anc_mask_sample[:,0,:,:]),
                'ns': neg_sample,
                # 'ns_mask': torch.ceil(neg_mask_sample[:,0,:,:])
            }

            yield triplet_data

if __name__ == '__main__':

    train_set_csv = './Protocols/ND0405/train_split.csv'
    root_dir = '/data/wyl2/Iris/'

    train_dataGenerator = DataGenerator(
            root_dir=root_dir,
            txt=train_set_csv,
            batch_size=20,
            people_per_batch=25,
            imgs_per_person=20
        )
    
    

