import torch
import numpy as np
from enum import Enum
from torch.utils.data import Dataset
from numpy import random


class LabelType(Enum):
    """Formats of label."""
    MULTI_CLASS = 1
    MULTI_LABEL = 2


datasets = {}


def register_dataset(name: str, type: LabelType):
    """
    Register dataset with dataset name and label type.
    :param str name: dataset name.
    :param LabelType type: the type of label for the dataset.
    :return: function decorator that registers the dataset.
    """

    def dataset_decor(get_fn):
        datasets[name] = (type, get_fn)
        return get_fn

    return dataset_decor


class DatasetOnMemory(Dataset):
    """
    A PyTorch dataset where all data lives on CPU memory.
    """

    def __init__(self, X, y, n_class, meta_data=None):
        assert len(X) == len(y), "X and y must have the same length."
        self.X = X
        self.y = y
        self.n_class = n_class

        if meta_data is not None:
            assert len(X) == len(meta_data), "X and y must have the same length."
        self.meta_data = meta_data

    def __len__(self):
        return len(self.y)

    def __getitem__(self, item):
        x = self.X[item]
        y = self.y[item]
        return x, y

    def get_inputs(self):
        return self.X

    def get_labels(self):
        return self.y


class TransformDataset(Dataset):
    """
    A PyTorch Dataset where you can dynamically set transforms.

    Be careful about its behavior when combined with dataloaders!
    See https://discuss.pytorch.org/t/changing-transformation-applied-to-data-during-training/15671 for details.
    """

    def __init__(self, dataset, transform=None, target_transform=None, ignore_metadata=False):
        self.dataset = dataset
        self.__transform = transform
        self.__target_transform = target_transform
        self.__default_transform = transform
        self.__default_target_transform = target_transform
        self.ignore_metadata = ignore_metadata
        self.__transform_seed = 42

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, item):
        if self.ignore_metadata:
            x, y = self.dataset[item][:2]
        else:
            x, y = self.dataset[item]

        if self.__transform:
            x = self.__transform(x)
        if self.__target_transform:
            y = self.__target_transform(y)
        return x, y

    def set_transform(self, transform):
        self.__transform = transform

    def set_target_transform(self, target_transform):
        self.__target_transform = target_transform

    def set_to_default_transform(self):
        self.__transform = self.__default_transform

    def set_to_default_target_transform(self):
        self.__target_transform = self.__default_target_transform


