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


class _DebugStructureError(RuntimeError): ...


def _debug_structure_checkpoint(project, label):
	if not _DEBUG_STRUCTURE_ENABLED:
		return
	
	debug_project = _reinflate(copy.deepcopy(project))
	error = _validate_block_structure(
		debug_project, f"structure debug after {label}", _DEBUG_STRUCTURE_BASELINE
	)
	
	if error:
		raise _DebugStructureError(error)


def apply_transforms(project, opts: Options, assets=None, progress=None):
	global _DEBUG_STRUCTURE_ENABLED, _DEBUG_STRUCTURE_BASELINE
	_DEBUG_STRUCTURE_ENABLED = os.environ.get(
		"MINIFY_DEBUG_STRUCTURE", ""
	).strip().lower() in ("1", "true", "yes", "on")
	_DEBUG_STRUCTURE_BASELINE = (
		copy.deepcopy(project) if _DEBUG_STRUCTURE_ENABLED else None
	)
	inherited_dangling = [
		_dangling_block_ids(target) for target in project.get("targets", [])
	]
	progress = progress or ProgressBar(progress_stage_count(opts))
	stage = progress.next
	stage("Normalize block defaults")
	stats = minify_blocks(project)
	_debug_structure_checkpoint(project, "block-default normalization")

	if opts.convert_wav_to_mp3 and assets is not None:
		stage("Convert WAV sounds to MP3")
		opts.wav_conversions = convert_wav_sounds_to_mp3(project, assets, stats)
	if opts.deduplicate_assets and assets is not None:
		stage("Deduplicate assets")
		opts.asset_deduplications = deduplicate_assets(project, assets, stats)
	if opts.comments:
		stage("Strip sprite comments")
		strip_sprite_comments(project, stats)
	if opts.positions:
		stage("Round positions")
		round_positions(project, stats)
	if opts.covered:
		stage("Reset covered shadow values")
		clear_covered_values(project, stats)
	if opts.monitors:
		stage("Clean monitors")
		clean_monitors(project, stats)
	if opts.cleared_lists:
		stage("Clear selected large lists")
		clear_large_lists(project, opts.cleared_lists, stats)
	if opts.remove_unreachable:
		stage("Remove unreachable blocks")
		before = [
			set((target.get("blocks") or {})) for target in project.get("targets", [])
		]
		remove_unreachable_blocks(project, stats, "unreachable_blocks_removed_first")
		_debug_structure_checkpoint(project, "first unreachable-block removal")
		opts.unreachable_removed_blocks_first = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.remove_unused_procedures:
		stage("Remove unused procedures")
		before = [
			set((target.get("blocks") or {})) for target in project.get("targets", [])
		]
		remove_unused_procedures(project, stats)
		_debug_structure_checkpoint(project, "initial unused-procedure removal")
		opts.unused_procedure_removed_blocks = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.remove_unreachable or opts.remove_unused_procedures:
		stage("Re-scan unreachable blocks")
		before = [
			set((target.get("blocks") or {})) for target in project.get("targets", [])
		]
		remove_unreachable_blocks(project, stats, "unreachable_blocks_removed_second")
		_debug_structure_checkpoint(project, "second unreachable-block removal")
		opts.unreachable_removed_blocks_second = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.folded_constant_variables:
		stage("Fold constant variables")
		fold_constant_variables(project, opts.folded_constant_variables, stats)
		remove_constant_variable_setters(
			project, opts.folded_constant_variable_setters, stats, opts
		)
		_debug_structure_checkpoint(project, "constant-variable folding")
	if (
		opts.constant_propagation
		or opts.fold_constant_expressions
		or opts.associative_constant_merging
		or opts.factor_math_expressions
	):
		stage("Propagate, fold & merge associative constants")
		constant_passes = 1
		while True:
			progress.detail(f"pass {constant_passes}")
			round_changes = 0

			if opts.constant_propagation:
				round_changes += constant_propagation(project, stats, opts)
				_debug_structure_checkpoint(project, "constant propagation")

			if opts.fold_constant_expressions:
				round_changes += fold_constant_expressions(project, stats, opts)
				_debug_structure_checkpoint(project, "constant-expression folding")

			if opts.associative_constant_merging:
				round_changes += merge_associative_constants(project, stats, opts)
				_debug_structure_checkpoint(project, "associative-constant merging")

			if opts.factor_math_expressions:
				round_changes += factor_math_expressions(project, stats, opts)
				_debug_structure_checkpoint(project, "math expression factoring")

			if round_changes == 0:
				break

			constant_passes += 1

	if (
		opts.branch_swapping
		or opts.branch_factoring
		or opts.trivial_loops
		or opts.nested_conditionals
	):
		stage("Rewrite script control structures")
		simplify_script_structures(project, stats, opts)
		_debug_structure_checkpoint(project, "script control-structure rewrites")
	if opts.specialize_procedures:
		stage("Specialize custom procedures")
		specialize_procedures(project, stats, opts, progress)
		_debug_structure_checkpoint(project, "procedure specialization")
	if opts.simplify_boolean_control:
		stage("Simplify boolean control flow")
		simplify_boolean_controls(project, stats, opts)
		_debug_structure_checkpoint(project, "boolean-control simplification")
	elif opts.trivial_loops:
		stage("Simplify trivial boolean loops")
		simplify_boolean_controls(project, stats, opts, only_repeat_one=True)
		_debug_structure_checkpoint(project, "trivial-loop simplification")
	if opts.simplify_blocks:
		stage("Simplify setter RHS blocks")
		_simplify_setter_rhs_blocks(project, stats, opts)
		_debug_structure_checkpoint(project, "setter-RHS simplification")
	if opts.optimize_procedure_arguments:
		stage("Optimize procedure arguments")
		optimize_procedure_arguments(project, stats, opts)
		_debug_structure_checkpoint(project, "procedure-argument optimization")
	if opts.clear_procedure_definition_shadows:
		stage("Clear procedure definition shadows")
		clear_procedure_definition_shadows(project, stats, opts)
		_debug_structure_checkpoint(project, "procedure-definition shadow removal")
	if opts.compact_procedure_prototypes:
		stage("Compact procedure prototypes")
		compact_procedure_prototypes(project, stats, opts)
		_debug_structure_checkpoint(project, "procedure-prototype compaction")
	if opts.merge_duplicate_procedures:
		stage("Merge duplicate procedures")
		merge_duplicate_procedures(project, stats, opts)
		_debug_structure_checkpoint(project, "duplicate-procedure merging")
	if opts.inline_single_use_procedures:
		stage("Inline safe single-use procedures")
		inline_single_use_procedures(project, stats, opts)
		_debug_structure_checkpoint(project, "single-use procedure inlining")
	if opts.remove_unused_procedures and (
		opts.optimize_procedure_arguments
		or opts.merge_duplicate_procedures
		or opts.inline_single_use_procedures
		or opts.specialize_procedures
	):
		stage("Remove procedures made unused")
		before = [
			set((target.get("blocks") or {})) for target in project.get("targets", [])
		]
		remove_unused_procedures(project, stats)
		final_removed = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
		if not hasattr(opts, "unused_procedure_removed_blocks"):
			opts.unused_procedure_removed_blocks = [
				set() for _ in project.get("targets", [])
			]
		_debug_structure_checkpoint(project, "post-rewrite unused-procedure removal")
		for ti, removed_ids in enumerate(final_removed):
			if ti >= len(opts.unused_procedure_removed_blocks):
				opts.unused_procedure_removed_blocks.append(set())
			opts.unused_procedure_removed_blocks[ti].update(removed_ids)
	if opts.group_similar_sequences:
		stage("Group similar sequences")
		group_similar_sequences(project, stats, opts.sequence_threshold, opts)
		_debug_structure_checkpoint(project, "similar-sequence grouping")
	if opts.remove_unused_variables or opts.remove_unused_lists:
		stage("Remove unused variables/lists")
		remove_unused_data(
			project, stats, opts.remove_unused_variables, opts.remove_unused_lists
		)
	stage("Repair dangling broadcast references")
	opts.repaired_broadcast_ids, opts.broadcast_repair_conflicts = (
		repair_dangling_broadcast_refs(project, stats)
	)
	if opts.remove_unused_broadcasts:
		stage("Remove unused broadcasts")
		remove_unused_broadcasts(project, stats)
	if (
		opts.rename_identifiers
		or opts.rename_variable_names
		or opts.rename_list_names
		or opts.rename_broadcast_names
		or opts.rename_argument_names
		or opts.rename_procedure_names
	):
		stage("Rename identifiers")
		opts.renamed_identifiers = rename_identifiers(
			project,
			stats,
			rename_variable_names=(
				opts.rename_identifiers or opts.rename_variable_names
			),
			rename_list_names=(opts.rename_identifiers or opts.rename_list_names),
			rename_broadcast_names=(
				opts.rename_identifiers or opts.rename_broadcast_names
			),
			rename_argument_names=(
				opts.rename_identifiers or opts.rename_argument_names
			),
			rename_procedure_names=(
				opts.rename_identifiers or opts.rename_procedure_names
			),
		)
	used_data_ids = set()
	if opts.rename_variable_ids or opts.rename_list_ids:
		stage("Rename variable/list IDs")
		opts.renamed_variable_ids, opts.renamed_list_ids = rename_variable_list_ids(
			project,
			stats,
			opts.rename_variable_ids,
			opts.rename_list_ids,
			frequency_order=opts.frequency_data_ids,
		)
		used_data_ids.update(opts.renamed_variable_ids.values())
		used_data_ids.update(opts.renamed_list_ids.values())
	if opts.rename_broadcast_ids:
		stage("Rename broadcast IDs")
		opts.renamed_broadcast_ids = rename_broadcast_ids(
			project,
			stats,
			existing_ids=used_data_ids,
			frequency_order=opts.frequency_data_ids,
		)
	if opts.rename_argument_ids:
		stage("Rename argument IDs")
		opts.renamed_argument_ids = rename_argument_ids(project, stats)
	if opts.rename_block_ids:
		stage("Rename block IDs")
		opts.renamed_block_ids = rename_block_ids(
			project, stats, frequency_order=opts.frequency_block_ids
		)
	if opts.compact_numeric_inputs:
		stage("Compact numeric inputs")
		compact_numeric_inputs(project, stats)
	if opts.compact_field_ids:
		stage("Compact redundant field IDs")
		compact_redundant_field_ids(project, stats)
	if opts.compact_mutation_hasnext:
		stage("Compact mutation hasnext")
		compact_mutation_hasnext(project, stats)
	if opts.compact_mutation_metadata:
		stage("Compact mutation metadata")
		compact_mutation_metadata(project, stats)
	if opts.normalize_numbers:
		stage("Normalize numbers")
		normalize_numbers(project, stats, opts.normalize_epsilon)
	if not opts.keep_sound_metadata:
		stage("Remove sound metadata")
		remove_sound_metadata(project, stats)
	if opts.remove_empty_fields:
		stage("Remove empty fields")
		remove_empty_fields(project, stats)
	if opts.remove_empty_inputs:
		stage("Remove empty inputs")
		remove_empty_inputs(project, stats)
	if opts.remove_costume_metadata:
		stage("Remove costume metadata")
		remove_costume_metadata(project, stats, assets)
	if opts.remove_default_target_properties:
		stage("Remove default target properties")
		remove_default_target_properties(project, stats)
	if opts.remove_empty_target_containers:
		stage("Remove empty target containers")
		remove_empty_target_containers(project, stats)
	if opts.remove_project_meta:
		stage("Remove project metadata")
		remove_project_meta(project, stats)
	if opts.compact_data_literals:
		stage("Compact data literals")
		compact_data_literals(project, stats)
	if opts.strip_reference_names:
		stage("Strip reference names")
		strip_reference_names(project, stats)
	if opts.remove_unused_extensions:
		stage("Remove unused extensions")
		remove_unused_extensions(project, stats, opts)

	stage("Repair dangling block links")

	for ti, target in enumerate(project.get("targets", [])):
		stats["dangling_block_refs_fixed"] += repair_dangling_block_refs(
			target,
			preserve_comments=not opts.comments,
			preserve_refs=inherited_dangling[ti],
		)
	_debug_structure_checkpoint(project, "dangling-block-link repair")

	return stats


