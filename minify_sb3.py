from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import zlib

from collections import Counter, defaultdict
from collections.abc import Generator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from itertools import permutations, product
from types import SimpleNamespace, NoneType
from typing import Any, NoReturn


from minifier.options import Options, DEFAULT_LIST_BYTES, DEFAULT_LIST_ITEMS, build_options
from minifier.output import Ansi, ProgressBar, print_transform_stats, format_size_report


from pathlib import Path as _Path
import __future__ as _future

_PART_FILES = (
    "json_layout.py",
    "metadata.py",
    "json_optimizer.py",
    "block_utils.py",
    "procedures.py",
    "transforms.py",
    "verification.py",
    "cli.py",
)
_PARTS_DIR = _Path(__file__).resolve().parent / "minifier"
for _part_name in _PART_FILES:
    _part_path = _PARTS_DIR / _part_name
    _part_source = _part_path.read_text(encoding="utf-8")
    exec(
        compile(
            _part_source,
            str(_part_path),
            "exec",
            flags=_future.annotations.compiler_flag,
            dont_inherit=True,
        ),
        globals(),
    )

del _PART_FILES, _PARTS_DIR, _part_name, _part_path, _part_source, _Path, _future
