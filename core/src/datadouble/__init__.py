# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Realistic, safe copies of production data for development and testing."""

from importlib.metadata import version

__version__: str = version("datadouble")

__all__ = ["__version__"]
