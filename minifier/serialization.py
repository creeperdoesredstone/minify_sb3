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


class JsonNumber(str):
	__slots__ = ()

	def __deepcopy__(self, memo):
		return self


def get_unique_object(pairs) -> dict:
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError(f"duplicate JSON object key: {key!r}")
		result[key] = value
	return result


def invalid_constant(value) -> NoReturn:
	raise ValueError(f"non-finite JSON number: {value}")


def loads_exact(data) -> dict:
	if isinstance(data, (bytes, bytearray)):
		data = data.decode("utf-8-sig")
	return json.loads(
		data,
		parse_int=JsonNumber,
		parse_float=JsonNumber,
		parse_constant=invalid_constant,
		object_pairs_hook=get_unique_object,
	)


def copy_json(value, memo=None):
	kind = type(value)

	if value is None or kind in (str, bool, int, float, JsonNumber):
		return value

	if memo is None:
		memo = {}

	if kind not in (dict, list):
		return copy.deepcopy(value, memo)

	identity = id(value)
	if identity in memo:
		return memo[identity]

	result = {} if kind is dict else []
	memo[identity] = result

	if kind is dict:
		for key, child in value.items():
			result[key if type(key) in (str, JsonNumber) else copy_json(key, memo)] = (
				copy_json(child, memo)
			)
	else:
		result.extend(copy_json(child, memo) for child in value)
	return result


def _number_components(token: str) -> tuple[bool, str, int]:
	sign = token.startswith("-")
	mantissa, _, power = (token[1:] if sign else token).lower().partition("e")
	whole, _, fraction = mantissa.partition(".")
	digits = (whole + fraction).lstrip("0")

	if not digits:
		return sign, "0", 0

	coefficient = digits.rstrip("0")

	return (
		sign,
		coefficient,
		int(power or "0") - len(fraction) + len(digits) - len(coefficient),
	)


def get_shortest_num_repr(token) -> str:
	sign, coefficient, exponent = _number_components(token)
	prefix = "-" if sign else ""
	if coefficient == "0":
		return prefix + "0"

	n = len(coefficient)
	best_length, best_point = len(token), None
	lower = min(n, n + 2 + len(prefix) - best_length)
	upper = max(n, best_length - len(prefix))

	for point_position in range(lower, upper + 1):
		mantissa_length = (
			n
			if point_position == n
			else (
				n + 1
				if 0 < point_position < n
				else point_position if point_position > n else 2 - point_position + n
			)
		)
		power = exponent + n - point_position
		length = len(prefix) + mantissa_length + (1 + len(str(power)) if power else 0)
		if length < best_length:
			best_length, best_point = length, point_position

	if best_point is None:
		return token

	point_position = best_point
	if point_position <= 0:
		mantissa = "0." + "0" * -point_position + coefficient
	elif point_position < n:
		mantissa = coefficient[:point_position] + "." + coefficient[point_position:]
	else:
		mantissa = coefficient + "0" * (point_position - n)

	power = exponent + n - point_position
	return prefix + mantissa + (f"e{power}" if power else "")


def quote(value) -> str:
	return json.dumps(value, ensure_ascii=False)


def encode_parts(value: Any, short_numbers: bool) -> list[str] | NoReturn:
	parts = []

	def emit(item: Any):
		match item:
			case JsonNumber():
				parts.append(get_shortest_num_repr(item) if short_numbers else item)
			case str():
				parts.append(quote(item))
			case None:
				parts.append("null")
			case bool():
				parts.append(str(item).lower())
			case dict():
				parts.append("{")
				for index, (key, child) in enumerate(item.items()):
					if index:
						parts.append(",")
					parts.append(quote(key))
					parts.append(":")
					emit(child)
				parts.append("}")
			case list():
				parts.append("[")
				for index, child in enumerate(item):
					if index:
						parts.append(",")
					emit(child)
				parts.append("]")
			case _:
				raise TypeError(f"unsupported exact JSON value: {type(item).__name__}")

	emit(value)
	return parts


def dumps_exact(project, short_numbers: bool = False) -> str:
	return "".join(encode_parts(project, short_numbers)).encode(
		"utf-8", "backslashreplace"
	)


def dumps_exact_layout(project, order, short_numbers: bool = False) -> bytes:
	parts = []
	order_tuple = tuple(order or ())

	def emit(value: Any, is_block: bool = False):
		match value:
			case JsonNumber():
				parts.append(get_shortest_num_repr(value) if short_numbers else value)
			case str():
				parts.append(quote(value))
			case None:
				parts.append("null")
			case bool():
				parts.append(str(item).lower())
			case dict():
				parts.append("{")
				if is_block and order_tuple:
					keys = dict.fromkeys((*order_tuple, *value))
					items = ((key, value[key]) for key in keys if key in value)
				else:
					items = value.items()
				for index, (key, item) in enumerate(items):
					if index:
						parts.append(",")
					parts.append(quote(key))
					parts.append(":")
					emit(item, False)
				parts.append("}")
			case list():
				parts.append("[")
				for index, item in enumerate(value):
					if index:
						parts.append(",")
					emit(item, False)
				parts.append("]")
			case _:
				raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")

	emit(
		{
			key: (
				[
					(
						{**target, "blocks": target["blocks"]}
						if isinstance(target, dict) and "blocks" in target
						else target
					)
					for target in value
				]
				if key == "targets" and isinstance(value, list)
				else value
			)
			for key, value in project.items()
		}
	)
	return "".join(parts).encode("utf-8", "backslashreplace")


