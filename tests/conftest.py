"""Shared fixtures for the apt-mirror test suite.

The tool is a single file (``apt-mirror.py``) whose name is not a valid
identifier, so it is loaded from its path rather than imported normally.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def load_aptmirror() -> ModuleType:
    """Load ``apt-mirror.py`` from the repository root as a module."""
    spec = importlib.util.spec_from_file_location(
        "aptmirror", Path(__file__).parent.parent / "apt-mirror.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def am() -> ModuleType:
    """The loaded apt-mirror module, shared by all unit tests."""
    return load_aptmirror()
