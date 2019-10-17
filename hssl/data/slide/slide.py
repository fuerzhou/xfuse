from abc import ABC, abstractmethod

import numpy as np
import torch
from pyvips import Image
from scipy.ndimage.morphology import binary_fill_holes
from torch.utils.data import Dataset


class Slide(ABC, Dataset):
    """
    Abstract class yielding observations from a single sample (tissue slide)
    """

    def __init__(self, data: torch.Tensor, image: Image, label: Image):
        if data.is_sparse:  # type: ignore
            data = data.to_dense()

        self._image = image
        self._label = label
        self._data = data
        self._zero_data = np.concatenate(
            [np.array([0]), np.where(data.sum(1) == 0)[0] + 1]
        )

        self.H, self.W = self._image.height, self._image.width

        assert self.H == self._label.height and self.W == self._label.width

    @property
    def image(self):
        return self._image

    @property
    def label(self):
        return self._label

    @abstractmethod
    def _get_patch(self, idx: int):
        pass

    @abstractmethod
    def __len__(self):
        pass

    def __getitem__(self, idx):
        image, label = self._get_patch(idx)

        # remove partially visible labels
        label[
            np.invert(binary_fill_holes(np.isin(label, self._zero_data)))
        ] = 0

        labels = [*sorted(np.unique(label))]
        data = self._data[[x - 1 for x in labels if x > 0]]
        if data.shape[0] == 0:
            return self.__getitem__((idx + 1) % len(self))
        label = np.searchsorted(labels, label)

        return dict(
            image=torch.tensor(image / 255 * 2 - 1).permute(2, 0, 1).float(),
            label=torch.tensor(label).long(),
            data=data,
            type="ST",
        )