def is_record_order_irrelevant(path: list) -> bool:
	return len(path) == 4 and path[0] == "targets" and path[2] == "blocks"


def get_ref_name_slots(project) -> Generator:
	def primitive(node, path):
		if not isinstance(node, list) or len(node) < 3:
			return

		tag = node[0]

		if isinstance(tag, JsonNumber):
			tag = {
				(False, str(PRIMITIVE_VARIABLE), 0): PRIMITIVE_VARIABLE,
				(False, str(PRIMITIVE_LIST), 0): PRIMITIVE_LIST,
			}.get(_number_components(tag))

		if type(tag) in (int, float) and tag in CONTAINER_TAGS:
			yield path + (1,), node, 1, node[2], (
				"variables" if tag == PRIMITIVE_VARIABLE else "lists"
			)

	for ti, target in enumerate(project.get("targets", [])):
		for bid, block in target.get("blocks", {}).items():
			path = ("targets", ti, "blocks", bid)
			if isinstance(block, list):
				yield from primitive(block, path)
			elif isinstance(block, dict):
				for field, (kind, opcodes) in _REFERENCE_FIELDS.items():
					value = block.get("fields", {}).get(field)
					if (
						block.get("opcode") in opcodes
						and isinstance(value, list)
						and len(value) == 2
					):
						yield path + ("fields", field, 0), value, 0, value[1], kind

				for key, desc in block.get("inputs", {}).items():
					if not isinstance(desc, list) or not desc:
						continue

					tag = _input_tag(desc[0])

					for index in (
						(1, 2)
						if tag == INPUT_DIFF_BLOCK_SHADOW
						else (
							(1,)
							if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
							else ()
						)
					):
						if len(desc) > index:
							yield from primitive(
								desc[index], path + ("inputs", key, index)
							)


def get_ref_scopes(project):
	scopes = []
	stages = []

	for ti, target in enumerate(project.get("targets", [])):
		scope = {}
		for kind in ("variables", "lists", "broadcasts"):
			for ident, entry in (target.get(kind) or {}).items():
				valid = (
					kind != "broadcasts"
					and isinstance(entry, list)
					and len(entry) >= 2
					and type(entry[0]) is str
				)
				scope[ident] = (
					(kind, entry[0]) if valid and ident not in scope else None
				)
		scopes.append(scope)

		if target.get("isStage") is True:
			stages.append(ti)

	global_scope = scopes[stages[0]] if len(stages) == 1 else {}
	return [dict(global_scope, **scope) for scope in scopes]


def restore_ref_names(original, result, *, copy_result=True):
	if copy_result:
		result = copy.deepcopy(result)

	scopes = get_ref_scopes(original)

	for path, container, index, ident, kind in get_ref_name_slots(original):
		if (
			type(ident) is not str
			or not ident
			or ident in ("__proto__", "constructor", "prototype")
			or type(container[index]) is not str
		):
			continue

		if scopes[path[1]].get(ident) != (kind, container[index]):
			continue

		try:
			other = result
			for key in path[:-1]:
				other = other[key]
			if type(other[path[-1]]) is str and other[path[-1]] == "":
				other[path[-1]] = container[index]
		except (KeyError, IndexError, TypeError):
			...

	return result


def get_orphan_argument_ids(target):
	blocks = target.get("blocks", {})
	if not isinstance(blocks, dict):
		return set()

	for block in blocks.values():
		if not isinstance(block, dict):
			continue

		if any(
			block.get(key) is not None and type(block[key]) is not str
			for key in ("next", "parent")
		):
			return set()

		inputs = block.get("inputs", {})
		if not isinstance(inputs, dict):
			return set()

		for desc in inputs.values():
			if not isinstance(desc, list) or not desc or _input_tag(desc[0]) is None:
				return set()

			for value in desc[1:3] if _input_tag(desc[0]) == 3 else desc[1:2]:
				if (
					value is not None
					and type(value) is not str
					and not isinstance(value, list)
				):
					return set()

	referenced = {container[key] for container, key in _block_slots(target)}
	allowed = {"opcode", "next", "parent", "inputs", "fields", "shadow", "topLevel"}
	orphans = set()

	for bid, block in blocks.items():
		if not isinstance(block, dict) or bid in referenced or set(block) - allowed:
			continue

		if block.get("opcode") not in (
			"argument_reporter_boolean",
			"argument_reporter_string_number",
		):
			continue

		if block.get("shadow") is not True or block.get("topLevel", False) is not False:
			continue

		if block.get("next") is not None or block.get("inputs", {}) != {}:
			continue

		fields = block.get("fields")
		if not isinstance(fields, dict) or set(fields) != {"VALUE"}:
			continue

		value = fields["VALUE"]
		if (
			not isinstance(value, list)
			or len(value) != 2
			or type(value[0]) is not str
			or value[1] is not None
		):
			continue

		parent = block.get("parent")
		prototype = blocks.get(parent) if type(parent) is str else None
		if (
			isinstance(prototype, dict)
			and prototype.get("opcode") == "procedures_prototype"
		):
			orphans.add(bid)

	return orphans


