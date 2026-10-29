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


def _terminal_link_ids(project):
	if not isinstance(project, dict) or not isinstance(project.get("targets"), list):
		return {}

	if project.get("extensionURLs") or project.get("extensionStorage"):
		return {}

	extensions = project.get("extensions", [])
	if not isinstance(extensions, list) or any(
		value not in ("pen", "music") for value in extensions
	):
		return {}

	for target in project["targets"]:
		if (
			not isinstance(target, dict)
			or not isinstance(target.get("blocks", {}), dict)
			or target.get("extensionStorage")
		):
			return {}

		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and (
				type(block.get("opcode")) is not str
				or block["opcode"] not in _DATA_PRUNING_OPCODES
			):
				return {}

	primitive_tags = {
		_number_components(str(i)): i
		for i in range(PRIMITIVE_NUMBER, PRIMITIVE_LIST + 1)
	}
	result = {}

	for ti, target in enumerate(project["targets"]):
		blocks, memo = target.get("blocks", {}), {}

		def input_refs(block):
			inputs = block.get("inputs", {})
			if not isinstance(inputs, dict):
				return None

			refs = []

			for desc in inputs.values():
				if not isinstance(desc, list) or not desc:
					return None

				tag = _input_tag(desc[0])
				if tag not in INPUT_TAGS or len(desc) != (
					3 if tag == INPUT_DIFF_BLOCK_SHADOW else 2
				):
					return None

				for value in desc[1:]:
					if type(value) is str:
						refs.append(value)
					elif isinstance(value, list) and value:
						kind = (
							primitive_tags.get(_number_components(value[0]))
							if isinstance(value[0], JsonNumber)
							else value[0] if type(value[0]) in (int, float) else None
						)
						if kind not in range(
							PRIMITIVE_NUMBER, PRIMITIVE_LIST + 1
						) or len(value) != (3 if kind >= PRIMITIVE_BROADCAST else 2):
							return None
					else:
						return None

			return refs

		def synchronous(root):
			pending, active = [(root, None)], set()

			while pending:
				bid, children = pending.pop()
				if children is not None:
					memo[bid] = all(memo.get(child, False) for child in children)
					active.discard(bid)
					continue

				if bid in memo:
					continue

				if bid in active:
					memo[bid] = False
					continue

				block = blocks.get(bid)
				if (
					not isinstance(block, dict)
					or block.get("next") is not None
					or not (
						block.get("opcode") in _SYNC_REPORTERS
						or block.get("opcode") in _SYNC_TERMINAL_MENUS
						and block.get("shadow") is True
					)
				):
					memo[bid] = False
					continue

				refs = input_refs(block)
				if refs is None:
					memo[bid] = False
					continue

				active.add(bid)
				pending.append((bid, refs))
				pending.extend((child, None) for child in refs)

			return memo[root]

		eligible = set()

		for bid, block in blocks.items():
			if not isinstance(block, dict) or block.get("next", False) is not None:
				continue

			opcode = block.get("opcode")
			candidate = (
				opcode in _SYNC_TERMINAL_COMMANDS
				or opcode in _SYNC_TERMINAL_MENUS
				and block.get("shadow") is True
			)

			if opcode == "procedures_prototype":
				parent = (
					blocks.get(block.get("parent"))
					if type(block.get("parent")) is str
					else None
				)
				inputs = (
					parent.get("inputs")
					if isinstance(parent, dict)
					and parent.get("opcode") == "procedures_definition"
					else None
				)
				desc = inputs.get("custom_block") if isinstance(inputs, dict) else None
				candidate = (
					isinstance(desc, list)
					and desc[1] == bid
					and (
						(
							len(desc) == 2
							and _input_tag(desc[0])
							in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
						)
						or (
							len(desc) == 3
							and _input_tag(desc[0]) == INPUT_DIFF_BLOCK_SHADOW
						)
					)
				)

			if not candidate:
				continue

			refs = input_refs(block)
			if refs is not None and all(synchronous(child) for child in refs):
				eligible.add(bid)

		if eligible:
			result[ti] = eligible

	return result


def _covered_shadow_slots(project) -> Generator:
	token_kinds = {
		_number_components(str(value)): value
		for value in range(PRIMITIVE_NUMBER, PRIMITIVE_LIST + 1)
	}

	def kind(value):
		if isinstance(value, JsonNumber):
			return token_kinds.get(_number_components(value))

		return (
			value
			if type(value) in (int, float)
			and value in range(PRIMITIVE_NUMBER, PRIMITIVE_LIST + 1)
			else None
		)

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		for bid, block in blocks.items():
			if (
				not isinstance(block, dict)
				or block.get("opcode") not in _COVERED_INPUT_OPCODES
			):
				continue
			if block.get("opcode") == "procedures_call":
				mutation = block.get("mutation", {})
				if (
					not isinstance(mutation, dict)
					or type(mutation.get("proccode")) is not str
				):
					continue
				if set(mutation) - {
					"tagName",
					"children",
					"proccode",
					"argumentids",
					"warp",
				}:
					continue
			for name, desc in block.get("inputs", {}).items():
				if (
					not isinstance(desc, list)
					or len(desc) != 3
					or _input_tag(desc[0]) != INPUT_DIFF_BLOCK_SHADOW
				):
					continue

				active, shadow = desc[1:]
				if (
					not isinstance(shadow, list)
					or len(shadow) != 2
					or kind(shadow[0])
					not in (
						PRIMITIVE_NUMBER,
						PRIMITIVE_POSITIVE_NUMBER,
						PRIMITIVE_WHOLE_NUMBER,
						PRIMITIVE_INTEGER,
						PRIMITIVE_ANGLE,
						PRIMITIVE_COLOR,
						PRIMITIVE_TEXT,
					)
				):
					continue

				if not isinstance(shadow[1], str):
					continue

				if type(active) is str:
					child = blocks.get(active)
					if (
						not active
						or not isinstance(child, dict)
						or child.get("parent") != bid
						or child.get("topLevel", False) is not False
					):
						continue
				elif isinstance(active, list):
					if (
						len(active) != 3
						or kind(active[0]) not in CONTAINER_TAGS
						or type(active[1]) is not str
						or type(active[2]) is not str
						or not active[2]
					):
						continue
				else:
					continue

				yield ti, bid, name, desc


