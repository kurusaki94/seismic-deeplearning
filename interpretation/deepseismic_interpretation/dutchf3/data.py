# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import itertools
import warnings
import segyio
from os import path
import scipy
from cv_lib.utils import generate_path, mask_to_disk, image_to_disk

from matplotlib import pyplot as plt
from PIL import Image

# bugfix for scipy imports
import scipy.misc
import numpy as np
import torch
from toolz import curry
from torch.utils import data
import logging
from deepseismic_interpretation.dutchf3.utils.batch import (
    interpolate_to_fit_data,
    parse_labels_in_image,
    get_coordinates_for_slice,
)


def _train_data_for(data_dir):
    return path.join(data_dir, "train", "train_seismic.npy")


def _train_labels_for(data_dir):
    return path.join(data_dir, "train", "train_labels.npy")


def _test1_data_for(data_dir):
    return path.join(data_dir, "test_once", "test1_seismic.npy")


def _test1_labels_for(data_dir):
    return path.join(data_dir, "test_once", "test1_labels.npy")


def _test2_data_for(data_dir):
    return path.join(data_dir, "test_once", "test2_seismic.npy")


def _test2_labels_for(data_dir):
    return path.join(data_dir, "test_once", "test2_labels.npy")


def read_labels(fname, data_info):
    """
    Read labels from an image.

    Args:
        fname: filename of labelling mask (image)
        data_info: dictionary describing the data

    Returns:
        list of labels and list of coordinates
    """

    # Alternative writings for slice-type
    inline_alias = ["inline", "in-line", "iline", "y"]
    crossline_alias = ["crossline", "cross-line", "xline", "x"]
    timeslice_alias = ["timeslice", "time-slice", "t", "z", "depthslice", "depth"]

    label_imgs = []
    label_coordinates = {}

    # Find image files in folder

    tmp = fname.split("/")[-1].split("_")
    slice_type = tmp[0].lower()
    tmp = tmp[1].split(".")
    slice_no = int(tmp[0])

    if slice_type not in inline_alias + crossline_alias + timeslice_alias:
        print("File:", fname, "could not be loaded.", "Unknown slice type")
        return None

    if slice_type in inline_alias:
        slice_type = "inline"
    if slice_type in crossline_alias:
        slice_type = "crossline"
    if slice_type in timeslice_alias:
        slice_type = "timeslice"

    # Read file
    print("Loading labels for", slice_type, slice_no, "with")
    img = scipy.misc.imread(fname)
    img = interpolate_to_fit_data(img, slice_type, slice_no, data_info)
    label_img = parse_labels_in_image(img)

    # Get coordinates for slice
    coords = get_coordinates_for_slice(slice_type, slice_no, data_info)

    # Loop through labels in label_img and append to label_coordinates
    for cls in np.unique(label_img):
        if cls > -1:
            if str(cls) not in label_coordinates.keys():
                label_coordinates[str(cls)] = np.array(np.zeros([3, 0]))
            inds_with_cls = label_img == cls
            cords_with_cls = coords[:, inds_with_cls.ravel()]
            label_coordinates[str(cls)] = np.concatenate((label_coordinates[str(cls)], cords_with_cls), 1)
            print(" ", str(np.sum(inds_with_cls)), "labels for class", str(cls))
    if len(np.unique(label_img)) == 1:
        print(" ", 0, "labels", str(cls))

    # Add label_img to output
    label_imgs.append([label_img, slice_type, slice_no])

    return label_imgs, label_coordinates


