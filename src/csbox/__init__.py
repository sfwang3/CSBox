"""CSBox: 计算机实验与项目交付工具。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("csbox")
except PackageNotFoundError:
    __version__ = "0.7.0rc1"

__all__ = ["__version__"]
