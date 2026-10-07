"""Sphinx configuration for the citysports-efc-ble API reference."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

project = "citysports-efc-ble"
author = "Daan Vervacke"
copyright = "2026, Daan Vervacke"
try:
    release = _version("citysports-efc-ble")
except PackageNotFoundError:
    release = "0.0.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

html_theme = "furo"