def _prune_orphan_arguments(project, result=None):
	pruned = dict(project)
	pruned["targets"] = []

	for ti, target in enumerate(project.get("targets", [])):
		target = dict(target)
		blocks = target.get("blocks")

		if isinstance(blocks, dict):
			orphans = get_orphan_argument_ids(target)
			if result is not None:
				other_targets = result.get("targets", [])
				other = (
					other_targets[ti].get("blocks", {})
					if ti < len(other_targets)
					else {}
				)

				if len(other) != len(blocks) - len(orphans):
					orphans = set()

			target["blocks"] = {
				bid: block for bid, block in blocks.items() if bid not in orphans
			}

		pruned["targets"].append(target)

	return pruned


def exact_difference(
	original: dict[str, Any],
	result: dict[str, Any],
	compact_defaults: bool = False,
	compact_costume_references: bool = False,
	compact_block_flags: bool = False,
	relabel_block_ids: bool = False,
	compact_reference_names: bool = False,
	prune_orphan_arguments: bool = False,
	compact_procedure_symbols: bool = False,
	compact_reporter_defaults: bool = False,
	strip_covered_shadows: bool = False,
	strip_editor_comments: bool = False,
	strip_script_positions: bool = False,
	prune_unused_data: bool = False,
	rebuild_procedure_displays: bool = False,
	compact_terminal_links: bool = False,
) -> str | None:
	if rebuild_procedure_displays:
		original = _rebuild_procedure_displays(original)
		result = _rebuild_procedure_displays(result)

	if prune_orphan_arguments:
		original = _prune_orphan_arguments(original, result)

	if (
		relabel_block_ids
		or compact_procedure_symbols
		or compact_reference_names
		or strip_covered_shadows
		or strip_editor_comments
		or strip_script_positions
		or prune_unused_data
	):
		result = copy_json(result)

	if relabel_block_ids:
		try:
			result = restore_block_labels(original, result, copy_result=False)
		except (ValueError, KeyError, TypeError) as error:
			return f"block ID correspondence failed: {error}"

	if compact_procedure_symbols:
		try:
			result = _restore_procedure_symbols(original, result, copy_result=False)
		except (ValueError, KeyError, TypeError, IndexError) as error:
			return f"procedure symbol correspondence failed: {error}"

	if compact_reference_names:
		result = restore_ref_names(original, result, copy_result=False)

	if strip_covered_shadows:
		result = _restore_covered_shadows(original, result)

	if prune_unused_data:
		try:
			result = _restore_unused_data(original, result)
		except (ValueError, KeyError, TypeError) as error:
			return f"unused data verification failed: {error}"
	if strip_editor_comments or strip_script_positions:
		try:
			result = _restore_editor_metadata(
				original, result, strip_editor_comments, strip_script_positions
			)
		except (ValueError, KeyError, TypeError) as error:
			return f"editor metadata verification failed: {error}"

	terminal = _terminal_link_ids(original) if compact_terminal_links else {}
	stack = [((), original, result)]

	def mismatch(reason):
		return f"{'/'.join(map(str, path)) or 'project'}: {reason}"

	while stack:
		path, left, right = stack.pop()
		if type(left) is not type(right):
			return mismatch("JSON value type changed")

		if isinstance(left, JsonNumber):
			if _number_components(left) != _number_components(right):
				return mismatch("exact numeric value changed")

		elif isinstance(left, dict):
			block_record = is_record_order_irrelevant(path)

			if block_record and (
				compact_reporter_defaults
				or compact_defaults
				or compact_block_flags
				or compact_terminal_links
			):
				right = dict(right)

			if (
				block_record
				and path[3] in terminal.get(path[1], ())
				and left.get("next", False) is None
				and "next" not in right
			):
				right["next"] = None

			if (
				compact_reporter_defaults
				and block_record
				and left.get("opcode") in _SYNC_REPORTERS
			):
				if "next" in left and left["next"] is None and "next" not in right:
					right["next"] = None

				if isinstance(right.get("fields"), dict):
					right["fields"] = dict(right["fields"])
					for name, value in left.get("fields", {}).items():
						if (
							name not in ("VARIABLE", "LIST", "BROADCAST_OPTION")
							and isinstance(value, list)
							and len(value) == 2
							and value[1] is None
							and right["fields"].get(name) == value[:1]
						):
							right["fields"][name] = value

			if (
				compact_costume_references
				and len(path) == 4
				and path[0] == "targets"
				and path[2] == "costumes"
			):
				removed = {}
				canonical = _canonical_costume_filename(left)

				if (
					canonical is not None
					and left.get("md5ext") == canonical
					and "md5ext" not in right
				):
					removed["md5ext"] = canonical
				if removed:
					if list(right) != [key for key in left if key not in removed]:
						return mismatch("costume keys or their order changed")
					right = {
						key: removed[key] if key in removed else right[key]
						for key in left
					}

			if compact_defaults and block_record:
				for key in ("inputs", "fields"):
					if left.get(key) == {} and key not in right:
						right[key] = {}

			if compact_block_flags and block_record:
				for key in ("topLevel", "shadow"):
					if left.get(key) is False and key not in right:
						right[key] = False

			if set(left) != set(right):
				return mismatch("object keys changed")

			if not block_record and list(left) != list(right):
				return mismatch("collection order changed")

			stack.extend((path + (key,), left[key], right[key]) for key in left)
		elif isinstance(left, list):
			if len(left) != len(right):
				return mismatch("array length changed")

			stack.extend(
				(path + (index,), a, b) for index, (a, b) in enumerate(zip(left, right))
			)
		elif left != right:
			return mismatch("JSON value changed")
	return None


