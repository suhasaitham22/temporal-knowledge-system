"""Submission entry point.

The harness loads this module and calls ``build()``. Implementation lives in
``ksys`` so the contract surface stays obvious.
"""

from ksys import build

__all__ = ["build"]
