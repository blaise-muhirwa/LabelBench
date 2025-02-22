import torch
import numpy as np
from enum import Enum
from torch.utils.data import Dataset
from LabelBench.dataset.feature_extractor import FeatureExtractor


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

    def __init__(self, X, y, n_class):
        if isinstance(X, tuple):
            assert len(X[0]) == len(y) and len(X[1]) == len(y), "X and y must have the same length."
        else:
            assert len(X) == len(y), "X and y must have the same length."
        self.X = X
        self.y = y
        self.n_class = n_class
        self._return_indices = False

    def __len__(self):
        return len(self.y)

    def __getitem__(self, item):
        if isinstance(self.X, tuple):
            x = (self.X[0][item], self.X[1][item])
        else:
            x = self.X[item]
        y = self.y[item]

        if self._return_indices:
            return x, y, item
        return x, y

    def get_inputs(self):
        if isinstance(self.X, tuple):
            raise Exception("Dataset has tuple as input.")
        return self.X

    def get_labels(self):
        return self.y

    def set_return_indices(self, return_indices):
        self._return_indices = return_indices


class TransformDataset(Dataset):
    """
    A PyTorch Dataset where you can dynamically set transforms.

    Be careful about its behavior when combined with dataloaders!
    See https://discuss.pytorch.org/t/changing-transformation-applied-to-data-during-training/15671 for details.
    """

    def __init__(self, dataset, transform=None, target_transform=None, ignore_metadata=False):
        self.dataset = dataset
        self.__transform = transform
        self.__strong_transform = None
        self.__target_transform = target_transform
        self.__default_transform = transform
        self.__default_target_transform = target_transform
        self.ignore_metadata = ignore_metadata
        self._return_indices = False

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, item):
        if self.ignore_metadata:
            x_orig, y = self.dataset[item][:2]
        else:
            x_orig, y = self.dataset[item]

        if self.__transform:
            x = self.__transform(x_orig)
        if self.__target_transform:
            y = self.__target_transform(y)
        if self.__strong_transform:
            xs = self.__strong_transform(x_orig)
            x = (x, xs)

        if self._return_indices:
            return x, y, item
        else:
            return x, y

    def get_transform(self):
        return self.__transform

    def set_transform(self, transform):
        self.__transform = transform

    def set_strong_transform(self, strong_transform):
        self.__strong_transform = strong_transform

    def set_target_transform(self, target_transform):
        self.__target_transform = target_transform

    def set_to_default_transform(self):
        self.__transform = self.__default_transform

    def set_to_default_target_transform(self):
        self.__target_transform = self.__default_target_transform

    def set_return_indices(self, return_indices):
        self._return_indices = return_indices


class ALDataset:
    """
    Dataset for active learning. The dataset contains all of training, validation and testing data as well as their
    embeddings. The dataset also tracks the examples that have been labeled.
    """

    def __init__(self, train_dataset, val_dataset, test_dataset, train_labels, val_labels, test_labels, label_type,
                 num_classes, classnames, train_emb_mean=np.mean, train_emb_std=np.std):
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
        assert isinstance(train_dataset, TransformDataset), "Training dataset must be a TransformDataset."
        assert isinstance(val_dataset, TransformDataset), "Validation dataset must be a TransformDataset."
        assert isinstance(test_dataset, TransformDataset), "Test dataset must be a TransformDataset."
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
        self.feature_extractor = None


        self.classnames = classnames

    def update_embedding_dataset(self, epoch, feature_extractor, use_strong=False):
        """
        Update the embedding dataset with the updat_embed_dataset_fn and the current epoch.

        :param int epoch: current epoch, used to update the transform of the dataset.
        :param FeatureExtractor feature_extractor: class to extract the embeddings.
        :param bool use_strong: Flag for getting weak and strong augmented embeddings for semi-supervised learning.
        """
        assert isinstance(feature_extractor, FeatureExtractor), "Feature extractor must be a FeatureExtractor."

        for _, dataset_split in enumerate(["train", "val", "test"]):
            dataset = getattr(self, dataset_split + "_dataset")
            feat_emb = feature_extractor.get_feature(dataset, dataset_split, epoch, use_strong)
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
        # To avoid changing mean and std every time updating an augmented embedding, we will only set them once.
        if callable(self.__train_emb_mean):
            if isinstance(self.train_emb, tuple):
                self.__train_emb_mean = self.__train_emb_mean(self.train_emb[0], axis=0)
            else:
                self.__train_emb_mean = self.__train_emb_mean(self.train_emb, axis=0)
        if callable(self.__train_emb_std):
            if isinstance(self.train_emb, tuple):
                self.__train_emb_std = self.__train_emb_std(self.train_emb[0], axis=0)
            else:
                self.__train_emb_std = self.__train_emb_std(self.train_emb, axis=0)
        if isinstance(self.train_emb, tuple):
            train_dataset = DatasetOnMemory(((self.train_emb[0] - self.__train_emb_mean) / self.__train_emb_std,
                                             (self.train_emb[1] - self.__train_emb_mean) / self.__train_emb_std),
                                            self.__train_labels,
                                            self.num_classes)
        else:
            train_dataset = DatasetOnMemory((self.train_emb - self.__train_emb_mean) / self.__train_emb_std,
                                            self.__train_labels,
                                            self.num_classes)
        val_dataset = DatasetOnMemory((self.val_emb - self.__train_emb_mean) / self.__train_emb_std,
                                      self.__val_labels,
                                      self.num_classes)
        test_dataset = DatasetOnMemory((self.test_emb - self.__train_emb_mean) / self.__train_emb_std,
                                       self.__test_labels,
                                       self.num_classes)
        return train_dataset, val_dataset, test_dataset

    def get_embedding_dim(self):
        """Dimension of the embedding."""
        assert self.train_emb is not None, "Embedding is not initialized."
        if isinstance(self.train_emb, tuple):
            return self.train_emb[0].shape[1]
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

    def unlabeled_idxs(self):
        """Indexes of the unlabeled examples."""
        labeled_set = set(list(self.labeled_idxs()))
        all_set = set(list(range(self.__len__())))
        return np.array(list(all_set - labeled_set))

    def get_train_labels(self):
        if callable(self.__train_labels):
            self.__train_labels = self.__train_labels()
        return np.array(self.__train_labels)