def _ordered_blocks(target, order):
	result = dict(target)
	blocks = target.get("blocks")

	if isinstance(blocks, dict):
		result["blocks"] = {
			bid: (
				{
					key: block[key]
					for key in dict.fromkeys((*order, *block))
					if key in block
				}
				if isinstance(block, dict)
				else block
			)
			for bid, block in blocks.items()
		}

	return result


def _canonical_costume_filename(costume):
	aid, fmt = costume.get("assetId"), costume.get("dataFormat")

	if (
		isinstance(aid, str)
		and not isinstance(aid, JsonNumber)
		and isinstance(fmt, str)
		and not isinstance(fmt, JsonNumber)
		and aid
		and fmt
	):
		return f"{aid}.{fmt}"

	return None


def compact_representation(
	project: dict[str, Any],
	compact_defaults: bool = False,
	compact_costume_references: bool = False,
	compact_block_flags: bool = False,
	compact_reference_names: bool = False,
	prune_orphan_arguments: bool = False,
	compact_procedure_symbols: bool = False,
	compact_reporter_defaults: bool = False,
	strip_covered_shadows: bool = False,
	strip_editor_comments: bool = False,
	strip_script_positions: bool = False,
	prune_unused_data: bool = False,
	rebuild_procedure_displays: bool = False,
	compact_terminal_links: bool = False,
) -> dict[str, Any]:
	if (
		compact_procedure_symbols
		or compact_reference_names
		or strip_covered_shadows
		or rebuild_procedure_displays
	):
		project = copy_json(project)

	if prune_orphan_arguments:
		project = _prune_orphan_arguments(project)

	if rebuild_procedure_displays:
		project = _rebuild_procedure_displays(project)

	if strip_covered_shadows:
		for ti, bid, name, desc in _covered_shadow_slots(project):
			project["targets"][ti]["blocks"][bid]["inputs"][name] = [
				JsonNumber(str(INPUT_BLOCK_NO_SHADOW)),
				desc[1],
			]

	if compact_procedure_symbols:
		project = _compact_procedure_symbols(project, copy_result=False)

	result = dict(project)
	result["targets"] = []
	unused = _unused_data_ids(project) if prune_unused_data else {}
	terminal = _terminal_link_ids(project) if compact_terminal_links else {}

	for ti, source in enumerate(project.get("targets", [])):
		target = dict(source)

		for kind in ("variables", "lists"):
			if unused.get((ti, kind)):
				target[kind] = {
					ident: entry
					for ident, entry in source[kind].items()
					if ident not in unused[ti, kind]
				}

		removed_comments = (
			_editor_comment_ids(source) if strip_editor_comments else frozenset()
		)

		if removed_comments:
			target["comments"] = {
				cid: comment
				for cid, comment in source["comments"].items()
				if cid not in removed_comments
			}

		if isinstance(source.get("blocks"), dict) and (
			compact_defaults
			or compact_block_flags
			or compact_reporter_defaults
			or removed_comments
			or strip_script_positions
			or compact_terminal_links
		):
			target["blocks"] = {}
			for bid, block in source["blocks"].items():
				if isinstance(block, dict):
					block = {
						key: value
						for key, value in block.items()
						if not (
							compact_defaults
							and key in ("inputs", "fields")
							and value == {}
						)
						and not (
							compact_block_flags
							and key in ("topLevel", "shadow")
							and value is False
						)
					}

					if (
						isinstance(block.get("comment"), str)
						and block["comment"] in removed_comments
					):
						del block["comment"]

					if strip_script_positions:
						for key in _editor_position_keys(block):
							del block[key]

					if bid in terminal.get(ti, ()):
						block.pop("next")

					if (
						compact_reporter_defaults
						and block.get("opcode") in _SYNC_REPORTERS
					):
						if block.get("next", False) is None:
							block.pop("next")

						if isinstance(block.get("fields"), dict):
							block["fields"] = {
								key: (
									value[:1]
									if key
									not in ("VARIABLE", "LIST", "BROADCAST_OPTION")
									and isinstance(value, list)
									and len(value) == 2
									and value[1] is None
									else value
								)
								for key, value in block["fields"].items()
							}

				target["blocks"][bid] = block

		if compact_costume_references and isinstance(source.get("costumes"), list):
			target["costumes"] = []

			for costume in source["costumes"]:
				if isinstance(costume, dict):
					removed = set()
					canonical = _canonical_costume_filename(costume)

					if canonical is not None and costume.get("md5ext") == canonical:
						removed.add("md5ext")
					costume = {
						key: value
						for key, value in costume.items()
						if key not in removed
					}

				target["costumes"].append(costume)

		result["targets"].append(target)

	if compact_reference_names:
		strip_reference_names(result, Counter())

	return result