class SectionLoader(data.Dataset):
    """
    Base class for section data loader
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param bool debug: enable debugging output
    """

    def __init__(self, config, split="train", is_transform=True, augmentations=None, debug=False):
        self.data_dir = config.DATASET.ROOT
        self.n_classes = config.DATASET.NUM_CLASSES
        self.MIN = config.DATASET.MIN
        self.MAX = config.DATASET.MAX
        self.split = split
        self.is_transform = is_transform
        self.augmentations = augmentations
        self.sections = list()
        self.debug = debug

    def __len__(self):
        return len(self.sections)

    def __getitem__(self, index):

        section_name = self.sections[index]
        direction, number = section_name.split(sep="_")

        if direction == "i":
            im = self.seismic[int(number), :, :]
            lbl = self.labels[int(number), :, :]
        elif direction == "x":
            im = self.seismic[:, int(number), :]
            lbl = self.labels[:, int(number), :]

        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        if self.debug and "test" in self.split:
            outdir = f"debug/test/sectionLoader_{self.split}_raw"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{section_name}"
            image_to_disk(im, path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.augmentations is not None:
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]

        if self.is_transform:
            im, lbl = self.transform(im, lbl)

        if self.debug and "test" in self.split:
            outdir = f"debug/test/sectionLoader_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{section_name}"
            image_to_disk(np.array(im[0]), path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(np.array(lbl[0]), path_prefix + "_lbl.png", self.n_classes)

        return im, lbl

    def transform(self, img, lbl):
        # to be in the BxCxHxW that PyTorch uses:
        lbl = np.expand_dims(lbl, 0)
        if len(img.shape) == 2:
            img = np.expand_dims(img, 0)
        return torch.from_numpy(img).float(), torch.from_numpy(lbl).long()


class TrainSectionLoader(SectionLoader):
    """
    Training data loader for sections
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param str seismic_path: Override file path for seismic data
    :param str label_path: Override file path for label data
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="train",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TrainSectionLoader, self).__init__(
            config,
            split=split,
            is_transform=is_transform,
            augmentations=augmentations,
            seismic_path=seismic_path,
            label_path=label_path,
            debug=debug,
        )

        if seismic_path is not None and label_path is not None:
            # Load npy files (seismc and corresponding labels) from provided
            # location (path)
            if not path.isfile(seismic_path):
                raise Exception(f"{seismic_path} does not exist")
            if not path.isfile(label_path):
                raise Exception(f"{label_path} does not exist")
            self.seismic = np.load(seismic_path)
            self.labels = np.load(label_path)
        else:
            self.seismic = np.load(_train_data_for(self.data_dir))
            self.labels = np.load(_train_labels_for(self.data_dir))

        # reading the file names for split
        txt_path = path.join(self.data_dir, "splits", "section_" + split + ".txt")
        file_list = tuple(open(txt_path, "r"))
        file_list = [id_.rstrip() for id_ in file_list]
        self.sections = file_list


class TrainSectionLoaderWithDepth(TrainSectionLoader):
    """
    Section data loader that includes additional channel for depth
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param str seismic_path: Override file path for seismic data
    :param str label_path: Override file path for label data
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="train",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TrainSectionLoaderWithDepth, self).__init__(
            config,
            split=split,
            is_transform=is_transform,
            augmentations=augmentations,
            seismic_path=seismic_path,
            label_path=label_path,
            debug=debug,
        )
        self.seismic = add_section_depth_channels(self.seismic)  # NCWH

    def __getitem__(self, index):

        section_name = self.sections[index]
        direction, number = section_name.split(sep="_")

        if direction == "i":
            im = self.seismic[int(number), :, :, :]
            lbl = self.labels[int(number), :, :]
        elif direction == "x":
            im = self.seismic[:, :, int(number), :]
            lbl = self.labels[:, int(number), :]

            im = np.swapaxes(im, 0, 1)  # From WCH to CWH

        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        if self.augmentations is not None:
            im = _transform_CHW_to_HWC(im)
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]
            im = _transform_HWC_to_CHW(im)

        if self.is_transform:
            im, lbl = self.transform(im, lbl)

        return im, lbl


