"""The conftest guard really keeps the default install folder inside a temporary home."""
import os
from pathlib import Path


def test_the_default_install_folder_is_inside_the_temporary_home(_sandbox_home):
    from ssapp import cli
    real = os.path.realpath(cli.default_install_dir())
    assert Path(real).is_relative_to(os.path.realpath(_sandbox_home))
