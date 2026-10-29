from __future__ import annotations

from .blocks import *
from .cli import (
	Ansi,
	DEFAULT_LIST_BYTES,
	DEFAULT_LIST_ITEMS,
	Options,
	build_options,
	format_size_report,
	humanize,
	minify_sb3,
	print_transform_stats,
)
from .core import *
from .expressions import *
from .names import *
from .procedures import *
from .serialization import *
from .subgraph_factoring import *
from .verification import *

from . import blocks as _blocks
from . import cli as _cli
from . import constants as _constants
from . import expressions as _expressions
from . import names as _names
from . import procedures as _procedures
from . import serialization as _serialization
from . import subgraph_factoring as _subgraph_factoring
from . import verification as _verification

_implementation_modules = (
	_blocks,
	_cli,
	_expressions,
	_names,
	_procedures,
	_serialization,
	_subgraph_factoring,
	_verification,
)
_shared_implementation_names = {
	name: value
	for module in _implementation_modules
	for name, value in vars(module).items()
	if getattr(value, "__module__", None) == module.__name__
}
_shared_implementation_names.update(
	(name, getattr(_constants, name)) for name in _constants.__all__
)

for _module in _implementation_modules:
	for _name, _value in _shared_implementation_names.items():
		_module.__dict__.setdefault(_name, _value)

__all__ = [
	"JsonNumber",
	"get_unique_object",
	"invalid_constant",
	"loads_exact",
	"copy_json",
	"_number_components",
	"get_shortest_num_repr",
	"quote",
	"encode_parts",
	"dumps_exact",
	"dumps_exact_layout",
	"humanize",
	"print_transform_stats",
	"format_size_report",
	"minify_sb3",
	"Options",
	"DEFAULT_LIST_BYTES",
	"DEFAULT_LIST_ITEMS",
	"Ansi",
	"build_options",
]
