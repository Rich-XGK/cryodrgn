import os.path
import argparse
import numpy as np
import torch
import pytest
from cryodrgn import dataset, mrc
from cryodrgn.source import ImageSource
from cryodrgn.commands_utils import flip_hand

DATA_FOLDER = os.path.join(os.path.dirname(__file__), "..", "testing", "data")


@pytest.fixture
def mrcs_data():
    return ImageSource.from_mrc(f"{DATA_FOLDER}/toy_projections.mrc").images()


def test_invert_contrast(mrcs_data):
    args = flip_hand.add_args(argparse.ArgumentParser()).parse_args(
        [
            f"{DATA_FOLDER}/toy_projections.mrc",
            "-o",
            "output/toy_projections_flipped.mrc",
        ]
    )
    flip_hand.main(args)

    flipped_data = ImageSource.from_mrcs("output/toy_projections_flipped.mrc").images()
    assert np.allclose(flipped_data, np.array(mrcs_data)[::-1])