class TestSectionLoader(SectionLoader):
    """
    Test data loader for sections
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param str seismic_path: Override file path for seismic data
    :param str label_path: Override file path for label data
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="test1",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TestSectionLoader, self).__init__(
            config, split=split, is_transform=is_transform, augmentations=augmentations, debug=debug,
        )

        if "test1" in self.split:
            self.seismic = np.load(_test1_data_for(self.data_dir))
            self.labels = np.load(_test1_labels_for(self.data_dir))
        elif "test2" in self.split:
            self.seismic = np.load(_test2_data_for(self.data_dir))
            self.labels = np.load(_test2_labels_for(self.data_dir))
        elif seismic_path is not None and label_path is not None:
            # Load npy files (seismc and corresponding labels) from provided
            # location (path)
            if not path.isfile(seismic_path):
                raise Exception(f"{seismic_path} does not exist")
            if not path.isfile(label_path):
                raise Exception(f"{label_path} does not exist")
            self.seismic = np.load(seismic_path)
            self.labels = np.load(label_path)

        # We are in test mode. Only read the given split. The other one might not
        # be available.
        txt_path = path.join(self.data_dir, "splits", "section_" + split + ".txt")
        file_list = tuple(open(txt_path, "r"))
        file_list = [id_.rstrip() for id_ in file_list]
        self.sections = file_list


class TestSectionLoaderWithDepth(TestSectionLoader):
    """
    Test data loader for sections that includes additional channel for depth
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param str seismic_path: Override file path for seismic data
    :param str label_path: Override file path for label data
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="test1",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TestSectionLoaderWithDepth, self).__init__(
            config,
            split=split,
            is_transform=is_transform,
            augmentations=augmentations,
            seismic_path=seismic_path,
            label_path=label_path,
            debug=debug,
        )
        self.seismic = add_section_depth_channels(self.seismic)  # NCWH

    def __getitem__(self, index):

        section_name = self.sections[index]
        direction, number = section_name.split(sep="_")

        if direction == "i":
            im = self.seismic[int(number), :, :, :]
            lbl = self.labels[int(number), :, :]
        elif direction == "x":
            im = self.seismic[:, :, int(number), :]
            lbl = self.labels[:, int(number), :]

            im = np.swapaxes(im, 0, 1)  # From WCH to CWH

        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        # dump images before augmentation
        if self.debug:
            outdir = f"debug/test/testSectionLoaderWithDepth_{self.split}_raw"
            generate_path(outdir)
            # this needs to take the first dimension of image (no depth) but lbl only has 1 dim
            path_prefix = f"{outdir}/index_{index}_section_{section_name}"
            image_to_disk(im[0, :, :], path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.augmentations is not None:
            im = _transform_CHW_to_HWC(im)
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]
            im = _transform_HWC_to_CHW(im)

        if self.is_transform:
            im, lbl = self.transform(im, lbl)

        # dump images and labels to disk after augmentation
        if self.debug:
            outdir = f"debug/test/testSectionLoaderWithDepth_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{section_name}"
            image_to_disk(np.array(im[0, :, :]), path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(np.array(lbl[0, :, :]), path_prefix + "_lbl.png", self.n_classes)

        return im, lbl


def _transform_WH_to_HW(numpy_array):
    assert len(numpy_array.shape) >= 2, "This method needs at least 2D arrays"
    return np.swapaxes(numpy_array, -2, -1)


class PatchLoader(data.Dataset):
    """
    Base Data loader for the patch-based deconvnet
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param bool debug: enable debugging output
    """

    def __init__(self, config, split="train", is_transform=True, augmentations=None, debug=False):
        self.data_dir = config.DATASET.ROOT
        self.n_classes = config.DATASET.NUM_CLASSES
        self.split = split
        self.MIN = config.DATASET.MIN
        self.MAX = config.DATASET.MAX
        self.patch_size = config.TRAIN.PATCH_SIZE
        self.stride = config.TRAIN.STRIDE
        self.is_transform = is_transform
        self.augmentations = augmentations
        self.patches = list()
        self.debug = debug

    def pad_volume(self, volume, value):
        """
        Pads a 3D numpy array with a constant value along the depth direction only. 

        Args:
            volume (numpy ndarrray): numpy array containing the seismic amplitude or labels. 
            value (int): value to pad the array with. 
        """

        return np.pad(
            volume,
            pad_width=[(0, 0), (0, 0), (self.patch_size, self.patch_size)],
            mode="constant",
            constant_values=value,
        )

    def __len__(self):
        return len(self.patches)

    def __getitem__(self, index):

        patch_name = self.patches[index]
        direction, idx, xdx, ddx = patch_name.split(sep="_")
        idx, xdx, ddx = int(idx), int(xdx), int(ddx)

        if direction == "i":
            im = self.seismic[idx, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
            lbl = self.labels[idx, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
        elif direction == "x":
            im = self.seismic[idx : idx + self.patch_size, xdx, ddx : ddx + self.patch_size]
            lbl = self.labels[idx : idx + self.patch_size, xdx, ddx : ddx + self.patch_size]

        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        # dump raw images before augmentation
        if self.debug:
            outdir = f"debug/patchLoader_{self.split}_raw"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{patch_name}"
            image_to_disk(im, path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.augmentations is not None:
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]

        # dump images and labels to disk
        if self.debug:
            outdir = f"patchLoader_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            generate_path(outdir)
            path_prefix = f"{outdir}/{index}"
            image_to_disk(im, path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.is_transform:
            im, lbl = self.transform(im, lbl)

        # dump images and labels to disk
        if self.debug:
            outdir = f"debug/patchLoader_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{patch_name}"
            image_to_disk(np.array(im[0, :, :]), path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(np.array(lbl[0, :, :]), path_prefix + "_lbl.png", self.n_classes)

        return im, lbl

    def transform(self, img, lbl):
        # to be in the BxCxHxW that PyTorch uses:
        lbl = np.expand_dims(lbl, 0)
        if len(img.shape) == 2:
            img = np.expand_dims(img, 0)
        return torch.from_numpy(img).float(), torch.from_numpy(lbl).long()


class TrainPatchLoader(PatchLoader):
    """
    Train data loader for the patch-based deconvnet
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="train",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TrainPatchLoader, self).__init__(
            config, is_transform=is_transform, augmentations=augmentations, debug=debug,
        )

        if seismic_path is not None and label_path is not None:
            # Load npy files (seismc and corresponding labels) from provided
            # location (path)
            if not path.isfile(seismic_path):
                raise Exception(f"{seismic_path} does not exist")
            if not path.isfile(label_path):
                raise Exception(f"{label_path} does not exist")
            self.seismic = np.load(seismic_path)
            self.labels = np.load(label_path)
        else:
            self.seismic = np.load(_train_data_for(self.data_dir))
            self.labels = np.load(_train_labels_for(self.data_dir))

        # pad the data:
        self.seismic = self.pad_volume(self.seismic, value=0)
        self.labels = self.pad_volume(self.labels, value=255)

        self.split = split
        # reading the file names for split
        txt_path = path.join(self.data_dir, "splits", "patch_" + split + ".txt")
        patch_list = tuple(open(txt_path, "r"))
        patch_list = [id_.rstrip() for id_ in patch_list]
        self.patches = patch_list


class TrainPatchLoaderWithDepth(TrainPatchLoader):
    """
    Train data loader for the patch-based deconvnet with patch depth channel
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="train",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TrainPatchLoaderWithDepth, self).__init__(
            config,
            split=split,
            is_transform=is_transform,
            augmentations=augmentations,
            seismic_path=seismic_path,
            label_path=label_path,
            debug=debug,
        )

    def __getitem__(self, index):

        patch_name = self.patches[index]
        direction, idx, xdx, ddx = patch_name.split(sep="_")
        idx, xdx, ddx = int(idx), int(xdx), int(ddx)

        if direction == "i":
            im = self.seismic[idx, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
            lbl = self.labels[idx, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
        elif direction == "x":
            im = self.seismic[idx : idx + self.patch_size, xdx, ddx : ddx + self.patch_size]
            lbl = self.labels[idx : idx + self.patch_size, xdx, ddx : ddx + self.patch_size]
        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        if self.augmentations is not None:
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]

        im = add_patch_depth_channels(im)

        if self.is_transform:
            im, lbl = self.transform(im, lbl)
        return im, lbl


def _transform_CHW_to_HWC(numpy_array):
    return np.moveaxis(numpy_array, 0, -1)


def _transform_HWC_to_CHW(numpy_array):
    return np.moveaxis(numpy_array, -1, 0)


class TrainPatchLoaderWithSectionDepth(TrainPatchLoader):
    """
    Train data loader for the patch-based deconvnet section depth channel
    :param config: configuration object to define other attributes in loaders
    :param str split: split file to use for loading patches
    :param bool is_transform: Transform patch to dimensions expected by PyTorch
    :param list augmentations: Data augmentations to apply to patches
    :param str seismic_path: Override file path for seismic data
    :param str label_path: Override file path for label data
    :param bool debug: enable debugging output
    """

    def __init__(
        self,
        config,
        split="train",
        is_transform=True,
        augmentations=None,
        seismic_path=None,
        label_path=None,
        debug=False,
    ):
        super(TrainPatchLoaderWithSectionDepth, self).__init__(
            config,
            split=split,
            is_transform=is_transform,
            augmentations=augmentations,
            seismic_path=seismic_path,
            label_path=label_path,
            debug=debug,
        )
        self.seismic = add_section_depth_channels(self.seismic)

    def __getitem__(self, index):

        patch_name = self.patches[index]
        direction, idx, xdx, ddx = patch_name.split(sep="_")
        idx, xdx, ddx = int(idx), int(xdx), int(ddx)

        if direction == "i":
            im = self.seismic[idx, :, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
            lbl = self.labels[idx, xdx : xdx + self.patch_size, ddx : ddx + self.patch_size]
        elif direction == "x":
            im = self.seismic[idx : idx + self.patch_size, :, xdx, ddx : ddx + self.patch_size]
            lbl = self.labels[idx : idx + self.patch_size, xdx, ddx : ddx + self.patch_size]
            im = np.swapaxes(im, 0, 1)  # From WCH to CWH

        im, lbl = _transform_WH_to_HW(im), _transform_WH_to_HW(lbl)

        # dump images before augmentation
        if self.debug:
            outdir = f"debug/patchLoaderWithSectionDepth_{self.split}_raw"
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{patch_name}"
            image_to_disk(im[0, :, :], path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.augmentations is not None:
            im = _transform_CHW_to_HWC(im)
            augmented_dict = self.augmentations(image=im, mask=lbl)
            im, lbl = augmented_dict["image"], augmented_dict["mask"]
            im = _transform_HWC_to_CHW(im)

        # dump images and labels to disk
        if self.debug:
            outdir = f"patchLoaderWithSectionDepth_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            generate_path(outdir)
            path_prefix = f"{outdir}/{index}"
            image_to_disk(im[0, :, :], path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(lbl, path_prefix + "_lbl.png", self.n_classes)

        if self.is_transform:
            im, lbl = self.transform(im, lbl)

        # dump images and labels to disk after augmentation
        if self.debug:
            outdir = (
                f"debug/patchLoaderWithSectionDepth_{self.split}_{'aug' if self.augmentations is not None else 'noaug'}"
            )
            generate_path(outdir)
            path_prefix = f"{outdir}/index_{index}_section_{patch_name}"
            image_to_disk(np.array(im[0, :, :]), path_prefix + "_img.png", self.MIN, self.MAX)
            mask_to_disk(np.array(lbl[0, :, :]), path_prefix + "_lbl.png", self.n_classes)

        return im, lbl

    def __repr__(self):
        unique, counts = np.unique(self.labels, return_counts=True)
        ratio = counts / np.sum(counts)
        return "\n".join(f"{lbl}: {cnt} [{rat}]" for lbl, cnt, rat in zip(unique, counts, ratio))


_TRAIN_PATCH_LOADERS = {
    "section": TrainPatchLoaderWithSectionDepth,
    "patch": TrainPatchLoaderWithDepth,
}


def get_patch_loader(cfg):
    assert str(cfg.TRAIN.DEPTH).lower() in [
        "section",
        "patch",
        "none",
    ], f"Depth {cfg.TRAIN.DEPTH} not supported for patch data. \
            Valid values: section, patch, none."
    return _TRAIN_PATCH_LOADERS.get(cfg.TRAIN.DEPTH, TrainPatchLoader)


_TRAIN_SECTION_LOADERS = {"section": TrainSectionLoaderWithDepth}


def get_section_loader(cfg):
    assert str(cfg.TRAIN.DEPTH).lower() in [
        "section",
        "none",
    ], f"Depth {cfg.TRAIN.DEPTH} not supported for section data. \
        Valid values: section, none."
    return _TRAIN_SECTION_LOADERS.get(cfg.TRAIN.DEPTH, TrainSectionLoader)


_TEST_LOADERS = {"section": TestSectionLoaderWithDepth}


def get_test_loader(cfg):
    logger = logging.getLogger(__name__)
    logger.info(f"Test loader {cfg.TRAIN.DEPTH}")
    return _TEST_LOADERS.get(cfg.TRAIN.DEPTH, TestSectionLoader)


def add_patch_depth_channels(image_array):
    """Add 2 extra channels to a 1 channel numpy array
    One channel is a linear sequence from 0 to 1 starting from the top of the image to the bottom
    The second channel is the product of the input channel and the 'depth' channel
    
    Args:
        image_array (np.array): 1D Numpy array
    
    Returns:
        [np.array]: 3D numpy array
    """
    h, w = image_array.shape
    image = np.zeros([3, h, w])
    image[0] = image_array
    for row, const in enumerate(np.linspace(0, 1, h)):
        image[1, row, :] = const
    image[2] = image[0] * image[1]
    return image


def add_section_depth_channels(sections_numpy):
    """Add 2 extra channels to a 1 channel section
    One channel is a linear sequence from 0 to 1 starting from the top of the section to the bottom
    The second channel is the product of the input channel and the 'depth' channel
    
    Args:
        sections_numpy (numpy array): 3D Matrix (NWH)Image tensor
    
    Returns:
        [pytorch tensor]: 3D image tensor
    """
    n, w, h = sections_numpy.shape
    image = np.zeros([3, n, w, h])
    image[0] = sections_numpy
    for row, const in enumerate(np.linspace(0, 1, h)):
        image[1, :, :, row] = const
    image[2] = image[0] * image[1]
    return np.swapaxes(image, 0, 1)