def _restore_covered_shadows(
	original: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
	for ti, bid, name, before in _covered_shadow_slots(original):
		try:
			inputs = result["targets"][ti]["blocks"][bid]["inputs"]
			after = inputs[name]
			if (
				isinstance(after, list)
				and len(after) == 2
				and _input_tag(after[0]) == INPUT_BLOCK_NO_SHADOW
			):
				inputs[name] = [before[0], after[1], before[2]]
		except (KeyError, IndexError, TypeError):
			...

	return result


def _procedure_symbol_info(target: dict[str, Any]) -> list[Counter] | None:
	counts = [Counter(), Counter(), Counter()]

	for block in target.get("blocks", {}).values():
		if not isinstance(block, dict):
			continue

		op = block.get("opcode")

		if op in ("procedures_prototype", "procedures_call"):
			mutation = block.get("mutation")
			if (
				not isinstance(mutation, dict)
				or type(mutation.get("proccode")) is not str
			):
				return None

			if set(mutation) - {
				"tagName",
				"children",
				"proccode",
				"argumentids",
				"argumentnames",
				"argumentdefaults",
				"warp",
				"hasnext",
			}:
				return None

			if (
				mutation.get("tagName", "mutation") != "mutation"
				or mutation.get("children", []) != []
			):
				return None

			code = mutation["proccode"]
			if code in _RESERVED_PROCEDURE_SYMBOLS:
				return None

			if "%" in re.sub(r"%[snb]", "", code):
				return None

			counts[0][code] += 1
			for key, index in (("argumentids", 1), ("argumentnames", 2)):
				if key not in mutation:
					if key == "argumentids" or op == "procedures_prototype":
						return None
					continue

				try:
					values = json.loads(mutation[key])
				except (TypeError, ValueError):
					return None
				if not isinstance(values, list) or any(
					type(v) is not str for v in values
				):
					return None

				if len(values) != len(re.findall(r"%[snb]", code)):
					return None

				if any(v in _RESERVED_PROCEDURE_SYMBOLS for v in values):
					return None

				counts[index].update(values)
			inputs = block.get("inputs", {})
			if not isinstance(inputs, dict):
				return None

			if list(inputs) != javascript_keys(inputs):
				return None

			if set(inputs) - set(json.loads(mutation["argumentids"])):
				return None

			counts[1].update(inputs.keys())
		elif op in ("argument_reporter_string_number", "argument_reporter_boolean"):
			value = block.get("fields", {}).get("VALUE")
			if not isinstance(value, list) or not value or type(value[0]) is not str:
				return None

			counts[2][value[0]] += 1

	return counts


def _compact_procedure_symbols(project: dict[str, Any], *, copy_result=True):
	if copy_result:
		project = copy.deepcopy(project)

	for target in project.get("targets", []):
		counts = _procedure_symbol_info(target)
		if counts is None:
			continue

		maps = []
		for index, frequencies in enumerate(counts):

			def names():
				return (
					name
					for name in _identifier_names(_RESERVED_PROCEDURE_SYMBOLS)
					if name.strip() and "%" not in name and "\\" not in name
				)

			pool = names()
			procedure_pools = {}
			mapping = {}

			for old in sorted(frequencies, key=lambda value: -frequencies[value]):
				if index == 2 and old.lower() in _SPECIAL_ARGUMENT_NAMES:
					mapping[old] = old
					continue

				if index == 0:
					kinds = tuple(re.findall(r"%[snb]", old))
					first = kinds not in procedure_pools
					if first:
						procedure_pools[kinds] = names()
					label = "" if kinds and first else next(procedure_pools[kinds])
					name = " ".join(kinds) + label if kinds else label
				else:
					name = next(pool)
				mapping[old] = name

			maps.append(mapping)

		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue

			op = block.get("opcode")
			if op in ("procedures_prototype", "procedures_call"):
				mutation = block["mutation"]
				mutation["proccode"] = maps[0][mutation["proccode"]]

				for key, index in (("argumentids", 1), ("argumentnames", 2)):
					if key in mutation:
						mutation[key] = json.dumps(
							[maps[index][v] for v in json.loads(mutation[key])],
							ensure_ascii=False,
							separators=(",", ":"),
						)

				if "inputs" in block:
					block["inputs"] = {
						maps[1][key]: value for key, value in block["inputs"].items()
					}
			elif op in ("argument_reporter_string_number", "argument_reporter_boolean"):
				block["fields"]["VALUE"][0] = maps[2][block["fields"]["VALUE"][0]]

	return project


def _restore_procedure_symbols(
	original: dict[str, Any],
	result: dict[str, Any],
	*,
	copy_result: bool = True,
	opts: Options | None = None,
):
	if copy_result:
		result = copy.deepcopy(result)

	mut_edits: dict = getattr(opts, "procedure_argument_mutation_edits", {})
	removed_in: dict = getattr(opts, "procedure_argument_removed_inputs", {})
	spec_calls: dict = getattr(opts, "specialized_procedure_call_proccodes", {})
	arg_renames: dict = getattr(opts, "renamed_argument_ids", {})
	proc_names: dict = getattr(opts, "renamed_identifiers", {}).get("procedures", {})
	proc_forward: dict[tuple, Any] = {
		(pk[0], original): pk[1]
		for pk, original in proc_names.items()
		if isinstance(pk, tuple) and len(pk) == 2
	}

	def renamed_ids(ti, proccode, ids):
		return [arg_renames.get((ti, proccode, i), i) for i in ids]

	for ti, (left, right) in enumerate(
		zip(original.get("targets", []), result.get("targets", []))
	):
		if _procedure_symbol_info(left) is None:
			continue

		try:
			forward, reverse = [{}, {}, {}], [{}, {}, {}]

			def pair(index, old, new):
				if (
					type(old) is not str
					or type(new) is not str
					or new in _RESERVED_PROCEDURE_SYMBOLS
				):
					raise ValueError("invalid procedure symbol")

				if (old in forward[index] and forward[index][old] != new) or (
					new in reverse[index] and reverse[index][new] != old
				):
					raise ValueError("procedure symbol correspondence is not bijective")

				if (
					index == 2
					and (
						old.lower() in _SPECIAL_ARGUMENT_NAMES
						or new.lower() in _SPECIAL_ARGUMENT_NAMES
					)
					and new != old
				):
					raise ValueError("special argument reporter name changed")

				forward[index][old], reverse[index][new] = new, old

			pairs = []
			for bid, block in left.get("blocks", {}).items():
				if not isinstance(block, dict):
					continue

				other = right.get("blocks", {}).get(bid)
				if not isinstance(other, dict):
					continue

				op = block.get("opcode")
				if op in ("procedures_prototype", "procedures_call"):
					a, b = block["mutation"], other.get("mutation", {})
					expected = mut_edits.get((ti, bid))
					if isinstance(expected, dict):
						a = dict(expected)

					if (ti, bid) in spec_calls:
						a = dict(a)
						a["proccode"] = spec_calls[(ti, bid)]

					arg_proc = a.get("proccode")
					compare = a
					if arg_renames or proc_forward:
						compare = dict(a)
						current_code = proc_forward.get((ti, arg_proc), arg_proc)
						if arg_renames and isinstance(compare.get("argumentids"), str):
							compare["argumentids"] = json.dumps(
								renamed_ids(
									ti, arg_proc, json.loads(compare["argumentids"])
								),
								separators=(",", ":"),
								ensure_ascii=False,
							)

						compare["proccode"] = current_code

					pair(0, compare["proccode"], b.get("proccode"))

					if re.findall(r"%[snb]", compare["proccode"]) != re.findall(
						r"%[snb]", b["proccode"]
					):
						raise ValueError("procedure placeholder types changed")

					arg_id_forward = {}
					arg_id_restore = {}

					for key, index in (("argumentids", 1), ("argumentnames", 2)):
						if key not in compare:
							continue

						x, y = json.loads(compare[key]), json.loads(b.get(key))
						if not isinstance(y, list) or len(x) != len(y):
							raise ValueError("procedure argument count changed")

						if index == 1:
							if any(
								type(old) is not str or type(new) is not str
								for old, new in zip(x, y)
							):
								raise ValueError("invalid procedure argument ID")

							arg_id_forward = dict(zip(x, y))
							original_ids = json.loads(a["argumentids"])
							arg_id_restore = dict(zip(y, original_ids))
						else:
							for old, new in zip(x, y):
								pair(index, old, new)

					pairs.append(
						(block, other, a, bid, arg_proc, arg_id_forward, arg_id_restore)
					)
				elif op in (
					"argument_reporter_string_number",
					"argument_reporter_boolean",
				):
					pair(2, block["fields"]["VALUE"][0], other["fields"]["VALUE"][0])

			for block, other, a, bid, arg_proc, arg_id_forward, arg_id_restore in pairs:
				b = other["mutation"]
				b["proccode"] = reverse[0][b["proccode"]]
				for key in ("argumentids", "argumentnames"):
					if key in a:
						b[key] = a[key]

				if "inputs" in other:
					removed = removed_in.get((ti, bid)) or ()
					expected_inputs = [
						key for key in block.get("inputs", {}) if key not in removed
					]
					expected_result_inputs = [
						arg_id_forward.get(
							arg_renames.get((ti, arg_proc, key), key),
							arg_id_forward.get(key, key),
						)
						for key in expected_inputs
					]

					if list(other["inputs"]) != expected_result_inputs:
						raise ValueError(
							"procedure input keys or evaluation order changed"
						)

					reverse_input_ids = arg_id_restore
					other["inputs"] = {
						reverse_input_ids.get(key, key): value
						for key, value in other["inputs"].items()
					}

			for block in right.get("blocks", {}).values():
				if isinstance(block, dict) and block.get("opcode") in (
					"argument_reporter_string_number",
					"argument_reporter_boolean",
				):
					value = block["fields"]["VALUE"]
					value[0] = reverse[2].get(value[0], value[0])
		except (ValueError, KeyError, TypeError, IndexError):
			continue

	return result


def _unused_data_ids(project):
	targets = project.get("targets", [])
	stages = [ti for ti, target in enumerate(targets) if target.get("isStage") is True]
	extensions = project.get("extensions", [])

	if (
		len(stages) != 1
		or not isinstance(extensions, list)
		or any(ext not in ("pen", "music") for ext in extensions)
	):
		return {}

	if any(
		project.get(key)
		for key in ("extensionURLs", "extensionData", "extensionStorage")
	):
		return {}

	stage = stages[0]
	ids, names = set(), set()
	scopes = []

	for target in targets:
		seen = set()

		for kind in ("variables", "lists", "broadcasts"):
			table = target.get(kind, {})

			if not isinstance(table, dict) or seen.intersection(table):
				return {}

			seen.update(table)

		scopes.append(seen)

	primitive_kinds = {
		_number_components(str(tag)): tag
		for tag in range(PRIMITIVE_NUMBER, PRIMITIVE_LIST + 1)
	}

	def mark(ti, kind, name, ident):
		if type(ident) is str:
			for owner in (ti, stage):
				if ident in scopes[owner]:
					ids.add((owner, ident))
					return

		for owner in (ti, stage):
			if type(name) is str:
				names.add((owner, kind, name))

	def primitive(ti, value):
		if not isinstance(value, list) or not value:
			return False

		tag = value[0]

		if isinstance(tag, JsonNumber):
			tag = primitive_kinds.get(_number_components(tag))

		if type(tag) not in (int, float):
			return False

		if tag in (
			PRIMITIVE_NUMBER,
			PRIMITIVE_POSITIVE_NUMBER,
			PRIMITIVE_WHOLE_NUMBER,
			PRIMITIVE_INTEGER,
			PRIMITIVE_ANGLE,
			PRIMITIVE_COLOR,
			PRIMITIVE_TEXT,
		):
			return len(value) == 2

		if (
			tag not in (PRIMITIVE_BROADCAST, PRIMITIVE_VARIABLE, PRIMITIVE_LIST)
			or len(value) not in (3, 5)
			or type(value[1]) is not str
			or type(value[2]) is not str
		):
			return False

		mark(
			ti,
			{
				PRIMITIVE_BROADCAST: "broadcasts",
				PRIMITIVE_VARIABLE: "variables",
				PRIMITIVE_LIST: "lists",
			}[tag],
			value[1],
			value[2],
		)
		return True

	for ti, target in enumerate(targets):
		if target.get("extensionStorage"):
			return {}

		blocks = target.get("blocks", {})
		if not isinstance(blocks, dict):
			return {}

		for block in blocks.values():
			if isinstance(block, list):
				if not primitive(ti, block):
					return {}

				continue

			if (
				not isinstance(block, dict)
				or block.get("opcode") not in _DATA_PRUNING_OPCODES
			):
				return {}

			fields, inputs = block.get("fields", {}), block.get("inputs", {})

			if not isinstance(fields, dict) or not isinstance(inputs, dict):
				return {}

			if fields.keys() & inputs.keys():
				return {}

			op = block["opcode"]
			data_field = (
				"VARIABLE"
				if op
				in (
					"data_variable",
					"data_setvariableto",
					"data_changevariableby",
					"data_showvariable",
					"data_hidevariable",
					"control_for_each",
				)
				else "LIST" if op.startswith("data_") else None
			)

			if data_field and data_field not in fields:
				return {}

			for field, kind in (
				("VARIABLE", "variables"),
				("LIST", "lists"),
				("BROADCAST_OPTION", "broadcasts"),
				("BROADCAST_INPUT", "broadcasts"),
			):
				if field not in fields:
					continue

				value = fields[field]

				if (
					not isinstance(value, list)
					or len(value) not in (1, 2)
					or type(value[0]) is not str
				):
					return {}

				ident = value[1] if len(value) > 1 else None

				if ident is not None and type(ident) is not str:
					return {}

				mark(ti, kind, value[0], ident)

			for desc in inputs.values():
				if not isinstance(desc, list) or not desc:
					return {}

				tag = _input_tag(desc[0])

				if tag is None or len(desc) != (
					3 if tag == INPUT_DIFF_BLOCK_SHADOW else 2
				):
					return {}

				for value in desc[1:]:
					if value is None or type(value) is str:
						continue

					if not primitive(ti, value):
						return {}

			if block.get("opcode") in ("sensing_of", "sensing_of_property_menu"):
				prop = fields.get("PROPERTY")
				if not isinstance(prop, list) or not prop or type(prop[0]) is not str:
					return {}

				for owner in range(len(targets)):
					names.add((owner, "variables", prop[0]))

	monitors = project.get("monitors", [])
	if not isinstance(monitors, list):
		return {}

	for monitor in monitors:
		if (
			not isinstance(monitor, dict)
			or monitor.get("opcode") not in _DATA_PRUNING_OPCODES
		):
			return {}

		if type(monitor.get("id")) is not str:
			return {}

		params = monitor.get("params", {})
		if not isinstance(params, dict):
			return {}

		if monitor["opcode"] in ("sensing_of", "sensing_of_property_menu"):
			if type(params.get("PROPERTY")) is not str:
				return {}

			for owner in range(len(targets)):
				names.add((owner, "variables", params["PROPERTY"]))

		for owner in range(len(targets)):
			for field, kind in (("VARIABLE", "variables"), ("LIST", "lists")):
				mark(owner, kind, params.get(field), monitor.get("id"))

	return {
		(ti, kind): frozenset(
			ident
			for ident, entry in target.get(kind, {}).items()
			if ident
			and ident
			not in ("__proto__", "constructor", "prototype", "null", "undefined")
			and isinstance(entry, list)
			and len(entry) == 2
			and type(entry[0]) is str
			and (kind != "lists" or isinstance(entry[1], list))
			and (ti, ident) not in ids
			and (ti, kind, entry[0]) not in names
		)
		for ti, target in enumerate(targets)
		for kind in ("variables", "lists")
	}


def _restore_unused_data(
	original: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
	eligible = _unused_data_ids(original)
	for ti, (left, right) in enumerate(
		zip(original.get("targets", []), result.get("targets", []))
	):
		for kind in ("variables", "lists"):
			before, after = left.get(kind), right.get(kind)
			if not isinstance(before, dict) or not isinstance(after, dict):
				continue

			missing = before.keys() - after.keys()
			if not missing:
				continue

			if not missing <= eligible.get((ti, kind), frozenset()):
				raise ValueError(
					"referenced, cloud, or unsupported data declaration removed"
				)
			if list(after) != [ident for ident in before if ident not in missing]:
				raise ValueError("retained data declaration order changed")

			right[kind] = {
				ident: before[ident] if ident in missing else after[ident]
				for ident in before
			}

	return result


def _editor_comment_ids(target: dict[str, Any]) -> frozenset:
	if target.get("isStage") is not False or not isinstance(
		target.get("comments"), dict
	):
		return frozenset()

	return frozenset(
		cid
		for cid, comment in target["comments"].items()
		if isinstance(comment, dict)
		and isinstance(comment.get("text"), str)
		and "_twconfig_" not in comment["text"]
	)


def _editor_position_keys(block: Any) -> tuple:
	if (
		not isinstance(block, dict)
		or block.get("topLevel") is not True
		or block.get("parent", False) is not None
	):
		return ()

	return tuple(
		key for key in ("x", "y") if type(block.get(key)) in (JsonNumber, int, float)
	)


def _restore_editor_metadata(
	original: dict[str, Any],
	result: dict[str, Any],
	strip_editor_comments: bool,
	strip_script_positions: bool,
) -> dict[str, Any]:
	for left, right in zip(original.get("targets", []), result.get("targets", [])):
		missing = set()

		if strip_editor_comments:
			eligible = _editor_comment_ids(left)
			before, after = left.get("comments"), right.get("comments")

			if eligible and isinstance(after, dict):
				missing = eligible - after.keys()

				if missing:
					if list(after) != [cid for cid in before if cid not in missing]:
						raise ValueError("retained comment keys or order changed")

					right["comments"] = {
						cid: before[cid] if cid in missing else after[cid]
						for cid in before
					}

		for bid, block in left.get("blocks", {}).items():
			other = right.get("blocks", {}).get(bid)

			if not isinstance(block, dict) or not isinstance(other, dict):
				continue

			if isinstance(block.get("comment"), str) and block["comment"] in missing:
				if "comment" in other:
					raise ValueError("removed editor comment retains a block link")
				other["comment"] = block["comment"]

			if strip_script_positions:
				for key in _editor_position_keys(block):
					if key not in other:
						other[key] = block[key]

	return result


def _procedure_display_plans(target: dict[str, Any]) -> list:
	blocks = target.get("blocks", {})
	if not isinstance(blocks, dict):
		return []

	candidates = []
	allowed = {
		"opcode",
		"next",
		"parent",
		"inputs",
		"fields",
		"shadow",
		"topLevel",
		"mutation",
	}

	for bid, prototype in blocks.items():
		if (
			not isinstance(prototype, dict)
			or prototype.get("opcode") != "procedures_prototype"
		):
			continue

		if (
			set(prototype) - allowed
			or prototype.get("shadow") is not True
			or prototype.get("topLevel", False) is not False
		):
			continue

		if prototype.get("next") is not None or prototype.get("fields", {}) != {}:
			continue

		definition_id = prototype.get("parent")
		definition = blocks.get(definition_id) if type(definition_id) is str else None

		if (
			not isinstance(definition, dict)
			or definition.get("opcode") != "procedures_definition"
		):
			continue

		if (
			definition.get("parent") is not None
			or definition.get("topLevel") is not True
		):
			continue

		inputs = definition.get("inputs")

		if not isinstance(inputs, dict) or set(inputs) != {"custom_block"}:
			continue

		desc = inputs["custom_block"]

		if (
			not isinstance(desc, list)
			or len(desc) != 2
			or _input_tag(desc[0]) != INPUT_SAME_BLOCK_SHADOW
			or desc[1] != bid
		):
			continue

		mutation = prototype.get("mutation")

		if not isinstance(mutation, dict) or set(mutation) - {
			"tagName",
			"children",
			"proccode",
			"argumentids",
			"argumentnames",
			"argumentdefaults",
			"warp",
		}:
			continue

		if mutation.get("tagName") != "mutation" or mutation.get("children") != []:
			continue

		if mutation.get("warp") not in (True, False, "true", "false"):
			continue

		code = mutation.get("proccode")
		if type(code) is not str or "%" in re.sub(r"%[snb]", "", code):
			continue

		kinds = re.findall(r"%[snb]", code)

		try:
			ids, names, defaults = [
				json.loads(mutation[key])
				for key in ("argumentids", "argumentnames", "argumentdefaults")
			]
		except (KeyError, TypeError, ValueError):
			continue

		if not all(isinstance(value, list) for value in (ids, names, defaults)):
			continue

		if not kinds or not len(kinds) == len(ids) == len(names) == len(defaults):
			continue

		if any(type(value) is not str for value in ids + names) or len(set(ids)) != len(
			ids
		):
			continue

		inputs = prototype.get("inputs")
		if not isinstance(inputs, dict) or list(inputs) != ids:
			continue

		children = []
		for ident, name, kind in zip(ids, names, kinds):
			desc = inputs[ident]
			if (
				not isinstance(desc, list)
				or len(desc) != 2
				or _input_tag(desc[0]) != INPUT_SAME_BLOCK_SHADOW
				or type(desc[1]) is not str
			):
				break

			child_id = desc[1]
			child = blocks.get(child_id)

			if not isinstance(child, dict) or set(child) - (allowed - {"mutation"}):
				break

			expected = (
				"argument_reporter_boolean"
				if kind == "%b"
				else "argument_reporter_string_number"
			)

			if (
				child.get("opcode") != expected
				or child.get("parent") != bid
				or child.get("shadow") is not True
			):
				break

			if (
				child.get("next") is not None
				or child.get("inputs", {}) != {}
				or child.get("topLevel", False) is not False
			):
				break

			if child.get("fields") not in ({"VALUE": [name]}, {"VALUE": [name, None]}):
				break

			children.append(child_id)
		else:
			if len(set(children)) == len(children):
				candidates.append((definition_id, bid, children))

	if not candidates:
		return []

	try:
		references, direct = Counter(), Counter()
		for container, key in _block_slots(target):
			references[container[key]] += 1
			if key != "parent":
				direct[container[key]] += 1
	except (KeyError, TypeError, IndexError):
		return []
	return [
		(definition, prototype, children)
		for definition, prototype, children in candidates
		if direct[prototype] == 1 and all(references[child] == 1 for child in children)
	]


def _rebuild_procedure_displays(project):
	if not isinstance(project, dict) or not isinstance(project.get("targets"), list):
		return project

	if project.get("extensionURLs") or project.get("extensionStorage"):
		return project

	extensions = project.get("extensions", [])

	if not isinstance(extensions, list) or any(
		extension not in ("pen", "music") for extension in extensions
	):
		return project

	for target in project.get("targets", []):
		if not isinstance(target, dict) or not isinstance(
			target.get("blocks", {}), dict
		):
			return project

		if any(
			isinstance(block, dict) and block.get("opcode") not in _DATA_PRUNING_OPCODES
			for block in target.get("blocks", {}).values()
		):
			return project

	result = dict(project)
	result["targets"] = []

	for source in project.get("targets", []):
		plans = _procedure_display_plans(source)
		target = source

		if plans:
			target = dict(source)
			blocks = target["blocks"] = dict(source["blocks"])

			for definition, prototype, children in plans:
				blocks[prototype] = dict(blocks[prototype])
				blocks[prototype].pop("inputs")
				blocks[prototype].pop("shadow")
				blocks[definition] = dict(blocks[definition])
				blocks[definition]["inputs"] = {
					"custom_block": [JsonNumber(str(INPUT_BLOCK_NO_SHADOW)), prototype]
				}

				for child in children:
					del blocks[child]

		result["targets"].append(target)

	return result


class _LayoutRecord:
	def __init__(self, prefix, block, short_numbers=True):
		self.prefix = prefix
		self.fields = {
			key: quote(key).encode("utf-8", "backslashreplace")
			+ b":"
			+ dumps_exact(value, short_numbers)
			for key, value in block.items()
		}
		self.shape = tuple(block)

	def render(self, order, shapes):
		key = (self.shape, order)
		if key not in shapes:
			shapes[key] = tuple(
				name
				for name in dict.fromkeys((*(order or ()), *self.shape))
				if name in self.fields
			)
		return (
			self.prefix
			+ b"{"
			+ b",".join(self.fields[name] for name in shapes[key])
			+ b"}"
		)


def _layout_parts(project, short_numbers=True, path=()):
	parts, pending = [], bytearray()

	def emit(data):
		pending.extend(data)

	def visit(value, path):
		if isinstance(value, dict):
			emit(b"{")
			for index, (key, item) in enumerate(value.items()):
				prefix = (
					(b"," if index else b"")
					+ quote(key).encode("utf-8", "backslashreplace")
					+ b":"
				)
				if (
					len(path) == 3
					and path[0] == "targets"
					and path[2] == "blocks"
					and isinstance(item, dict)
				):
					if pending:
						parts.append(bytes(pending))
						pending.clear()
					parts.append(_LayoutRecord(prefix, item, short_numbers))
				else:
					emit(prefix)
					visit(item, path + (key,))
			emit(b"}")
		elif isinstance(value, list):
			emit(b"[")
			for index, item in enumerate(value):
				if index:
					emit(b",")
				visit(item, path + (index,))
			emit(b"]")
		else:
			emit(dumps_exact(value, short_numbers))

	visit(project, path)
	if pending:
		parts.append(bytes(pending))
	return parts


def _window_layout(project, orders, level, width=128, unbounded_layout=False):
	parts, output, shapes = _layout_parts(project), [], {}
	state = zlib.compressobj(level, zlib.DEFLATED, -15)
	trials, index = 0, 0

	while index < len(parts):
		part = parts[index]
		if isinstance(part, bytes):
			output.append(part)
			state.compress(part)
			index += 1
			continue

		end = index
		while (
			end < len(parts)
			and isinstance(parts[end], _LayoutRecord)
			and end - index < width
		):
			end += 1

		records = parts[index:end]
		lookahead = (
			_render_layout(parts[end : end + 16], shapes=shapes)[:4096]
			if unbounded_layout
			else _render_layout(parts[end : end + 16], shapes=shapes)[:4096]
		)

		def score(candidate):
			probe = state.copy()
			return len(probe.compress(candidate + lookahead)) + len(probe.flush())

		best = _render_layout(records, shapes=shapes)
		best_size = score(best)

		for order in orders:
			candidate = _render_layout(records, order, shapes)
			if candidate == best:
				continue

			size = score(candidate)
			trials += 1

			if size < best_size:
				best, best_size = candidate, size

		output.append(best)
		state.compress(best)
		index = end

	return b"".join(output), trials


def _sample_blocks(project, width=64):
	result = {"targets": []}

	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		items = list(blocks.items())
		if len(items) > width * 3:
			starts = (0, len(items) // 2 - width // 2, len(items) - width)
			items = [item for start in starts for item in items[start : start + width]]

		result["targets"].append({"blocks": dict(items)})

	return result


def _screen_record_orders(project, level, unbounded_json_ordering=False):
	parts = _layout_parts(_sample_blocks(project))
	shapes, scores = {}, []

	for order in permutations(("opcode", "next", "parent", "inputs", "fields")):
		scores.append(
			(len(deflate(_render_layout(parts, order, shapes), level)), order)
		)

	ordered = sorted(scores)
	return [order for _, order in (ordered if unbounded_json_ordering else ordered[:4])], len(scores)


def _specialized_layout(project, orders, level, grouping, unbounded_layout=False):
	groups = {}

	def group_key(ti, block):
		shape = frozenset(block)

		if grouping == "shape":
			return shape

		key = (shape, block.get("opcode"))
		return (ti, key) if grouping == "target-opcode" else key

	for ti, target in enumerate(project.get("targets", [])):
		for bid, block in target.get("blocks", {}).items():
			if isinstance(block, dict):
				groups.setdefault(group_key(ti, block), []).append((ti, bid, block))

	chosen = {}
	trials = 0

	for key, members in groups.items():
		if not unbounded_layout and len(members) > 96:
			members = (
				members[:32]
				+ members[len(members) // 2 - 16 : len(members) // 2 + 16]
				+ members[-32:]
			)

		probe_targets = {}

		for ti, bid, block in members:
			probe_targets.setdefault(ti, {"blocks": {}})["blocks"][bid] = block

		probe = {"targets": list(probe_targets.values())}
		best_order = None
		parts, shapes = _layout_parts(probe, False), {}
		best_size = len(deflate(_render_layout(parts, shapes=shapes), level))

		for order in orders:
			size = len(deflate(_render_layout(parts, order, shapes), level))
			trials += 1
			if size < best_size:
				best_order, best_size = order, size

		chosen[key] = best_order

	result = dict(project)
	result["targets"] = []

	for ti, target in enumerate(project.get("targets", [])):
		target = dict(target)
		if isinstance(target.get("blocks"), dict):
			new_blocks = {}

			for bid, block in target["blocks"].items():
				order = (
					chosen.get(group_key(ti, block))
					if isinstance(block, dict)
					else None
				)
				new_blocks[bid] = (
					{
						name: block[name]
						for name in dict.fromkeys((*order, *block))
						if name in block
					}
					if order is not None
					else block
				)

			target["blocks"] = new_blocks

		result["targets"].append(target)

	return result, trials


def _render_layout(parts, order=None, shapes=None):
	if shapes is None:
		shapes = {}
	return b"".join(
		part if isinstance(part, bytes) else part.render(order, shapes)
		for part in parts
	)


def _dangling_block_ids(target):
	blocks = target.get("blocks", {})
	ids = set(blocks)
	dangling = set()
	for block in blocks.values():
		if not isinstance(block, dict):
			continue
		for key in ("next", "parent"):
			v = block.get(key)
			if isinstance(v, str) and v not in ids:
				dangling.add(v)
		for value in block.get("inputs", {}).values():
			collect_dangling_in_input(value, ids, dangling)
	for comment in target.get("comments", {}).values():
		if isinstance(comment, dict):
			bid = comment.get("blockId")
			if isinstance(bid, str) and bid not in ids:
				dangling.add(bid)
	return dangling


def _input_tag(value) -> int | None:
	if isinstance(value, JsonNumber):
		components = _number_components(value)
		return (
			int(components[1])
			if not components[0]
			and components[2] == 0
			and int(components[1]) in INPUT_TAGS
			else None
		)

	return value if type(value) in (int, float) and value in INPUT_TAGS else None


def _block_slots(target):
	for block in target.get("blocks", {}).values():
		if not isinstance(block, dict):
			continue

		for key in ("next", "parent"):
			if isinstance(block.get(key), str) and not isinstance(
				block[key], JsonNumber
			):
				yield block, key

		for desc in block.get("inputs", {}).values():
			if not isinstance(desc, list) or not desc:
				continue

			tag = _input_tag(desc[0])
			positions = (
				(1,)
				if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
				else (1, 2) if tag == INPUT_DIFF_BLOCK_SHADOW else ()
			)

			for position in positions:
				if (
					len(desc) > position
					and isinstance(desc[position], str)
					and not isinstance(desc[position], JsonNumber)
				):
					yield desc, position

	for comment in target.get("comments", {}).values():
		if (
			isinstance(comment, dict)
			and isinstance(comment.get("blockId"), str)
			and not isinstance(comment["blockId"], JsonNumber)
		):
			yield comment, "blockId"


def _identifier_names(reserved):
	chars = tuple(chr(value) for value in range(32, 128) if chr(value) not in '"\\<&')

	def allowed(name):
		return (
			name not in reserved
			and name not in ("__proto__", "constructor", "prototype")
			and not _index_key(name)
		)

	for name in chars:
		if allowed(name):
			yield name

	for pair in product(chars, repeat=2):
		name = "".join(pair)

		if allowed(name):
			yield name

	for name in ("\\", *(chr(value) for value in range(128, 2048))):
		if allowed(name):
			yield name

	length = 3
	while True:
		for digits in product(chars, repeat=length):
			name = "".join(digits)
			if allowed(name):
				yield name
		length += 1


def relabel_blocks(project, *, copy_result=True):
	result = copy.deepcopy(project) if copy_result else project
	maps, cost = {}, dict(before=0, after=0, identifiers=0, occurrences=0)
	for ti, target in enumerate(result.get("targets", [])):
		blocks = target.get("blocks")

		if not isinstance(blocks, dict) or not blocks:
			continue

		for block in blocks.values():
			if not isinstance(block, dict):
				continue

			for key in ("next", "parent"):
				value = block.get(key)
				if value is not None and (
					not isinstance(value, str) or isinstance(value, JsonNumber)
				):
					raise ValueError(
						"verified block relabeling requires string or null next/parent links"
					)

			for desc in block.get("inputs", {}).values():
				if (
					not isinstance(desc, list)
					or not desc
					or _input_tag(desc[0]) is None
				):
					raise ValueError(
						"verified block relabeling requires serialized SB3 input descriptors"
					)

				for position in (
					(1, 2) if _input_tag(desc[0]) == INPUT_DIFF_BLOCK_SHADOW else (1,)
				):
					if len(desc) <= position:
						raise ValueError(
							"verified block relabeling rejects truncated input descriptors"
						)

					value = desc[position]
					if value is not None and (
						not isinstance(value, (str, list))
						or isinstance(value, JsonNumber)
					):
						raise ValueError(
							"verified block relabeling rejects numeric or opaque input references"
						)

		for comment in target.get("comments", {}).values():
			if isinstance(comment, dict):
				value = comment.get("blockId")
				if value is not None and (
					not isinstance(value, str) or isinstance(value, JsonNumber)
				):
					raise ValueError(
						"verified block relabeling requires string or null comment block IDs"
					)

		ids = javascript_keys(blocks)
		if any(key in blocks for key in ("__proto__", "constructor", "prototype")):
			raise ValueError("cannot relabel prototype-sensitive block IDs")

		frequencies = {bid: 1 for bid in ids}
		slots = list(_block_slots(target))
		reserved = set()
		for container, key in slots:
			value = container[key]
			if value in frequencies:
				frequencies[value] += 1
			else:
				reserved.add(value)

		names = _identifier_names(reserved)
		ordered = sorted(ids, key=lambda bid: -frequencies[bid])
		mapping = {bid: next(names) for bid in ordered}

		if any(len(quote(name).encode("utf-8")) - 2 > 3 for name in mapping.values()):
			raise ValueError(
				"block ID minimum certificate supports names of at most 3 payload bytes"
			)

		maps[ti] = mapping
		for container, key in slots:
			container[key] = mapping.get(container[key], container[key])

		target["blocks"] = {mapping[bid]: blocks[bid] for bid in ids}
		for bid in ids:
			frequency = frequencies[bid]
			cost["before"] += frequency * (
				len(quote(bid).encode("utf-8", "backslashreplace")) - 2
			)
			cost["after"] += frequency * (len(quote(mapping[bid]).encode("utf-8")) - 2)
			cost["occurrences"] += frequency

		cost["identifiers"] += len(ids)

	return result, maps, cost


def restore_block_labels(original, result, *, copy_result=True):
	if len(original.get("targets", [])) != len(result.get("targets", [])):
		raise ValueError("target count changed")

	if copy_result:
		result = copy.deepcopy(result)

	for left, right in zip(original.get("targets", []), result.get("targets", [])):
		if "blocks" not in left and "blocks" not in right:
			continue

		if "blocks" not in left or "blocks" not in right:
			raise ValueError("block table presence changed")

		a, b = left.get("blocks", {}), right.get("blocks", {})
		if len(a) != len(b):
			raise ValueError("block count changed")

		if javascript_keys(a) != javascript_keys(b) and any(
			not bid
			or _index_key(bid)
			or bid in ("__proto__", "constructor", "prototype")
			or any(
				char in '<&"\t\r\n'
				or not (
					0x20 <= ord(char) <= 0xD7FF
					or 0xE000 <= ord(char) <= 0xFFFD
					or 0x10000 <= ord(char) <= 0x10FFFF
				)
				for char in bid
			)
			for bid in b
		):
			raise ValueError(
				"output block IDs violate editor XML or property-order contracts"
			)

		inverse = dict(zip(javascript_keys(b), javascript_keys(a)))

		for container, key in _block_slots(right):
			container[key] = inverse.get(container[key], container[key])

		restored = {inverse[bid]: block for bid, block in b.items()}
		right["blocks"] = {bid: restored[bid] for bid in a}

	return result