def _reinflate(project):
	for target in project.get("targets", []):
		if not target.get("isStage"):
			target.setdefault("broadcasts", {})
			target.setdefault("comments", {})
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue
			block.setdefault("fields", {})
			block.setdefault("inputs", {})
			block.setdefault("topLevel", False)
			block.setdefault("shadow", False)
			mut = block.get("mutation")
			if isinstance(mut, dict):
				if mut.get("warp") is True:
					mut["warp"] = "true"
				elif mut.get("warp") is False:
					mut["warp"] = "false"
	return project


def _restore_block_ids(project, renamed_ids):
	for ti, target in enumerate(project.get("targets", [])):
		block_ids = {new: old for old, new in (renamed_ids.get(ti) or {}).items()}
		if not block_ids:
			continue
		blocks = target.get("blocks", {})
		target["blocks"] = {
			block_ids.get(block_id, block_id): block
			for block_id, block in blocks.items()
		}
		for block in target["blocks"].values():
			if isinstance(block, dict):
				for key in ("next", "parent"):
					if key in block:
						block[key] = block_ids.get(block[key], block[key])
				for value in (block.get("inputs") or {}).values():
					replace_input_block_ids(value, block_ids)
		for comment in (target.get("comments") or {}).values():
			if (
				isinstance(comment, dict)
				and "blockId" in comment
				and comment["blockId"] is not None
			):
				comment["blockId"] = block_ids.get(
					comment["blockId"], comment["blockId"]
				)


def _restore_data_ids(project, variable_ids, list_ids):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)

	var_rev, list_rev = {}, {}
	for (ti, old), new in (variable_ids or {}).items():
		var_rev.setdefault(ti, {})[new] = old
	for (ti, old), new in (list_ids or {}).items():
		list_rev.setdefault(ti, {})[new] = old

	current_var_ids = [set((t.get("variables") or {}).keys()) for t in targets]
	current_list_ids = [set((t.get("lists") or {}).keys()) for t in targets]

	def restore_var(ti, i):
		if i in current_var_ids[ti]:
			return var_rev.get(ti, {}).get(i, i)
		if stage_index is not None:
			return var_rev.get(stage_index, {}).get(i, i)
		return i

	def restore_list(ti, i):
		if i in current_list_ids[ti]:
			return list_rev.get(ti, {}).get(i, i)
		if stage_index is not None:
			return list_rev.get(stage_index, {}).get(i, i)
		return i

	def restore_nested(ti, value):
		if isinstance(value, list):
			if len(value) > 2 and value[0] == PRIMITIVE_VARIABLE:
				value[2] = restore_var(ti, value[2])
			elif len(value) > 2 and value[0] == PRIMITIVE_LIST:
				value[2] = restore_list(ti, value[2])
			else:
				for v in value:
					restore_nested(ti, v)
		elif isinstance(value, dict):
			for v in value.values():
				restore_nested(ti, v)

	for ti, target in enumerate(targets):
		target["variables"] = {
			var_rev.get(ti, {}).get(k, k): v
			for k, v in (target.get("variables") or {}).items()
		}
		target["lists"] = {
			list_rev.get(ti, {}).get(k, k): v
			for k, v in (target.get("lists") or {}).items()
		}
		for block in target.get("blocks", {}).values():
			if isinstance(block, list):
				if len(block) > 2 and block[0] == PRIMITIVE_VARIABLE:
					block[2] = restore_var(ti, block[2])
				elif len(block) > 2 and block[0] == PRIMITIVE_LIST:
					block[2] = restore_list(ti, block[2])
				continue
			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			f = fields.get("VARIABLE")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = restore_var(ti, f[1])
			f = fields.get("LIST")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = restore_list(ti, f[1])
			for value in (block.get("inputs") or {}).values():
				restore_nested(ti, value)

	name_to_index = {
		t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")
	}
	for m in project.get("monitors", []):
		if not isinstance(m, dict):
			continue
		sprite = m.get("spriteName")
		ti = name_to_index.get(sprite, stage_index) if sprite else stage_index
		if ti is None:
			continue
		if m.get("opcode") == "data_variable":
			m["id"] = restore_var(ti, m.get("id"))
		elif m.get("opcode") == "data_listcontents":
			m["id"] = restore_list(ti, m.get("id"))


def _restore_broadcast_ids(project, mapping):
	rev = {new: old for old, new in (mapping or {}).items()}
	for target in project.get("targets", []):
		if target.get("broadcasts"):
			target["broadcasts"] = {
				rev.get(new, new): name for new, name in target["broadcasts"].items()
			}
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict):
				_replace_field_id(block, ("BROADCAST_OPTION", "BROADCAST_INPUT"), rev)
				for value in (block.get("inputs") or {}).values():
					replace_broadcast_ids_in_value(value, rev)


def _restore_argument_ids(project, mapping):
	rev = {}  # {(ti, proccode): {new_id: old_id}}
	for (ti, proc, old), new in (mapping or {}).items():
		rev.setdefault((ti, proc), {})[new] = old

	for ti, target in enumerate(project.get("targets", [])):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict) or block.get("opcode") not in (
				"procedures_prototype",
				"procedures_call",
			):
				continue
			mut = block.get("mutation")
			vals = parse_argument_ids(mut)
			if vals is None:
				continue
			proc = mut.get("proccode")
			local_rev = rev.get((ti, proc))
			if not local_rev:
				continue
			mut["argumentids"] = json.dumps(
				[local_rev.get(x, x) for x in vals],
				separators=(",", ":"),
				ensure_ascii=False,
			)
			inputs = block.get("inputs") or {}
			block["inputs"] = {local_rev.get(k, k): v for k, v in inputs.items()}


def _num_eq(a, b, epsilon=0):
	if (
		not isinstance(a, (int, float))
		or not isinstance(b, (int, float))
		or isinstance(a, bool)
		or isinstance(b, bool)
	):
		return False
	if (isinstance(a, float) and not math.isfinite(a)) or (
		isinstance(b, float) and not math.isfinite(b)
	):
		return a == b
	try:
		da = Decimal(str(a))
		db = Decimal(str(b))
		if da == db:
			return True
		if epsilon:
			return abs(da - db) <= Decimal(str(epsilon))
		return False
	except (InvalidOperation, ValueError):
		if a == b:
			return True
		return bool(epsilon) and abs(a - b) <= epsilon


def _position_num_eq(original, minified):
	if _num_eq(original, minified):
		return True
	if (
		not isinstance(original, (int, float))
		or isinstance(original, bool)
		or not isinstance(minified, (int, float))
		or isinstance(minified, bool)
	):
		return False
	if isinstance(original, float) and not math.isfinite(original):
		return False
	try:
		return minified == round(original)
	except (TypeError, ValueError, OverflowError):
		return False


def _check_broadcast_consistency(project):
	definitions = {}
	for ti, target in enumerate(project.get("targets", [])):
		for bid, name in (target.get("broadcasts") or {}).items():
			if not isinstance(bid, str) or not bid:
				return f"target {ti} ({target.get('name')!r}): invalid broadcast ID {bid!r}"
			if not isinstance(name, str):
				return f"target {ti} ({target.get('name')!r}), broadcast {bid!r}: name is not a string"
			if bid in definitions and definitions[bid][0] != name:
				return f"broadcast {bid!r} has conflicting definitions {definitions[bid][0]!r} and {name!r}"
			definitions[bid] = (name, ti)

	for bid, info in get_broadcast_refs(project).items():
		if bid not in definitions:
			names = sorted(info["names"])
			if len(names) > 1:
				return f"broadcast {bid!r} has conflicting reference names {names} and no definition"
			return f"broadcast reference {bid!r} has no matching definition"
		defined_name = definitions[bid][0]
		conflicts = sorted(n for n in info["names"] if n != defined_name)
		if conflicts:
			return f"broadcast {bid!r}: definition name {defined_name!r} conflicts with reference name(s) {conflicts}"
	return None


def _check_broadcast_ids_resolve(project):
	valid = get_all_broadcast_ids(project)
	for target in project.get("targets", []):
		for bid, b in target.get("blocks", {}).items():
			if not isinstance(b, dict):
				continue
			for name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
				f = (b.get("fields") or {}).get(name)
				if (
					isinstance(f, list)
					and len(f) > 1
					and isinstance(f[1], str)
					and f[1] not in valid
				):
					return f"{target.get('name')!r}/{bid!r}: broadcast id {f[1]!r} has no matching message"
			for value in (b.get("inputs") or {}).values():
				bad = _find_dangling_broadcast(value, valid)
				if bad is not None:
					return f"{target.get('name')!r}/{bid!r}: broadcast id {bad!r} has no matching message"
	return _check_broadcast_consistency(project)


def _find_dangling_broadcast(value, valid):
	if not isinstance(value, list) or not value:
		return None
	if len(value) > 2 and value[0] == PRIMITIVE_BROADCAST and isinstance(value[2], str):
		return value[2] if value[2] not in valid else None
	for v in value:
		bad = _find_dangling_broadcast(v, valid)
		if bad is not None:
			return bad
	return None


def _check_block_references_resolve(project, original=None):
	original_targets = original.get("targets", []) if original is not None else []
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		old_blocks = (
			original_targets[ti].get("blocks", {}) if ti < len(original_targets) else {}
		)
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			old_block = old_blocks.get(bid)
			if not isinstance(old_block, dict):
				old_block = {}
			parent = block.get("parent")
			if (
				isinstance(parent, str)
				and parent not in blocks
				and not is_orphan_arg_reporter(block, blocks)
				and not (parent not in old_blocks and old_block.get("parent") == parent)
			):
				return f"{target.get('name')!r}/{bid!r}: parent points to missing block {parent!r}"
			nxt = block.get("next")
			if (
				isinstance(nxt, str)
				and nxt not in blocks
				and not (nxt not in old_blocks and old_block.get("next") == nxt)
			):
				return f"{target.get('name')!r}/{bid!r}: next points to missing block {nxt!r}"
			for name, value in (block.get("inputs") or {}).items():
				for child_id in iter_input_block_refs(value):
					if child_id not in blocks:
						if child_id not in old_blocks and any(
							child_id in iter_input_block_refs(value)
							for value in (old_block.get("inputs") or {}).values()
						):
							continue
						return f"{target.get('name')!r}/{bid!r}: input {name!r} points to missing block {child_id!r}"
	return None


def _check_argument_id_consistency(target, where, opts=None):
	for bid, b in target.get("blocks", {}).items():
		if not isinstance(b, dict) or b.get("opcode") not in (
			"procedures_prototype",
			"procedures_call",
		):
			continue
		vals = parse_argument_ids(b.get("mutation"))
		if vals is None:
			continue
		if (
			b.get("opcode") == "procedures_prototype"
			and getattr(opts, "compact_procedure_prototypes", False)
			and not (b.get("inputs") or {})
		):
			continue
		if set(vals) != set((b.get("inputs") or {}).keys()):
			return (
				f"{where}/{bid!r}: procedures argumentids {vals} does not match "
				f"inputs keys {sorted((b.get('inputs') or {}).keys())}"
			)
	return None


def _input_val_eq(vo, vm, opts):
	if vo == vm:
		return True

	if opts.normalize_numbers and (_num_eq(vo, vm, opts.normalize_epsilon) or vo == vm):
		return True

	if getattr(opts, "optimize_json", False):
		if (
			isinstance(vo, (int, float))
			and isinstance(vm, (int, float))
			and not isinstance(vo, bool)
			and not isinstance(vm, bool)
			and not (isinstance(vo, float) and not math.isfinite(vo))
			and not (isinstance(vm, float) and not math.isfinite(vm))
		):
			try:
				if Decimal(str(vo)) == Decimal(str(vm)):
					return True
			except InvalidOperation:
				...

	if opts.compact_numeric_inputs:
		if (
			isinstance(vo, str)
			and isinstance(vm, (int, float))
			and not isinstance(vm, bool)
		):
			try:
				iv = int(vo)
			except (ValueError, OverflowError):
				iv = None
			if iv is not None and str(iv) == vo:
				if isinstance(vm, float) and not math.isfinite(vm):
					return False
				return Decimal(str(iv)) == Decimal(str(vm))

			try:
				fv = float(vo)
			except (ValueError, OverflowError):
				fv = None
			if (
				fv is not None
				and "." in vo
				and "e" not in vo.lower()
				and str(fv) == vo
				and math.isfinite(fv)
			):
				try:
					return Decimal(str(fv)) == Decimal(str(vm))
				except InvalidOperation:
					return False

	return False