def minimum_json_size(
	project: dict[str, Any],
	compact_defaults: bool = False,
	compact_costume_references: bool = False,
	compact_block_flags: bool = False,
	relabel_block_ids: bool = False,
	compact_reference_names: bool = False,
	prune_orphan_arguments: bool = False,
	compact_procedure_symbols: bool = False,
	compact_reporter_defaults: bool = False,
	strip_covered_shadows: bool = False,
	strip_editor_comments: bool = False,
	strip_script_positions: bool = False,
	prune_unused_data: bool = False,
	rebuild_procedure_displays: bool = False,
	compact_terminal_links: bool = False,
	*,
	_prepared=None,
) -> dict[str, Any]:
	if _prepared is None:
		project = compact_representation(
			project,
			compact_defaults,
			compact_costume_references,
			compact_block_flags,
			compact_reference_names,
			prune_orphan_arguments,
			compact_procedure_symbols,
			compact_reporter_defaults,
			strip_covered_shadows,
			strip_editor_comments,
			strip_script_positions,
			prune_unused_data,
			rebuild_procedure_displays,
			compact_terminal_links,
		)
		identifier_cost = None

		if relabel_block_ids:
			project, _, identifier_cost = relabel_blocks(
				project,
				copy_result=not (
					compact_procedure_symbols
					or compact_reference_names
					or strip_covered_shadows
				),
			)
	else:
		project, identifier_cost = _prepared
	counts = dict(structure=0, keys=0, strings=0, numbers=0, literals=0)

	@lru_cache(maxsize=65536)
	def string_size(value) -> int:
		return 2 + sum(
			(
				2
				if char in '"\\\b\f\n\r\t'
				else (
					6
					if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
					else len(char.encode("utf-8"))
				)
			)
			for char in value
		)

	def visit(value) -> None | NoReturn:
		if isinstance(value, JsonNumber):
			counts["numbers"] += len(get_shortest_num_repr(value))
		elif isinstance(value, str):
			counts["strings"] += string_size(value)
		elif isinstance(value, dict):
			counts["structure"] += 2 + len(value) + max(0, len(value) - 1)
			for key, item in value.items():
				counts["keys"] += string_size(key)
				visit(item)
		elif isinstance(value, list):
			counts["structure"] += 2 + max(0, len(value) - 1)
			for item in value:
				visit(item)
		elif value is None or isinstance(value, bool):
			counts["literals"] += 4 if value is None or value is True else 5
		else:
			raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")

	def get_model() -> str:
		if compact_terminal_links:
			return "selected-terminal-links-v13"
		if rebuild_procedure_displays:
			return "selected-procedure-displays-v12"
		if prune_unused_data:
			return "selected-unused-data-v11"
		if strip_editor_comments or strip_script_positions:
			return "selected-editor-metadata-v10"
		if compact_procedure_symbols:
			return "selected-signature-symbols-v9"
		if strip_covered_shadows:
			return "selected-covered-shadows-v8"
		if compact_reporter_defaults:
			return "fixed-sync-reporters-v6"
		if prune_orphan_arguments:
			return "fixed-live-arguments-v5"
		if compact_reference_names:
			return "fixed-resolved-references-v4"
		if relabel_block_ids:
			return "fixed-graph-block-ids-v3"

		return "fixed-tree-v2"

	visit(project)
	return {
		"model": get_model(),
		"minimum_bytes": sum(counts.values()),
		"components": counts,
		"rebuild_procedure_displays": rebuild_procedure_displays,
		"procedure_editing_state_preserved": not rebuild_procedure_displays,
		"compact_terminal_links": compact_terminal_links,
		"terminal_link_presence_preserved": not compact_terminal_links,
		"compact_block_defaults": compact_defaults,
		"compact_costume_references": compact_costume_references,
		"compact_block_flags": compact_block_flags,
		"compact_reference_names": compact_reference_names,
		"prune_orphan_arguments": prune_orphan_arguments,
		"compact_procedure_symbols": compact_procedure_symbols,
		"compact_reporter_defaults": compact_reporter_defaults,
		"strip_covered_shadows": strip_covered_shadows,
		"strip_editor_comments": strip_editor_comments,
		"strip_script_positions": strip_script_positions,
		"prune_unused_data": prune_unused_data,
		"covered_defaults_preserved": not strip_covered_shadows,
		"symbol_assignment_optimal_proven": not compact_procedure_symbols,
		"identifier_cost": identifier_cost,
		"global_scratch_minimum_proven": False,
	}


