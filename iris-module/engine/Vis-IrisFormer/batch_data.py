"""
dataset for iris dataset finetunning
Created by RenMin 20211206
Modified by Yunlong Wang 20230823
"""
import os.path as op
import torch
from torch.utils.data import Dataset
from torch.nn import functional as F
from torchvision import transforms
from PIL import Image
import pandas as pd
import numpy as np
import time


import pdb

from torchvision.transforms.functional import rotate

transform_uni = transforms.Compose([
        transforms.Resize(size=[64,512]),
        transforms.Grayscale(1),
        transforms.ToTensor(),
        ])

class ImageClass():
    "Stores the paths to images for a given class"

    def __init__(self, name, image_paths):
        self.name = name
        self.image_paths = image_paths

    def __str__(self):
        return self.name + ', ' + str(len(self.image_paths)) + ' images'

    def __len__(self):
        return len(self.image_paths)


class CSVDataset(Dataset):
    
    def __init__ (self, csv_file, root_dir=None, transform=None, patch_size=[16,16]):
        super(CSVDataset, self).__init__()
        assert op.isfile(csv_file), csv_file
        self.img_list = pd.read_csv(csv_file, header=0, names=['iris_img_path','class_index'])
        self.root_dir = root_dir
        self.classes = self.img_list['class_index'].value_counts().to_dict()
        self.dataset = self.get_dataset()
        self.transform = transform
        self.patch_size = patch_size


    def get_dataset(self):
        dataset = []
        norf_classes = len(self.classes)

        classes_list = list(self.classes.keys())

        for i in range(norf_classes):
            class_name = classes_list[i]
            img_paths = self.img_list[self.img_list['class_index'] == class_name]['iris_img_path'].tolist()
            dataset.append(ImageClass(class_name, img_paths))

        return dataset, norf_classes

    def __getitem__(self, index):
        fn = self.img_list['iris_img_path'][index]
        if self.root_dir is not None:
            fn = op.join(self.root_dir, fn)
        label = self.img_list['class_index'][index]
        img = Image.open(fn).convert('RGB')
        if self.transform is not None:
            img = self.transform(img)

        return img, int(label)

    def __len__(self):
        return len(self.img_list['iris_img_path'])


class TestDataset(Dataset):
    def __init__(self, pair_list, root_dir=None, transform=None):
        super(TestDataset, self).__init__()
        self.pair_list = pair_list
        self.root_dir = root_dir
        self.transform = transform


    def read_img(self, img_path):
        if self.root_dir is not None: img_path = op.join(self.root_dir, img_path)
        img = Image.open(img_path).convert('RGB')
        if self.transform is not None:
            img = self.transform(img)

        return img

    def __getitem__(self, index):
        img1 = self.read_img(self.pair_list[index][0])
        img2 = self.read_img(self.pair_list[index][1])
        return img1, img2

    def __len__(self):
        return len(self.pair_list)


    
if __name__ == '__main__':

    # pdb.set_trace()
    ROOT_DIR = '/data/wyl2/Iris'
    TRAIN_CSV = './Protocols/ND0405/train.csv'
    TEST_CSV = './Protocols/ND0405/test.csv'
    
    train_dataset = CSVDataset(TRAIN_CSV, ROOT_DIR, transform=transform_uni, shift=30)
    print ('length of train set: {}'.format(len(train_dataset)) )
    
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=4,
        num_workers=8,
        pin_memory=True,
        drop_last=False
    )
    
    #val_dataset  = CSVDataset(TEST_CSV, ROOT_DIR, transform=transform_uni)
    #print ('length of validation set:', len(val_dataset))

    for img, label in train_loader:
        print(img.size())
        break
    

