"""Ashigaru 3.x: the Desk and its clients."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ashigaru")
except PackageNotFoundError:  # run from a checkout that was never installed
    __version__ = "0"