def _index_key(key):
	return (
		len(key) <= 10
		and key.isascii()
		and key.isdigit()
		and str(int(key)) == key
		and int(key) < 4294967295
	)


def javascript_keys(mapping):
	return sorted((key for key in mapping if _index_key(key)), key=int) + [
		key for key in mapping if not _index_key(key)
	]


def deflate(data, level=9, memory=8, strategy=zlib.Z_DEFAULT_STRATEGY):
	compressor = zlib.compressobj(level, zlib.DEFLATED, -15, memory, strategy)
	return compressor.compress(data) + compressor.flush()


def _compressions(data, level, thorough=True):
	yield "zlib", deflate(data, level)
	if level and thorough:
		yield "zlib-mem9", deflate(data, level, 9)
		yield "zlib-filtered", deflate(data, level, 8, zlib.Z_FILTERED)


def get_zopfli(required=False):
	try:
		import zopfli.zlib
	except ImportError:
		if required:
			raise ValueError(
				"Zopfli is unavailable; install it with: python -m pip install zopfli"
			) from None
		return None

	return zopfli.zlib.compress


def zopfli_deflate(data, compress, iterations=15, *, parallel=False):
	options = (
		{"numiterations": iterations},
		{"numiterations": iterations, "blocksplittingmax": 0},
	)

	if parallel and len(data) >= 65536 and (os.cpu_count() or 1) > 1:
		with ThreadPoolExecutor(max_workers=2) as pool:
			jobs = [pool.submit(compress, data, **kwargs) for kwargs in options]
			candidates = [job.result()[2:-4] for job in jobs]
	else:
		candidates = [compress(data, **kwargs)[2:-4] for kwargs in options]

	return min(candidates, key=len)


