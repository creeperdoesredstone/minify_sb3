from __future__ import annotations

from minifier.core import *
from minifier.cli import Ansi, build_options, minify_sb3

__all__ = [name for name in globals() if not name.startswith("_")]


if __name__ == "__main__":
	import os
	import sys

	raw_flags: list[str] = [a for a in sys.argv[1:] if a.startswith("--")]
	args: list[str] = [a for a in sys.argv[1:] if not a.startswith("--")]

	opts = build_options(raw_flags, args)
	dst: str = (
		args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + "_minified.sb3"
	)
	if os.path.abspath(args[0]) == os.path.abspath(dst):
		print(Ansi.error("Error: output path must differ from input path."))
		raise SystemExit(1)
	raise SystemExit(minify_sb3(args[0], dst, opts))
