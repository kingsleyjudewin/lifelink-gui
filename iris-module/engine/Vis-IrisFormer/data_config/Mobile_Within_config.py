# -*- coding: utf-8 -*-

class Config(object):
    def __init__(self):
        # image path
        self._root_path = []
        self._train_list = []
        self._num_class = []
        self._normalize = []   
        ####################### training dataset #########################    
        self._root_path = "/path/to/your/dataset/"
        self._train_list = './Protocols/Mobile/train.csv'
        self._num_class = 180

        ####################### testing dataset #########################
        self._root_path_test = "/path/to/your/dataset/"
        self._val_list = './Protocols/Mobile/test.csv'
        self._test_list = './Protocols/Mobile/test.csv'

        self.data_name = 'CASIAMobile'
        self.test_type = 'Within'
        

    def num_classGet(self):
        return self._num_class

    def load_detailGet(self):
        return self._root_path, self._train_list
    
    def test_loaderGet(self):
        return  self._root_path_test, self._test_list