def optimize_project_json(
	project,
	level=9,
	use_zopfli=False,
	iterations=15,
	zopfli_required=True,
	compact_defaults=False,
	compact_costume_references=False,
	compact_block_flags=False,
	minimum_json=False,
	search_rounds=0,
	relabel_block_ids=False,
	compact_reference_names=False,
	prune_orphan_arguments=False,
	compact_procedure_symbols=False,
	compact_reporter_defaults=False,
	fast_json=False,
	strip_covered_shadows=False,
	strip_editor_comments=False,
	strip_script_positions=False,
	prune_unused_data=False,
	rebuild_procedure_displays=False,
	compact_terminal_links=False,
	unbounded_json_ordering=False,
	unbounded_layout=False
):
	unbounded_json_search = bool(unbounded_json_ordering or unbounded_layout)
	fast_json = bool(fast_json and not unbounded_json_search)
	baseline = dumps_exact(project)
	search_project = compact_representation(
		project,
		compact_defaults,
		compact_costume_references,
		compact_block_flags,
		compact_reference_names,
		prune_orphan_arguments,
		compact_procedure_symbols,
		compact_reporter_defaults,
		strip_covered_shadows,
		strip_editor_comments,
		strip_script_positions,
		prune_unused_data,
		rebuild_procedure_displays,
		compact_terminal_links,
	)
	identifier_cost = None
	
	if relabel_block_ids:
		search_project, _, identifier_cost = relabel_blocks(
			search_project,
			copy_result=not (
				compact_procedure_symbols
				or compact_reference_names
				or strip_covered_shadows
			),
		)
	
	best_raw = dumps_exact(search_project, True) if minimum_json else baseline
	best_compressed = deflate(best_raw, level)
	rank = (
		(lambda encoded, raw: (len(raw), len(encoded)))
		if minimum_json
		else (lambda encoded, raw: (len(encoded), len(raw)))
	)
	pool = []

	def remember(raw, compressed):
		if not search_rounds:
			return
		score = rank(compressed, raw)
		for index, (previous, candidate) in enumerate(pool):
			if raw == candidate:
				if score < previous:
					pool[index] = (score, raw)
				break
		else:
			pool.append((score, raw))
		pool.sort(key=lambda item: item[0])
		del pool[4:]

	best_label = "original-order/zlib"
	trials = 1
	seen = set()
	screened_orders, screen_trials = (
		((), 0)
		if fast_json
		else _screen_record_orders(search_project, level, unbounded_json_ordering)
	)
	trials += screen_trials
	orders = (
		BLOCK_KEY_ORDERS[:3]
		if fast_json
		else tuple(dict.fromkeys((*BLOCK_KEY_ORDERS, *screened_orders)))
	)

	for short_numbers in (False, True):
		if minimum_json and not short_numbers:
			continue
		parts, shapes = _layout_parts(search_project, short_numbers), {}

		for order_index, order in enumerate((None, *orders)):
			candidate = _render_layout(parts, order, shapes)
			fingerprint = hashlib.sha256(candidate).digest()
			if fingerprint in seen or len(candidate) > len(baseline):
				continue
			seen.add(fingerprint)

			for compressor, compressed in _compressions(candidate, level, False):
				trials += 1
				remember(candidate, compressed)
				if rank(compressed, candidate) < rank(best_compressed, best_raw):
					best_raw, best_compressed = candidate, compressed
					best_label = f"layout-{order_index}/{'short-numbers' if short_numbers else 'original-numbers'}/{compressor}"

	if not fast_json and isinstance(search_project.get("targets"), list):
		adaptive = dict(search_project)
		adaptive["targets"] = []

		for target in search_project["targets"]:
			parts, shapes = _layout_parts(target, path=("targets", 0)), {}
			chosen_order = None
			chosen_size = len(deflate(_render_layout(parts, shapes=shapes), level))

			for order in orders:
				size = len(deflate(_render_layout(parts, order, shapes), level))
				trials += 1
				if size < chosen_size:
					chosen_order, chosen_size = order, size

			adaptive["targets"].append(
				target
				if chosen_order is None
				else _ordered_blocks(target, chosen_order)
			)
		
		candidate = dumps_exact(adaptive, True)
		
		if hashlib.sha256(candidate).digest() not in seen and len(candidate) <= len(
			baseline
		):
			for compressor, compressed in _compressions(candidate, level, False):
				trials += 1
				remember(candidate, compressed)

				if rank(compressed, candidate) < rank(best_compressed, best_raw):
					best_raw, best_compressed = candidate, compressed
					best_label = f"per-target/short-numbers/{compressor}"

	for grouping in (() if fast_json else ("shape", "opcode", "target-opcode")):
		specialized, specialized_trials = _specialized_layout(
			search_project, orders, level, grouping, unbounded_layout
		)
		trials += specialized_trials

		for short_numbers in (False, True):
			if minimum_json and not short_numbers:
				continue

			candidate = dumps_exact(specialized, short_numbers)
			fingerprint = hashlib.sha256(candidate).digest()

			if fingerprint in seen or len(candidate) > len(baseline):
				continue

			seen.add(fingerprint)
			for compressor, compressed in _compressions(candidate, level, False):
				trials += 1
				remember(candidate, compressed)
				if rank(compressed, candidate) < rank(best_compressed, best_raw):
					best_raw, best_compressed = candidate, compressed
					best_label = f"per-{grouping}/{'short-numbers' if short_numbers else 'original-numbers'}/{compressor}"

	if unbounded_layout and not fast_json:
		parts = _layout_parts(search_project)
		longest_run = current_run = 0

		for part in parts:
			if isinstance(part, _LayoutRecord):
				current_run += 1
				longest_run = max(longest_run, current_run)
			else:
				current_run = 0

		while True:
			previous = best_raw

			for width in range(1, longest_run + 1):
				candidate, window_trials = _window_layout(
					loads_exact(best_raw), orders, level, width, True
				)
				trials += window_trials

				for compressor, compressed in _compressions(candidate, level, False):
					trials += 1
					remember(candidate, compressed)
					if rank(compressed, candidate) < rank(best_compressed, best_raw):
						best_raw, best_compressed = candidate, compressed
						best_label = f"unbounded-window-{width}/{compressor}"
			
			if best_raw == previous:
				break
	else:
		for round_index in range(0 if fast_json else search_rounds):
			candidate, window_trials = _window_layout(
				loads_exact(best_raw), orders, level, 128 if round_index % 2 == 0 else 64
			)
			trials += window_trials

			for compressor, compressed in _compressions(candidate, level, False):
				trials += 1
				remember(candidate, compressed)
				if rank(compressed, candidate) < rank(best_compressed, best_raw):
					best_raw, best_compressed = candidate, compressed
					best_label = f"window-{round_index + 1}/{compressor}"

	for compressor, compressed in (
		*_compressions(best_raw, level),
		("zlib-mem5", deflate(best_raw, level, 5)),
	):
		trials += 1

		if rank(compressed, best_raw) < rank(best_compressed, best_raw):
			best_compressed = compressed
			best_label += "/" + compressor

	zopfli = get_zopfli(zopfli_required) if use_zopfli else None

	if zopfli:
		remember(best_raw, best_compressed)
		if search_rounds > 1:
			for _, candidate in pool[:2]:
				compressed = zopfli_deflate(
					candidate, zopfli, min(iterations, 2), parallel=True
				)
				trials += 1

				if rank(compressed, candidate) < rank(best_compressed, best_raw):
					best_raw, best_compressed = candidate, compressed
					best_label = "Zopfli-ranked layouts"

		compressed = zopfli_deflate(best_raw, zopfli, iterations, parallel=True)
		trials += 1

		if len(compressed) < len(best_compressed):
			best_compressed = compressed
			best_label += "/zopfli"

	result = loads_exact(best_raw)
	difference = exact_difference(
		project,
		result,
		compact_defaults,
		compact_costume_references,
		compact_block_flags,
		relabel_block_ids,
		compact_reference_names,
		prune_orphan_arguments,
		compact_procedure_symbols,
		compact_reporter_defaults,
		strip_covered_shadows,
		strip_editor_comments,
		strip_script_positions,
		prune_unused_data,
		rebuild_procedure_displays,
		compact_terminal_links,
	)

	if difference or zlib.decompress(best_compressed, -15) != best_raw:
		raise ValueError(
			f"JSON encoding verification failed: {difference or 'DEFLATE mismatch'}"
		)

	removed = sum(
		len(a.get("blocks", {})) - len(b.get("blocks", {}))
		for a, b in zip(project.get("targets", []), result.get("targets", []))
	)
	orphan_removed = removed

	if rebuild_procedure_displays:
		orphan_removed = (
			sum(
				min(
					len(get_orphan_argument_ids(a)),
					max(0, len(a.get("blocks", {})) - len(b.get("blocks", {}))),
				)
				for a, b in zip(project.get("targets", []), result.get("targets", []))
			)
			if prune_orphan_arguments
			else 0
		)

	return (
		best_raw,
		best_compressed,
		{
			"json_encoding_trials": trials,
			"json_encoding_bytes_saved": len(baseline) - len(best_raw),
			"json_deflate_bytes_saved": len(deflate(baseline, level))
			- len(best_compressed),
			"json_encoding": best_label,
			"zopfli_available": zopfli is not None,
			"orphan_argument_blocks_removed": orphan_removed,
			"procedure_display_blocks_removed": removed - orphan_removed,
			"terminal_links_removed": (
				sum(len(ids) for ids in _terminal_link_ids(project).values())
				if compact_terminal_links
				else 0
			),
			"covered_defaults_removed": (
				sum(1 for _ in _covered_shadow_slots(project))
				- sum(1 for _ in _covered_shadow_slots(result))
				if strip_covered_shadows
				else 0
			),
			"editor_comments_removed": (
				sum(
					len(a.get("comments", {})) - len(b.get("comments", {}))
					for a, b in zip(
						project.get("targets", []), result.get("targets", [])
					)
				)
				if strip_editor_comments
				else 0
			),
			"script_position_values_removed": (
				sum(
					len(_editor_position_keys(block))
					for target in project.get("targets", [])
					for block in target.get("blocks", {}).values()
				)
				- sum(
					len(_editor_position_keys(block))
					for target in result.get("targets", [])
					for block in target.get("blocks", {}).values()
				)
				if strip_script_positions
				else 0
			),
			"unused_data_declarations_removed": (
				sum(
					len(a.get(kind, {})) - len(b.get(kind, {}))
					for a, b in zip(
						project.get("targets", []), result.get("targets", [])
					)
					for kind in ("variables", "lists")
				)
				if prune_unused_data
				else 0
			),
			"json_minimum": minimum_json_size(
				project,
				compact_defaults,
				compact_costume_references,
				compact_block_flags,
				relabel_block_ids,
				compact_reference_names,
				prune_orphan_arguments,
				compact_procedure_symbols,
				compact_reporter_defaults,
				strip_covered_shadows,
				strip_editor_comments,
				strip_script_positions,
				prune_unused_data,
				rebuild_procedure_displays,
				compact_terminal_links,
				_prepared=(search_project, identifier_cost),
			),
		},
	)


