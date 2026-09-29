# -*- coding: utf-8 -*-

class Config(object):
    def __init__(self):
 
        ####################### training dataset #########################  
        self._root_path='/path/to/your/dataset/'
        self._train_list='./Protocols/ND0405/train_split.csv'
        self._val_list ='./Protocols/ND0405/val_split.csv'
        self._num_class = 356

        ####################### testing dataset #########################
        # CASIA-Distance
        self._root_path_test = '/path/to/your/dataset/'
        self._test_list = './Protocols/ND0405/test.csv'
        
        self.data_name = 'ND0405'
        self.test_type = 'Within'
        
    def num_classGet(self):
        return self._num_class

    def load_detailGet(self):
        return self._root_path, self._train_list

    def test_loaderGet(self):
        return  self._root_path_test, self._test_list
