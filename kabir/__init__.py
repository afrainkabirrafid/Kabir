"""Kabir -- a small programming language, built from scratch in Python.

Version 1.0.0. Author: Afrain Kabir Rafid.
"""

__version__ = "1.0.0"
__author__ = "Afrain Kabir Rafid"

from .interpreter import Interpreter  # noqa: F401
from .parser import parse  # noqa: F401
from .lexer import tokenize  # noqa: F401
from .errors import KabirError  # noqa: F401

__all__ = ["__version__", "__author__", "Interpreter", "parse", "tokenize",
           "KabirError"]