class ALDataset:
    """
    Dataset for active learning. The dataset contains all of training, validation and testing data as well as their
    embeddings. The dataset also tracks the examples that have been labeled.
    """

    def __init__(self, train_dataset, val_dataset, test_dataset, train_labels, val_labels, test_labels, label_type,
                 num_classes, classnames, train_emb_mean=np.mean, train_emb_std=np.std,
                train_weak_labels=None, val_weak_labels=None, test_weak_labels=None):
        """
        :param torch.utils.data.Dataset train_dataset: Training dataset that contains both examples and labels.
        :param torch.utils.data.Dataset val_dataset: Validation dataset that contains both examples and labels.
        :param torch.utils.data.Dataset test_dataset: Testing dataset that contains both examples and labels.
        :param Optional[numpy.ndarray] train_labels: All training labels for easy accessibility.
        :param Optional[numpy.ndarray] val_labels: All validation labels for easy accessibility.
        :param Optional[numpy.ndarray] test_labels: All testing labels for easy accessibility.
        :param LabelType label_type: Type of labels.
        :param int num_classes: Number of classes of the dataset.
        :param List[str] classnames: A list of names of each class.
        """
        assert isinstance(
            train_dataset, TransformDataset), "Training dataset must be a TransformDataset."
        assert isinstance(
            val_dataset, TransformDataset), "Validation dataset must be a TransformDataset."
        assert isinstance(
            test_dataset, TransformDataset), "Test dataset must be a TransformDataset."
        self.train_dataset = train_dataset
        self.val_dataset = val_dataset
        self.test_dataset = test_dataset
        self.label_type = label_type
        self.num_classes = num_classes
        self.train_emb = None
        self.val_emb = None
        self.test_emb = None
        self.__train_emb_mean = train_emb_mean
        self.__train_emb_std = train_emb_std
        self.__labeled_idxs = None
        self.__train_labels = train_labels
        self.__val_labels = val_labels
        self.__test_labels = test_labels
        self.__train_weak_labels = train_weak_labels
        self.__val_weak_labels =  val_weak_labels
        self.__test_weak_labels =  test_weak_labels

        self.classnames = classnames

    def update_embedding_dataset(self, epoch, get_feature_fn):
        """
        Update the embedding dataset with the updat_embed_dataset_fn and the current epoch.

        :param int epoch: current epoch, used to update the transform of the dataset.
        :param callable update_embed_dataset_fn: function to update the embedding dataset.
        """
        assert callable(get_feature_fn), "Update_embed_dataset_fn must be a function."

        for _, dataset_split in enumerate(["train", "val", "test"]):
            dataset = getattr(self, dataset_split + "_dataset")
            feat_emb = get_feature_fn(dataset, dataset_split, epoch)
            setattr(self, dataset_split + "_emb", feat_emb)

    def update_labeled_idxs(self, new_idxs):
        """
        Insert the examples that have been newly labeled to update the dataset tracker.

        :param List new_idxs: list of newly labeled indexes.
        """
        if self.__labeled_idxs is None:
            self.__labeled_idxs = np.array(new_idxs)
        else:
            self.__labeled_idxs = np.concatenate(
                (self.__labeled_idxs, np.array(new_idxs)))

    def get_embedding_datasets(self):
        """
        Construct PyTorch datasets of (embedding, label) pairs for all of training, validation and testing.
        :return: three PyTorch datasets for training, validation and testing respectively.
        """
        if self.train_emb is None or self.val_emb is None or self.test_emb is None:
            raise Exception("Embedding is not initialized.")
        if callable(self.__train_labels):
            self.__train_labels = self.__train_labels()
        if callable(self.__val_labels):
            self.__val_labels = self.__val_labels()
        if callable(self.__test_labels):
            self.__test_labels = self.__test_labels()
        if callable(self.__train_weak_labels):
            self.__train_weak_labels = self.__train_weak_labels()
        if callable(self.__val_weak_labels):
            self.__val_weak_labels = self.__val_weak_labels()
        if callable(self.__test_weak_labels):
            self.__test_weak_labels = self.__test_weak_labels()
        # To avoid changing mean and std every time updating an augmented embedding, we will only set them once.
        if callable(self.__train_emb_mean):
            self.__train_emb_mean = self.__train_emb_mean(self.train_emb, axis=0)
        if callable(self.__train_emb_std):
            self.__train_emb_std = self.__train_emb_std(self.train_emb, axis=0)
        return DatasetOnMemory((self.train_emb - self.__train_emb_mean) / self.__train_emb_std, self.__train_labels,
                               self.num_classes, self.__train_weak_labels), \
               DatasetOnMemory((self.val_emb - self.__train_emb_mean) / self.__train_emb_std, self.__val_labels,
                               self.num_classes, self.__val_weak_labels), \
               DatasetOnMemory((self.test_emb - self.__train_emb_mean) / self.__train_emb_std, self.__test_labels,
                               self.num_classes, self.__test_weak_labels)

    def get_embedding_dim(self):
        """Dimension of the embedding."""
        assert self.train_emb is not None, "Embedding is not initialized."
        return self.train_emb.shape[1]

    def get_input_datasets(self):
        """
        Retrieves PyTorch datasets of (raw data, label) pairs for all of training, validation and testing.
        :return: three PyTorch datasets for training, validation and testing respectively.
        """
        return self.train_dataset, self.val_dataset, self.test_dataset

    def __len__(self):
        """Length of the training set."""
        return len(self.train_dataset)

    def get_num_classes(self):
        """Number of classes of the dataset."""
        return self.num_classes

    def get_classnames(self):
        """Class names of the dataset."""
        return self.classnames

    def num_labeled(self):
        """Number of labeled examples in the pool."""
        return len(self.__labeled_idxs)

    def labeled_idxs(self):
        """Indexes of the labeled examples in chronological order."""
        return np.array(self.__labeled_idxs)

    def get_train_labels(self):
        if callable(self.__train_labels):
            self.__train_labels = self.__train_labels()
        return np.array(self.__train_labels)
