from __future__ import annotations

from .constants import *

import copy
import hashlib
import json
import math
import os
import pickle
import re
import subprocess
import shutil
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
from minify_options import (
	Options,
	DEFAULT_LIST_BYTES,
	DEFAULT_LIST_ITEMS,
	Ansi,
	build_options,
)
from types import SimpleNamespace, NoneType
from typing import Any, NoReturn

from .blocks import *
from .expressions import *
from .names import *
from .procedures import *
from .serialization import *
from .verification import *
from .verification import (
	_DebugStructureError,
	_debug_structure_checkpoint,
	_reinflate,
	_restore_block_ids,
	_restore_representation_for_verify,
	_validate_block_structure,
)

def humanize(n):
	return f"{n/1048576:.4f} MiB" if n >= 1048576 else f"{n/1024:.4f} KiB"


def print_transform_stats(stats, opts):
	print(Ansi.heading("Transforms applied:"))
	for number, title, entries in STAT_GROUPS:
		print(Ansi.subheading(f"  {number} {title}"))
		for key, label in entries:
			print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
		if number == "12.":
			if opts.simplify_blocks:
				print(Ansi.subheading("  12a. Simplify set-variable arithmetic"))
				for key, label in (
					("blocks_simplified", "set blocks simplified"),
					("set_to_change_add", "addition forms simplified"),
					("set_to_change_subtract", "subtraction forms simplified"),
					("simplify_blocks_removed", "redundant reporter blocks removed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.group_similar_sequences:
				print(Ansi.subheading("  12b. Group similar sequences (optional)"))
				for key, label in (
					("sequence_groups_created", "similar sequence groups created"),
					("sequences_grouped", "sequence instances grouped"),
					("sequence_procedures_created", "sequence procedures created"),
					("sequence_parameters", "sequence parameters created"),
					("sequence_blocks_removed", "sequence blocks removed"),
					("sequence_blocks_added", "sequence procedure blocks added"),
					("sequence_bytes_saved", "sequence grouping JSON bytes saved"),
					(
						"sequence_groups_rejected_size",
						"candidate groups rejected for size",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.simplify_boolean_control:
				print(Ansi.subheading("  12c. Simplify constant boolean control flow"))
				for key, label in (
					(
						"boolean_control_simplifications",
						"constant control blocks simplified",
					),
					("control_blocks_removed", "control blocks removed"),
					(
						"control_branch_blocks_removed",
						"branch/condition blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.branch_factoring:
				print(Ansi.subheading("  12d. Factor common branch sequences"))
				for key, label in (
					("branch_prefixes_factored", "common prefixes factored"),
					("branch_suffixes_factored", "common suffixes factored"),
					("branch_controls_factored", "identical conditionals eliminated"),
					("branch_factor_blocks_removed", "duplicate branch blocks removed"),
					("branch_factor_bytes_saved", "branch factoring JSON bytes saved"),
					("branch_factor_rejected_size", "candidates rejected for size"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.specialize_procedures:
				print(Ansi.subheading("  12d. Specialize custom procedures"))
				for key, label in (
					("procedure_specialized", "procedure variants specialized"),
					(
						"procedure_specialization_calls",
						"calls rewritten to specialized variants",
					),
					(
						"procedure_specialization_blocks_added",
						"specialized blocks added",
					),
					(
						"procedure_specialization_blocks_removed",
						"generic procedure blocks replaced",
					),
					(
						"procedure_specialization_argument_reporters_removed",
						"constant argument reporters removed",
					),
					(
						"procedure_specialization_passes",
						"specialization passes completed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.clear_procedure_definition_shadows:
				print(Ansi.subheading("  12e. Clear procedure definition shadows"))
				print(
					Ansi.muted(
						f"    {'procedure definition shadows cleared':40} {stats['procedure_definition_shadows_cleared']:>8,}"
					)
				)
			if opts.compact_procedure_prototypes:
				print(Ansi.subheading("  12f. Compact procedure prototypes"))
				for key, label in (
					(
						"procedure_prototypes_compacted",
						"procedure prototypes compacted",
					),
					(
						"procedure_prototype_argument_reporters_removed",
						"prototype argument reporters removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.optimize_procedure_arguments:
				print(Ansi.subheading("  12g. Optimize custom procedure arguments"))
				for key, label in (
					("procedure_arguments_removed", "procedure arguments removed"),
					(
						"procedure_constant_arguments_folded",
						"constant procedure arguments folded",
					),
					(
						"procedure_argument_reporters_removed",
						"argument reporter blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.merge_duplicate_procedures:
				print(Ansi.subheading("  12g. Merge duplicate custom procedures"))
				for key, label in (
					("duplicate_procedures_merged", "duplicate procedures merged"),
					(
						"duplicate_procedure_blocks_removed",
						"duplicate procedure blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.inline_single_use_procedures:
				print(Ansi.subheading("  12h. Inline safe single-use procedures"))
				for key, label in (
					("procedure_inlined", "single-use procedures inlined"),
					(
						"procedure_inline_blocks_removed",
						"procedure scaffolding blocks removed",
					),
					(
						"procedure_inline_argument_reporters_removed",
						"argument reporters removed during inlining",
					),
					("procedure_inline_passes", "inlining passes completed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.constant_propagation:
				print(Ansi.subheading("  12i. Propagate script constants"))
				for key, label in (
					("script_constants_propagated", "input expressions rewritten"),
					("script_constant_cfg_changes", "CFG data-flow changes"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))


def format_size_report(before_json, after_json, before_archive, after_archive):
	mib = 1024 * 1024
	saving = before_json - after_json
	percent = saving / max(1, before_json) * 100
	sign = "-" if saving >= 0 else "+"
	return (
		f"project.json : {before_json / mib:.4f} MiB -> {after_json / mib:.4f} MiB  ({sign}{abs(saving) / mib:.4f} MiB, {percent:.1f}%)",
		f"archive      : {before_archive / mib:.4f} MiB -> {after_archive / mib:.4f} MiB",
	)


def minify_sb3(src, dst, opts=None):
	opts = opts or Options()

	if os.path.normcase(os.path.realpath(src)) == os.path.normcase(
		os.path.realpath(dst)
	) or (os.path.exists(src) and os.path.exists(dst) and os.path.samefile(src, dst)):
		print(Ansi.error("Error: output path must differ from input path."))
		return 1

	if not os.path.isfile(src):
		print(Ansi.error(f'Error: file not found: "{src}"'))
		return 1

	try:
		zopfli = (
			get_zopfli(opts.zopfli_required)
			if (opts.zopfli or opts.zopfli_assets)
			else None
		)
	except ValueError as error:
		print(Ansi.error(f"Error: {error}"))
		return 1

	with zipfile.ZipFile(src) as zin:
		if "project.json" not in zin.namelist():
			print(Ansi.error("Error: no project.json in archive"))
			return 1

		raw = zin.read("project.json")
		project = json.loads(raw.decode("utf-8"))

		if opts.lists:
			candidates = find_large_lists(project, opts.list_bytes, opts.list_items)
			picked = prompt_for_lists(project, candidates, len(raw))
			if picked is None:
				print(Ansi.warning("Nothing was written."))
				return 130
			opts.cleared_lists = frozenset(picked)

		if opts.fold_constant_variables:
			candidates = get_constant_variables(project)
			picked = prompt_for_constant_variables(project, candidates)

			if picked is None:
				print(Ansi.warning("Nothing was written."))
				return 130

			opts.folded_constant_variables = picked
			opts.folded_constant_variable_literals = dict(picked)
			opts.folded_constant_variable_setters = {
				(c["ti"], c["id"]): (c["setter_ti"], c["setter"])
				for c in candidates
				if (c["ti"], c["id"]) in picked
			}

		asset_infos = {
			item.filename: item
			for item in zin.infolist()
			if item.filename != "project.json"
		}
		assets = {
			item.filename: zin.read(item.filename)
			for item in zin.infolist()
			if item.filename != "project.json"
		}

		print("\n" + Ansi.prompt("--- BEGIN ---") + "\n")
		progress = ProgressBar(progress_stage_count(opts))
		try:
			stats = apply_transforms(project, opts, assets, progress)
		except _DebugStructureError as error:
			print(
				Ansi.error(
					f"\nStructural debug stopped before writing output:\n{error}"
				)
			)
			return 1

		if opts.relabel_block_ids:
			progress.next("Minimize block IDs")
			project, label_maps, label_cost = relabel_blocks(project)
			previous_maps = getattr(opts, "renamed_block_ids", {})
			opts.renamed_block_ids = {
				ti: (
					{
						old: label_maps.get(ti, {}).get(new, new)
						for old, new in previous_maps[ti].items()
					}
					if ti in previous_maps
					else mapping
				)
				for ti, mapping in label_maps.items()
			}
			opts.rename_block_ids = True
			stats["block_id_bytes_saved"] = label_cost["before"] - label_cost["after"]
			if _DEBUG_STRUCTURE_ENABLED:
				_debug_restored = copy.deepcopy(project)
				_restore_block_ids(_debug_restored, opts.renamed_block_ids)
				try:
					_debug_structure_checkpoint(
						_debug_restored, "block-ID relabeling and restoration"
					)
				except _DebugStructureError as error:
					print(
						Ansi.error(
							f"\nStructural debug stopped before writing output:\n{error}"
						)
					)
					return 1

		out_json = dumps_compact(project, opts.sort_keys).encode("utf-8")
		compressed_json = None

		if opts.optimize_json:
			progress.next("Optimize project.json")
			out_json, compressed_json, encoding_stats = optimize_project_json(
				loads_exact(out_json),
				opts.compression_level,
				opts.zopfli,
				opts.zopfli_iterations,
				opts.zopfli_required,
				opts.compact_block_defaults,
				opts.compact_costume_references,
				opts.compact_block_flags,
				opts.minimum_json,
				opts.json_search_rounds,
				False,
				opts.strip_reference_names,
				opts.prune_orphan_arguments,
				opts.compact_procedure_symbols,
				opts.compact_reporter_defaults,
				opts.fast_json,
				opts.strip_covered_shadows,
				opts.strip_editor_comments,
				opts.strip_script_positions,
				opts.prune_unused_data,
				opts.rebuild_procedure_displays,
				opts.compact_terminal_links,
				opts.unbounded_json_ordering,
				opts.unbounded_layout,
			)
			stats.update(
				{
					key: value
					for key, value in encoding_stats.items()
					if isinstance(value, int)
				}
			)
			if _DEBUG_STRUCTURE_ENABLED:
				try:
					_debug_original_raw = loads_exact(raw)
					_debug_minified_raw = loads_exact(out_json)
					_debug_original_raw, _debug_minified_raw, _ = (
						_restore_representation_for_verify(
							_debug_original_raw, _debug_minified_raw, opts
						)
					)
					_debug_original = _reinflate(_debug_original_raw)
					_debug_minified = _reinflate(_debug_minified_raw)
					_debug_error = _validate_block_structure(
						_debug_minified,
						"structure debug after project.json optimization",
						_debug_original,
					)
					if _debug_error:
						print(
							Ansi.error(
								f"\nStructural debug stopped before writing output:\n{_debug_error}"
							)
						)
						return 1
				except Exception as error:
					print(
						Ansi.error(
							f"\nStructural debug itself failed before writing output: {type(error).__name__}: {error}"
						)
					)
					return 1

		progress.next(
			"Process assets and write archive"
			if opts.optimize_assets
			else "Write archive"
		)
		with zipfile.ZipFile(
			dst, "w", zipfile.ZIP_DEFLATED, compresslevel=opts.compression_level
		) as zout:
			if compressed_json is None:
				zout.writestr("project.json", out_json)
			else:
				json_info = zipfile.ZipInfo("project.json")
				json_info.compress_type = zipfile.ZIP_DEFLATED
				json_info.file_size = len(out_json)
				json_info.CRC = zlib.crc32(out_json)
				write_compressed_entry(zout, json_info, compressed_json)
			asset_count = len(assets)
			last_progress_render = time.monotonic()
			for asset_index, (name, data) in enumerate(assets.items(), 1):
				if (
					opts.optimize_assets
					and time.monotonic() - last_progress_render >= 0.15
				):
					progress.detail(f"{asset_index:,}/{asset_count:,} assets")
					last_progress_render = time.monotonic()
				if opts.optimize_assets:
					_ext = os.path.splitext(name)[1].lower()
					if (
						_ext
						in (
							".png",
							".jpg",
							".jpeg",
							".gif",
							".webp",
							".mp3",
							".ogg",
							".m4a",
							".aac",
						)
						and name in asset_infos
					):
						info = copy.copy(asset_infos[name])
						previous = read_compressed_entry(zin, info)
						write_compressed_entry(zout, info, previous)
						continue
					info = (
						copy.copy(asset_infos[name])
						if name in asset_infos
						else zipfile.ZipInfo(name)
					)
					original = None
					if name in asset_infos and zin.read(name) == data:
						original = (
							info.compress_type,
							read_compressed_entry(zin, info),
						)
					asset_zopfli = (
						zopfli
						if opts.zopfli_assets
						and (not opts.auto_zopfli or opts.zopfli_required)
						else False
					)
					method, compressed = optimize_asset(
						data,
						opts.compression_level,
						original,
						asset_zopfli,
						opts.zopfli_iterations,
					)
					info.compress_type = method
					info.file_size = len(data)
					info.CRC = zlib.crc32(data)
					write_compressed_entry(zout, info, compressed)
					continue
				written = False
				if (
					opts.preserve_asset_compression
					and name in asset_infos
					and zin.read(name) == data
				):
					orig_info = asset_infos[name]
					if orig_info.compress_type == zipfile.ZIP_DEFLATED:
						py_comp_size = (
							len(zlib.compress(data, opts.compression_level)) - 6
						)
						if orig_info.compress_size <= py_comp_size:
							zin.fp.seek(orig_info.header_offset)
							header = zin.fp.read(30)
							fn_len = int.from_bytes(header[26:28], "little")
							extra_len = int.from_bytes(header[28:30], "little")
							zin.fp.seek(
								orig_info.header_offset + 30 + fn_len + extra_len
							)
							raw_comp = zin.fp.read(orig_info.compress_size)

							zinfo = zipfile.ZipInfo(name)
							zinfo.date_time = orig_info.date_time
							zinfo.compress_type = zipfile.ZIP_DEFLATED
							zinfo.CRC = orig_info.CRC
							zinfo.file_size = orig_info.file_size
							zinfo.compress_size = orig_info.compress_size
							zinfo.header_offset = zout.fp.tell()

							zout.fp.write(zinfo.FileHeader())
							zout.fp.write(raw_comp)
							zout.filelist.append(zinfo)
							zout.NameToInfo[name] = zinfo
							zout.start_dir = zout.fp.tell()
							written = True
				if not written:
					zout.writestr(
						name,
						data,
						compress_type=zipfile.ZIP_DEFLATED,
						compresslevel=opts.compression_level,
					)

	progress.next("Verify output")
	ok, msg = verify(src, dst, opts)

	if not ok:
		progress.detail(f"failed: {msg}")
		progress.close()
		print(Ansi.error(f"Verification failed: {msg}"))
		os.remove(dst)
		print(Ansi.muted("Output deleted."))
		return 2

	progress.finish("Complete")
	print(Ansi.success("Verified successfully."))

	print(f'Input : "{src}"\nOutput: "{dst}"\n')
	print_transform_stats(stats, opts)

	b, a = len(raw), len(out_json)
	print()

	for line in format_size_report(b, a, os.path.getsize(src), os.path.getsize(dst)):
		print(line)
	return 0