def _input_has_dangling_block_ref(value, blocks):
	if not isinstance(value, list) or not value:
		return False
	tag = value[0]
	if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		if len(value) > 1 and isinstance(value[1], str):
			return value[1] not in blocks
		if len(value) > 1 and isinstance(value[1], (list, dict)):
			return _input_has_dangling_block_ref(value[1], blocks)
		return False
	if tag == INPUT_DIFF_BLOCK_SHADOW:
		if len(value) > 1 and isinstance(value[1], str) and value[1] not in blocks:
			return True
		if len(value) > 2:
			if isinstance(value[2], str):
				return value[2] not in blocks
			if isinstance(value[2], (list, dict)):
				return _input_has_dangling_block_ref(value[2], blocks)
	for sub in value[1:]:
		if isinstance(sub, (list, dict)) and _input_has_dangling_block_ref(sub, blocks):
			return True
	return False


def _folded_input_equivalent(value, literals, project, target_index):
	if not isinstance(value, list):
		if isinstance(value, dict):
			return {
				k: _folded_input_equivalent(v, literals, project, target_index)
				for k, v in value.items()
			}
		return value
	if (
		value
		and value[0] == PRIMITIVE_VARIABLE
		and len(value) > 2
		and isinstance(value[2], str)
	):
		owner = _resolve_variable_owner(project, target_index, value[2])
		if owner in literals:
			return copy.deepcopy(literals[owner])
	return [
		(
			_folded_input_equivalent(v, literals, project, target_index)
			if isinstance(v, (list, dict))
			else v
		)
		for v in value
	]


def _check_inputs_match(
	oi,
	mi,
	opts,
	original_blocks=None,
	remaining_blocks=None,
	target_index=None,
	_allow_folded=True,
):
	if oi == mi:
		return True
	if _allow_folded:
		folded_literals = getattr(
			opts, "folded_constant_variable_literals", None
		) or getattr(opts, "folded_constant_variables", None)
		verify_project = getattr(opts, "_verify_project", None)
		if folded_literals and target_index is not None and verify_project is not None:
			folded = _folded_input_equivalent(
				oi, folded_literals, verify_project, target_index
			)
			if folded != oi:
				return _check_inputs_match(
					folded,
					mi,
					opts,
					original_blocks,
					remaining_blocks,
					target_index,
					_allow_folded=False,
				)
	if (
		(opts.remove_unreachable or opts.remove_unused_procedures)
		and remaining_blocks is not None
		and _input_has_dangling_block_ref(oi, remaining_blocks)
	):
		expected = copy.deepcopy(oi)
		expected, _ = repair_dangling_block_ref(expected, remaining_blocks)
		if expected is None:
			return False
		if expected != oi:
			return expected == mi or _check_inputs_match(
				expected,
				mi,
				opts,
				None,
				None,
				target_index,
				_allow_folded=_allow_folded,
			)

	if (
		target_index is not None
		and getattr(opts, "_verify_block_opcode", None) == "procedures_definition"
		and getattr(opts, "_verify_block_input_name", None) == "custom_block"
		and isinstance(oi, list)
		and isinstance(mi, list)
		and len(oi) == len(mi) == 2
		and oi[1] == mi[1]
		and _input_tag(oi[0]) in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
		and _input_tag(mi[0]) in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
	):
		return True
	if not (isinstance(oi, list) and isinstance(mi, list) and len(oi) == len(mi)):
		return False
	if not oi or not mi or oi[0] != mi[0]:
		return False
	if oi[0] in CONTAINER_TAGS and len(oi) >= 3 and len(mi) >= 3:
		if oi[2] != mi[2]:
			return False
		return oi[1] == mi[1] or bool(
			getattr(opts, "strip_reference_names", False) and mi[1] == ""
		)
	if oi[0] in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		if len(oi) > 1 and len(mi) > 1:
			if (
				isinstance(oi[1], list)
				and isinstance(mi[1], list)
				and len(oi[1]) == len(mi[1])
			):
				if oi[1][0] == mi[1][0] and oi[1][0] in NUMERIC_TAGS:
					return _input_val_eq(oi[1][1], mi[1][1], opts)
				return oi[1] == mi[1]
			return oi[1] == mi[1]
	elif oi[0] == INPUT_DIFF_BLOCK_SHADOW:
		if len(oi) > 1 and len(mi) > 1 and oi[1] != mi[1]:
			return False
		if len(oi) > 2 and len(mi) > 2:
			if (
				opts.covered
				and isinstance(oi[2], list)
				and isinstance(mi[2], list)
				and len(oi[2]) == 2
				and len(mi[2]) == 2
				and oi[2][0] == mi[2][0]
				and oi[2][0] in NUMERIC_TAGS + (PRIMITIVE_TEXT,)
			):
				return True
			if (
				isinstance(oi[2], list)
				and isinstance(mi[2], list)
				and len(oi[2]) == len(mi[2])
			):
				if oi[2][0] == mi[2][0] and oi[2][0] in NUMERIC_TAGS:
					return _input_val_eq(oi[2][1], mi[2][1], opts)
				return oi[2] == mi[2]
	return False


def _check_fields_match(original_fields, minified_fields, opts):
	if original_fields == minified_fields:
		return True
	if (
		getattr(opts, "strip_reference_names", False)
		and isinstance(original_fields, dict)
		and isinstance(minified_fields, dict)
		and set(original_fields) == set(minified_fields)
	):
		good = True
		for key, ov in original_fields.items():
			mv = minified_fields[key]
			if (
				key in ("VARIABLE", "LIST")
				and isinstance(ov, list)
				and isinstance(mv, list)
				and len(ov) == len(mv)
				and len(ov) >= 2
				and ov[1] == mv[1]
				and mv[0] == ""
			):
				continue
			if ov != mv:
				good = False
				break
		if good:
			return True
	if not (
		opts.compact_field_ids
		and isinstance(original_fields, dict)
		and isinstance(minified_fields, dict)
	):
		return False
	if set(original_fields) != set(minified_fields):
		return False
	for name, ov in original_fields.items():
		mv = minified_fields[name]
		if ov == mv:
			continue
		if (
			isinstance(ov, list)
			and len(ov) == 2
			and ov[1] is None
			and isinstance(mv, list)
			and len(mv) == 1
			and ov[0] == mv[0]
		):
			continue
		return False
	return True


def _cached_json_loads(value):
	return json.loads(value)


def _check_mutation_match(
	original_mutation, minified_mutation, opts, target_index=None, block_id=None
):
	if original_mutation == minified_mutation:
		return True
	if not (
		isinstance(original_mutation, dict) and isinstance(minified_mutation, dict)
	):
		return False
	missing_keys = set(original_mutation) - set(minified_mutation)
	extra_keys = set(minified_mutation) - set(original_mutation)
	allowed_missing = set()
	if (
		opts.compact_mutation_hasnext
		and missing_keys == {"hasnext"}
		and original_mutation.get("hasnext") in (False, "false")
	):
		allowed_missing.add("hasnext")
	if opts.compact_mutation_metadata:
		if missing_keys or extra_keys:
			if missing_keys - allowed_missing or extra_keys:
				return False
	else:
		if set(original_mutation) != set(minified_mutation) and (
			missing_keys - allowed_missing or extra_keys
		):
			return False

	def _specialized_proccode_match(expected_values, mv):
		if not expected_values:
			return False
		if isinstance(expected_values, str):
			expected_values = (expected_values,)
		try:
			candidates = set(expected_values)
		except TypeError:
			return False
		if mv in candidates:
			return True

		if getattr(opts, "rename_procedure_names", False):
			rename_map = getattr(opts, "renamed_identifiers", {}) or {}
			procedure_names = rename_map.get("procedures", {})
			if isinstance(procedure_names, dict):
				for key, original_name in procedure_names.items():
					if (
						isinstance(key, tuple)
						and len(key) == 2
						and key[0] == target_index
						and original_name in candidates
						and mv == key[1]
					):
						return True
		return False

	for key, ov in original_mutation.items():
		if key not in minified_mutation:
			if key in allowed_missing:
				continue
			return False
		mv = minified_mutation[key]
		if key == "proccode" and target_index is not None and block_id is not None:
			specialized_calls = (
				getattr(opts, "specialized_procedure_call_proccodes", {}) or {}
			)
			expected_specialized = specialized_calls.get((target_index, block_id))
			if _specialized_proccode_match(expected_specialized, mv):
				continue

			specialized_prototypes = (
				getattr(opts, "specialized_procedure_prototype_proccodes", {}) or {}
			)
			expected_specialized = specialized_prototypes.get((target_index, block_id))
			if _specialized_proccode_match(expected_specialized, mv):
				continue

			prototype_pairs = (
				getattr(opts, "specialized_procedure_prototype_code_pairs", {}) or {}
			)
			expected_variants = prototype_pairs.get((target_index, ov))
			if _specialized_proccode_match(expected_variants, mv):
				continue

			specialized_prototype_blocks = (
				getattr(opts, "specialized_procedure_prototype_blocks", {}) or {}
			)
			if specialized_prototype_blocks.get((target_index, block_id)):
				if re.findall(r"%[snb]", ov) == re.findall(r"%[snb]", mv):
					continue

		if ov == mv:
			continue
		if (
			(
				opts.compact_mutation_metadata
				or (opts.rename_argument_ids and key == "argumentids")
			)
			and key in ("argumentids", "argumentnames", "argumentdefaults")
			and isinstance(ov, str)
			and isinstance(mv, str)
		):
			try:
				if _cached_json_loads(ov) == _cached_json_loads(mv):
					continue
			except (TypeError, ValueError):
				...
		return False
	return True