def read_compressed_entry(archive, info):
	archive.fp.seek(info.header_offset)
	header = archive.fp.read(30)
	if len(header) != 30 or header[:4] != b"PK\x03\x04":
		raise ValueError(f"invalid ZIP local header: {info.filename!r}")

	offset = int.from_bytes(header[26:28], "little") + int.from_bytes(
		header[28:30], "little"
	)
	archive.fp.seek(offset, 1)
	compressed = archive.fp.read(info.compress_size)

	if len(compressed) != info.compress_size:
		raise ValueError(f"truncated ZIP entry: {info.filename!r}")

	return compressed


def write_compressed_entry(archive, info, compressed):
	info = copy.copy(info)
	info.flag_bits &= ~0x08
	info.compress_size = len(compressed)
	info.header_offset = archive.fp.tell()
	archive._didModify = True
	archive.fp.write(info.FileHeader())
	archive.fp.write(compressed)
	archive.filelist.append(info)
	archive.NameToInfo[info.filename] = info
	archive.start_dir = archive.fp.tell()


def optimize_asset(data, level=9, original=None, use_zopfli=False, iterations=15):
	if original is not None and original[0] in (0, 8):
		method, best = original
	else:
		method, best = 8, deflate(data, level)

	if len(data) < len(best):
		method, best = 0, data

	for _, compressed in _compressions(data, level):
		if len(compressed) < len(best):
			method, best = 8, compressed

	if use_zopfli:
		compressed = zopfli_deflate(data, use_zopfli, iterations)
		if len(compressed) < len(best):
			method, best = 8, compressed

	if (zlib.decompress(best, -15) if method == 8 else best) != data:
		raise ValueError("lossless asset compression verification failed")

	return method, best