def _check_blocks(
	o,
	m,
	opts,
	where,
	original_blocks=None,
	remaining_blocks=None,
	target_index=None,
	block_id=None,
):
	if o == m:
		return None
	remaining_blocks = (
		remaining_blocks if remaining_blocks is not None else set(original_blocks or ())
	)
	if set(o) != set(m):
		gone = set(o) - set(m)
		extra = set(m) - set(o)
		allowed_gone = set()
		if opts.comments:
			allowed_gone.add("comment")
		if getattr(opts, "strip_script_positions", False):
			allowed_gone.update(("x", "y"))
		if (
			"next" in gone
			and len(gone) == 1
			and getattr(opts, "compact_terminal_links", False)
			and o.get("next") is None
			and block_id is not None
			and block_id
			in (getattr(opts, "_terminal_link_ids", {}) or {}).get(target_index, ())
		):
			allowed_gone.add("next")
		if extra or not gone <= allowed_gone:
			return f"{where} (opcode: {o.get('opcode')}): block keys changed. Missing keys: {sorted(gone)}, unexpected extra keys: {sorted(extra)}"
	allow_repairs = (
		opts.remove_unreachable
		or opts.remove_unused_procedures
		or getattr(opts, "inline_single_use_procedures", False)
	)
	for k in o:
		if k not in m:
			continue
		if k == "opcode":
			script_opcodes = getattr(opts, "script_rewrite_opcode_edits", {}) or {}
			expected_opcode = script_opcodes.get((target_index, block_id))
			if expected_opcode is not None and m[k] == expected_opcode:
				continue
			folded_opcodes = (
				getattr(opts, "folded_constant_expression_opcode_edits", {}) or {}
			)
			expected_opcode = folded_opcodes.get((target_index, block_id))
			if expected_opcode is not None and m[k] == expected_opcode:
				continue
			simplified_opcodes = (
				getattr(opts, "simplified_block_opcode_edits", {}) or {}
			)
			expected_opcode = simplified_opcodes.get((target_index, block_id))
			if expected_opcode is not None and m[k] == expected_opcode:
				continue
			if o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): property {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "fields":
			if not _check_fields_match(o[k], m[k], opts):
				return f"{where} (opcode: {o.get('opcode')}): fields changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "shadow":
			prototype_shadows = (
				getattr(opts, "procedure_prototype_compaction_shadow_edits", {}) or {}
			)
			if (target_index, block_id) in prototype_shadows and m[k] is False:
				continue
			if o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): property {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "mutation":
			procedure_mutations = (
				getattr(opts, "procedure_argument_mutation_edits", {}) or {}
			)
			procedure_mutation = procedure_mutations.get((target_index, block_id))
			if procedure_mutation is not None:
				expected_mutation = copy.deepcopy(procedure_mutation)
				if isinstance(expected_mutation, dict):
					if expected_mutation.get("warp") is True:
						expected_mutation["warp"] = "true"
					elif expected_mutation.get("warp") is False:
						expected_mutation["warp"] = "false"
				if _check_mutation_match(
					expected_mutation, m[k], opts, target_index, block_id
				):
					continue
			if not _check_mutation_match(o[k], m[k], opts, target_index, block_id):
				return f"{where} (opcode: {o.get('opcode')}): mutation changed from original {o[k]!r} to minified {m[k]!r}"
		elif k in ("x", "y") and opts.positions:
			if not _position_num_eq(o[k], m[k]) and o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): coordinate {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "inputs":
			gone_inputs = set(o["inputs"]) - set(m["inputs"])
			extra_inputs = set(m["inputs"]) - set(o["inputs"])
			allowed_gone = set()
			if allow_repairs and remaining_blocks is not None:
				for name in gone_inputs:
					fixed, changed = repair_dangling_block_ref(
						copy.deepcopy(o["inputs"][name]), remaining_blocks
					)
					if changed and fixed is None:
						allowed_gone.add(name)
			procedure_removed = (
				getattr(opts, "procedure_argument_removed_inputs", {}) or {}
			)
			allowed_procedure_removed = procedure_removed.get(
				(target_index, block_id), set()
			)
			allowed_gone.update(gone_inputs & set(allowed_procedure_removed))
			prototype_inputs = (
				getattr(opts, "procedure_prototype_compaction_input_edits", {}) or {}
			)
			prototype_edit = prototype_inputs.get((target_index, block_id))
			if prototype_edit is not None:
				if o.get("opcode") == "procedures_prototype" and m.get("inputs") == {}:
					allowed_gone.update(gone_inputs)
				elif isinstance(prototype_edit, dict):
					for name, expected in prototype_edit.items():
						if (
							name in o.get("inputs", {})
							and m.get("inputs", {}).get(name) == expected
						):
							continue
						return f"{where} (opcode: {o.get('opcode')}): input {name!r} changed unexpectedly during procedure prototype compaction"
			script_removed_inputs = (
				getattr(opts, "script_rewrite_removed_inputs", {}) or {}
			)
			allowed_gone.update(
				gone_inputs
				& set(script_removed_inputs.get((target_index, block_id), set()))
			)

			script_opcodes = getattr(opts, "script_rewrite_opcode_edits", {}) or {}
			expected_script_opcode = script_opcodes.get((target_index, block_id))
			script_input_edits = getattr(opts, "script_rewrite_input_edits", {}) or {}
			recorded_substack = script_input_edits.get(
				(target_index, block_id, "SUBSTACK")
			)
			script_removed = getattr(opts, "script_rewrite_removed_inputs", {}) or {}
			recorded_removed = set(script_removed.get((target_index, block_id), set()))
			schema_match = False
			if (
				o.get("opcode") == "control_if_else"
				and expected_script_opcode == "control_if"
				and gone_inputs == {"SUBSTACK2"}
				and extra_inputs == {"SUBSTACK"}
				and "SUBSTACK2" in recorded_removed
			):
				schema_match = _check_inputs_match(
					o["inputs"]["SUBSTACK2"],
					m["inputs"]["SUBSTACK"],
					opts,
					original_blocks,
					remaining_blocks,
					target_index,
				)
				if not schema_match and recorded_substack is not None:
					schema_match = _check_inputs_match(
						recorded_substack,
						m["inputs"]["SUBSTACK"],
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				if not schema_match:
					inlined_substack = (
						getattr(opts, "inlined_procedure_input_edits", {}) or {}
					).get((target_index, block_id, "SUBSTACK"))
					schema_match = (
						inlined_substack is not None
						and inlined_substack == m["inputs"]["SUBSTACK"]
					)
			if schema_match:
				gone_inputs.clear()
				extra_inputs.clear()

			if extra_inputs or gone_inputs - allowed_gone:
				return f"{where} (opcode: {o.get('opcode')}): input names changed. Original inputs: {sorted(o['inputs'])}, minified inputs: {sorted(m['inputs'])}"
			for name in set(o["inputs"]) & set(m["inputs"]):
				oi, mi = o["inputs"][name], m["inputs"][name]
				if (
					name == "custom_block"
					and o.get("opcode") == "procedures_definition"
					and isinstance(oi, list)
					and isinstance(mi, list)
					and len(oi) == len(mi) == 2
					and oi[1] == mi[1]
					and (
						_input_tag(oi[0])
						in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
						or (
							type(oi[0]) is str
							and oi[0]
							in (
								str(INPUT_SAME_BLOCK_SHADOW),
								str(INPUT_BLOCK_NO_SHADOW),
							)
						)
					)
					and (
						_input_tag(mi[0])
						in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
						or (
							type(mi[0]) is str
							and mi[0]
							in (
								str(INPUT_SAME_BLOCK_SHADOW),
								str(INPUT_BLOCK_NO_SHADOW),
							)
						)
					)
				):
					continue
				setter_inputs = getattr(opts, "variable_setter_input_edits", None) or {}
				setter_input = setter_inputs.get((target_index, block_id, name))
				if setter_input is not None and (
					setter_input == mi
					or _check_inputs_match(
						setter_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				procedure_arg_inputs = (
					getattr(opts, "procedure_argument_input_edits", {}) or {}
				)
				procedure_arg_input = procedure_arg_inputs.get(
					(target_index, block_id, name)
				)
				if procedure_arg_input is not None and (
					procedure_arg_input == mi
					or _check_inputs_match(
						procedure_arg_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				inlined_inputs = (
					getattr(opts, "inlined_procedure_input_edits", {}) or {}
				)
				inlined_input = inlined_inputs.get((target_index, block_id, name))
				if inlined_input is not None and (
					inlined_input == mi
					or _check_inputs_match(
						inlined_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				folded_inputs = getattr(opts, "folded_constant_expression_inputs", {})
				folded_input = folded_inputs.get((target_index, block_id, name))
				if folded_input is not None and (
					folded_input == mi
					or _check_inputs_match(
						folded_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				grouped_inputs = getattr(opts, "grouped_sequence_input_edits", {}) or {}
				grouped_input = grouped_inputs.get((target_index, block_id, name))
				if grouped_input is not None and (
					grouped_input == mi
					or _check_inputs_match(
						grouped_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				script_inputs = getattr(opts, "script_rewrite_input_edits", {}) or {}
				script_input = script_inputs.get((target_index, block_id, name))
				if script_input is not None and (
					script_input == mi
					or _check_inputs_match(
						script_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				simplified_inputs = (
					getattr(opts, "simplified_block_input_edits", {}) or {}
				)
				simplified_input = simplified_inputs.get((target_index, block_id, name))
				if simplified_input is not None and (
					simplified_input == mi
					or _check_inputs_match(
						simplified_input,
						mi,
						opts,
						original_blocks,
						remaining_blocks,
						target_index,
					)
				):
					continue
				if not _check_inputs_match(
					oi, mi, opts, original_blocks, remaining_blocks, target_index
				):
					return f"{where} (opcode: {o.get('opcode')}): input {name!r} changed from original {oi!r} to minified {mi!r}"
		else:
			link_edits = getattr(opts, "variable_setter_link_edits", None) or {}
			fold_link_edits = (
				getattr(opts, "folded_constant_expression_link_edits", None) or {}
			)
			script_link_edits = getattr(opts, "script_rewrite_link_edits", None) or {}
			group_link_edits = getattr(opts, "grouped_sequence_link_edits", None) or {}
			simplified_link_edits = (
				getattr(opts, "simplified_block_link_edits", None) or {}
			)
			if k in ("next", "parent"):
				link_key = (target_index, block_id, k)
				found_link = False
				expected_link = None
				for mapping in (
					link_edits,
					fold_link_edits,
					getattr(opts, "inlined_procedure_link_edits", {}) or {},
					group_link_edits,
					simplified_link_edits,
					script_link_edits,
				):
					if link_key in mapping:
						expected_link = mapping[link_key]
						found_link = True
						break
				if found_link and m[k] == expected_link:
					continue
			if (
				k == "parent"
				and m[k] is None
				and isinstance(o[k], str)
				and is_orphan_arg_reporter(o, original_blocks or {})
				and o[k] not in (original_blocks or {})
			):
				continue

			if (
				allow_repairs
				and k in ("next", "parent")
				and isinstance(o[k], str)
				and remaining_blocks is not None
				and o[k] not in remaining_blocks
				and m[k] is None
			):
				continue
			if o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): property {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
	return None


def _zip_entry_names(zf):
	infos = zf.infolist()
	names = [info.filename for info in infos]
	if len(names) != len(set(names)):
		dupes = sorted(name for name, n in Counter(names).items() if n > 1)
		return None, f"archive contains duplicate entry names: {dupes[:10]}"
	for name in names:
		if not isinstance(name, str) or not name or name.startswith("/"):
			return (
				None,
				f"archive contains an invalid absolute/empty entry name: {name!r}",
			)
		parts = name.replace("\\", "/").split("/")
		if any(part in ("", ".", "..") for part in parts if name != "project.json"):
			return None, f"archive contains an unsafe entry path: {name!r}"
	return set(names), None


def _valid_asset_id(value):
	return (
		isinstance(value, str)
		and len(value) == 32
		and all(c in "0123456789abcdefABCDEF" for c in value)
	)


def _asset_filename(entry, kind):
	if not isinstance(entry, dict):
		return None
	md5ext = entry.get("md5ext")
	if isinstance(md5ext, str) and md5ext:
		return md5ext
	aid = entry.get("assetId")
	fmt = entry.get("dataFormat")
	if _valid_asset_id(aid) and isinstance(fmt, str) and fmt:
		return f"{aid}.{fmt}"
	return None


def _validate_asset_entries(project, zf, label, payload_cache=None):
	try:
		names, err = _zip_entry_names(zf)
	except Exception as exc:
		return f"{label}: unable to inspect archive entries: {exc}"
	if err:
		return f"{label}: {err}"

	for ti, target in enumerate(project.get("targets", [])):
		for kind, key in (("costume", "costumes"), ("sound", "sounds")):
			for index, entry in enumerate(target.get(key, [])):
				where = f"{label}, target {ti} ({target.get('name')!r}), {kind} {index}"
				if not isinstance(entry, dict):
					return f"{where}: entry is not an object"
				if not _valid_asset_id(entry.get("assetId")):
					return f"{where}: invalid assetId {entry.get('assetId')!r}"
				if not isinstance(entry.get("name"), str):
					return f"{where}: name is not a string"
				fmt = entry.get("dataFormat")
				if fmt not in _ASSET_EXTENSIONS[kind]:
					return f"{where}: unsupported dataFormat {fmt!r}"
				md5ext = entry.get("md5ext")
				if md5ext is not None:
					if not isinstance(md5ext, str) or "." not in md5ext:
						return f"{where}: invalid md5ext {md5ext!r}"
					stem, ext = md5ext.rsplit(".", 1)
					if not _valid_asset_id(stem):
						return f"{where}: md5ext stem does not match an asset ID: {md5ext!r}"
					if stem.lower() != entry["assetId"].lower():
						return f"{where}: md5ext {md5ext!r} disagrees with assetId {entry['assetId']!r}"
					allowed_exts = _ASSET_EXTENSIONS[kind]
					if ext.lower() not in allowed_exts:
						return (
							f"{where}: md5ext extension {ext!r} is invalid for {kind}"
						)
					filename = md5ext
				else:
					filename = _asset_filename(entry, kind)
					if filename is None:
						return f"{where}: cannot derive an asset filename"

				if filename not in names:
					return f"{where}: referenced asset {filename!r} is missing from archive"
				if payload_cache is not None and filename in payload_cache:
					payload = payload_cache[filename]
				else:
					payload = zf.read(filename)
					if payload_cache is not None:
						payload_cache[filename] = payload
				actual_id = hashlib.md5(payload).hexdigest()
				if actual_id.lower() != entry["assetId"].lower():
					return (
						f"{where}: asset {filename!r} has MD5 {actual_id}, "
						f"but project declares {entry['assetId']}"
					)

				if kind == "costume":
					if "bitmapResolution" in entry:
						br = entry["bitmapResolution"]
						if isinstance(br, bool) or not isinstance(br, int) or br <= 0:
							return f"{where}: invalid bitmapResolution {br!r}"
					for axis in ("rotationCenterX", "rotationCenterY"):
						if axis in entry and (
							isinstance(entry[axis], bool)
							or not isinstance(entry[axis], (int, float))
						):
							return f"{where}: invalid {axis} {entry[axis]!r}"
				else:
					for field in ("rate", "sampleCount"):
						if field in entry and (
							isinstance(entry[field], bool)
							or not isinstance(entry[field], (int, float))
						):
							return f"{where}: invalid {field} {entry[field]!r}"
	return None


def _serialized_input_refs(value, out):
	out.update(iter_input_block_refs(value))


def _validate_block_structure(project, label, original=None):
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		if not isinstance(blocks, dict):
			return f"{label}, target {ti} ({target.get('name')!r}): blocks is not an object"
		original_targets = original.get("targets", []) if original is not None else []
		original_target = original_targets[ti] if ti < len(original_targets) else {}
		original_blocks = original_target.get("blocks", {})

		input_parents = {}
		for bid, block in blocks.items():
			where = f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}"
			if not isinstance(bid, str) or not bid:
				return f"{where}: invalid block ID"
			if isinstance(block, list):
				if len(block) < 3 or block[0] not in CONTAINER_TAGS:
					return f"{where}: malformed primitive block {block!r}"
				continue
			if not isinstance(block, dict):
				return f"{where}: block is neither object nor variable/list primitive"
			if not isinstance(block.get("opcode"), str) or not block.get("opcode"):
				return f"{where}: missing/invalid opcode"
			for key in ("next", "parent"):
				ref = block.get(key)
				if ref is not None and not isinstance(ref, str):
					return f"{where}: {key} must be null or a string"
				if isinstance(ref, str) and ref not in blocks:
					old_block = original_blocks.get(bid)
					if (
						ref not in original_blocks
						and isinstance(old_block, dict)
						and old_block.get(key) == ref
					):
						continue
					if key == "parent" and is_orphan_arg_reporter(block, blocks):
						continue
					return f"{where}: {key} points to missing block {ref!r}"
			if not isinstance(block.get("inputs"), dict) or not isinstance(
				block.get("fields"), dict
			):
				return f"{where}: inputs/fields must be objects"
			if (
				isinstance(block.get("topLevel"), bool)
				and block.get("topLevel")
				and block.get("parent") is not None
			):
				return (
					f"{where}: topLevel block has non-null parent {block['parent']!r}"
				)
			if (
				isinstance(block.get("shadow"), bool)
				and block.get("shadow")
				and block.get("topLevel")
			):
				return f"{where}: shadow block cannot be topLevel"
			mut = block.get("mutation")
			if mut is not None:
				if not isinstance(mut, dict):
					return f"{where}: mutation is not an object"
				if "tagName" in mut and mut["tagName"] != "mutation":
					return f"{where}: mutation.tagName is {mut['tagName']!r}, not 'mutation'"
				if "children" in mut and not isinstance(mut["children"], list):
					return f"{where}: mutation.children is not an array"

			refs = set()
			for name, value in block["inputs"].items():
				_serialized_input_refs(value, refs)
			for child in refs:
				if child in blocks:
					input_parents.setdefault(child, set()).add(bid)

		# next must be reciprocated by parent
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			nxt = block.get("next")
			if isinstance(nxt, str):
				child = blocks.get(nxt)
				if isinstance(child, dict) and child.get("parent") != bid:
					old_block, old_child = original_blocks.get(
						bid
					), original_blocks.get(nxt)
					if not (
						isinstance(old_block, dict)
						and old_block.get("next") == nxt
						and isinstance(old_child, dict)
						and old_child.get("parent") == child.get("parent")
					):
						return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: next -> {nxt!r} but child.parent is {child.get('parent')!r}"
			parent = block.get("parent")
			if isinstance(parent, str) and parent in blocks:
				pb = blocks.get(parent)
				if not isinstance(pb, dict):
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: parent is not an object"
				if pb.get("next") != bid and parent not in input_parents.get(
					bid, set()
				):
					old_block, old_parent = original_blocks.get(
						bid
					), original_blocks.get(parent)
					inherited = (
						isinstance(old_block, dict)
						and old_block.get("parent") == parent
						and isinstance(old_parent, dict)
						and old_parent.get("next") != bid
						and isinstance(old_parent.get("inputs"), dict)
						and not any(
							bid in iter_input_block_refs(value)
							for value in old_parent["inputs"].values()
						)
					)
					if not inherited:
						parent_refs = set()
						for input_value in (pb.get("inputs") or {}).values():
							parent_refs.update(iter_input_block_refs(input_value))
						old_child = original_blocks.get(bid)
						old_parent = original_blocks.get(parent)
						old_parent_refs = set()
						if isinstance(old_parent, dict):
							for input_value in (
								old_parent.get("inputs") or {}
							).values():
								old_parent_refs.update(
									iter_input_block_refs(input_value)
								)
						return (
							f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: "
							f"parent {parent!r} does not reference this block; "
							f"child opcode={block.get('opcode')!r}, child.next={block.get('next')!r}, "
							f"parent opcode={pb.get('opcode')!r}, parent.next={pb.get('next')!r}, "
							f"parent input refs={sorted(parent_refs)!r}; "
							f"original child parent={(old_child.get('parent') if isinstance(old_child, dict) else None)!r}, "
							f"original parent next={(old_parent.get('next') if isinstance(old_parent, dict) else None)!r}, "
							f"original parent input refs={sorted(old_parent_refs)!r}"
						)
			for child in input_parents.get(bid, set()):
				cb = blocks.get(bid)
				if isinstance(cb, dict) and cb.get("parent") != child:
					old_block, old_owner = original_blocks.get(
						bid
					), original_blocks.get(child)
					if not (
						isinstance(old_block, dict)
						and old_block.get("parent") == cb.get("parent")
						and isinstance(old_owner, dict)
						and isinstance(old_owner.get("inputs"), dict)
						and any(
							bid in iter_input_block_refs(value)
							for value in old_owner["inputs"].values()
						)
					):
						return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: input owner {child!r} disagrees with parent {cb.get('parent')!r}"
	return None


def _final_asset_name(name, opts):
	conversions = getattr(opts, "wav_conversions", {}) or {}
	dedup = getattr(opts, "asset_deduplications", {}) or {}
	name = conversions.get(name, name)
	return _asset_alias(name, dedup)


def _expected_asset_entry(entry, kind, opts):
	if not isinstance(entry, dict):
		return entry
	expected = dict(entry)
	old_name = _asset_filename(entry, kind)
	if old_name is None:
		return expected
	conversions = getattr(opts, "wav_conversions", {}) or {}
	if entry.get("dataFormat") == "wav" and old_name in conversions:
		conv_name = conversions[old_name]
		expected["assetId"] = conv_name.rsplit(".", 1)[0]
		expected["dataFormat"] = "mp3"
		if "md5ext" in expected:
			expected["md5ext"] = conv_name
	new_name = _final_asset_name(
		conv_name if old_name in conversions else old_name, opts
	)
	if new_name != old_name:
		stem, new_ext = new_name.rsplit(".", 1)
		expected["assetId"] = stem
		expected["dataFormat"] = new_ext.lower()
		if "md5ext" in expected:
			expected["md5ext"] = new_name
	return expected


def _check_costumes(original_target, minified_target, opts, zf):
	original = original_target.get("costumes", [])
	minified = minified_target.get("costumes", [])
	if len(original) != len(minified):
		return f"Target {original_target.get('name')!r}: costume count changed from {len(original)} to {len(minified)}"

	allowed_keys = {
		"assetId",
		"name",
		"md5ext",
		"dataFormat",
		"bitmapResolution",
		"rotationCenterX",
		"rotationCenterY",
	}
	for index, (co, cm) in enumerate(zip(original, minified)):
		where = f"Target {original_target.get('name')!r}, costume {index}"
		if not isinstance(co, dict) or not isinstance(cm, dict):
			if co != cm:
				return f"{where}: costume entry changed"
			continue

		expected = _expected_asset_entry(co, "costume", opts)
		extra = set(cm) - set(expected)
		if extra:
			return f"{where}: unexpected attribute(s) appeared: {sorted(extra)}"

		allowed_removed = set()
		if (
			(opts.remove_costume_metadata or opts.compact_costume_references)
			and _canonical_costume_filename(co) is not None
			and co.get("md5ext") == _canonical_costume_filename(co)
		):
			allowed_removed.add("md5ext")
		if (
			opts.remove_costume_metadata
			and co.get("dataFormat") == "svg"
			and co.get("bitmapResolution") == 1
		):
			allowed_removed.add("bitmapResolution")
		missing = (set(expected) - set(cm)) - allowed_removed
		if missing:
			return f"{where}: attribute(s) were removed unexpectedly: {sorted(missing)}"

		for key in set(expected) & set(cm):
			ov, mv = expected[key], cm[key]
			if key in ("rotationCenterX", "rotationCenterY") and opts.positions:
				if ov != mv and not _position_num_eq(ov, mv):
					return f"{where}: {key!r} changed from {ov!r} to {mv!r}"
			elif ov != mv:
				return f"{where}: attribute {key!r} changed from {ov!r} to {mv!r}"

	return None


def _reachable_block_ids(target):
	blocks = target.get("blocks", {})
	edges = build_block_graph(target)
	roots = {
		bid
		for bid, block in blocks.items()
		if isinstance(block, list)
		or (isinstance(block, dict) and block.get("topLevel"))
	}
	reachable = set()
	stack = list(roots)
	while stack:
		bid = stack.pop()
		if bid in reachable or bid not in blocks:
			continue
		reachable.add(bid)
		stack.extend(edges.get(bid, ()))
	return reachable


def _dead_procedure_block_ids(target):
	blocks = target.get("blocks", {})
	if not blocks:
		return set()
	children = {}
	for bid, block in blocks.items():
		if not isinstance(block, dict):
			continue
		parent = block.get("parent")
		if isinstance(parent, str) and parent in blocks:
			children.setdefault(parent, set()).add(bid)
	definitions = {}
	for bid, block in blocks.items():
		if (
			not isinstance(block, dict)
			or block.get("opcode") != "procedures_definition"
		):
			continue
		custom = block.get("inputs", {}).get("custom_block", [None, None])
		proto_id = custom[1] if len(custom) > 1 and isinstance(custom[1], str) else None
		proto = blocks.get(proto_id)
		proc = procedure_key(proto) if isinstance(proto, dict) else None
		if proc is None:
			continue
		todo, seen = [bid], set()
		while todo:
			x = todo.pop()
			if x in seen or x not in blocks:
				continue
			seen.add(x)
			todo.extend(children.get(x, ()))
		definitions[(proc, bid)] = seen
	if not definitions:
		return set()
	owned = set().union(*definitions.values())
	live = set()
	queue = []

	def seed_calls(ids):
		for bid in ids:
			b = blocks.get(bid)
			if isinstance(b, dict) and b.get("opcode") == "procedures_call":
				proc = procedure_key(b)
				if proc is not None and proc not in live:
					queue.append(proc)

	seed_calls(set(blocks) - owned)
	while queue:
		proc = queue.pop()
		if proc in live:
			continue
		live.add(proc)
		for (defined_proc, _definition_id), closure in definitions.items():
			if defined_proc == proc:
				seed_calls(closure)
	dead = {proc for proc, _ in definitions} - live
	result = set()
	for (proc, _definition_id), closure in definitions.items():
		if proc in dead:
			result.update(closure)
	return result


def _expected_removed_blocks(project, opts):
	allowed = []
	for ti, target in enumerate(project.get("targets", [])):
		ids = set()
		first_removed = getattr(opts, "unreachable_removed_blocks_first", None) or []
		second_removed = getattr(opts, "unreachable_removed_blocks_second", None) or []
		procedure_removed = getattr(opts, "unused_procedure_removed_blocks", None) or []
		if opts.remove_unreachable and ti < len(first_removed):
			ids.update(first_removed[ti])
		elif opts.remove_unreachable:
			ids.update(set(target.get("blocks", {})) - _reachable_block_ids(target))
		if opts.remove_unused_procedures and ti < len(procedure_removed):
			ids.update(procedure_removed[ti])
		elif opts.remove_unused_procedures:
			ids.update(_dead_procedure_block_ids(target))
		if (opts.remove_unreachable or opts.remove_unused_procedures) and ti < len(
			second_removed
		):
			ids.update(second_removed[ti])
		folded_blocks = getattr(opts, "folded_constant_expression_blocks", ())
		if ti < len(folded_blocks):
			ids.update(folded_blocks[ti])
		setter_blocks = getattr(opts, "removed_variable_setter_blocks", ())
		if ti < len(setter_blocks):
			ids.update(setter_blocks[ti])
		simplified_removed = getattr(opts, "simplified_block_removed_blocks", ())
		if ti < len(simplified_removed):
			ids.update(simplified_removed[ti])
		grouped_removed = getattr(opts, "grouped_sequence_removed_blocks", ())
		if ti < len(grouped_removed):
			ids.update(grouped_removed[ti])
		control_removed = getattr(opts, "constant_control_removed_blocks", ())
		if ti < len(control_removed):
			ids.update(control_removed[ti])
		procedure_arg_removed = getattr(opts, "procedure_argument_removed_blocks", ())
		if ti < len(procedure_arg_removed):
			ids.update(procedure_arg_removed[ti])
		prototype_removed = getattr(
			opts, "procedure_prototype_compaction_removed_blocks", ()
		)
		if ti < len(prototype_removed):
			ids.update(prototype_removed[ti])
		merged_removed = getattr(opts, "merged_procedure_removed_blocks", ())
		if ti < len(merged_removed):
			ids.update(merged_removed[ti])
		inlined_removed = getattr(opts, "inlined_procedure_removed_blocks", ())
		if ti < len(inlined_removed):
			ids.update(inlined_removed[ti])
		script_removed = getattr(opts, "script_rewrite_removed_blocks", ())
		if ti < len(script_removed):
			ids.update(script_removed[ti])
		allowed.append(ids)
	return allowed


def _target_default_properties(target):
	if target.get("isStage"):
		return {
			"currentCostume": 0,
			"volume": 100,
			"tempo": 60,
			"videoTransparency": 50,
			"videoState": "on",
			"textToSpeechLanguage": None,
		}
	return {
		"currentCostume": 0,
		"volume": 100,
		"visible": True,
		"x": 0,
		"y": 0,
		"size": 100,
		"direction": 90,
		"draggable": False,
		"rotationStyle": "all around",
	}


def _reference_primitive_head_equal(original, minified, opts):
	if not (isinstance(original, list) and isinstance(minified, list)):
		return False
	if original[:3] == minified[:3]:
		return True
	return bool(
		getattr(opts, "strip_reference_names", False)
		and len(original) >= 3
		and len(minified) >= 3
		and original[0] in CONTAINER_TAGS
		and minified[0] == original[0]
		and minified[1] == ""
		and original[2] == minified[2]
	)


def _filter_verify_map(mapping, target_index):
	if not isinstance(mapping, dict):
		return {}
	return {
		key: value
		for key, value in mapping.items()
		if isinstance(key, tuple) and key and key[0] == target_index
	}


def _make_verify_worker_state(to, tm, opts, target_index, changed):
	attrs = {}
	for name in _VERIFY_BLOCK_OPT_ATTRS:
		attrs[name] = getattr(opts, name, False)
	for name in _VERIFY_BLOCK_MAP_ATTRS:
		value = getattr(opts, name, None)
		if name in ("folded_constant_variables", "folded_constant_variable_literals"):
			attrs[name] = dict(value) if isinstance(value, dict) else {}
		else:
			attrs[name] = _filter_verify_map(value, target_index)

	verify_project = getattr(opts, "_verify_project", None) or {}
	owner_targets = [
		{
			"variables": target.get("variables", {}),
			"isStage": bool(target.get("isStage", False)),
		}
		for target in verify_project.get("targets", [])
		if isinstance(target, dict)
	]
	attrs["_verify_project"] = {"targets": owner_targets}
	terminal_links = getattr(opts, "_terminal_link_ids", {}) or {}
	if isinstance(terminal_links, dict):
		attrs["_terminal_link_ids"] = {
			target_index: set(terminal_links.get(target_index, ()))
		}
	else:
		attrs["_terminal_link_ids"] = {}

	return {
		"checks": changed,
		"remaining_blocks": set(tm.get("blocks", {})),
		"target_index": target_index,
		"target_name": to.get("name", f"target_{target_index}"),
		"opts": SimpleNamespace(**attrs),
	}


def _init_verify_worker(state_path):
	global _VERIFY_WORKER_STATE, _VERIFY_WORKER_STATE_PATH
	if _VERIFY_WORKER_STATE_PATH != state_path:
		with open(state_path, "rb") as handle:
			_VERIFY_WORKER_STATE = pickle.load(handle)
		_VERIFY_WORKER_STATE_PATH = state_path


def _verify_one_changed_block(item, state):
	position, bid, bo, bm = item
	opts = state["opts"]
	target_index = state["target_index"]
	target_name = state["target_name"]
	remaining_blocks = state["remaining_blocks"]
	where = f"Target {target_index} ({target_name!r})"

	if isinstance(bo, list) or isinstance(bm, list):
		ok = (
			isinstance(bo, list)
			and isinstance(bm, list)
			and len(bo) == len(bm)
			and _reference_primitive_head_equal(bo, bm, opts)
			and all(
				_num_eq(x, y, opts.normalize_epsilon) or x == y
				for x, y in zip(bo[3:], bm[3:])
			)
		)
		if not (ok and (opts.positions or bo == bm)):
			return (
				position,
				f"{where}, primitive block {bid!r} changed from {bo!r} to {bm!r}",
			)
		return None

	err = _check_blocks(
		bo,
		bm,
		opts,
		f"{where}, block {bid!r}",
		None,
		remaining_blocks,
		target_index,
		bid,
	)
	if err:
		return position, err
	if not isinstance(bm.get("inputs"), dict) or not isinstance(bm.get("fields"), dict):
		return (
			position,
			f"{where}, block {bid!r} ({bm.get('opcode')}): inputs or fields is not a dict (violates Scratch VM format)",
		)
	if "next" not in bm:
		terminal_links = getattr(opts, "_terminal_link_ids", {}) or {}
		if not (
			getattr(opts, "compact_terminal_links", False)
			and bid in terminal_links.get(target_index, ())
			and bo.get("next") is None
		):
			return (
				position,
				f"{where}, block {bid!r} ({bm.get('opcode')}): missing required 'next' or 'parent' attribute",
			)
	if "parent" not in bm:
		return (
			position,
			f"{where}, block {bid!r} ({bm.get('opcode')}): missing required 'next' or 'parent' attribute",
		)
	mu = bm.get("mutation")
	if mu is not None:
		if not isinstance(mu, dict):
			return (
				position,
				f"{where}, block {bid!r} ({bm.get('opcode')}): mutation is not an object",
			)
		if not getattr(opts, "compact_mutation_metadata", False) and (
			"tagName" not in mu or "children" not in mu
		):
			return (
				position,
				f"{where}, block {bid!r} ({bm.get('opcode')}): lost required mutation tagName or children",
			)
	return None


def _verify_block_chunk(bounds):
	state = _VERIFY_WORKER_STATE
	start, stop = bounds
	checks = state["checks"]
	for index in range(start, stop):
		result = _verify_one_changed_block(checks[index], state)
		if result is not None:
			return result
	return None


def _verify_target_blocks_parallel(to, tm, opts, target_index):
	original_blocks = to.get("blocks", {})
	minified_blocks = tm.get("blocks", {})
	changed = []

	for position, bid in enumerate(original_blocks):
		if bid not in minified_blocks:
			continue
		bo = original_blocks[bid]
		bm = minified_blocks[bid]
		if bo != bm:
			changed.append((position, bid, bo, bm))

	if not changed:
		return None

	if len(changed) < 1200:
		state = _make_verify_worker_state(to, tm, opts, target_index, changed)
		for item in changed:
			result = _verify_one_changed_block(item, state)
			if result is not None:
				return result[1]
		return None

	workers = min(VERIFY_PARALLEL_MAX_WORKERS, max(2, (os.cpu_count() or 2) - 1))
	chunk_count = min(len(changed), workers * 2)
	chunk_size = (len(changed) + chunk_count - 1) // chunk_count
	state = _make_verify_worker_state(to, tm, opts, target_index, changed)

	with tempfile.TemporaryDirectory(prefix=".minify-verify-") as temp_dir:
		state_path = os.path.join(temp_dir, "state.pkl")
		with open(state_path, "wb") as handle:
			pickle.dump(state, handle, protocol=pickle.HIGHEST_PROTOCOL)
		bounds = [
			(start, min(start + chunk_size, len(changed)))
			for start in range(0, len(changed), chunk_size)
		]
		results = []
		with ProcessPoolExecutor(
			max_workers=workers, initializer=_init_verify_worker, initargs=(state_path,)
		) as pool:
			for result in pool.map(_verify_block_chunk, bounds):
				if result is not None:
					results.append(result)
		if results:
			return min(results, key=lambda item: item[0])[1]
	return None


def _restore_representation_for_verify(original, result, opts):
	restored_block_ids = False

	if getattr(opts, "rebuild_procedure_displays", False):
		original = _rebuild_procedure_displays(original)
		result = _rebuild_procedure_displays(result)

	if getattr(opts, "prune_orphan_arguments", False):
		original = _prune_orphan_arguments(original, result)

	if getattr(opts, "relabel_block_ids", False):
		_rename_map = getattr(opts, "renamed_block_ids", {}) or {}
		if _rename_map:
			_restore_block_ids(result, _rename_map)
			restored_block_ids = True
		else:
			try:
				result = restore_block_labels(original, result, copy_result=False)
				restored_block_ids = True
			except (ValueError, KeyError, TypeError):
				if getattr(opts, "rename_block_ids", False):
					_restore_block_ids(result, _rename_map)
					restored_block_ids = bool(_rename_map)

	if getattr(opts, "rename_block_ids", False) and not restored_block_ids:
		_rename_map = getattr(opts, "renamed_block_ids", {}) or {}
		_restore_block_ids(result, _rename_map)
		restored_block_ids = bool(_rename_map)

	if getattr(opts, "compact_procedure_symbols", False):
		try:
			result = _restore_procedure_symbols(
				original, result, copy_result=False, opts=opts
			)
		except (ValueError, KeyError, TypeError, IndexError):
			...

	if getattr(opts, "compact_reference_names", False):
		result = restore_ref_names(original, result, copy_result=False)

	if getattr(opts, "prune_unused_data", False):
		try:
			result = _restore_unused_data(original, result)
		except (ValueError, KeyError, TypeError):
			...

	if getattr(opts, "strip_editor_comments", False) or getattr(
		opts, "strip_script_positions", False
	):
		try:
			result = _restore_editor_metadata(
				original,
				result,
				getattr(opts, "strip_editor_comments", False)
				and not getattr(opts, "comments", False),
				getattr(opts, "strip_script_positions", False),
			)
		except (ValueError, KeyError, TypeError):
			...

	terminal = (
		_terminal_link_ids(original)
		if getattr(opts, "compact_terminal_links", False)
		else {}
	)
	opts._terminal_link_ids = terminal

	for ti, (left_target, right_target) in enumerate(
		zip(original.get("targets", []), result.get("targets", []))
	):
		left_blocks = left_target.get("blocks", {})
		right_blocks = right_target.get("blocks", {})

		for bid, left in left_blocks.items():
			right = right_blocks.get(bid)
			if not isinstance(left, dict) or not isinstance(right, dict):
				continue

			if getattr(opts, "compact_defaults", False):
				for key in ("inputs", "fields"):
					if left.get(key) == {} and key not in right:
						right[key] = {}

			if getattr(opts, "compact_block_flags", False):
				for key in ("topLevel", "shadow"):
					if left.get(key) is False and key not in right:
						right[key] = False

			if (
				bid in terminal.get(ti, ())
				and left.get("next", False) is None
				and "next" not in right
			):
				right["next"] = None

			if (
				getattr(opts, "compact_reporter_defaults", False)
				and left.get("opcode") in _SYNC_REPORTERS
			):
				if left.get("next", False) is None and "next" not in right:
					right["next"] = None
				left_fields = left.get("fields", {})
				right_fields = right.get("fields")

				if isinstance(right_fields, dict):
					for name, value in left_fields.items():
						if (
							name not in ("VARIABLE", "LIST", "BROADCAST_OPTION")
							and isinstance(value, list)
							and len(value) == 2
							and value[1] is None
							and right_fields.get(name) == value[:1]
						):
							right_fields[name] = value

	if getattr(opts, "compact_costume_references", False):
		for left_target, right_target in zip(
			original.get("targets", []), result.get("targets", [])
		):
			left_costumes = left_target.get("costumes")
			right_costumes = right_target.get("costumes")
			if not isinstance(left_costumes, list) or not isinstance(
				right_costumes, list
			):
				continue

			for left, right in zip(left_costumes, right_costumes):
				if not isinstance(left, dict) or not isinstance(right, dict):
					continue
				canonical = _canonical_costume_filename(left)

				if (
					canonical is not None
					and left.get("md5ext") == canonical
					and "md5ext" not in right
				):
					right["md5ext"] = canonical

	return original, result, restored_block_ids


def verify(original_path, minified_path, opts):
	with zipfile.ZipFile(original_path) as a, zipfile.ZipFile(minified_path) as b:
		for zf, zpath in ((a, original_path), (b, minified_path)):
			names, err = _zip_entry_names(zf)

			if err:
				return False, f"archive '{zpath}': {err}"

			if "project.json" not in names:
				return False, f"archive '{zpath}' has no project.json"

		orig_assets = {name for name in a.namelist() if name != "project.json"}
		mini_assets = {name for name in b.namelist() if name != "project.json"}
		original_asset_cache = {}
		minified_asset_cache = {}
		conversions = getattr(opts, "wav_conversions", {}) or {}
		expected_assets = {
			_final_asset_name(conversions.get(name, name), opts) for name in orig_assets
		}

		if mini_assets != expected_assets:
			diff_missing = sorted(expected_assets - mini_assets)
			diff_extra = sorted(mini_assets - expected_assets)
			return (
				False,
				f"zip archive entries differ. Missing: {diff_missing[:10]}, unexpected extra: {diff_extra[:10]}",
			)

		for name in orig_assets:
			if name in conversions:
				new_name = _final_asset_name(conversions[name], opts)
				new_bytes = minified_asset_cache.get(new_name)

				if new_bytes is None:
					new_bytes = b.read(new_name)
					minified_asset_cache[new_name] = new_bytes

				if hashlib.md5(new_bytes).hexdigest() != new_name.rsplit(".", 1)[0]:
					return (
						False,
						f"converted asset {new_name!r} does not match its MD5 asset ID",
					)
			else:
				final_name = _final_asset_name(name, opts)
				original_bytes = original_asset_cache.get(name)

				if original_bytes is None:
					original_bytes = a.read(name)
					original_asset_cache[name] = original_bytes

				minified_bytes = minified_asset_cache.get(final_name)

				if minified_bytes is None:
					minified_bytes = b.read(final_name)
					minified_asset_cache[final_name] = minified_bytes

				if final_name != name:
					if original_bytes != minified_bytes:
						return (
							False,
							f"deduplicated asset byte mismatch: {name!r} -> {final_name!r}",
						)
				elif original_bytes != minified_bytes:
					return (
						False,
						f"asset byte-for-byte mismatch: {name!r} ({len(original_bytes)} bytes vs {len(minified_bytes)} bytes)",
					)

		try:
			orig_raw = json.loads(a.read("project.json"))
			mini_raw_project = json.loads(b.read("project.json"))
		except (UnicodeDecodeError, json.JSONDecodeError) as exc:
			return False, f"project.json is not valid UTF-8 JSON: {exc}"

		if not isinstance(orig_raw, dict) or not isinstance(mini_raw_project, dict):
			return False, "project.json root must be an object"

		if not isinstance(orig_raw.get("targets"), list) or not isinstance(
			mini_raw_project.get("targets"), list
		):
			return False, "project.json targets must be arrays"

		orig_raw, mini_raw_project, representation_block_ids_restored = (
			_restore_representation_for_verify(orig_raw, mini_raw_project, opts)
		)
		orig = _reinflate(orig_raw)
		mini = _reinflate(mini_raw_project)
		opts._verify_project = orig
		err = _validate_asset_entries(mini, b, "minified project", minified_asset_cache)

		if err:
			return False, err

		err = _validate_asset_entries(orig, a, "original project")

		if err:
			return False, err

		if opts.rename_block_ids and not representation_block_ids_restored:
			_restore_block_ids(mini, opts.renamed_block_ids)

		err = _validate_block_structure(mini, "minified project", orig)

		if err:
			return False, err

		conflicts = getattr(opts, "broadcast_repair_conflicts", {}) or {}
		if conflicts:
			first_id = sorted(conflicts)[0]
			return (
				False,
				f"broadcast {first_id!r} has conflicting reference names {conflicts[first_id]!r}; refusing to guess a definition",
			)
		err = _check_broadcast_ids_resolve(mini_raw_project)
		if err:
			return False, err
		err = _check_block_references_resolve(mini_raw_project, orig)
		if err:
			return False, err

		if opts.rename_variable_ids or opts.rename_list_ids:
			_restore_data_ids(mini, opts.renamed_variable_ids, opts.renamed_list_ids)
		if opts.rename_broadcast_ids:
			_restore_broadcast_ids(mini, opts.renamed_broadcast_ids)
		if opts.rename_argument_ids:
			_restore_argument_ids(mini, opts.renamed_argument_ids)
		if opts.strip_covered_shadows:
			mini = _restore_covered_shadows(orig, mini)
		if (
			opts.rename_identifiers
			or opts.rename_variable_names
			or opts.rename_list_names
			or opts.rename_broadcast_names
			or opts.rename_argument_names
			or opts.rename_procedure_names
		):
			restore_identifier_names(mini, opts.renamed_identifiers)

		for key in set(orig) | set(mini):
			if key == "extensions" and getattr(opts, "remove_unused_extensions", False):
				original_ext = orig.get("extensions", [])
				minified_ext = mini.get("extensions", [])
				removed_ext = getattr(opts, "removed_extensions", set()) or set()
				expected_ext = [x for x in original_ext if x not in removed_ext]
				if minified_ext != expected_ext:
					return (
						False,
						f"Top-level extensions changed unexpectedly: original {original_ext!r}, minified {minified_ext!r}, removed {sorted(removed_ext)!r}",
					)
				continue
			if key not in ("targets", "monitors") and orig.get(key) != mini.get(key):
				if key == "meta" and opts.remove_project_meta:
					mo = orig.get("meta", {})
					mm = mini.get("meta", {})
					if mo.get("semver") == mm.get("semver") and mo.get("vm") == mm.get(
						"vm"
					):
						continue
				return (
					False,
					f"Top-level project key {key!r} changed: original has {orig.get(key)!r}, minified has {mini.get(key)!r}",
				)
		if len(orig["targets"]) != len(mini["targets"]):
			return (
				False,
				f"Target count changed: original has {len(orig['targets'])}, minified has {len(mini['targets'])}",
			)

		allowed_removed_by_target = _expected_removed_blocks(orig, opts)

		orig_var_owners = {}
		orig_list_owners = {}
		for ti, target in enumerate(orig.get("targets", [])):
			for vid in target.get("variables", {}):
				orig_var_owners[vid] = ti
			for lid in target.get("lists", {}):
				orig_list_owners[lid] = ti

		approved = set(getattr(opts, "cleared_lists", ()) or ())
		mini_var_refs, mini_list_refs, mini_bcast_refs = get_data_refs(
			mini, var_owners=orig_var_owners, list_owners=orig_list_owners
		)

		for ti, (to, tm) in enumerate(zip(orig["targets"], mini["targets"])):
			name = to.get("name", f"target_{ti}")
			missing_target_keys = set(to) - set(tm)
			extra_target_keys = set(tm) - set(to)
			allowed_container_keys = set()
			if opts.remove_empty_target_containers:
				allowed_container_keys.update(
					k
					for k in missing_target_keys
					if k in ("lists", "broadcasts", "comments") and to.get(k) == {}
				)
			if (
				opts.remove_empty_target_containers
				and (opts.remove_unreachable or opts.remove_unused_procedures)
				and "comments" in missing_target_keys
			):
				# comments attached to removed blocks are dropped, leaving {} to prune
				allowed_container_keys.add("comments")
			if opts.remove_unused_lists and "lists" in missing_target_keys:
				allowed_container_keys.add("lists")
			if opts.remove_unused_broadcasts and "broadcasts" in missing_target_keys:
				allowed_container_keys.add("broadcasts")
			if (
				opts.comments
				and not to.get("isStage")
				and "comments" in missing_target_keys
			):
				allowed_container_keys.add("comments")
			allowed_default_keys = set()
			if opts.remove_default_target_properties:
				defaults = _target_default_properties(to)
				allowed_default_keys = {
					k
					for k in missing_target_keys
					if k in defaults and to.get(k) == defaults[k]
				}
			if (
				missing_target_keys - allowed_default_keys - allowed_container_keys
				or extra_target_keys
			):
				return (
					False,
					f"Target {ti} ({name!r}): target keys changed. Missing: {sorted(missing_target_keys - allowed_default_keys - allowed_container_keys)}, unexpected extra: {sorted(extra_target_keys)}",
				)
			for k in to:
				if k in (
					"blocks",
					"comments",
					"lists",
					"variables",
					"sounds",
					"costumes",
					"broadcasts",
				):
					continue
				if k not in tm and k in allowed_default_keys:
					continue
				if to[k] != tm[k]:
					if opts.normalize_numbers and _num_eq(
						to[k], tm[k], opts.normalize_epsilon
					):
						continue
					return (
						False,
						f"Target {ti} ({name!r}): property {k!r} changed from {to[k]!r} to {tm[k]!r}",
					)

			to_bcasts = to.get("broadcasts", {})
			tm_bcasts = tm.get("broadcasts", {})
			unexpected_bcasts = set(tm_bcasts) - set(to_bcasts)
			allowed_repaired = set(getattr(opts, "repaired_broadcast_ids", ()) or ())
			if unexpected_bcasts - allowed_repaired:
				return (
					False,
					f"Target {ti} ({name!r}): unexpected new broadcast ID(s) appeared in minified project: {sorted(unexpected_bcasts - allowed_repaired)}",
				)
			missing_bcasts = set(to_bcasts) - set(tm_bcasts)
			if missing_bcasts and not (
				opts.remove_unused_broadcasts
				or (
					opts.remove_empty_containers
					and not to.get("isStage")
					and not to_bcasts
				)
			):
				return (
					False,
					f"Target {ti} ({name!r}): {len(missing_bcasts)} broadcast(s) removed without --remove-unused-broadcasts: {sorted(missing_bcasts)}",
				)
			if missing_bcasts and opts.remove_unused_broadcasts:
				still_used_bcasts = {
					bid: mini_bcast_refs.get(bid, [])
					for bid in missing_bcasts
					if bid in mini_bcast_refs
				}
				if still_used_bcasts:
					first_bid = next(iter(still_used_bcasts))
					return False, (
						f"Target {ti} ({name!r}): broadcast {to_bcasts.get(first_bid)!r} (id: {first_bid!r}) was removed, "
						f"but is still referenced in the minified project by: {', '.join(still_used_bcasts[first_bid])}"
					)
			for bid, bo in to_bcasts.items():
				if bid in tm_bcasts and bo != tm_bcasts[bid]:
					return (
						False,
						f"Target {ti} ({name!r}): broadcast name for id {bid!r} changed from {bo!r} to {tm_bcasts[bid]!r}",
					)

			to_vars = to.get("variables", {})
			tm_vars = tm.get("variables", {})
			if set(tm_vars) - set(to_vars):
				return (
					False,
					f"Target {ti} ({name!r}): unexpected new variable ID(s) appeared in minified project: {sorted(set(tm_vars) - set(to_vars))}",
				)
			missing_vars = set(to_vars) - set(tm_vars)
			if missing_vars and not (
				opts.remove_unused_variables or opts.prune_unused_data
			):
				return (
					False,
					f"Target {ti} ({name!r}): {len(missing_vars)} variable(s) removed without --remove-unused-variables: {sorted(missing_vars)}",
				)
			if missing_vars:
				still_used = {
					vid: mini_var_refs.get((ti, vid), [])
					for vid in missing_vars
					if (ti, vid) in mini_var_refs
				}
				if still_used:
					first_vid = next(iter(still_used))
					first_name = (
						to_vars[first_vid][0]
						if isinstance(to_vars[first_vid], list) and to_vars[first_vid]
						else first_vid
					)
					return False, (
						f"Target {ti} ({name!r}): variable {first_name!r} (id: {first_vid!r}) was removed, "
						f"but is still referenced in the minified project by: {', '.join(still_used[first_vid])}"
					)
			for vid, vo in to_vars.items():
				if vid in tm_vars:
					vm = tm_vars[vid]
					if vo == vm:
						continue
					if getattr(
						opts, "compact_data_literals", False
					) and _compact_data_entry_equal(vo, vm):
						continue
					if (
						opts.normalize_numbers
						and isinstance(vo, list)
						and isinstance(vm, list)
						and len(vo) == len(vm)
						and vo[0] == vm[0]
						and (
							_num_eq(vo[1], vm[1], opts.normalize_epsilon)
							or vo[1] == vm[1]
						)
						and (len(vo) < 3 or vo[2:] == vm[2:])
					):
						continue
					return (
						False,
						f"Target {ti} ({name!r}): variable {vo[0] if isinstance(vo, list) and vo else vid!r} (id: {vid!r}) value changed from {vo!r} to {vm!r}",
					)

			to_lists = to.get("lists", {})
			tm_lists = tm.get("lists", {})
			if set(tm_lists) - set(to_lists):
				return (
					False,
					f"Target {ti} ({name!r}): unexpected new list ID(s) appeared in minified project: {sorted(set(tm_lists) - set(to_lists))}",
				)
			missing_lists = set(to_lists) - set(tm_lists)
			if missing_lists and not (
				opts.remove_unused_lists or opts.prune_unused_data
			):
				return (
					False,
					f"Target {ti} ({name!r}): {len(missing_lists)} list(s) removed without --remove-unused-lists: {sorted(missing_lists)}",
				)
			if missing_lists:
				still_used = {
					lid: mini_list_refs.get((ti, lid), [])
					for lid in missing_lists
					if (ti, lid) in mini_list_refs
				}
				if still_used:
					first_lid = next(iter(still_used))
					first_name = (
						to_lists[first_lid][0]
						if isinstance(to_lists[first_lid], list) and to_lists[first_lid]
						else first_lid
					)
					return False, (
						f"Target {ti} ({name!r}): list {first_name!r} (id: {first_lid!r}) was removed, "
						f"but is still referenced in the minified project by: {', '.join(still_used[first_lid])}"
					)
			for lid, lo in to_lists.items():
				if lid not in tm_lists:
					continue
				lm = tm_lists[lid]
				if lo == lm:
					continue
				if getattr(
					opts, "compact_data_literals", False
				) and _compact_data_entry_equal(lo, lm):
					continue
				allow_numeric_reencoding = (
					opts.normalize_numbers
					or getattr(opts, "optimize_json", False)
					or getattr(opts, "minimum_json", False)
				)
				if (
					allow_numeric_reencoding
					and isinstance(lo, list)
					and isinstance(lm, list)
					and len(lo) == len(lm)
					and isinstance(lo[1], list)
					and isinstance(lm[1], list)
					and len(lo[1]) == len(lm[1])
					and all(
						x == y or _num_eq(x, y, opts.normalize_epsilon)
						for x, y in zip(lo[1], lm[1])
					)
					and (
						lo[0] == lm[0]
						or getattr(opts, "rename_list_names", False)
						or getattr(opts, "rename_identifiers", False)
					)
				):
					continue
				if not (
					(ti, lid) in approved
					and isinstance(lm, list)
					and len(lm) == 2
					and lm[0] == lo[0]
					and lm[1] == []
				):
					return (
						False,
						f"Target {ti} ({name!r}): list {lo[0]!r} (id: {lid!r}) changed from {len(lo[1]) if isinstance(lo, list) and len(lo)>1 and isinstance(lo[1], list) else 'N/A'} items to {len(lm[1]) if isinstance(lm, list) and len(lm)>1 and isinstance(lm[1], list) else 'N/A'} items but was not approved for clearing",
					)

			new_block_sets = (
				getattr(opts, "folded_constant_expression_new_blocks", None) or []
			)
			allowed_new_blocks = (
				new_block_sets[ti] if ti < len(new_block_sets) else set()
			)
			group_new_block_sets = (
				getattr(opts, "grouped_sequence_new_blocks", None) or []
			)
			if ti < len(group_new_block_sets):
				allowed_new_blocks = set(allowed_new_blocks) | set(
					group_new_block_sets[ti]
				)
			script_new_block_sets = (
				getattr(opts, "script_rewrite_new_blocks", None) or []
			)
		specialized_new_block_sets = (
			getattr(opts, "specialized_procedure_new_blocks", None) or []
		)
		if ti < len(specialized_new_block_sets):
			allowed_new_blocks = set(allowed_new_blocks) | set(
				specialized_new_block_sets[ti]
			)
			if ti < len(script_new_block_sets):
				allowed_new_blocks = set(allowed_new_blocks) | set(
					script_new_block_sets[ti]
				)
			unexpected_blocks = (
				set(tm["blocks"]) - set(to["blocks"]) - allowed_new_blocks
			)
			if unexpected_blocks:
				extra_blocks = sorted(unexpected_blocks)
				return (
					False,
					f"Target {ti} ({name!r}): {len(extra_blocks)} unexpected new block ID(s) appeared: {extra_blocks[:10]}",
				)
			missing_blocks = set(to["blocks"]) - set(tm["blocks"])
			illegal_missing = missing_blocks - allowed_removed_by_target[ti]
			if illegal_missing:
				return False, (
					f"Target {ti} ({name!r}): block(s) were removed even though the original "
					f"project proves they were reachable/live: {sorted(illegal_missing)[:10]}"
				)
			err = _verify_target_blocks_parallel(to, tm, opts, ti)
			if err:
				return False, err

			co, cm = to.get("comments", {}), tm.get("comments", {})
			if not isinstance(cm, dict):
				return (
					False,
					f"Target {ti} ({name!r}): comments is not a dict (violates Scratch VM format)",
				)
			if opts.comments and not to.get("isStage"):
				if cm:
					return (
						False,
						f"Target {ti} ({name!r}): comments remain ({len(cm)} comments) despite comment stripping enabled",
					)
			else:
				_mini_blocks = tm.get("blocks", {})
				_missing_cids = set(co) - set(cm)
				_orphaned_ok = {
					cid
					for cid in _missing_cids
					if isinstance(co[cid], dict)
					and isinstance(co[cid].get("blockId"), str)
					and co[cid]["blockId"] not in _mini_blocks
					and (
						opts.remove_unreachable
						or opts.remove_unused_procedures
						or opts.fold_constant_expressions
						or opts.factor_math_expressions
						or opts.simplify_blocks
						or opts.group_similar_sequences
						or opts.fold_constant_variables
					)
				}
				co = {k: v for k, v in co.items() if k not in _orphaned_ok}
				if set(co) != set(cm):
					return (
						False,
						f"Target {ti} ({name!r}): comment ID set changed. Missing: {sorted(set(co) - set(cm))}, extra: {sorted(set(cm) - set(co))}",
					)
				for cid in co:
					for f in set(co[cid]) | set(cm[cid]):
						x, y = co[cid].get(f), cm[cid].get(f)
						if f in ("x", "y", "width", "height") and opts.positions:
							if not (x == y or _num_eq(x, y, opts.normalize_epsilon)):
								return (
									False,
									f"Target {ti} ({name!r}): comment {cid!r} attribute {f!r} changed from {x!r} to {y!r}",
								)
						elif x != y:
							return (
								False,
								f"Target {ti} ({name!r}): comment {cid!r} attribute {f!r} changed from {x!r} to {y!r}",
							)
			if opts.comments and not to.get("isStage"):
				dangling_comments = [
					bid
					for bid, x in tm["blocks"].items()
					if isinstance(x, dict) and "comment" in x
				]
				if dangling_comments:
					return (
						False,
						f"Target {ti} ({name!r}): dangling block.comment link in block(s): {dangling_comments[:10]}",
					)

			err = _check_sounds(to, tm, opts)
			if err:
				return False, err

			err = _check_costumes(to, tm, opts, b)
			if err:
				return False, err

			if _check_argument_id_consistency(to, name, opts) is None:
				err = _check_argument_id_consistency(tm, name, opts)
				if err:
					return False, err

		err = _check_monitors(orig, mini, opts)
		if err:
			return False, err
		err = _check_broadcast_ids_resolve(mini)
		if err:
			return False, err
		err = _check_block_references_resolve(mini, orig)
		if err:
			return False, err
		err = _validate_block_structure(mini, "minified project (final)", orig)
		if err:
			return False, err
	return True, "ok"


def _check_sounds(original_target, minified_target, opts):
	original = original_target.get("sounds", [])
	minified = minified_target.get("sounds", [])
	if len(original) != len(minified):
		return f"Target {original_target.get('name')!r}: sound count changed from {len(original)} to {len(minified)}"
	conversions = getattr(opts, "wav_conversions", {}) or {}
	for index, (so, sm) in enumerate(zip(original, minified)):
		if not isinstance(so, dict) or not isinstance(sm, dict):
			if so != sm:
				return f"Target {original_target.get('name')!r}, sound {index}: sound entry changed"
			continue

		expected = _expected_asset_entry(so, "sound", opts)

		allowed_missing = set()
		if not opts.keep_sound_metadata:
			allowed_missing.update(("rate", "sampleCount"))
		for key in set(sm) - set(expected):
			return f"Target {original_target.get('name')!r}, sound {index}: unexpected attribute {key!r} appeared"
		for key, value in expected.items():
			if key not in sm:
				if key in allowed_missing:
					continue
				return f"Target {original_target.get('name')!r}, sound {index}: required attribute {key!r} was removed"
			if sm[key] != value:
				return f"Target {original_target.get('name')!r}, sound {index}: attribute {key!r} changed from {value!r} to {sm[key]!r}"
	return None


def _check_monitors(orig, mini, opts):
	mo, mm = orig.get("monitors", []), mini.get("monitors", [])
	if not opts.monitors:
		return (
			None
			if mo == mm
			else "monitors changed without monitor optimization (use --keep-monitors to preserve)"
		)

	def key(m):
		return (m.get("id"), m.get("spriteName"), m.get("opcode"))

	by_key = {key(m): m for m in mo}
	order_index = {key(m): i for i, m in enumerate(mo)}
	prev = -1
	for m in mm:
		k = key(m)
		if k not in by_key:
			return f"Monitor {k} appeared in minified project but was not present in original"
		position = order_index[k]
		if position < prev:
			return f"Monitor {k} order changed in project monitors list"
		prev = position
		o = by_key[k]
		for f in set(o) | set(m):
			if o.get(f) == m.get(f):
				continue
			is_list = o.get("opcode") == "data_listcontents"
			if f == "params" and is_list and m.get(f) == {}:
				continue
			if f == "value" and m.get(f) == ([] if is_list else 0):
				continue
			return f"Monitor {k} (opcode: {o.get('opcode')}, sprite: {o.get('spriteName')}): field {f!r} changed from {o.get(f)!r} to {m.get(f)!r}"

	survivors = {key(m) for m in mm}
	targets = orig["targets"]
	stage = next((t for t in targets if t.get("isStage")), None)
	by_name = {t.get("name"): t for t in targets if not t.get("isStage")}
	shown = _variable_ids_used_by_blocks(orig)

	for m in mo:
		if key(m) in survivors:
			continue
		if m.get("opcode") not in ("data_variable", "data_listcontents"):
			return f"Removed non-variable/list monitor {key(m)} (opcode: {m.get('opcode')!r})"
		owner = by_name.get(m.get("spriteName")) if m.get("spriteName") else stage
		store = "lists" if m["opcode"] == "data_listcontents" else "variables"
		orphan = owner is None or m.get("id") not in owner.get(store, {})
		unused = (
			m.get("visible") is False
			and m.get("id") not in shown
			and _is_default_layout(m)
		)
		if not (orphan or unused):
			return f"Monitor {key(m)} (opcode: {m.get('opcode')}, sprite: {m.get('spriteName')}, id: {m.get('id')}) was removed but is neither orphaned nor a default-layout unused monitor"
		if m.get("visible") is True:
			return f"A visible monitor {key(m)} (opcode: {m.get('opcode')}, sprite: {m.get('spriteName')}) was removed"
	return None
