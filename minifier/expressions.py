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


def normalize_numbers(project, stats, epsilon):
	changed = 0

	def rec(v):
		nonlocal changed

		if isinstance(v, bool):
			return v

		if isinstance(v, float):
			if math.isnan(v) or math.isinf(v):
				return v

			if v.is_integer() and not (v == 0 and str(v).startswith("-")):
				changed += 1
				return int(v)

			if round(v) != 0 and abs(v - round(v)) < epsilon:
				changed += 1
				return round(v)

			return v
		if isinstance(v, list):
			return [rec(x) for x in v]

		if isinstance(v, dict):
			return {k: rec(x) for k, x in v.items()}

		return v

	for i, target in enumerate(project.get("targets", [])):
		project["targets"][i] = rec(target)

	project["monitors"] = rec(project.get("monitors", []))
	stats["numbers_normalized"] += changed
	return changed


def _ffmpeg_available():
	try:
		r = subprocess.run(
			["ffmpeg", "-version"],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			timeout=5,
		)
		return r.returncode == 0
	except (OSError, subprocess.TimeoutExpired):
		return False


def _encode_wav_to_mp3(wav_bytes):
	try:
		proc = subprocess.run(
			[
				"ffmpeg",
				"-hide_banner",
				"-loglevel",
				"error",
				"-y",
				"-i",
				"pipe:0",
				"-c:a",
				"libmp3lame",
				"-b:a",
				WAV_TO_MP3_BITRATE,
				"-ar",
				str(WAV_TO_MP3_SAMPLE_RATE),
				"-ac",
				str(WAV_TO_MP3_CHANNELS),
				"-f",
				"mp3",
				"pipe:1",
			],
			input=wav_bytes,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=120,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None

	if proc.returncode != 0 or not proc.stdout:
		return None

	return proc.stdout


def convert_wav_sounds_to_mp3(project, assets, stats):
	if not _ffmpeg_available():
		stats["wav_ffmpeg_unavailable"] += 1
		return {}

	conversions = {}
	cache = {}  # source filename -> converted filename or None on failure

	for target in project.get("targets", []):
		for sound in target.get("sounds", []):
			if not isinstance(sound, dict) or sound.get("dataFormat") != "wav":
				continue

			old_name = sound.get("md5ext") or f"{sound.get('assetId')}.wav"

			if old_name in cache:
				new_name = cache[old_name]
				if new_name is None:
					continue

				sound["assetId"] = new_name.rsplit(".", 1)[0]
				sound["dataFormat"] = "mp3"
				sound["md5ext"] = new_name
				stats["wav_converted"] += 1
				continue

			wav_bytes = assets.get(old_name)

			if wav_bytes is None:
				cache[old_name] = None
				continue

			mp3_bytes = _encode_wav_to_mp3(wav_bytes)

			if mp3_bytes is None:
				cache[old_name] = None
				stats["wav_conversion_failed"] += 1
				continue

			new_name = f"{hashlib.md5(mp3_bytes).hexdigest()}.mp3"
			cache[old_name] = new_name
			conversions[old_name] = new_name
			assets[new_name] = mp3_bytes

			sound["assetId"] = new_name.rsplit(".", 1)[0]
			sound["dataFormat"] = "mp3"
			sound["md5ext"] = new_name
			stats["wav_converted"] += 1
			stats["wav_bytes_saved"] += len(wav_bytes) - len(mp3_bytes)

	for old_name in conversions:
		assets.pop(old_name, None)

	return conversions


def remove_sound_metadata(project, stats):
	removed = 0

	for target in project.get("targets", []):
		for sound in target.get("sounds", []):
			if not isinstance(sound, dict):
				continue

			for key in ("rate", "sampleCount"):
				if key in sound:
					del sound[key]
					removed += 1

	stats["sound_metadata_removed"] += removed
	return removed


def _asset_alias(name, mapping):
	seen = set()

	while isinstance(name, str) and name in mapping and name not in seen:
		seen.add(name)
		name = mapping[name]

	return name


def deduplicate_assets(project, assets, stats):
	if not assets:
		return {}

	referenced = Counter()
	for target in project.get("targets", []):
		for kind, key in (("costume", "costumes"), ("sound", "sounds")):
			for entry in target.get(key, []):
				name = _asset_filename(entry, kind)
				if name in assets:
					referenced[name] += 1

	buckets = {}
	all_extensions = _ASSET_EXTENSIONS["costume"] | _ASSET_EXTENSIONS["sound"]
	ext_compat = {
		"jpeg": "jpeg",
		"jpg": "jpeg",
		"wav": "wav",
		"wave": "wav",
	}

	for name, data in assets.items():
		if not isinstance(name, str) or "." not in name:
			continue

		ext = name.rsplit(".", 1)[1].lower()
		if ext not in all_extensions:
			continue

		kind = "costume" if ext in _ASSET_EXTENSIONS["costume"] else "sound"
		family = ext_compat.get(ext, ext)
		key = (kind, family, hashlib.md5(data).digest())
		buckets.setdefault(key, []).append(name)

	mapping = {}

	for names in buckets.values():
		if len(names) < 2:
			continue

		canonical = min(names, key=lambda n: (-referenced.get(n, 0), n))
		for duplicate in sorted(names):
			if duplicate != canonical:
				mapping[duplicate] = canonical

	for target in project.get("targets", []):
		for kind, key in (("costume", "costumes"), ("sound", "sounds")):
			for entry in target.get(key, []):
				if not isinstance(entry, dict):
					continue
				old_name = _asset_filename(entry, kind)
				new_name = _asset_alias(old_name, mapping)
				if old_name == new_name or new_name not in assets:
					continue
				stem, new_ext = new_name.rsplit(".", 1)
				entry["assetId"] = stem
				entry["dataFormat"] = new_ext.lower()
				if "md5ext" in entry:
					entry["md5ext"] = new_name

	removed_bytes = 0
	for old_name in mapping:
		data = assets.pop(old_name, None)
		if data is not None:
			removed_bytes += len(data)

	stats["assets_deduplicated"] += len(mapping)
	stats["asset_bytes_saved"] += removed_bytes
	return mapping


def compact_redundant_field_ids(project, stats):
	count = 0

	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue

			for value in (block.get("fields") or {}).values():
				if isinstance(value, list) and len(value) == 2 and value[1] is None:
					del value[1]
					count += 1

	stats["field_ids_compacted"] += count
	return count


def compact_mutation_hasnext(project, stats):
	count = 0

	for target in project.get("targets", []):
		for block in (target.get("blocks") or {}).values():
			if not isinstance(block, dict):
				continue

			mut = block.get("mutation")
			if not isinstance(mut, dict):
				continue

			if mut.get("hasnext") in (False, "false"):
				del mut["hasnext"]
				count += 1

	stats["mutation_hasnext_compacted"] += count
	return count


def compact_mutation_metadata(project, stats):
	count = 0

	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue

			mut = block.get("mutation")

			if not isinstance(mut, dict):
				continue

			for key in ("argumentids", "argumentnames", "argumentdefaults"):
				value = mut.get(key)
				if not isinstance(value, str):
					continue

				try:
					decoded = json.loads(value)
				except (TypeError, ValueError):
					continue

				if not isinstance(decoded, list):
					continue

				compact = json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)
				if compact != value:
					mut[key] = compact
					count += 1

	stats["mutation_metadata_compacted"] += count
	return count


def _resolve_variable_owner(project, ti, variable_id):
	targets = project.get("targets", [])

	if not isinstance(variable_id, str):
		return None

	if 0 <= ti < len(targets) and variable_id in (targets[ti].get("variables") or {}):
		return (ti, variable_id)

	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	if stage_index is not None and variable_id in (
		targets[stage_index].get("variables") or {}
	):
		return (stage_index, variable_id)

	return None


def _variable_literal(value):
	if isinstance(value, bool):
		return None

	if isinstance(value, (int, float)):
		if isinstance(value, float) and not math.isfinite(value):
			return None

		return [PRIMITIVE_NUMBER, value]

	if isinstance(value, str):
		return [PRIMITIVE_TEXT, value]

	return None


def _is_literal_value(value):
	if not (isinstance(value, list) and len(value) == 2 and value[0] == 1):
		return False

	literal = value[1]

	if not isinstance(literal, list) or len(literal) != 2:
		return False

	tag, raw = literal

	if tag in NUMERIC_TAGS:
		return (
			not isinstance(raw, bool)
			and isinstance(raw, (str, int, float))
			and (not isinstance(raw, float) or math.isfinite(raw))
		)

	return tag == PRIMITIVE_TEXT and isinstance(raw, str)


def _primitive_json_len(value):
	return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def get_constant_variables(project):
	targets = project.get("targets", [])
	set_blocks = {}
	reporter_counts = Counter()
	change_counts = Counter()
	setter_ids = {}
	setter_locations = {}
	literal_setter_values = {}
	literal_setters = set()

	def scan_input(value, ti):
		if not isinstance(value, list) or not value:
			if isinstance(value, dict):
				for child in value.values():
					scan_input(child, ti)
			return

		if (
			value[0] == PRIMITIVE_VARIABLE
			and len(value) > 2
			and isinstance(value[2], str)
		):
			owner = _resolve_variable_owner(project, ti, value[2])

			if owner is not None:
				reporter_counts[owner] += 1

		for child in value:
			if isinstance(child, (list, dict)):
				scan_input(child, ti)

	for ti, target in enumerate(targets):
		for bid, block in (target.get("blocks") or {}).items():
			if not isinstance(block, dict):
				continue

			fields = block.get("fields") or {}
			f = fields.get("VARIABLE")

			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				owner = _resolve_variable_owner(project, ti, f[1])
				if owner is not None:
					if block.get("opcode") == "data_setvariableto":
						set_blocks[owner] = set_blocks.get(owner, 0) + 1
						setter_ids.setdefault(owner, []).append(bid)
						setter_locations.setdefault(owner, (ti, bid))
						value = (block.get("inputs") or {}).get("VALUE")

						if _is_literal_value(value):
							literal_setter_values.setdefault(owner, []).append(
								copy.deepcopy(value[1])
							)
							literal_setters.add(owner)
					elif block.get("opcode") == "data_changevariableby":
						change_counts[owner] = change_counts.get(owner, 0) + 1

			for value in (block.get("inputs") or {}).values():
				scan_input(value, ti)

	candidates = []

	for owner, count in set_blocks.items():
		# DON'T FOLD a variable whose runtime value can differ from its initial value
		# Ex: a `set variable to 5` on an initially-0 variable is not constant
		if (
			count != 1
			or owner not in literal_setters
			or reporter_counts.get(owner, 0) <= 0
			or change_counts.get(owner, 0) > 0
		):
			continue

		setter_values = literal_setter_values.get(owner, [])

		if len(setter_values) != 1:
			continue

		ti, vid = owner
		entry = (targets[ti].get("variables") or {}).get(vid)

		if not isinstance(entry, list) or len(entry) < 2:
			continue

		# cloud variables are externally mutable -> not foldable
		if len(entry) >= 3 and entry[2] is True:
			continue

		if not _literal_storage_equal(setter_values[0], entry[1]):
			continue

		literal = _variable_literal(entry[1])

		if literal is None:
			continue

		old = [PRIMITIVE_VARIABLE, entry[0], vid]
		per_use = _primitive_json_len(old) - _primitive_json_len(literal)
		total = per_use * (reporter_counts[owner] + 1)  # include the setter as well
		scope = (
			"GLOBAL"
			if targets[ti].get("isStage")
			else f"local:{targets[ti].get('name')}"
		)
		candidates.append(
			{
				"ti": ti,
				"id": vid,
				"scope": scope,
				"name": str(entry[0]),
				"value": entry[1],
				"reporters": reporter_counts[owner],
				"bytes": total,
				"per_use": per_use,
				"changes": change_counts.get(owner, 0),
				"setter": setter_ids[owner][0],
				"setter_ti": setter_locations[owner][0],
				"literal": literal,
			}
		)

	candidates.sort(key=lambda c: (-c["bytes"], c["scope"], c["name"], c["id"]))
	return candidates


def _replace_constant_variable_reporters(value, ti, selected, project, stats) -> int:
	if not isinstance(value, list) or not value:
		if isinstance(value, dict):
			changed = 0

			for child in value.values():
				changed += _replace_constant_variable_reporters(
					child, ti, selected, project, stats
				)

			return changed

		return 0

	changed = 0

	if value[0] == PRIMITIVE_VARIABLE and len(value) > 2 and isinstance(value[2], str):
		owner = _resolve_variable_owner(project, ti, value[2])

		if owner in selected:
			literal = selected[owner]
			before = _primitive_json_len(value)
			after = _primitive_json_len(literal)
			value[:] = copy.deepcopy(literal)
			stats["constant_variable_reporters"] += 1
			stats["constant_variable_bytes_saved"] += before - after
			return 1

	for child in value:
		if isinstance(child, (list, dict)):
			changed += _replace_constant_variable_reporters(
				child, ti, selected, project, stats
			)

	return changed


def fold_constant_variables(project, selected, stats) -> int:
	if not selected:
		return 0

	selected_map = {(ti, vid): literal for (ti, vid), literal in selected.items()}
	changed = 0

	for ti, target in enumerate(project.get("targets", [])):
		for block in (target.get("blocks") or {}).values():
			if not isinstance(block, dict):
				continue

			for input_name, value in (block.get("inputs") or {}).items():
				if is_boolean_slot(block, input_name):
					continue  # would leave a literal in a boolean slot

				changed += _replace_constant_variable_reporters(
					value, ti, selected_map, project, stats
				)

	return changed


def _literal_storage_equal(literal, initial) -> bool:
	if not (isinstance(literal, list) and len(literal) == 2):
		return False

	tag, raw = literal

	if tag in NUMERIC_TAGS:
		return (
			not isinstance(raw, bool)
			and isinstance(raw, (int, float))
			and not isinstance(initial, bool)
			and isinstance(initial, (int, float))
			and math.isfinite(float(raw))
			and math.isfinite(float(initial))
			and float(raw) == float(initial)
		)

	if tag == PRIMITIVE_TEXT:
		return isinstance(raw, str) and isinstance(initial, str) and raw == initial

	return False


def _setter_matches_initial(block, initial) -> bool:
	value = (block.get("inputs") or {}).get("VALUE")

	if not _is_literal_value(value):
		return False

	return _literal_storage_equal(value[1], initial)


def remove_constant_variable_setters(project, setters, stats, opts):
	targets = project.get("targets", [])
	opts.removed_variable_setter_blocks = [set() for _ in targets]
	opts.variable_setter_link_edits = {}  # (ti, block, "next"|"parent") -> new value
	opts.variable_setter_input_edits = {}  # (ti, block, input name) -> new value
	removed = kept = 0

	for (owner_ti, vid), (ti, bid) in setters.items():
		target = targets[ti]
		blocks = target.get("blocks") or {}
		block = blocks.get(bid)
		entry = (targets[owner_ti].get("variables") or {}).get(vid)
		commented = {
			c.get("blockId")
			for c in (target.get("comments") or {}).values()
			if isinstance(c, dict)
		}

		if (
			not isinstance(block, dict)
			or block.get("opcode") != "data_setvariableto"
			or not isinstance(entry, list)
			or len(entry) < 2
			or not _setter_matches_initial(block, entry[1])
			or "comment" in block
			or bid in commented
		):
			kept += 1
			continue

		parent, nxt = block.get("parent"), block.get("next")

		if nxt is not None and not isinstance(blocks.get(nxt), dict):
			kept += 1
			continue

		input_edit = None  # (parent block id, input name, new value)

		if parent is None:
			if nxt is not None:
				kept += 1
				continue
		else:
			pblock = blocks.get(parent)

			if not isinstance(pblock, dict):
				kept += 1
				continue

			if pblock.get("next") != bid:
				found = None

				for name, value in (pblock.get("inputs") or {}).items():
					if (
						isinstance(value, list)
						and len(value) > 1
						and value[0]
						in (
							INPUT_SAME_BLOCK_SHADOW,
							INPUT_BLOCK_NO_SHADOW,
							INPUT_DIFF_BLOCK_SHADOW,
						)
						and value[1] == bid
					):
						found = (name, value)
						break

				if found is None:
					kept += 1
					continue

				name, value = found

				if nxt is None:
					new_value = [INPUT_BLOCK_NO_SHADOW, None]  # empty substack
				else:
					new_value = copy.deepcopy(value)
					new_value[1] = nxt

				input_edit = (name, new_value)

		if parent is not None:
			pblock = blocks[parent]

			if input_edit is None:
				pblock["next"] = nxt
				opts.variable_setter_link_edits[(ti, parent, "next")] = nxt
			else:
				pblock["inputs"][input_edit[0]] = input_edit[1]
				opts.variable_setter_input_edits[(ti, parent, input_edit[0])] = (
					copy.deepcopy(input_edit[1])
				)

			if nxt is not None:
				blocks[nxt]["parent"] = parent
				opts.variable_setter_link_edits[(ti, nxt, "parent")] = parent

		del blocks[bid]
		opts.removed_variable_setter_blocks[ti].add(bid)
		removed += 1

	stats["constant_variable_setters_removed"] += removed
	stats["constant_variable_setters_kept"] += kept
	return removed


def _to_scratch_number(val) -> float:
	if isinstance(val, bool):
		return 1.0 * val

	if isinstance(val, (int, float)):
		return float(val)

	if isinstance(val, str):
		text = val.strip()
		if not text:
			return 0.0
		lower = text.lower()
		if lower in ("+infinity", "infinity"):
			return float("inf")
		if lower == "-infinity":
			return float("-inf")

		prefixes = (
			("0b", 2),
			("0o", 8),
			("0x", 16),
		)

		for prefix, base in prefixes:
			if lower.startswith(prefix):
				digits = lower[2:]
				if not digits:
					return 0.0
				try:
					return float(int(digits, base))
				except (ValueError, OverflowError):
					return 0.0

		try:
			num = float(text)
		except (ValueError, OverflowError, TypeError):
			return 0.0

		return 0.0 if math.isnan(num) else num

	return 0.0


def _to_scratch_bool(val) -> bool:
	if isinstance(val, bool):
		return val

	if isinstance(val, str):
		if val == "" or val == "0" or val.lower() == "false":
			return False

		return True

	if isinstance(val, (int, float)):
		return val != 0 and not (isinstance(val, float) and math.isnan(val))

	return bool(val)


def _scratch_string(val) -> str:
	if isinstance(val, bool):
		return str(val).lower()

	if isinstance(val, str):
		return val

	if isinstance(val, (int, float)):
		if isinstance(val, float):
			if math.isnan(val):
				return "NaN"

			if math.isinf(val):
				return "Infinity" if val > 0 else "-Infinity"

			if val == 0:
				return "0"

			if val.is_integer():
				return str(int(val))

		return str(val)

	return str(val)


def _constant(kind, value) -> tuple:
	return (kind, value)


def constant_to_number(value):
	if value is None:
		return None

	kind, raw = value

	if kind == "number":
		return float(raw)

	if kind == "bool":
		return 1.0 if raw else 0.0

	if kind == "string":
		return _to_scratch_number(raw)

	return None


def _constant_to_bool(value):
	if value is None:
		return None

	kind, raw = value
	return _to_scratch_bool(raw)


def _constant_to_string(value):
	if value is None:
		return None

	return _scratch_string(value[1])


def _scratch_compare(left, right):
	left_raw = left[1]
	right_raw = right[1]
	n1 = constant_to_number(left)
	n2 = constant_to_number(right)

	if n1 == 0 and isinstance(left_raw, str) and left_raw.strip() == "":
		n1 = float("nan")
	elif n2 == 0 and isinstance(right_raw, str) and right_raw.strip() == "":
		n2 = float("nan")

	if math.isnan(n1) or math.isnan(n2):
		s1 = _scratch_string(left).lower()
		s2 = _scratch_string(right).lower()
		return (s1 > s2) - (s1 < s2)

	if (math.isinf(n1) or math.isinf(n2)) and n1 == n2:
		return 0

	return (n1 > n2) - (n1 < n2)


def _scratch_mod(a, b):
	if b == 0:
		return None

	try:
		result = math.fmod(a, b)
	except (ValueError, OverflowError, ZeroDivisionError):
		return None

	if result / b < 0:
		result += b
	return result


def _scratch_round(n):
	if not math.isfinite(n):
		return None

	result = math.floor(n + 0.5)
	return float(result)


def _utf16_length(text):
	return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_char_at(text, index):
	if index < 0:
		return ""

	data = text.encode("utf-16-le", "surrogatepass")
	start = index * 2

	if start >= len(data):
		return ""

	return data[start : start + 2].decode("utf-16-le", "surrogatepass")


def _folded_literal_input(value):
	if not (isinstance(value, list) and len(value) > 1 and value[0] in INPUT_TAGS):
		return None

	child = value[1]

	if value[0] == INPUT_DIFF_BLOCK_SHADOW and isinstance(child, str):
		# block reference
		return None

	if isinstance(child, list) and len(child) == 2:
		tag, raw = child
		if (
			tag in NUMERIC_TAGS
			and isinstance(raw, (int, float, str))
			and not isinstance(raw, bool)
		):
			num = _to_scratch_number(raw)
			if isinstance(raw, str) and _scratch_string(num) != raw:
				# Scratch keeps the raw text, so compare/join must see the string
				return _constant("string", raw)
			return _constant("number", num)

		if tag == PRIMITIVE_TEXT and isinstance(raw, str):
			return _constant("string", raw)

	return None


def constexpr_from_input(value, blocks, visiting=frozenset()):
	if not (isinstance(value, list) and len(value) > 1):
		return None

	if value[0] in INPUT_TAGS:
		child = value[1]
	else:
		return None

	literal = _folded_literal_input(value)
	if literal is not None:
		return literal, set()

	if isinstance(child, str):
		return _constant_expression_block(child, blocks, visiting)

	return None


def _constant_expression_block(block_id, blocks, visiting):
	if block_id in visiting:
		return None

	block = blocks.get(block_id)
	if not isinstance(block, dict):
		return None

	operation = block.get("opcode")
	if operation is None:
		return None

	inputs = block.get("inputs") or {}
	current_visiting = visiting | {block_id}

	def get_in(name):
		return constexpr_from_input(inputs.get(name), blocks, current_visiting)

	match operation:
		case "operator_not":
			operand = get_in("OPERAND")

			if operand is None:
				return None

			res = not _constant_to_bool(operand[0])
			return _constant("bool", res), operand[1] | {block_id}
		case "operator_and" | "operator_or":
			left = get_in("OPERAND1")

			if left is None:
				return None

			left_bool = _constant_to_bool(left[0])

			if operation == "operator_and" and not left_bool:
				return _constant("bool", False), left[1] | {block_id}

			if operation == "operator_or" and left_bool:
				return _constant("bool", True), left[1] | {block_id}

			right = get_in("OPERAND2")

			if right is None:
				return None

			result = (
				left_bool and _constant_to_bool(right[0])
				if operation == "operator_and"
				else left_bool or _constant_to_bool(right[0])
			)
			return _constant("bool", result), left[1] | right[1] | {block_id}
		case "operator_gt" | "operator_lt" | "operator_equals":
			left = get_in("OPERAND1")
			right = get_in("OPERAND2")

			if left is None or right is None:
				return None

			cmp = _scratch_compare(left[0], right[0])

			if operation == "operator_gt":
				result = cmp > 0
			elif operation == "operator_lt":
				result = cmp < 0
			else:
				result = cmp == 0

			return _constant("bool", result), left[1] | right[1] | {block_id}
		case "operator_join":
			left = get_in("STRING1")
			right = get_in("STRING2")

			if left is None or right is None:
				return None

			return _constant(
				"string", _constant_to_string(left[0]) + _constant_to_string(right[0])
			), left[1] | right[1] | {block_id}
		case "operator_letter_of":
			letter = get_in("LETTER")
			string = get_in("STRING")

			if letter is None or string is None:
				return None

			n = constant_to_number(letter[0])

			if n is None or not math.isfinite(n):
				return None

			index = math.trunc(n) - 1
			text = _constant_to_string(string[0])
			return _constant("string", _utf16_char_at(text, index)), letter[1] | string[
				1
			] | {block_id}
		case "operator_length":
			operand = get_in("STRING")

			if operand is None:
				return None

			text = _constant_to_string(operand[0])
			return _constant("number", _utf16_length(text)), operand[1] | {block_id}
		case "operator_contains":
			left = get_in("STRING1")
			right = get_in("STRING2")

			if left is None or right is None:
				return None

			return _constant(
				"bool",
				_constant_to_string(right[0]).lower()
				in _constant_to_string(left[0]).lower(),
			), left[1] | right[1] | {block_id}
		case "operator_round":
			operand = get_in("NUM")

			if operand is None:
				return None

			n = constant_to_number(operand[0])

			if n is None:
				return None

			result = _scratch_round(n)

			if result is None:
				return None

			return _constant("number", result), operand[1] | {block_id}
		case "operator_mathop":
			operand = get_in("NUM")

			if operand is None:
				return None

			num = constant_to_number(operand[0])

			if num is None:
				return None

			operator_field = (block.get("fields") or {}).get("OPERATOR")
			op_name = (
				operator_field[0]
				if isinstance(operator_field, list)
				and operator_field
				and isinstance(operator_field[0], str)
				else None
			)

			if op_name is None:
				return None

			op_name = op_name.lower()
			try:
				match op_name:
					case "abs":
						res = abs(num)
					case "floor":
						res = math.floor(num)
					case "ceiling":
						res = math.ceil(num)
					case "sqrt":
						if num < 0:
							return "NaN"
						res = math.sqrt(num)
					case "sin":
						res = float(f"{math.sin(math.radians(num)):.10f}")
					case "cos":
						res = float(f"{math.cos(math.radians(num)):.10f}")
					case "tan":
						angle = num % 360
						if angle == 90:
							return None
						if angle == 270:
							return None
						res = float(f"{math.tan(math.radians(angle)):.10f}")
					case "asin":
						if not -1 <= num <= 1:
							return None
						res = math.degrees(math.asin(num))
					case "acos":
						if not -1 <= num <= 1:
							return None
						res = math.degrees(math.acos(num))
					case "atan":
						res = math.degrees(math.atan(num))
					case "ln":
						if num <= 0:
							return None
						res = math.log(num)
					case "log":
						if num <= 0:
							return None
						res = math.log10(num)
					case "e ^":
						res = math.exp(num)
					case "10 ^":
						res = 10**num
					case _:
						return None
			except (ValueError, OverflowError, ZeroDivisionError):
				return None

			if not math.isfinite(res):
				return None

			return _constant("number", res), operand[1] | {block_id}

	# binary operators
	left = get_in("NUM1")
	right = get_in("NUM2")

	if left is None or right is None:
		return None

	left_num = constant_to_number(left[0])
	right_num = constant_to_number(right[0])

	if left_num is None or right_num is None:
		return None

	try:
		match operation:
			case "operator_add":
				result = left_num + right_num
			case "operator_subtract":
				result = left_num - right_num
			case "operator_multiply":
				result = left_num * right_num
			case "operator_divide":
				if right_num == 0:
					return None
				result = left_num / right_num
			case "operator_mod":
				result = _scratch_mod(left_num, right_num)
				if result is None:
					return None
			case _:
				return None
	except (OverflowError, ValueError, ZeroDivisionError):
		return None

	if not math.isfinite(result):
		return None

	return _constant("number", result), left[1] | right[1] | {block_id}


def _input_block_ids(value, blocks):
	return {ref for ref in iter_input_block_refs(value) if ref in blocks}


def _exclusive_input_block_subtree(value, blocks, owner_id, graph=None):
	roots = direct_input_block_refs(value, blocks)

	if not roots:
		return set()

	if graph is None:
		graph = ProjectGraph({"blocks": blocks})

	candidate = set()
	stack = list(roots)

	while stack:
		bid = stack.pop()

		if bid in candidate or bid not in blocks:
			continue

		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		candidate.add(bid)

		if block.get("topLevel") is True:
			return None

		stack.extend(graph.edges.get(bid, ()))

	for bid in candidate:
		owners = graph.parents.get(bid, ())

		if bid in roots:
			if any(owner_id != owner for owner in owners):
				return None

			if owner_id not in owners:
				return None

		else:
			if any(owner not in candidate for owner in owners):
				return None

	return candidate


def _reparent_serialized_input(value, blocks, parent_id):
	for bid in direct_input_block_refs(value, blocks):
		block = blocks.get(bid)
		if isinstance(block, dict):
			block["parent"] = parent_id


def _sequence_primary_input_block_id(value, blocks):
	if not (isinstance(value, list) and value):
		return None

	if len(value) > 1:
		ref = value[1]

		if isinstance(ref, str) and ref in blocks:
			return ref

		if isinstance(ref, (list, dict)):
			return _sequence_primary_input_block_id(ref, blocks)

	return None


def _variable_reporter_id(value, blocks):
	if not isinstance(value, list) or not value:
		return None

	tag = value[0]

	if tag == PRIMITIVE_VARIABLE:
		if len(value) > 2 and isinstance(value[2], str) and value[2]:
			return value[2]

		return None

	if tag not in INPUT_TAGS or len(value) <= 1:
		return None

	primary = value[1]

	if isinstance(primary, list):
		return _variable_reporter_id(primary, blocks)

	if not isinstance(primary, str):
		return None

	block = blocks.get(primary)

	if not isinstance(block, dict) or block.get("opcode") != "data_variable":
		return None

	field = (block.get("fields") or {}).get("VARIABLE")

	if (
		isinstance(field, list)
		and len(field) > 1
		and isinstance(field[1], str)
		and field[1]
	):
		return field[1]
	return None


def _simplify_setter_rhs_blocks(project, stats, opts):
	targets = project.get("targets", [])
	opts.simplified_block_removed_blocks = [set() for _ in targets]
	opts.simplified_block_link_edits = {}
	opts.simplified_block_input_edits = {}
	opts.simplified_block_opcode_edits = {}
	total = add_count = subtract_count = removed_count = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = ProjectGraph(target)
		comments = target.get("comments") or {}
		commented_ids = {
			c.get("blockId")
			for c in comments.values()
			if isinstance(c, dict) and isinstance(c.get("blockId"), str)
		}
		commented_ids.update(
			bid
			for bid, block in blocks.items()
			if isinstance(block, dict) and "comment" in block
		)

		for set_id, set_block in list(blocks.items()):
			if not isinstance(set_block, dict):
				continue

			if set_block.get("opcode") != "data_setvariableto":
				continue

			fields = set_block.get("fields") or {}
			var_field = fields.get("VARIABLE")

			if not (
				isinstance(var_field, list)
				and len(var_field) > 1
				and isinstance(var_field[1], str)
			):
				continue

			var_id = var_field[1]
			inputs = set_block.get("inputs") or {}
			rhs_value = inputs.get("VALUE")
			rhs_id = _sequence_primary_input_block_id(rhs_value, blocks)

			if rhs_id is None or rhs_id == set_id:
				continue

			rhs = blocks.get(rhs_id)

			if not isinstance(rhs, dict):
				continue

			operator = rhs.get("opcode")

			if operator not in ("operator_add", "operator_subtract"):
				continue

			if rhs.get("next") is not None:
				continue

			rhs_inputs = rhs.get("inputs") or {}
			left_raw = rhs_inputs.get("NUM1")
			right_raw = rhs_inputs.get("NUM2")

			if left_raw is None or right_raw is None:
				continue

			if _variable_reporter_id(left_raw, blocks) != var_id:
				continue

			if rhs_id in commented_ids:
				continue

			rhs_closure = _exclusive_input_block_subtree(
				rhs_value, blocks, set_id, graph=graph
			)

			if rhs_closure is None or rhs_id not in rhs_closure:
				continue

			left_closure = _exclusive_input_block_subtree(
				left_raw, blocks, rhs_id, graph=graph
			)

			if left_closure is None:
				continue

			right_closure = _exclusive_input_block_subtree(
				right_raw, blocks, rhs_id, graph=graph
			)

			if right_closure is None:
				continue

			left_to_remove = left_closure - right_closure

			if any(bid in commented_ids for bid in left_to_remove):
				continue

			if operator == "operator_add":
				new_value = copy.deepcopy(right_raw)
				for child_id in _input_block_ids(new_value, blocks):
					child = blocks.get(child_id)
					if isinstance(child, dict):
						opts.simplified_block_link_edits[(ti, child_id, "parent")] = (
							set_id
						)
						child["parent"] = set_id
				_reparent_serialized_input(new_value, blocks, set_id)

				removed = {rhs_id} | left_to_remove
				set_block["opcode"] = "data_changevariableby"
				opts.simplified_block_opcode_edits[(ti, set_id)] = (
					"data_changevariableby"
				)
				inputs["VALUE"] = new_value
				opts.simplified_block_input_edits[(ti, set_id, "VALUE")] = (
					copy.deepcopy(new_value)
				)

				for bid in removed:
					blocks.pop(bid, None)
				opts.simplified_block_removed_blocks[ti].update(removed)
				graph.refresh_after_mutation(changed={set_id}, removed=removed)

				total += 1
				add_count += 1
				removed_count += len(removed)
				continue

			# operator_subtract
			if _is_literal_value(right_raw):
				raw_literal = right_raw[1]
				number = _to_scratch_number(raw_literal[1])
				if not math.isfinite(number):
					continue
				number = -number
				if number == 0:
					number = 0
				else:
					number = int(number) if float(number).is_integer() else number

				new_value = [INPUT_SAME_BLOCK_SHADOW, [PRIMITIVE_NUMBER, number]]
				removed = {rhs_id} | left_to_remove
				set_block["opcode"] = "data_changevariableby"
				opts.simplified_block_opcode_edits[(ti, set_id)] = (
					"data_changevariableby"
				)
				inputs["VALUE"] = new_value
				opts.simplified_block_input_edits[(ti, set_id, "VALUE")] = (
					copy.deepcopy(new_value)
				)

				for bid in removed:
					blocks.pop(bid, None)
				opts.simplified_block_removed_blocks[ti].update(removed)
				graph.refresh_after_mutation(changed={set_id}, removed=removed)

				total += 1
				subtract_count += 1
				removed_count += len(removed)
				continue

			# set x to (x - a) -> change x by (0 - a)
			new_left = [INPUT_SAME_BLOCK_SHADOW, [PRIMITIVE_NUMBER, 0]]
			rhs_inputs["NUM1"] = new_left
			opts.simplified_block_input_edits[(ti, rhs_id, "NUM1")] = copy.deepcopy(
				new_left
			)

			removed = left_to_remove
			set_block["opcode"] = "data_changevariableby"
			opts.simplified_block_opcode_edits[(ti, set_id)] = "data_changevariableby"
			if removed:
				for bid in removed:
					blocks.pop(bid, None)
				opts.simplified_block_removed_blocks[ti].update(removed)
			graph.refresh_after_mutation(changed={set_id, rhs_id}, removed=removed)

			total += 1
			subtract_count += 1
			removed_count += len(removed)

	stats["blocks_simplified"] += total
	stats["set_to_change_add"] += add_count
	stats["set_to_change_subtract"] += subtract_count
	stats["simplify_blocks_removed"] += removed_count
	return total


def get_linear_var_offset(raw, blocks):
	variable_id = _variable_reporter_id(raw, blocks)
	root_id = _sequence_primary_input_block_id(raw, blocks)

	if variable_id is not None:
		if root_id is None:
			return variable_id, 0.0
		
		root = blocks.get(root_id)

		if isinstance(root, dict) and root.get("opcode") == "data_variable":
			return variable_id, 0.0

	if root_id is None:
		return None

	root = blocks.get(root_id)

	if not isinstance(root, dict) or root.get("next") is not None:
		return None
	
	operator = root.get("opcode")

	if operator not in ("operator_add", "operator_subtract"):
		return None

	inputs = root.get("inputs") or {}
	left_raw = inputs.get("NUM1")
	right_raw = inputs.get("NUM2")

	if left_raw is None or right_raw is None:
		return None

	def numeric_literal(value):
		if not _is_literal_value(value):
			return None
		
		literal = value[1]

		if not isinstance(literal, list) or len(literal) != 2:
			return None
		
		number = _to_scratch_number(literal[1])
		return number if math.isfinite(number) else None

	left_variable = _variable_reporter_id(left_raw, blocks)
	right_variable = _variable_reporter_id(right_raw, blocks)
	left_number = numeric_literal(left_raw)
	right_number = numeric_literal(right_raw)

	if operator == "operator_add":
		if left_variable is not None and right_number is not None:
			return left_variable, right_number
		
		if right_variable is not None and left_number is not None:
			return right_variable, left_number
		
		return None

	# subtraction is not commutative, accept only a - n
	if left_variable is not None and right_number is not None:
		return left_variable, -right_number
	return None


def _variable_write_id(block):
	if not isinstance(block, dict) or block.get("opcode") not in (
		"data_setvariableto",
		"data_changevariableby",
		"control_for_each",
	):
		return None
	field = (block.get("fields") or {}).get("VARIABLE")
	if isinstance(field, list) and len(field) > 1 and isinstance(field[1], str):
		return field[1]
	return None


def _project_variable_writers(project):
	writers = defaultdict(set)
	for ti, target in enumerate(project.get("targets", [])):
		for bid, block in (target.get("blocks") or {}).items():
			var_id = _variable_write_id(block)
			if var_id is not None:
				writers[var_id].add((ti, bid))
	return writers


def next_chains(target):
	blocks = target.get("blocks") or {}
	for root_id, root in list(blocks.items()):
		if not (
			isinstance(root, dict)
			and root.get("topLevel") is True
			and root.get("parent") is None
		):
			continue

		chain = []
		seen = set()
		bid = root_id

		while isinstance(bid, str) and bid in blocks and bid not in seen:
			seen.add(bid)
			chain.append(bid)
			block = blocks.get(bid)
			bid = block.get("next") if isinstance(block, dict) else None

		if chain:
			yield chain


def _numeric_literal_input(raw):
	if not _is_literal_value(raw):
		return None
	
	literal = raw[1]

	if not isinstance(literal, list) or len(literal) != 2:
		return None
	
	number = _to_scratch_number(literal[1])
	return number if math.isfinite(number) else None


def _reduce_repeated_assignment_deltas(project, stats, opts):
	targets = project.get("targets", [])
	writers = _project_variable_writers(project)
	rewrites = removed_count = 0
	chains_by_target = []
	chain_by_block = {}
	ambiguous_blocks = set()

	for ti, target in enumerate(targets):
		target_chains = []

		for chain_index, chain in enumerate(next_chains(target)):
			token = (ti, chain_index)
			target_chains.append((token, chain))

			for bid in chain:
				location = (ti, bid)

				if location in chain_by_block and chain_by_block[location] != token:
					ambiguous_blocks.add(location)
				else:
					chain_by_block[location] = token

		chains_by_target.append(target_chains)

	for location in ambiguous_blocks:
		chain_by_block.pop(location, None)

	variable_writer_chain = {}

	for var_id, locations in writers.items():
		tokens = set()

		for location in locations:
			token = chain_by_block.get(location)

			if token is None:
				break

			tokens.add(token)

		if token is not None and len(tokens) == 1:
			variable_writer_chain[var_id] = next(iter(tokens))

	if not hasattr(opts, "simplified_block_removed_blocks") or len(
		opts.simplified_block_removed_blocks
	) != len(targets):
		opts.simplified_block_removed_blocks = [set() for _ in targets]
	if not hasattr(opts, "simplified_block_link_edits"):
		opts.simplified_block_link_edits = {}
	if not hasattr(opts, "simplified_block_input_edits"):
		opts.simplified_block_input_edits = {}
	if not hasattr(opts, "simplified_block_opcode_edits"):
		opts.simplified_block_opcode_edits = {}

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = ProjectGraph(target)
		commented_ids = {
			comment.get("blockId")
			for comment in (target.get("comments") or {}).values()
			if isinstance(comment, dict) and isinstance(comment.get("blockId"), str)
		}
		commented_ids.update(
			bid
			for bid, block in blocks.items()
			if isinstance(block, dict) and "comment" in block
		)

		for chain_token, chain in chains_by_target[ti]:
			last_write_index = {}
			last_write_id = {}
			effective_forms = {}

			def writers_confined_to_chain(var_id):
				return not writers.get(var_id) or variable_writer_chain.get(var_id) == chain_token

			for index, bid in enumerate(chain):
				block = blocks.get(bid)
				if not isinstance(block, dict):
					continue

				opcode = block.get("opcode")
				var_id = _variable_write_id(block)
				if var_id is None:
					continue

				current_form = None
				if opcode == "data_setvariableto":
					current_form = get_linear_var_offset(
						(block.get("inputs") or {}).get("VALUE"), blocks
					)

				previous_id = last_write_id.get(var_id)
				previous_index = last_write_index.get(var_id)
				previous_form = effective_forms.get(previous_id)

				if current_form is not None and previous_form is not None:
					base_id, current_offset = current_form
					previous_base_id, previous_offset = previous_form
					can_rewrite = (
						previous_index is not None
						and base_id == previous_base_id
						and base_id != var_id
						and last_write_index.get(base_id, -1) <= previous_index
						and writers_confined_to_chain(var_id)
						and writers_confined_to_chain(base_id)
					)
					if can_rewrite:
						delta = current_offset - previous_offset
						if math.isfinite(delta) and delta != 0:
							value_raw = (block.get("inputs") or {}).get("VALUE")
							owned = _exclusive_input_block_subtree(
								value_raw, blocks, bid, graph=graph
							)

							if owned and not any(
								removed_id in commented_ids for removed_id in owned
							):
								if all(
									isinstance(blocks.get(removed_id), dict)
									and blocks[removed_id].get("opcode")
									in ("data_variable", "operator_add", "operator_subtract")
									and blocks[removed_id].get("next") is None
									for removed_id in owned
								):
									delta_value = (
										int(delta)
										if float(delta).is_integer()
										else delta
									)
									new_value = [
										INPUT_SAME_BLOCK_SHADOW,
										[PRIMITIVE_NUMBER, delta_value],
									]
									block["opcode"] = "data_changevariableby"
									block.setdefault("inputs", {})["VALUE"] = new_value
									opts.simplified_block_opcode_edits[(ti, bid)] = (
										"data_changevariableby"
									)
									opts.simplified_block_input_edits[(ti, bid, "VALUE")] = (
										copy.deepcopy(new_value)
									)

									for removed_id in owned:
										blocks.pop(removed_id, None)

									opts.simplified_block_removed_blocks[ti].update(owned)
									graph.refresh_after_mutation(changed={bid}, removed=owned)
									rewrites += 1
									removed_count += len(owned)

				if opcode == "data_setvariableto":
					effective_forms[bid] = current_form
				elif opcode == "data_changevariableby":
					change = _numeric_literal_input(
						(block.get("inputs") or {}).get("VALUE")
					)

					if (
						previous_form is not None
						and previous_index is not None
						and change is not None
						and previous_form[0] != var_id
						and last_write_index.get(previous_form[0], -1) <= previous_index
					):
						effective_forms[bid] = (
							previous_form[0], previous_form[1] + change
						)
					else:
						effective_forms[bid] = None
				else:
					effective_forms[bid] = None

				last_write_index[var_id] = index
				last_write_id[var_id] = bid

	stats["blocks_simplified"] += rewrites
	stats["set_to_change_delta"] += rewrites
	stats["simplify_blocks_removed"] += removed_count
	stats["assignment_delta_blocks_removed"] += removed_count
	return rewrites


def get_subtree_block_ids(raw, blocks):
	out = set()
	stack = list(_input_block_ids(raw, blocks))

	while stack:
		bid = stack.pop()

		if bid in out:
			continue

		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		out.add(bid)
		nxt = block.get("next")

		if isinstance(nxt, str) and nxt in blocks:
			stack.append(nxt)

		for v in (block.get("inputs") or {}).values():
			stack.extend(_input_block_ids(v, blocks))

	return out


def _expand_removed_closure(ids, blocks, keep=frozenset()):
	out = set(ids)
	stack = list(out)

	while stack:
		bid = stack.pop()
		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		children = []
		nxt = block.get("next")

		if isinstance(nxt, str) and nxt in blocks:
			children.append(nxt)

		for v in (block.get("inputs") or {}).values():
			children.extend(_input_block_ids(v, blocks))

		for child in children:
			if child in out or child in keep:
				continue

			out.add(child)
			stack.append(child)

	return out


def short_circuit(value, blocks, parent_id=None, incoming_refs=None, graph=None):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	block_id = value[1]

	if not isinstance(block_id, str) or block_id not in blocks:
		return None

	block = blocks.get(block_id)

	if not isinstance(block, dict) or block.get("opcode") not in (
		"operator_and",
		"operator_or",
	):
		return None

	inputs = block.get("inputs") or {}
	left_raw = inputs.get("OPERAND1")
	right_raw = inputs.get("OPERAND2")

	if left_raw is None or right_raw is None:
		return None

	left = constexpr_from_input(left_raw, blocks)
	right = constexpr_from_input(right_raw, blocks)
	left_bool = _constant_to_bool(left[0]) if left is not None else None
	right_bool = _constant_to_bool(right[0]) if right is not None else None
	removed_base = {block_id}
	operator = block.get("opcode")

	def discard(raw, constant_result, constant_ids):
		dead_ids = (
			get_exclusive_input_subtree(
				raw, blocks, block_id, incoming_refs, graph=graph
			)
			if graph is not None and incoming_refs is not None
			else _exclusive_input_block_subtree(raw, blocks, block_id)
		)

		if dead_ids is None:
			return None

		replacement, _ = constant_to_input(
			_constant("bool", constant_result), blocks, parent_id or block_id
		)
		return replacement, _expand_removed_closure(
			removed_base | constant_ids | dead_ids, blocks
		)

	def preserve(raw, constant_ids):
		owned_ids = (
			get_exclusive_input_subtree(
				raw, blocks, block_id, incoming_refs, graph=graph
			)
			if graph is not None and incoming_refs is not None
			else _exclusive_input_block_subtree(raw, blocks, block_id)
		)

		if owned_ids is None:
			return None

		return copy.deepcopy(raw), _expand_removed_closure(
			removed_base | constant_ids, blocks, keep=get_subtree_block_ids(raw, blocks)
		)

	if operator == "operator_and":
		if left is not None and left_bool is False:
			return discard(right_raw, False, left[1])

		if right is not None and right_bool is False:
			return discard(left_raw, False, right[1])

		if left is not None and left_bool is True and right is None:
			return preserve(right_raw, left[1])

		if right is not None and right_bool is True and left is None:
			return preserve(left_raw, right[1])
	else:
		if left is not None and left_bool is True:
			return discard(right_raw, True, left[1])

		if right is not None and right_bool is True:
			return discard(left_raw, True, right[1])

		if left is not None and left_bool is False and right is None:
			return preserve(right_raw, left[1])

		if right is not None and right_bool is False and left is None:
			return preserve(left_raw, right[1])

	return None


def remap_block_input(owner_value, block_id):
	if (
		isinstance(owner_value, list)
		and owner_value[0] == INPUT_DIFF_BLOCK_SHADOW
		and len(owner_value) > 2
	):
		return [INPUT_DIFF_BLOCK_SHADOW, block_id, copy.deepcopy(owner_value[2])]

	return [INPUT_BLOCK_NO_SHADOW, block_id]


def get_block_parts(raw, blocks, opcode=None):
	block_id = raw[1]
	if block_id is None:
		return None

	block = blocks[block_id]
	op = block.get("opcode")
	if op not in ("operator_and", "operator_or") or (
		opcode is not None and op != opcode
	):
		return None

	if block.get("next") is not None:
		return None

	inputs = block.get("inputs") or {}
	left, right = inputs.get("OPERAND1"), inputs.get("OPERAND2")
	if left is None or right is None:
		return None

	return block_id, block, left, right


def simplify_boolean_identities(
	value, blocks, parent_id=None, incoming_refs=None, graph=None, slot_is_boolean=False
):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	block_id: str = value[1]

	if not isinstance(block_id, str) or block_id not in blocks:
		return None

	block: dict = blocks.get(block_id)

	if not isinstance(block, dict) or block.get("opcode") not in (
		"operator_and",
		"operator_or",
	):
		return None

	inputs: dict = block.get("inputs") or {}
	left_raw: list = inputs.get("OPERAND1")
	right_raw: list = inputs.get("OPERAND2")

	if left_raw is None or right_raw is None:
		return None

	closure: set | None = get_exclusive_input_subtree(
		value, blocks, parent_id, incoming_refs, graph=graph
	)

	if closure is None or block_id not in closure:
		return None

	if not condition_is_pure(value, blocks, graph):
		return None

	dual: str = "operator_and" if block["opcode"] == "operator_or" else "operator_or"
	closures: dict = {}

	def closure_of(raw) -> set:
		key: int = id(raw)
		if key not in closures:
			closures[key] = get_subtree_block_ids(raw, blocks)
		return closures[key]

	def same(a, b) -> bool:
		return branch_factor_value_equal(
			a, b, blocks, closure_of(a), closure_of(b), {}, set()
		)

	def keepable(raw) -> bool:
		return slot_is_boolean or value_is_boolean(raw, blocks)

	def replace_with(law, kept_raw) -> dict[str, Any]:
		removed = closure - closure_of(kept_raw)
		if block_id not in removed or not removed:
			return None

		return {
			"law": law,
			"replacement": remap_block_input(value, kept_raw[1]),
			"removed": removed,
			"touched": set(removed),
		}

	def replace_with_boolean(law, result: bool):
		removed = set(closure)

		if block_id not in removed:
			return None

		return {
			"law": law,
			"replacement": {"boolean": result},
			"removed": removed,
			"touched": set(removed),
		}

	# complement law (1):
	# - A or not A -> true
	# - A and not A -> false
	right_block = (
		blocks.get(right_raw[1])
		if isinstance(right_raw, list)
		and len(right_raw) > 1
		and isinstance(right_raw[1], str)
		else None
	)

	if right_block is not None and right_block.get("opcode") == "operator_not":
		not_inputs = right_block.get("inputs", {})
		operand_raw = not_inputs.get("OPERAND")

		if operand_raw is not None and same(left_raw, operand_raw):
			result = block["opcode"] == "operator_or"
			plan = replace_with_boolean("complement", result)

			if plan is not None:
				return plan

	# complement law (2):
	# - not A or A -> true
	# - not A and A -> false
	left_block = (
		blocks.get(left_raw[1])
		if isinstance(left_raw, list)
		and len(left_raw) > 1
		and isinstance(left_raw[1], str)
		else None
	)

	if isinstance(left_block, dict) and left_block.get("opcode") == "operator_not":
		not_inputs = left_block.get("inputs", {})
		operand_raw = not_inputs.get("OPERAND")

		if operand_raw is not None and same(operand_raw, right_raw):
			result = block["opcode"] == "operator_or"
			plan = replace_with_boolean("complement", result)

			if plan is not None:
				return plan

	# idempotent law: A op A -> A
	if keepable(left_raw) and same(left_raw, right_raw):
		plan = replace_with("idempotent", left_raw)
		if plan is not None:
			return plan

	# absorption law: A op (A dual B) -> A
	for kept, other in ((left_raw, right_raw), (right_raw, left_raw)):
		parts = get_block_parts(other, blocks, dual)
		if parts is None or not keepable(kept):
			continue

		if same(kept, parts[2]) or same(kept, parts[3]):
			plan = replace_with("absorption", kept)
			if plan is not None:
				return plan

	# factoring: (A dual B) op (A dual C) -> A dual (B op C)
	left_parts = get_block_parts(left_raw, blocks, dual)
	right_parts = get_block_parts(right_raw, blocks, dual)

	if left_parts is None or right_parts is None:
		return None

	if block.get("parent") != parent_id:
		return None

	l_id, r_id = left_parts[0], right_parts[0]
	l_ops = (left_parts[2], left_parts[3])
	r_ops = (right_parts[2], right_parts[3])

	for i in (0, 1):
		for j in (0, 1):
			if l_ops[i][1] is None or not same(l_ops[i], r_ops[j]):
				continue

			removed = {r_id} | closure_of(r_ops[j])

			return {
				"law": "factoring",
				"outer_id": l_id,
				"inner_id": block_id,
				"outer_inputs": {
					"OPERAND1": copy.deepcopy(l_ops[i]),
					"OPERAND2": [INPUT_BLOCK_NO_SHADOW, block_id],
				},
				"inner_inputs": {
					"OPERAND1": copy.deepcopy(l_ops[1 - i]),
					"OPERAND2": copy.deepcopy(r_ops[1 - j]),
				},
				"owner_input": remap_block_input(value, l_id),
				"old_parent": block.get("parent"),
				"removed": removed,
				"touched": removed | {block_id, l_id},
			}

	return None


def apply_plan(
	plan,
	blocks,
	graph,
	owner_id,
	input_name,
	ti,
	opts,
	stats,
	commented_ids,
	stats_group="boolean",
) -> bool:
	if fold_blocked_by_comment(plan["touched"], blocks, commented_ids):
		return False

	owner = blocks.get(owner_id)
	owner_inputs = owner.get("inputs") if isinstance(owner, dict) else None

	if not isinstance(owner_inputs, dict) or input_name not in owner_inputs:
		return False

	link_edits = opts.folded_constant_expression_link_edits
	input_edits = opts.folded_constant_expression_inputs
	removed = plan["removed"]

	def set_parent(child_id, parent_id) -> None:
		child = blocks.get(child_id)

		if isinstance(child, dict) and child.get("parent") != parent_id:
			child["parent"] = parent_id
			link_edits[(ti, child_id, "parent")] = parent_id

	if plan["law"] != "factoring":
		new_input = copy.deepcopy(plan["replacement"])

		for child_id in _input_block_ids(new_input, blocks):
			set_parent(child_id, owner_id)

		owner_inputs[input_name] = new_input
		input_edits[(ti, owner_id, input_name)] = copy.deepcopy(new_input)
		changed_ids = (owner_id, *removed)
	else:
		outer_id, inner_id = plan["outer_id"], plan["inner_id"]
		outer, inner = blocks[outer_id], blocks[inner_id]

		for target, key in ((outer, "outer_inputs"), (inner, "inner_inputs")):
			target_inputs = target.get("inputs") or {}
			target_id = outer_id if target is outer else inner_id

			opcode_key = "outer_opcode" if target is outer else "inner_opcode"
			new_opcode = plan.get(opcode_key)
			if new_opcode is not None and target.get("opcode") != new_opcode:
				target["opcode"] = new_opcode
				opts.folded_constant_expression_opcode_edits[
					(ti, target_id)
				] = new_opcode

			for name, raw in plan[key].items():
				target_inputs[name] = copy.deepcopy(raw)
				input_edits[(ti, target_id, name)] = copy.deepcopy(raw)

			target["inputs"] = target_inputs

			for raw in plan[key].values():
				for child_id in _input_block_ids(raw, blocks):
					if child_id not in removed:
						set_parent(child_id, target_id)

		set_parent(outer_id, plan["old_parent"])
		set_parent(inner_id, outer_id)

		owner_inputs[input_name] = copy.deepcopy(plan["owner_input"])
		input_edits[(ti, owner_id, input_name)] = copy.deepcopy(plan["owner_input"])
		changed_ids = (owner_id, outer_id, inner_id, *removed)

	opts.folded_constant_expression_blocks[ti].update(removed)

	for remove_id in removed:
		blocks.pop(remove_id, None)

	graph.refresh_blocks(changed_ids)
	stats[f"{stats_group}_{plan['law']}"] += 1
	return True


def fold_blocked_by_comment(folded_ids, blocks, commented_ids) -> bool:
	return any(
		bid in commented_ids
		or (isinstance(blocks.get(bid), dict) and "comment" in blocks[bid])
		for bid in folded_ids
	)


def direct_input_block_refs(value, blocks) -> tuple:
	if not isinstance(value, list) or not value:
		return ()

	refs = []

	if value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW) and len(value) > 1:
		ref = value[1]

		if isinstance(ref, str) and ref in blocks:
			refs.append(ref)

	if value[0] == INPUT_DIFF_BLOCK_SHADOW and len(value) > 2:
		ref = value[2]

		if isinstance(ref, str) and ref in blocks:
			refs.append(ref)

	return tuple(refs)


def get_exclusive_input_subtree(value, blocks, owner_id, incoming_refs, graph=None):
	if graph is None:
		graph = ProjectGraph({"blocks": blocks})

	roots = direct_input_block_refs(value, blocks)

	if not roots:
		return set()

	candidate = set()
	stack = list(roots)

	while stack:
		bid = stack.pop()

		if bid in candidate:
			continue

		block = blocks.get(bid)

		if not isinstance(block, dict) or block.get("topLevel") is True:
			return None

		if incoming_refs.get(bid, 0) != 1:
			return None

		candidate.add(bid)
		stack.extend(graph.edges.get(bid, ()))

	roots_set = set(roots)

	for bid in candidate:
		owners = graph.parents.get(bid, set())
		if bid in roots_set:
			if owner_id not in owners or len(owners) != 1:
				return None
		elif not owners or not owners <= candidate:
			return None

	return candidate


def constant_to_input(constant, blocks, parent_id) -> tuple[list | None, set]:
	kind, value = constant

	if kind == "number":
		number = (
			int(value) if isinstance(value, float) and value.is_integer() else value
		)
		return [INPUT_SAME_BLOCK_SHADOW, [PRIMITIVE_NUMBER, number]], set()

	if kind == "string":
		return [INPUT_SAME_BLOCK_SHADOW, [PRIMITIVE_TEXT, value]], set()

	if kind == "bool":
		return [
			INPUT_SAME_BLOCK_SHADOW,
			[PRIMITIVE_TEXT, "true" if value else "false"],
		], set()
	return None, set()


def fold_ids_have_external_refs(folded_ids, blocks, owner_id, graph=None) -> bool:
	folded_ids = set(folded_ids)

	if graph is None:
		graph = ProjectGraph({"blocks": blocks})

	for bid in folded_ids:
		for owner in graph.parents.get(bid, ()):
			if owner != owner_id and owner not in folded_ids:
				return True

	return False


def demorgan_boolean_input(
	value, blocks, owner_id=None, incoming_refs=None, graph=None
):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	logic_id = value[1]
	if not isinstance(logic_id, str) or logic_id not in blocks:
		return None
	logic = blocks[logic_id]
	if not isinstance(logic, dict) or logic.get("opcode") not in (
		"operator_and",
		"operator_or",
	):
		return None

	if owner_id is None or incoming_refs is None:
		return None

	if owner_id == logic_id or logic.get("parent") != owner_id:
		return None

	if incoming_refs.get(logic_id, 0) != 1:
		return None

	inputs = logic.get("inputs") or {}
	left_raw = inputs.get("OPERAND1")
	right_raw = inputs.get("OPERAND2")

	for raw in (left_raw, right_raw):
		if not (
			isinstance(raw, list)
			and len(raw) > 1
			and raw[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
			and isinstance(raw[1], str)
		):
			return None

	left_not_id = left_raw[1]
	right_not_id = right_raw[1]
	if (
		left_not_id == right_not_id
		or left_not_id in (logic_id, owner_id)
		or right_not_id in (logic_id, owner_id)
	):
		return None

	if (
		incoming_refs.get(left_not_id, 0) != 1
		or incoming_refs.get(right_not_id, 0) != 1
	):
		return None

	left_not = blocks.get(left_not_id)
	right_not = blocks.get(right_not_id)

	if not (
		isinstance(left_not, dict)
		and isinstance(right_not, dict)
		and left_not.get("opcode") == "operator_not"
		and right_not.get("opcode") == "operator_not"
	):
		return None

	if (
		logic.get("next") is not None
		or left_not.get("next") is not None
		or right_not.get("next") is not None
	):
		return None

	if left_not.get("parent") != logic_id or right_not.get("parent") != logic_id:
		return None

	left_operand = (left_not.get("inputs") or {}).get("OPERAND")
	right_operand = (right_not.get("inputs") or {}).get("OPERAND")

	if not (
		isinstance(left_operand, list)
		and len(left_operand) > 1
		and isinstance(right_operand, list)
		and len(right_operand) > 1
	):
		return None

	left_owned = get_exclusive_input_subtree(
		left_operand, blocks, left_not_id, incoming_refs, graph=graph
	)
	right_owned = get_exclusive_input_subtree(
		right_operand, blocks, right_not_id, incoming_refs, graph=graph
	)

	if left_owned is None or right_owned is None:
		return None

	left_root = left_operand[1] if isinstance(left_operand[1], str) else None
	right_root = right_operand[1] if isinstance(right_operand[1], str) else None

	if left_root and left_root == right_root:
		return None

	for root in (left_root, right_root):
		if root in (logic_id, left_not_id, right_not_id, owner_id):
			return None

	new_owner_input = [INPUT_BLOCK_NO_SHADOW, left_not_id]
	new_outer_input = [INPUT_BLOCK_NO_SHADOW, logic_id]

	return {
		"logic_id": logic_id,
		"left_not_id": left_not_id,
		"right_not_id": right_not_id,
		"old_logic_parent": logic.get("parent"),
		"new_owner_input": new_owner_input,
		"new_outer_input": new_outer_input,
		"new_inner_left": copy.deepcopy(left_operand),
		"new_inner_right": copy.deepcopy(right_operand),
		"new_inner_opcode": (
			"operator_or" if logic.get("opcode") == "operator_and" else "operator_and"
		),
		"changed_ids": {logic_id, left_not_id, right_not_id} | left_owned | right_owned,
	}


def value_is_numeric(value, blocks, visiting=frozenset()):
	constant = constexpr_from_input(value, blocks, visiting)
	if constant is not None:
		return constant[0][0] in ("number", "bool")

	if not (isinstance(value, list) and len(value) > 1):
		return False

	child = value[1]

	if not isinstance(child, str) or child in visiting:
		return False

	block = blocks.get(child)
	return isinstance(block, dict) and block.get("opcode") in NUMERIC_REPORTER_OPCODES


def value_is_finite(value, blocks, visiting=frozenset()):
	constant = constexpr_from_input(value, blocks, visiting)
	if constant is not None and constant[0][0] in ("number", "bool"):
		number = constant_to_number(constant[0])
		return number is not None and math.isfinite(number)

	if not (isinstance(value, list) and len(value) > 1):
		return False

	child = value[1]

	if not isinstance(child, str) or child in visiting:
		return False

	block = blocks.get(child)
	return (
		isinstance(block, dict)
		and block.get("opcode") in FINITE_NUMERIC_REPORTER_OPCODES
	)


def value_is_boolean(value, blocks, visiting=frozenset()):
	constant = constexpr_from_input(value, blocks, visiting)
	if constant is not None:
		return constant[0][0] == "bool"

	if not (isinstance(value, list) and len(value) > 1):
		return False

	child = value[1]
	if not isinstance(child, str) or child in visiting:
		return False

	block = blocks.get(child)

	if not isinstance(block, dict):
		return False

	op = block.get("opcode")
	return op in BOOLEAN_REPORTER_OPCODES or op == "argument_reporter_boolean"


def simplify_double_boolean_negation(
	value, blocks, owner_id, input_name, incoming_refs, graph
):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	outer_id = value[1]
	if not isinstance(outer_id, str) or outer_id == owner_id:
		return None

	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("opcode") != "operator_not":
		return None

	inner_raw = (outer.get("inputs") or {}).get("OPERAND")

	if not (
		isinstance(inner_raw, list)
		and len(inner_raw) > 1
		and inner_raw[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	inner_id = inner_raw[1]
	if not isinstance(inner_id, str) or inner_id in (owner_id, outer_id):
		return None

	inner = blocks.get(inner_id)
	if not isinstance(inner, dict) or inner.get("opcode") != "operator_not":
		return None

	operand = (inner.get("inputs") or {}).get("OPERAND")
	if operand is None:
		return None

	if not (
		is_boolean_slot(blocks.get(owner_id), input_name)
		or value_is_boolean(operand, blocks)
	):
		return None

	closure = get_exclusive_input_subtree(
		value, blocks, owner_id, incoming_refs, graph=graph
	)

	if closure is None or outer_id not in closure or inner_id not in closure:
		return None

	preserved_ids = get_subtree_block_ids(operand, blocks)
	removed = closure - preserved_ids

	if owner_id in removed or not removed:
		return None

	return copy.deepcopy(operand), removed


def simplify_abs_patterns(value, blocks, owner_id, incoming_refs, graph):
	# <a > -x> and <a < x> should be simplified to <abs of (a) < x>
	root_id = get_input_root(value, blocks)
	if root_id is None or root_id == owner_id:
		return None

	root = blocks.get(root_id)

	if (
		not isinstance(root, dict)
		or root.get("opcode") != "operator_and"
		or root.get("parent") != owner_id
		or root.get("next") is not None
	):
		return None

	closure = get_exclusive_input_subtree(
		value, blocks, owner_id, incoming_refs, graph=graph
	)

	if closure is None or root_id not in closure:
		return None

	root_inputs = root.get("inputs") or {}
	left_id = get_input_root(root_inputs.get("OPERAND1"), blocks)
	right_id = get_input_root(root_inputs.get("OPERAND2"), blocks)

	if (
		left_id is None
		or right_id is None
		or left_id == right_id
		or left_id not in closure
		or right_id not in closure
	):
		return None

	comparisons = (blocks.get(left_id), blocks.get(right_id))
	if any(
		not isinstance(comparison, dict)
		or comparison.get("opcode") not in ("operator_gt", "operator_lt")
		or comparison.get("parent") != root_id
		or comparison.get("next") is not None
		for comparison in comparisons
	):
		return None

	if {comparison.get("opcode") for comparison in comparisons} != {
		"operator_gt",
		"operator_lt",
	}:
		return None

	gt_id = left_id if blocks[left_id]["opcode"] == "operator_gt" else right_id
	lt_id = left_id if blocks[left_id]["opcode"] == "operator_lt" else right_id
	gt = blocks[gt_id]
	lt = blocks[lt_id]
	gt_inputs = gt.get("inputs") or {}
	lt_inputs = lt.get("inputs") or {}
	gt_left, negative_raw = gt_inputs.get("OPERAND1"), gt_inputs.get("OPERAND2")
	lt_left, positive_raw = lt_inputs.get("OPERAND1"), lt_inputs.get("OPERAND2")

	if any(raw is None for raw in (gt_left, negative_raw, lt_left, positive_raw)):
		return None

	if not condition_is_pure(value, blocks, graph):
		return None

	def numeric_value(raw):
		constant = constexpr_from_input(raw, blocks)
		if constant is not None:
			if constant[0][0] == "string" and not constant[0][1].strip():
				return False
			
			number = constant_to_number(constant[0])
			return number is not None and math.isfinite(number)
		
		root = get_input_root(raw, blocks)
		reporter = blocks.get(root) if root is not None else None
		
		return value_is_numeric(raw, blocks) or (
			isinstance(reporter, dict)
			and reporter.get("opcode")
			in ("sensing_mousex", "sensing_mousey", "sensing_distanceto")
		)

	if not all(numeric_value(raw) for raw in (gt_left, negative_raw, lt_left, positive_raw)):
		return None

	def same_value(first, second):
		first_closure = get_subtree_block_ids(first, blocks)
		second_closure = get_subtree_block_ids(second, blocks)
		return branch_factor_value_equal(
			first, second, blocks, first_closure, second_closure, {}, set()
		)

	if not same_value(gt_left, lt_left):
		return None

	def is_zero(raw):
		constant = constexpr_from_input(raw, blocks)
		return (
			constant is not None
			and constant[0][0] == "number"
			and constant_to_number(constant[0]) == 0
		)

	negative_constant = constexpr_from_input(negative_raw, blocks)
	positive_constant = constexpr_from_input(positive_raw, blocks)
	negative_number = (
		constant_to_number(negative_constant[0])
		if negative_constant is not None
		else None
	)
	positive_number = (
		constant_to_number(positive_constant[0])
		if positive_constant is not None
		else None
	)
	bounds_match = (
		negative_number is not None
		and positive_number is not None
		and math.isfinite(negative_number)
		and math.isfinite(positive_number)
		and negative_number <= 0
		and positive_number == -negative_number
	)
	abs_operand = None

	if bounds_match:
		abs_operand = gt_left
	else:
		negative_id = get_input_root(negative_raw, blocks)
		negative_block = blocks.get(negative_id) if negative_id is not None else None

		if (
			not isinstance(negative_block, dict)
			or negative_block.get("parent") != gt_id
			or negative_block.get("next") is not None
		):
			return None

		negative_inputs = negative_block.get("inputs") or {}

		if negative_block.get("opcode") == "operator_subtract":
			candidate = negative_inputs.get("NUM2")

			if candidate is not None and is_zero(negative_inputs.get("NUM1")):
				if same_value(candidate, positive_raw):
					abs_operand = gt_left
		elif negative_block.get("opcode") == "operator_multiply":
			for coefficient_name, candidate_name in (
				("NUM1", "NUM2"),
				("NUM2", "NUM1"),
			):
				coefficient = constexpr_from_input(
					negative_inputs.get(coefficient_name), blocks
				)
				candidate = negative_inputs.get(candidate_name)
				coefficient_number = (
					constant_to_number(coefficient[0])
					if coefficient is not None
					else None
				)

				if (
					coefficient_number == -1
					and candidate is not None
					and same_value(candidate, positive_raw)
				):
					abs_operand = gt_left
					break

	if abs_operand is None:
		return None

	preserved = {lt_id} | get_subtree_block_ids(abs_operand, blocks)
	preserved.update(get_subtree_block_ids(positive_raw, blocks))
	for raw in (value, lt_inputs["OPERAND1"]):
		if (
			isinstance(raw, list)
			and len(raw) > 2
			and raw[0] == INPUT_DIFF_BLOCK_SHADOW
		):
			preserved.update(get_subtree_block_ids(raw[2], blocks))

	if not preserved <= closure:
		return None

	removed = closure - preserved

	if root_id not in removed or gt_id not in removed or not removed:
		return None

	new_id = uuid.uuid4().hex

	while new_id in blocks:
		new_id = uuid.uuid4().hex

	abs_block = {
		"opcode": "operator_mathop",
		"next": None,
		"parent": lt_id,
		"inputs": {"NUM": copy.deepcopy(abs_operand)},
		"fields": {"OPERATOR": ["abs", None]},
		"shadow": False,
		"topLevel": False,
	}
	new_lt_left = remap_block_input(lt_inputs["OPERAND1"], new_id)

	return {
		"owner_input": remap_block_input(value, lt_id),
		"outer_id": lt_id,
		"outer_left": new_lt_left,
		"abs_id": new_id,
		"abs_block": abs_block,
		"removed": removed,
	}


def simplify_algebraic_input(value, blocks, owner_id, incoming_refs, graph):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None
	block_id = value[1]
	if not isinstance(block_id, str) or block_id == owner_id:
		return None
	block = blocks.get(block_id)
	if not isinstance(block, dict) or block.get("next") is not None:
		return None

	op = block.get("opcode")
	if op not in (
		"operator_add",
		"operator_subtract",
		"operator_multiply",
		"operator_divide",
		"operator_mod",
	):
		return None

	inputs = block.get("inputs") or {}
	left_raw, right_raw = inputs.get("NUM1"), inputs.get("NUM2")
	if left_raw is None or right_raw is None:
		return None

	left = constexpr_from_input(left_raw, blocks)
	right = constexpr_from_input(right_raw, blocks)
	left_num = constant_to_number(left[0]) if left is not None else None
	right_num = constant_to_number(right[0]) if right is not None else None
	preserve = None
	dead_side = None

	if op == "operator_add":
		if left_num == 0 and value_is_numeric(right_raw, blocks):
			preserve, dead_side = right_raw, left
		elif right_num == 0 and value_is_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_subtract":
		if right_num == 0 and value_is_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_multiply":
		if left_num == 1 and value_is_numeric(right_raw, blocks):
			preserve, dead_side = right_raw, left
		elif left_num == 0 and value_is_finite(right_raw, blocks):
			preserve, dead_side = [
				INPUT_SAME_BLOCK_SHADOW,
				[PRIMITIVE_NUMBER, 0],
			], value
		elif right_num == 1 and value_is_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
		elif right_num == 0 and value_is_finite(left_raw, blocks):
			preserve, dead_side = [
				INPUT_SAME_BLOCK_SHADOW,
				[PRIMITIVE_NUMBER, 0],
			], value
	elif op == "operator_divide":
		if right_num == 1 and value_is_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_mod":
		if (
			right_num == 1
			and value_is_numeric(left_raw, blocks)
			and left_num is not None
		):
			preserve, dead_side = [
				INPUT_SAME_BLOCK_SHADOW,
				[PRIMITIVE_NUMBER, left_num % right_num],
			], right

	if preserve is None:
		return None

	closure = get_exclusive_input_subtree(
		value, blocks, owner_id, incoming_refs, graph=graph
	)
	if closure is None or block_id not in closure:
		return None

	removed = closure - get_subtree_block_ids(preserve, blocks)
	if owner_id in removed or not removed:
		return None
	if dead_side is not None and not (
		get_subtree_block_ids(dead_side, blocks) <= removed
	):
		return None
	return copy.deepcopy(preserve), removed


def plan_math_expr_factoring(value, blocks, owner_id, incoming_refs, graph):
	if not (
		isinstance(value, list)
		and len(value) > 1
		and value[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
	):
		return None

	block_id = value[1]

	if not isinstance(block_id, str) or block_id == owner_id:
		return None

	block = blocks.get(block_id)

	if (
		not isinstance(block, dict)
		or block.get("opcode") != "operator_add"
		or block.get("next") is not None
		or block.get("parent") != owner_id
	):
		return None

	inputs = block.get("inputs") or {}
	left_raw, right_raw = inputs.get("NUM1"), inputs.get("NUM2")

	if left_raw is None or right_raw is None:
		return None

	left_id = get_input_root(left_raw, blocks)
	right_id = get_input_root(right_raw, blocks)

	if (
		left_id is None
		or right_id is None
		or left_id == right_id
		or left_id in (owner_id, block_id)
		or right_id in (owner_id, block_id)
	):
		return None

	left = blocks.get(left_id)
	right = blocks.get(right_id)

	if not (
		isinstance(left, dict)
		and left.get("opcode") == "operator_multiply"
		and left.get("next") is None
		and left.get("parent") == block_id
		and isinstance(right, dict)
		and right.get("opcode") == "operator_multiply"
		and right.get("next") is None
		and right.get("parent") == block_id
	):
		return None

	left_inputs = left.get("inputs") or {}
	right_inputs = right.get("inputs") or {}

	left_factors = (left_inputs.get("NUM1"), left_inputs.get("NUM2"))
	right_factors = (right_inputs.get("NUM1"), right_inputs.get("NUM2"))

	if any(raw is None for raw in (*left_factors, *right_factors)):
		return None

	pure_opcodes = {
		"operator_add",
		"operator_subtract",
		"operator_multiply",
		"operator_divide",
		"operator_mod",
		"operator_round",
		"operator_mathop",
		"operator_gt",
		"operator_lt",
		"operator_equals",
		"operator_and",
		"operator_or",
		"operator_not",
		"operator_join",
		"operator_length",
		"operator_letter_of",
		"operator_contains",
	}

	commutative_opcodes = {
		"operator_add",
		"operator_multiply",
		"operator_equals",
		"operator_and",
		"operator_or",
	}

	forbidden_ids = {owner_id, block_id, left_id, right_id}

	def block_signature(block_id, visiting):
		if block_id in forbidden_ids or block_id in visiting:
			return None

		current = blocks.get(block_id)

		if (
			not isinstance(current, dict)
			or current.get("next") is not None
		):
			return None

		opcode = current.get("opcode")

		if opcode == "data_variable":
			fields = current.get("fields") or {}
			variable = fields.get("VARIABLE")

			if not isinstance(variable, list) or len(variable) < 2:
				return None

			return ("variable", variable[1], variable[0])

		if opcode not in pure_opcodes:
			return None

		visiting.add(block_id)
		try:
			fields_sig = dumps_compact(current.get("fields") or {})
			mutation_sig = dumps_compact(current.get("mutation"))

			input_signatures = {}

			for name, raw in sorted((current.get("inputs") or {}).items()):
				signature = input_signature(raw, visiting)

				if signature is None:
					return None
				
				input_signatures[name] = signature

			if (
				opcode in commutative_opcodes
				and set(input_signatures) == {"NUM1", "NUM2"}
			):
				operands = tuple(sorted(
					(
						input_signatures["NUM1"],
						input_signatures["NUM2"],
					),
					key=dumps_compact,
				))
				inputs_sig = ("COMMUTATIVE", operands)
			else:
				inputs_sig = tuple(sorted(input_signatures.items()))

			return (
				opcode,
				fields_sig,
				mutation_sig,
				inputs_sig,
			)
		finally:
			visiting.remove(block_id)

	def input_signature(raw, visiting=None):
		if visiting is None:
			visiting = set()

		constant = constexpr_from_input(raw, blocks)
		if constant is not None:
			number = constant_to_number(constant[0])

			if number is not None:
				try:
					number = float(number)
				except (TypeError, ValueError, OverflowError):
					number = None

				if number is not None and math.isfinite(number):
					return ("number", number)

		root_id = get_input_root(raw, blocks)

		if not isinstance(root_id, str):
			return None

		return block_signature(root_id, visiting)

	def subtree_weight(raw):
		subtree = get_subtree_block_ids(raw, blocks)
		return sum(
			len(dumps_compact(blocks[bid])) + len(bid)
			for bid in subtree
			if isinstance(blocks.get(bid), dict)
		)

	left_signatures = tuple(input_signature(raw) for raw in left_factors)
	right_signatures = tuple(input_signature(raw) for raw in right_factors)

	candidates = []

	for left_index in range(2):
		common_signature = left_signatures[left_index]

		if common_signature is None:
			continue

		for right_index in range(2):
			if common_signature != right_signatures[right_index]:
				continue

			left_term = left_factors[1 - left_index]
			right_term = right_factors[1 - right_index]

			if (
				input_signature(left_term) is None
				or input_signature(right_term) is None
			):
				continue

			left_common = left_factors[left_index]
			right_common = right_factors[right_index]
			candidates.append((
				subtree_weight(right_common),
				left_common,
				left_term,
				right_term,
			))
			candidates.append((
				subtree_weight(left_common),
				right_common,
				left_term,
				right_term,
			))

	if not candidates:
		return None

	_, raw_common, left_term, right_term = max(
		candidates,
		key=lambda candidate: candidate[0],
	)

	owned = get_exclusive_input_subtree(
		value, blocks, owner_id, incoming_refs, graph=graph
	)

	if owned is None or not {block_id, left_id, right_id} <= owned:
		return None

	outer_inputs = {
		"NUM1": copy.deepcopy(raw_common),
		"NUM2": [INPUT_BLOCK_NO_SHADOW, left_id],
	}
	inner_inputs = {
		"NUM1": copy.deepcopy(left_term),
		"NUM2": copy.deepcopy(right_term),
	}

	preserved = {block_id, left_id}

	for raw in (raw_common, left_term, right_term):
		preserved.update(get_subtree_block_ids(raw, blocks))

	if not preserved <= owned:
		return None

	removed = owned - preserved

	if not removed or right_id not in removed:
		return None

	link_updates = {}

	for raw, parent_id in (
		(raw_common, block_id),
		(left_term, left_id),
		(right_term, left_id),
	):
		for child_id in direct_input_block_refs(raw, blocks):
			child = blocks.get(child_id)

			if isinstance(child, dict) and child.get("parent") != parent_id:
				link_updates[child_id] = parent_id

	return {
		"outer_id": block_id,
		"inner_id": left_id,
		"outer_opcode": "operator_multiply",
		"inner_opcode": "operator_add",
		"outer_inputs": outer_inputs,
		"inner_inputs": inner_inputs,
		"link_updates": link_updates,
		"owner_input": remap_block_input(value, block_id),
		"old_parent": block.get("parent"),
		"removed": removed,
		"touched": removed | {block_id, left_id} | set(link_updates),
		"law": "factoring",
	}


def factor_math_expressions(project, stats, opts):
	targets = project.get("targets", [])

	if opts.folded_constant_expression_link_edits is None:
		opts.folded_constant_expression_link_edits = {}
	if opts.folded_constant_expression_inputs is None:
		opts.folded_constant_expression_inputs = {}
	if opts.folded_constant_expression_opcode_edits is None:
		opts.folded_constant_expression_opcode_edits = {}
	if opts.folded_constant_expression_blocks is None or len(
		opts.folded_constant_expression_blocks
	) != len(targets):
		opts.folded_constant_expression_blocks = [set() for _ in targets]

	total = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = ProjectGraph(target)
		commented_ids = {
			comment.get("blockId")
			for comment in (target.get("comments") or {}).values()
			if isinstance(comment, dict) and comment.get("blockId") is not None
		}

		while True:
			changed = False

			for owner_id, owner in list(blocks.items()):
				if not isinstance(owner, dict):
					continue
				for input_name, value in (owner.get("inputs") or {}).items():
					plan = plan_math_expr_factoring(
						value, blocks, owner_id, graph.incoming, graph
					)
					
					if plan is None:
						continue

					if apply_plan(
						plan,
						blocks,
						graph,
						owner_id,
						input_name,
						ti,
						opts,
						stats,
						commented_ids,
						"math",
					):
						total += 1
						changed = True
						break
				if changed:
					break
			
			if not changed:
				break

	return total


def get_control_substack(value, blocks):
	if not isinstance(value, list) or not value:
		return None
	tag = _input_tag(value[0])
	if tag not in INPUT_TAGS or len(value) <= 1:
		return None
	ref = value[1]
	return ref if isinstance(ref, str) and ref in blocks else None


def get_control_owner_edge(blocks, control_id):

	control = blocks.get(control_id)
	if not isinstance(control, dict):
		return None
	parent_id = control.get("parent")
	if not isinstance(parent_id, str) or parent_id not in blocks:
		return None
	owner = blocks.get(parent_id)
	if not isinstance(owner, dict):
		return None
	if owner.get("next") == control_id:
		return parent_id, "next", None
	for name, value in (owner.get("inputs") or {}).items():
		if (
			isinstance(value, list)
			and len(value) > 1
			and value[0] in INPUT_TAGS
			and value[1] == control_id
		):
			return parent_id, "input", name
	return None


def get_input_replacement(owner, edge_kind, edge_name, old_id, new_id):

	if not isinstance(owner, dict):
		return None
	if edge_kind == "next":
		if owner.get("next") != old_id:
			return None
		owner["next"] = new_id
		return owner
	inputs = owner.get("inputs")
	if not isinstance(inputs, dict) or edge_name not in inputs:
		return None
	value = inputs[edge_name]
	if (
		not isinstance(value, list)
		or len(value) <= 1
		or value[0] not in INPUT_TAGS
		or value[1] != old_id
	):
		return None
	value[1] = new_id
	return owner


def get_substack_closure(value, blocks, owner_id, incoming_refs, graph):
	if value is None:
		return set(), None
	root = get_control_substack(value, blocks)
	if root is None:
		return set(), None
	closure = get_exclusive_input_subtree(
		value, blocks, owner_id, incoming_refs, graph=graph
	)
	if closure is None or root not in closure:
		return None, None
	tail = get_substack_tail(blocks, closure)
	if tail is None:
		return None, None
	return closure, tail


def constant_control_plan(blocks, control_id, incoming_refs, graph):
	control = blocks.get(control_id)
	if not isinstance(control, dict):
		return None
	opcode = control.get("opcode")
	if opcode not in (
		"control_if",
		"control_if_else",
		"control_repeat_until",
		"control_wait_until",
		"control_repeat",
	):
		return None

	owner_info = get_control_owner_edge(blocks, control_id)
	if owner_info is None:
		return None
	owner_id, owner_edge_kind, owner_edge_name = owner_info
	owner = blocks.get(owner_id)
	if not isinstance(owner, dict) or control.get("parent") != owner_id:
		return None

	inputs = control.get("inputs") or {}
	condition_name = "TIMES" if opcode == "control_repeat" else "CONDITION"
	condition_raw = inputs.get(condition_name)
	constant = constexpr_from_input(condition_raw, blocks)
	if constant is None:
		return None

	if opcode == "control_repeat":
		n = constant_to_number(constant[0])
		if n is None or not math.isfinite(n) or n not in (0, 1):
			return None
		condition_closure = (
			get_exclusive_input_subtree(
				condition_raw, blocks, control_id, incoming_refs, graph=graph
			)
			if condition_raw is not None
			else set()
		)
		if condition_closure is None:
			return None
		selected_raw = inputs.get("SUBSTACK") if n == 1 else None
		dead_raws = [] if n == 1 else [inputs.get("SUBSTACK")]
	elif opcode == "control_wait_until":
		if not _constant_to_bool(constant[0]):
			return None
		condition_closure = (
			get_exclusive_input_subtree(
				condition_raw, blocks, control_id, incoming_refs, graph=graph
			)
			if condition_raw is not None
			else set()
		)
		if condition_closure is None:
			return None
		selected_raw = None
		dead_raws = []
	elif opcode == "control_repeat_until":
		if not _constant_to_bool(constant[0]):
			return None
		condition_closure = (
			get_exclusive_input_subtree(
				condition_raw, blocks, control_id, incoming_refs, graph=graph
			)
			if condition_raw is not None
			else set()
		)
		if condition_closure is None:
			return None
		selected_raw = None
		dead_raws = [inputs.get("SUBSTACK")]
	else:
		condition_closure = (
			get_exclusive_input_subtree(
				condition_raw, blocks, control_id, incoming_refs, graph=graph
			)
			if condition_raw is not None
			else set()
		)
		if condition_closure is None:
			return None
		condition_bool = _constant_to_bool(constant[0])
		if opcode == "control_if":
			selected_raw = inputs.get("SUBSTACK") if condition_bool else None
			dead_raws = [inputs.get("SUBSTACK")] if not condition_bool else []
		else:
			selected_raw = (
				inputs.get("SUBSTACK") if condition_bool else inputs.get("SUBSTACK2")
			)
			dead_raws = [
				inputs.get("SUBSTACK2") if condition_bool else inputs.get("SUBSTACK")
			]

	selected_closure, selected_tail = get_substack_closure(
		selected_raw, blocks, control_id, incoming_refs, graph
	)
	if selected_closure is None:
		return None

	removed = {control_id}
	removed.update(condition_closure)
	for raw in dead_raws:
		if raw is None:
			continue
		dead_closure = get_exclusive_input_subtree(
			raw, blocks, control_id, incoming_refs, graph=graph
		)
		if dead_closure is None:
			return None
		if selected_closure & dead_closure:
			return None
		removed.update(dead_closure)

	if selected_closure & removed:
		return None

	continuation = control.get("next")
	if continuation is not None and (
		not isinstance(continuation, str) or continuation not in blocks
	):
		return None
	if isinstance(continuation, str) and continuation in removed:
		return None
	if selected_tail is not None and blocks[selected_tail].get("next") is not None:
		return None

	for bid in removed:
		if bid == control_id:
			continue
		parents = graph.parents.get(bid, set())
		if any(parent not in removed and parent != control_id for parent in parents):
			return None

	replacement = (
		selected_closure and get_control_substack(selected_raw, blocks) or continuation
	)

	parent_updates = []
	next_updates = []
	if selected_closure:
		selected_id = get_control_substack(selected_raw, blocks)
		if selected_id is None:
			return None
		parent_updates.append((selected_id, owner_id))
		next_updates.append((selected_tail, continuation))
		if isinstance(continuation, str):
			parent_updates.append((continuation, selected_tail))
	else:
		if isinstance(continuation, str):
			parent_updates.append((continuation, owner_id))

	return {
		"owner_id": owner_id,
		"owner_edge_kind": owner_edge_kind,
		"owner_edge_name": owner_edge_name,
		"replacement": replacement,
		"selected_id": get_control_substack(selected_raw, blocks),
		"selected_tail": selected_tail,
		"continuation": continuation,
		"removed": removed,
		"parent_updates": parent_updates,
		"next_updates": next_updates,
	}


def simplify_boolean_controls(project, stats, opts, only_repeat_one=False):

	targets = project.get("targets", [])
	removed_sets = getattr(opts, "constant_control_removed_blocks", None)
	if removed_sets is None or len(removed_sets) != len(targets):
		opts.constant_control_removed_blocks = [set() for _ in targets]
		removed_sets = opts.constant_control_removed_blocks

	link_edits = getattr(opts, "folded_constant_expression_link_edits", None)
	if link_edits is None:
		opts.folded_constant_expression_link_edits = {}
		link_edits = opts.folded_constant_expression_link_edits
	input_edits = getattr(opts, "folded_constant_expression_inputs", None)
	if input_edits is None:
		opts.folded_constant_expression_inputs = {}
		input_edits = opts.folded_constant_expression_inputs

	total = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		commented_ids = {
			c.get("blockId")
			for c in (target.get("comments") or {}).values()
			if isinstance(c, dict) and c.get("blockId") is not None
		}
		commented_ids.update(
			bid
			for bid, block in blocks.items()
			if isinstance(block, dict) and "comment" in block
		)
		graph = ProjectGraph(target)

		while True:
			changed = False
			for control_id, block in list(blocks.items()):
				if not isinstance(block, dict):
					continue
				opcode = block.get("opcode")
				if opcode not in (
					"control_if",
					"control_if_else",
					"control_repeat_until",
					"control_wait_until",
					"control_repeat",
				):
					continue
				if only_repeat_one and opcode != "control_repeat":
					continue

				plan = constant_control_plan(blocks, control_id, graph.incoming, graph)
				if plan is None:
					continue
				removed = set(plan["removed"])
				if fold_blocked_by_comment(removed, blocks, commented_ids):
					continue

				owner_id = plan["owner_id"]
				owner = blocks.get(owner_id)
				if not isinstance(owner, dict):
					continue

				if any(
					not isinstance(blocks.get(bid), dict)
					for bid in (
						[owner_id]
						+ [bid for bid, _ in plan["parent_updates"]]
						+ [bid for bid, _ in plan["next_updates"]]
					)
				):
					continue
				replaced_owner = get_input_replacement(
					owner,
					plan["owner_edge_kind"],
					plan["owner_edge_name"],
					control_id,
					plan["replacement"],
				)
				if replaced_owner is None:
					continue

				for bid, parent_id in plan["parent_updates"]:
					blocks[bid]["parent"] = parent_id
				for bid, next_id in plan["next_updates"]:
					blocks[bid]["next"] = next_id

				if plan["owner_edge_kind"] == "next":
					link_edits[(ti, owner_id, "next")] = plan["replacement"]
				else:
					input_edits[(ti, owner_id, plan["owner_edge_name"])] = (
						copy.deepcopy(owner["inputs"][plan["owner_edge_name"]])
					)
				for bid, parent_id in plan["parent_updates"]:
					link_edits[(ti, bid, "parent")] = parent_id
				for bid, next_id in plan["next_updates"]:
					link_edits[(ti, bid, "next")] = next_id

				removed_sets[ti].update(removed)
				for bid in removed:
					blocks.pop(bid, None)

				graph.refresh_after_mutation(
					changed={bid}
					| {x for x, _ in plan["parent_updates"]}
					| {x for x, _ in plan["next_updates"]},
					removed=removed,
				)
				total += 1
				changed = True
				stats["boolean_control_simplifications"] += 1
				stats["control_blocks_removed"] += 1
				stats["control_branch_blocks_removed"] += len(removed) - 1
				break

			if not changed:
				break

	return total


def get_input_root(value, blocks):
	if not isinstance(value, list) or len(value) <= 1 or value[0] not in INPUT_TAGS:
		return None
	ref = value[1]
	return ref if isinstance(ref, str) and ref in blocks else None


def condition_is_pure(value, blocks, graph):
	seen = set()
	stack = list(_input_block_ids(value, blocks))
	while stack:
		bid = stack.pop()
		if bid in seen or bid not in blocks:
			continue
		seen.add(bid)
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		op = block.get("opcode", "")
		if op == "operator_random" or op.startswith(
			("event_", "control_", "procedures_")
		):
			return False
		if op.startswith("operator_") or op.startswith("argument_reporter_"):
			if op == "operator_random":
				return False
		elif op not in PURE_REPORTERS:
			return False
		stack.extend(graph.edges.get(bid, ()))
	return True


def get_comment_ids(target):
	ids = {
		c.get("blockId")
		for c in (target.get("comments") or {}).values()
		if isinstance(c, dict) and isinstance(c.get("blockId"), str)
	}
	ids.update(
		bid
		for bid, block in (target.get("blocks") or {}).items()
		if isinstance(block, dict) and "comment" in block
	)
	return ids


def script_prepare_maps(opts, targets):
	for name in ("script_rewrite_removed_blocks", "script_rewrite_new_blocks"):
		value = getattr(opts, name, None)
		if value is None or len(value) != len(targets):
			setattr(opts, name, [set() for _ in targets])
	for name in (
		"script_rewrite_link_edits",
		"script_rewrite_input_edits",
		"script_rewrite_opcode_edits",
	):
		if getattr(opts, name, None) is None:
			setattr(opts, name, {})
	if getattr(opts, "script_rewrite_removed_inputs", None) is None:
		opts.script_rewrite_removed_inputs = {}


def record_removed_blocks(opts: Options, ti: int, removed: Any):
	while len(opts.script_rewrite_removed_blocks) <= ti:
		opts.script_rewrite_removed_blocks.append(set())
	opts.script_rewrite_removed_blocks[ti].update(removed)


def record_new_blocks(opts: Options, ti: int, created: Any):
	while len(opts.script_rewrite_new_blocks) <= ti:
		opts.script_rewrite_new_blocks.append(set())
	opts.script_rewrite_new_blocks[ti].update(created)


def literal_from_input(value):
	if not (
		isinstance(value, list)
		and len(value) == 2
		and scratch_numeric_tag(value[0]) == INPUT_SAME_BLOCK_SHADOW
	):
		return None
	literal = value[1]
	if not (isinstance(literal, list) and len(literal) == 2):
		return None
	tag, raw = literal
	tag = scratch_numeric_tag(tag)
	if (
		tag in NUMERIC_TAGS
		and not isinstance(raw, bool)
		and isinstance(raw, (str, int, float))
	):
		if isinstance(raw, float) and not math.isfinite(raw):
			return None
		return copy.deepcopy(literal)
	if tag == PRIMITIVE_TEXT and isinstance(raw, str):
		return copy.deepcopy(literal)
	return None


def plan_unwrap_not(raw, blocks, owner_id, incoming_refs, graph):
	if not (isinstance(raw, list) and len(raw) == 2 and raw[0] == 2):
		return None
	not_id = raw[1]
	not_block = blocks.get(not_id)
	if not isinstance(not_id, str) or not isinstance(not_block, dict):
		return None
	if not_block.get("opcode") != "operator_not" or not_block.get("next") is not None:
		return None
	operand = (not_block.get("inputs") or {}).get("OPERAND")
	if operand is None or is_literal_input(operand):
		return None
	owned = get_exclusive_input_subtree(
		raw, blocks, owner_id, incoming_refs, graph=graph
	)
	if owned is None or not_id not in owned:
		return None
	operand_owned = get_exclusive_input_subtree(
		operand, blocks, not_id, incoming_refs, graph=graph
	)
	if operand_owned is None:
		return None
	moved_roots = direct_input_block_refs(operand, blocks)
	if any(
		(not isinstance(blocks.get(ref), dict)) or blocks[ref].get("parent") != not_id
		for ref in moved_roots
	):
		return None
	removed_ids = owned - operand_owned
	return not_id, copy.deepcopy(operand), removed_ids, moved_roots


def plan_branch_swap(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_if_else":
		return None
	condition = (control.get("inputs") or {}).get("CONDITION")
	plan = plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if plan is None:
		return None
	not_id, operand, remove_ids, moved_ids = plan
	removed = set(remove_ids) | {not_id}
	if removed & commented:
		return None
	return {"condition": operand, "removed": removed, "moved_ids": moved_ids}


def plan_empty_then(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_if_else":
		return None
	inputs = control.get("inputs") or {}

	if inputs.get("SUBSTACK") is not None:
		return None
	else_raw = inputs.get("SUBSTACK2")
	if get_control_substack(else_raw, blocks) is None:
		return None
	condition = inputs.get("CONDITION")
	if condition is None:
		return None
	unwrap = plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if unwrap is not None:
		not_id, operand, remove_ids, moved_ids = unwrap
		removed = set(remove_ids) | {not_id}
		if removed & commented:
			return None
		return {
			"mode": "unwrap",
			"condition": operand,
			"removed": removed,
			"moved_ids": moved_ids,
			"else_raw": copy.deepcopy(else_raw),
		}
	owned = get_exclusive_input_subtree(
		condition, blocks, control_id, incoming_refs, graph=graph
	)
	if owned is None or owned & commented:
		return None
	new_bid = _new_id()
	while new_bid in blocks:
		new_bid = _new_id()
	new_block = {
		"opcode": "operator_not",
		"next": None,
		"parent": control_id,
		"inputs": {"OPERAND": copy.deepcopy(condition)},
		"fields": {},
	}
	return {
		"mode": "wrap",
		"new_id": new_bid,
		"new_block": new_block,
		"condition_owned": owned,
		"else_raw": copy.deepcopy(else_raw),
		"removed": set(),
	}


def plan_repeat_until_not(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_repeat_until":
		return None
	condition = (control.get("inputs") or {}).get("CONDITION")
	unwrap = plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if unwrap is None:
		return None
	not_id, operand, remove_ids, moved_ids = unwrap
	removed = set(remove_ids) | {not_id}
	if removed & commented:
		return None
	return {"condition": operand, "removed": removed, "moved_ids": moved_ids}


def plan_nested_if(blocks, outer_id, incoming_refs, graph, commented):
	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("opcode") != "control_if":
		return None
	outer_inputs = outer.get("inputs") or {}
	inner_id = get_control_substack(outer_inputs.get("SUBSTACK"), blocks)
	if inner_id is None or inner_id == outer_id:
		return None
	inner = blocks.get(inner_id)
	if not isinstance(inner, dict) or inner.get("opcode") != "control_if":
		return None
	if inner.get("next") is not None or inner.get("parent") != outer_id:
		return None
	inner_inputs = inner.get("inputs") or {}
	body_raw = inner_inputs.get("SUBSTACK")
	body_root = get_control_substack(body_raw, blocks)
	if body_root is None or "SUBSTACK2" in inner_inputs:
		return None
	body_closure, _ = get_substack_closure(
		body_raw, blocks, inner_id, incoming_refs, graph
	)
	if body_closure is None:
		return None
	condition_a = outer_inputs.get("CONDITION")
	if condition_a is None:
		return None
	condition_b = inner_inputs.get("CONDITION")
	if not condition_is_pure(condition_b, blocks, graph):
		return None
	outer_owned = get_exclusive_input_subtree(
		condition_a, blocks, outer_id, incoming_refs, graph=graph
	)
	inner_owned = get_exclusive_input_subtree(
		condition_b, blocks, inner_id, incoming_refs, graph=graph
	)
	if outer_owned is None or inner_owned is None:
		return None
	if ({inner_id} | outer_owned | inner_owned) & commented:
		return None
	new_bid: str = _new_id()
	while new_bid in blocks:
		new_bid = _new_id()
	new_block = {
		"opcode": "operator_and",
		"next": None,
		"parent": outer_id,
		"inputs": {
			"OPERAND1": copy.deepcopy(condition_a),
			"OPERAND2": copy.deepcopy(condition_b),
		},
		"fields": {},
	}
	return {
		"outer_id": outer_id,
		"inner_id": inner_id,
		"new_id": new_bid,
		"new_block": new_block,
		"body_raw": copy.deepcopy(body_raw),
		"body_root": body_root,
	}


def branch_factor_sequence(blocks, root, closure):

	if not isinstance(root, str) or root not in closure:
		return None
	sequence = []
	current = root
	seen = set()
	while isinstance(current, str) and current in closure and current not in seen:
		block = blocks.get(current)
		if not isinstance(block, dict):
			return None
		seen.add(current)
		sequence.append(current)
		nxt = block.get("next")
		if nxt is None:
			return sequence
		if not isinstance(nxt, str) or nxt not in closure:
			return None
		current = nxt
	return None


def branch_factor_segment_closure(blocks, sequence, graph):

	closure = set(sequence)
	stack = []
	for bid in sequence:
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		for value in (block.get("inputs") or {}).values():
			stack.extend(direct_input_block_refs(value, blocks))
	while stack:
		bid = stack.pop()
		if bid in closure or bid not in blocks:
			continue
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		closure.add(bid)
		stack.extend(graph.edges.get(bid, ()))
	return closure


def branch_factor_value_equal(
	left, right, blocks, left_closure, right_closure, memo, active
):
	if left is right:
		return True
	if isinstance(left, str) or isinstance(right, str):
		if not isinstance(left, str) or not isinstance(right, str):
			return False
		left_local = left in left_closure
		right_local = right in right_closure
		if left_local or right_local:
			if not (left_local and right_local):
				return False
			return branch_factor_block_equal(
				left, right, blocks, left_closure, right_closure, memo, active, True
			)
		return left == right
	if isinstance(left, list) or isinstance(right, list):
		if (
			not isinstance(left, list)
			or not isinstance(right, list)
			or len(left) != len(right)
		):
			return False
		return all(
			branch_factor_value_equal(
				a, b, blocks, left_closure, right_closure, memo, active
			)
			for a, b in zip(left, right)
		)
	if isinstance(left, dict) or isinstance(right, dict):
		if (
			not isinstance(left, dict)
			or not isinstance(right, dict)
			or left.keys() != right.keys()
		):
			return False
		return all(
			branch_factor_value_equal(
				left[k], right[k], blocks, left_closure, right_closure, memo, active
			)
			for k in left
		)
	return left == right


def branch_factor_block_equal(
	a_id, b_id, blocks, left_closure, right_closure, memo, active, include_next=False
):
	pair = (a_id, b_id, include_next)
	if pair in memo:
		return memo[pair]
	if pair in active:
		return True
	if a_id not in left_closure or b_id not in right_closure:
		result = a_id == b_id
		memo[pair] = result
		return result
	active.add(pair)
	a = blocks.get(a_id)
	b = blocks.get(b_id)
	if not isinstance(a, dict) or not isinstance(b, dict):
		active.discard(pair)
		memo[pair] = False
		return False

	ignored = {"next", "parent", "topLevel", "x", "y"}
	if a.get("opcode") != b.get("opcode"):
		active.discard(pair)
		memo[pair] = False
		return False
	if {k: v for k, v in a.items() if k not in ignored} != {
		k: v for k, v in b.items() if k not in ignored
	}:
		for key in set(a) | set(b):
			if key in ignored:
				continue
			if key not in a or key not in b:
				active.discard(pair)
				memo[pair] = False
				return False
			if key in ("inputs",):
				continue
			if a[key] != b[key]:
				active.discard(pair)
				memo[pair] = False
				return False

	ain = a.get("inputs") or {}
	bin_ = b.get("inputs") or {}
	if ain.keys() != bin_.keys():
		active.discard(pair)
		memo[pair] = False
		return False
	for name in ain:
		if not branch_factor_value_equal(
			ain[name], bin_[name], blocks, left_closure, right_closure, memo, active
		):
			active.discard(pair)
			memo[pair] = False
			return False

	if include_next:
		an = a.get("next")
		bn = b.get("next")
		if an is None or bn is None:
			if an is not None or bn is not None:
				active.discard(pair)
				memo[pair] = False
				return False
		else:
			if not branch_factor_value_equal(
				an, bn, blocks, left_closure, right_closure, memo, active
			):
				active.discard(pair)
				memo[pair] = False
				return False

	active.discard(pair)
	memo[pair] = True
	return True


def branch_factor_common_prefix(left, right, blocks, left_closure, right_closure):
	limit = min(len(left), len(right))
	memo = {}
	count = 0
	for i in range(limit):
		if not branch_factor_block_equal(
			left[i], right[i], blocks, left_closure, right_closure, memo, set(), False
		):
			break
		count += 1
	return count


def branch_factor_common_suffix(left, right, blocks, left_closure, right_closure):
	limit = min(len(left), len(right))
	memo = {}
	count = 0
	for offset in range(1, limit + 1):
		if not branch_factor_block_equal(
			left[-offset],
			right[-offset],
			blocks,
			left_closure,
			right_closure,
			memo,
			set(),
			False,
		):
			break
		count += 1
	return count


def branch_factor_set_substack(inputs, name, root):
	value = inputs.get(name)
	if not isinstance(value, list) or len(value) < 2 or value[0] not in INPUT_TAGS:
		return None
	new = copy.deepcopy(value)
	new[1] = root
	return new


def branch_factor_record_removed_input(opts, ti, block_id, name):
	if getattr(opts, "script_rewrite_removed_inputs", None) is None:
		opts.script_rewrite_removed_inputs = {}
	opts.script_rewrite_removed_inputs.setdefault((ti, block_id), set()).add(name)


def branch_factor_owner_replace(blocks, control_id, new_id):
	owner = get_control_owner_edge(blocks, control_id)
	if owner is None:
		return None
	owner_id, edge_kind, edge_name = owner
	owner_block = blocks.get(owner_id)
	if not isinstance(owner_block, dict):
		return None
	preview = copy.deepcopy(owner_block)
	if get_input_replacement(preview, edge_kind, edge_name, control_id, new_id) is None:
		return None
	return owner_id, edge_kind, edge_name, owner_block, preview


def branch_factor_plan(target, control_id, graph, commented):
	blocks = target.get("blocks") or {}
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_if_else":
		return None
	inputs = control.get("inputs") or {}
	then_value = inputs.get("SUBSTACK")
	else_value = inputs.get("SUBSTACK2")
	then_root = get_control_substack(then_value, blocks)
	else_root = get_control_substack(else_value, blocks)
	if then_root is None or else_root is None:
		return None
	incoming = graph.incoming
	then_closure, then_tail = get_substack_closure(
		then_value, blocks, control_id, incoming, graph
	)
	else_closure, else_tail = get_substack_closure(
		else_value, blocks, control_id, incoming, graph
	)
	if then_closure is None or else_closure is None or then_closure & else_closure:
		return None
	then_sequence = branch_factor_sequence(blocks, then_root, then_closure)
	else_sequence = branch_factor_sequence(blocks, else_root, else_closure)
	if not then_sequence or not else_sequence:
		return None

	allow_prefix = (
		control.get("topLevel") is not True
		and get_control_owner_edge(blocks, control_id) is not None
	)
	prefix_len = (
		branch_factor_common_prefix(
			then_sequence, else_sequence, blocks, then_closure, else_closure
		)
		if allow_prefix
		else 0
	)
	suffix_len = branch_factor_common_suffix(
		then_sequence, else_sequence, blocks, then_closure, else_closure
	)

	if prefix_len == len(then_sequence) == len(else_sequence):
		keep_closure = branch_factor_segment_closure(blocks, then_sequence, graph)
		remove_closure = branch_factor_segment_closure(blocks, else_sequence, graph)
		condition_closure = get_exclusive_input_subtree(
			inputs.get("CONDITION"), blocks, control_id, incoming, graph=graph
		)
		if condition_closure is None:
			return None
		removed = {control_id} | remove_closure | condition_closure
		owner = branch_factor_owner_replace(blocks, control_id, then_sequence[0])
		if owner is not None and not (removed & commented):
			return {
				"kind": "eliminate",
				"control_id": control_id,
				"keep_root": then_sequence[0],
				"keep_tail": then_sequence[-1],
				"remove": removed,
				"owner": owner,
			}
		return None

	if prefix_len > 0:
		remove_segment = else_sequence[:prefix_len]
		remove = branch_factor_segment_closure(blocks, remove_segment, graph)
		if remove & commented:
			return None
		return {
			"kind": "prefix",
			"control_id": control_id,
			"keep_segment": then_sequence[:prefix_len],
			"remove": remove,
			"then_sequence": then_sequence,
			"else_sequence": else_sequence,
			"prefix_len": prefix_len,
			"owner": branch_factor_owner_replace(blocks, control_id, then_sequence[0]),
		}

	if suffix_len > 0 and not (suffix_len == len(then_sequence) == len(else_sequence)):
		remove_segment = else_sequence[-suffix_len:]
		remove = branch_factor_segment_closure(blocks, remove_segment, graph)
		if remove & commented:
			return None
		return {
			"kind": "suffix",
			"control_id": control_id,
			"keep_segment": then_sequence[-suffix_len:],
			"remove": remove,
			"then_sequence": then_sequence,
			"else_sequence": else_sequence,
			"suffix_len": suffix_len,
		}
	return None


def branch_factor_apply(target, plan, opts, stats, ti):
	blocks = target.get("blocks") or {}
	control_id = plan["control_id"]
	control = blocks.get(control_id)
	if not isinstance(control, dict):
		return False

	if plan["kind"] == "eliminate":
		owner_id, edge_kind, edge_name, _owner_block, preview = plan["owner"]
		keep_root = plan["keep_root"]
		keep_tail = plan["keep_tail"]
		old_next = control.get("next")
		if edge_kind == "next":
			preview["next"] = keep_root
			opts.script_rewrite_link_edits[(ti, owner_id, "next")] = keep_root
		else:
			opts.script_rewrite_input_edits[(ti, owner_id, edge_name)] = copy.deepcopy(
				preview.get("inputs", {}).get(edge_name)
			)
		blocks[owner_id] = preview
		blocks[keep_root]["parent"] = owner_id
		opts.script_rewrite_link_edits[(ti, keep_root, "parent")] = owner_id
		blocks[keep_tail]["next"] = old_next
		opts.script_rewrite_link_edits[(ti, keep_tail, "next")] = old_next
		if isinstance(old_next, str) and old_next in blocks:
			blocks[old_next]["parent"] = keep_tail
			opts.script_rewrite_link_edits[(ti, old_next, "parent")] = keep_tail
		record_removed_blocks(opts, ti, plan["remove"])
		for bid in plan["remove"]:
			blocks.pop(bid, None)
		stats["branch_controls_factored"] += 1
		stats["branch_factor_blocks_removed"] += len(plan["remove"])
		return True

	inputs = control.setdefault("inputs", {})
	if plan["kind"] == "prefix":
		owner = plan["owner"]
		if owner is None:
			return False
		owner_id, edge_kind, edge_name, _owner_block, preview = owner
		prefix_root = plan["keep_segment"][0]
		prefix_tail = plan["keep_segment"][-1]
		if edge_kind == "next":
			preview["next"] = prefix_root
			opts.script_rewrite_link_edits[(ti, owner_id, "next")] = prefix_root
		else:
			new_value = preview.get("inputs", {}).get(edge_name)
			opts.script_rewrite_input_edits[(ti, owner_id, edge_name)] = copy.deepcopy(
				new_value
			)
		blocks[owner_id] = preview
		blocks[prefix_root]["parent"] = owner_id
		opts.script_rewrite_link_edits[(ti, prefix_root, "parent")] = owner_id
		blocks[prefix_tail]["next"] = control_id
		opts.script_rewrite_link_edits[(ti, prefix_tail, "next")] = control_id
		control["parent"] = prefix_tail
		opts.script_rewrite_link_edits[(ti, control_id, "parent")] = prefix_tail
		for name, sequence in (
			("SUBSTACK", plan["then_sequence"]),
			("SUBSTACK2", plan["else_sequence"]),
		):
			remaining = sequence[plan["prefix_len"] :]
			if remaining:
				new_value = branch_factor_set_substack(inputs, name, remaining[0])
				if new_value is None:
					return False
				inputs[name] = new_value
				blocks[remaining[0]]["parent"] = control_id
				opts.script_rewrite_input_edits[(ti, control_id, name)] = copy.deepcopy(
					new_value
				)
				opts.script_rewrite_link_edits[(ti, remaining[0], "parent")] = (
					control_id
				)
			else:
				inputs.pop(name, None)
				branch_factor_record_removed_input(opts, ti, control_id, name)
		record_removed_blocks(opts, ti, plan["remove"])
		for bid in plan["remove"]:
			blocks.pop(bid, None)
		stats["branch_prefixes_factored"] += 1
		stats["branch_factor_blocks_removed"] += len(plan["remove"])
		return True

	suffix = plan["keep_segment"]
	suffix_root = suffix[0]
	suffix_tail = suffix[-1]
	then_seq = plan["then_sequence"]
	else_seq = plan["else_sequence"]
	split_then = len(then_seq) - plan["suffix_len"]
	split_else = len(else_seq) - plan["suffix_len"]
	if split_then:
		prev = blocks[then_seq[split_then - 1]]
		prev["next"] = None
		opts.script_rewrite_link_edits[(ti, then_seq[split_then - 1], "next")] = None
	else:
		inputs.pop("SUBSTACK", None)
		branch_factor_record_removed_input(opts, ti, control_id, "SUBSTACK")
	if split_else:
		prev = blocks[else_seq[split_else - 1]]
		prev["next"] = None
		opts.script_rewrite_link_edits[(ti, else_seq[split_else - 1], "next")] = None
	else:
		inputs.pop("SUBSTACK2", None)
		branch_factor_record_removed_input(opts, ti, control_id, "SUBSTACK2")

	old_next = control.get("next")
	control["next"] = suffix_root
	opts.script_rewrite_link_edits[(ti, control_id, "next")] = suffix_root
	blocks[suffix_root]["parent"] = control_id
	opts.script_rewrite_link_edits[(ti, suffix_root, "parent")] = control_id
	blocks[suffix_tail]["next"] = old_next
	opts.script_rewrite_link_edits[(ti, suffix_tail, "next")] = old_next
	if isinstance(old_next, str) and old_next in blocks:
		blocks[old_next]["parent"] = suffix_tail
		opts.script_rewrite_link_edits[(ti, old_next, "parent")] = suffix_tail
	for name, sequence, split in (
		("SUBSTACK", then_seq, split_then),
		("SUBSTACK2", else_seq, split_else),
	):
		if split:
			value = branch_factor_set_substack(inputs, name, sequence[0])
			if value is None:
				return False
			inputs[name] = value
			blocks[sequence[0]]["parent"] = control_id
			opts.script_rewrite_input_edits[(ti, control_id, name)] = copy.deepcopy(
				value
			)
			opts.script_rewrite_link_edits[(ti, sequence[0], "parent")] = control_id
	record_removed_blocks(opts, ti, plan["remove"])
	for bid in plan["remove"]:
		blocks.pop(bid, None)
	stats["branch_suffixes_factored"] += 1
	stats["branch_factor_blocks_removed"] += len(plan["remove"])
	return True


def factor_branches(project, stats, opts) -> int:
	total = 0

	for ti, target in enumerate(project.get("targets", [])):
		while True:
			graph = ProjectGraph(target)
			commented = get_comment_ids(target)
			candidates = [
				bid
				for bid, block in list((target.get("blocks") or {}).items())
				if isinstance(block, dict) and block.get("opcode") == "control_if_else"
			]
			changed = False

			for control_id in candidates:
				if control_id not in (target.get("blocks") or {}):
					continue

				plan = branch_factor_plan(target, control_id, graph, commented)
				if plan is None:
					continue

				before = _json_len(target)
				candidate_target = copy.deepcopy(target)
				trial = Options()
				try:
					branch_factor_apply(
						candidate_target, copy.deepcopy(plan), trial, Counter(), ti
					)
				except Exception:
					continue

				after = _json_len(candidate_target)
				if after >= before:
					stats["branch_factor_rejected_size"] += 1
					continue

				local = Counter()
				if not branch_factor_apply(target, plan, opts, local, ti):
					continue

				stats["branch_factor_bytes_saved"] += before - after
				for key, value in local.items():
					stats[key] += value
				_debug_structure_checkpoint(
					project,
					f"branch factoring ({plan['kind']}) in target {ti}, control block {control_id!r}",
				)
				total += 1
				changed = True
				break

			if not changed:
				break

	return total


def simplify_script_structures(project, stats, opts) -> int:
	targets = project.get("targets", [])
	script_prepare_maps(opts, targets)
	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = ProjectGraph(target)
		commented = get_comment_ids(target)
		candidate_ids = [
			bid
			for bid, block in blocks.items()
			if isinstance(block, dict)
			and block.get("opcode")
			in (
				"control_if",
				"control_if_else",
				"control_repeat_until",
				"control_repeat",
			)
		]
		while True:
			changed = False
			debug_rewrite_keys = (
				"branch_swaps",
				"empty_then_rewrites",
				"repeat_until_not_rewrites",
				"nested_if_merges",
			)
			debug_before_rewrites = {
				key: stats.get(key, 0) for key in debug_rewrite_keys
			}
			for bid in candidate_ids:
				block = blocks.get(bid)
				if not isinstance(block, dict) or bid not in blocks:
					continue
				incoming = graph.incoming

				if opts.branch_swapping:
					plan = plan_branch_swap(blocks, bid, incoming, graph, commented)
					if plan is not None:
						inputs = blocks[bid].setdefault("inputs", {})
						then_raw = copy.deepcopy(inputs.get("SUBSTACK"))
						else_raw = copy.deepcopy(inputs.get("SUBSTACK2"))
						inputs["CONDITION"] = plan["condition"]
						inputs["SUBSTACK"], inputs["SUBSTACK2"] = else_raw, then_raw
						for moved in plan["moved_ids"]:
							if isinstance(blocks.get(moved), dict):
								blocks[moved]["parent"] = bid
								opts.script_rewrite_link_edits[
									(ti, moved, "parent")
								] = bid

						for n in ("CONDITION", "SUBSTACK", "SUBSTACK2"):
							opts.script_rewrite_input_edits[(ti, bid, n)] = (
								copy.deepcopy(inputs[n])
							)

						record_removed_blocks(opts, ti, plan["removed"])

						for dead in plan["removed"]:
							blocks.pop(dead, None)

						graph.refresh_blocks((bid, *plan["removed"]))
						stats["branch_swaps"] += 1
						changed = True
						break

				if opts.branch_swapping:
					plan = plan_empty_then(blocks, bid, incoming, graph, commented)

					if plan is not None:
						control = blocks[bid]
						inputs = control.setdefault("inputs", {})
						inputs["CONDITION"] = copy.deepcopy(
							plan["condition"]
							if plan["mode"] == "unwrap"
							else [INPUT_BLOCK_NO_SHADOW, plan["new_id"]]
						)
						inputs["SUBSTACK"] = plan["else_raw"]
						inputs.pop("SUBSTACK2", None)

						control["opcode"] = "control_if"
						opts.script_rewrite_opcode_edits[(ti, bid)] = "control_if"
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = (
							copy.deepcopy(inputs["CONDITION"])
						)
						opts.script_rewrite_input_edits[(ti, bid, "SUBSTACK")] = (
							copy.deepcopy(inputs["SUBSTACK"])
						)
						opts.script_rewrite_removed_inputs[(ti, bid)] = {"SUBSTACK2"}

						if plan["mode"] == "unwrap":
							for moved in plan["moved_ids"]:
								if isinstance(blocks.get(moved), dict):
									blocks[moved]["parent"] = bid
									opts.script_rewrite_link_edits[
										(ti, moved, "parent")
									] = bid
						else:
							for ref in direct_input_block_refs(
								plan["new_block"]["inputs"]["OPERAND"], blocks
							):
								if isinstance(blocks.get(ref), dict):
									blocks[ref]["parent"] = plan["new_id"]
									opts.script_rewrite_link_edits[
										(ti, ref, "parent")
									] = plan["new_id"]

							blocks[plan["new_id"]] = plan["new_block"]
							record_new_blocks(opts, ti, {plan["new_id"]})

						record_removed_blocks(opts, ti, plan["removed"])

						for dead in plan["removed"]:
							blocks.pop(dead, None)

						graph.refresh_blocks(
							(bid, plan.get("new_id"), *plan["removed"])
						)
						stats["empty_then_rewrites"] += 1
						changed = True
						break

				if opts.trivial_loops:
					plan = plan_repeat_until_not(
						blocks, bid, incoming, graph, commented
					)

					if plan is not None:
						control = blocks[bid]
						control["opcode"] = "control_while"
						control.setdefault("inputs", {})["CONDITION"] = plan[
							"condition"
						]
						opts.script_rewrite_opcode_edits[(ti, bid)] = "control_while"
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = (
							copy.deepcopy(control["inputs"]["CONDITION"])
						)
						for moved in plan["moved_ids"]:
							if isinstance(blocks.get(moved), dict):
								blocks[moved]["parent"] = bid
								opts.script_rewrite_link_edits[
									(ti, moved, "parent")
								] = bid
						record_removed_blocks(opts, ti, plan["removed"])
						for dead in plan["removed"]:
							blocks.pop(dead, None)
						graph.refresh_blocks((bid, *plan["removed"]))
						stats["repeat_until_not_rewrites"] += 1
						changed = True
						break

				if opts.nested_conditionals:
					plan = plan_nested_if(blocks, bid, incoming, graph, commented)

					if plan is not None:
						outer = blocks[bid]
						new_id = plan["new_id"]

						for input_name in ("OPERAND1", "OPERAND2"):
							for ref in direct_input_block_refs(
								plan["new_block"]["inputs"][input_name], blocks
							):
								if isinstance(blocks.get(ref), dict):
									blocks[ref]["parent"] = new_id
									opts.script_rewrite_link_edits[
										(ti, ref, "parent")
									] = new_id

						blocks[new_id] = plan["new_block"]
						outer.setdefault("inputs", {})["CONDITION"] = [
							INPUT_BLOCK_NO_SHADOW,
							new_id,
						]
						outer["inputs"]["SUBSTACK"] = plan["body_raw"]
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = [
							INPUT_BLOCK_NO_SHADOW,
							new_id,
						]
						opts.script_rewrite_input_edits[(ti, bid, "SUBSTACK")] = (
							copy.deepcopy(plan["body_raw"])
						)

						if isinstance(blocks.get(plan["body_root"]), dict):
							blocks[plan["body_root"]]["parent"] = bid
							opts.script_rewrite_link_edits[
								(ti, plan["body_root"], "parent")
							] = bid

						record_new_blocks(opts, ti, {new_id})
						record_removed_blocks(opts, ti, {plan["inner_id"]})
						blocks.pop(plan["inner_id"], None)
						graph.refresh_blocks((bid, new_id, plan["inner_id"]))
						stats["nested_if_merges"] += 1
						changed = True
						break
			if changed and _DEBUG_STRUCTURE_ENABLED:
				debug_changed = next(
					(
						key
						for key in debug_rewrite_keys
						if stats.get(key, 0) > debug_before_rewrites[key]
					),
					"unknown control rewrite",
				)
				_debug_structure_checkpoint(
					project, f"{debug_changed} in target {ti}, control block {bid!r}"
				)
			if not changed:
				break

	if opts.branch_factoring:
		factor_branches(project, stats, opts)

	return (
		sum(
			stats.get(k, 0)
			for k in (
				"branch_swaps",
				"empty_then_rewrites",
				"repeat_until_not_rewrites",
				"nested_if_merges",
			)
		)
		+ stats.get("branch_prefixes_factored", 0)
		+ stats.get("branch_suffixes_factored", 0)
		+ stats.get("branch_controls_factored", 0)
	)


def plan_associations(blocks, outer_id, graph, commented):
	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("next") is not None:
		return None

	op = outer.get("opcode")

	if op not in OPERATOR_INPUTS.keys():
		return None

	left_name = OPERATOR_INPUTS[op] + "1"
	right_name = OPERATOR_INPUTS[op] + "2"
	outer_left = (outer.get("inputs") or {}).get(left_name)
	outer_right = (outer.get("inputs") or {}).get(right_name)
	inner_id = get_input_root(outer_left, blocks)
	inner = blocks.get(inner_id) if inner_id else None

	if (
		not isinstance(inner, dict)
		or inner.get("opcode") != op
		or inner.get("next") is not None
	):
		return None

	inner_inputs = inner.get("inputs") or {}
	inner_left = inner_inputs.get(left_name)
	inner_right = inner_inputs.get(right_name)
	c1 = constexpr_from_input(inner_right, blocks)
	c2 = constexpr_from_input(outer_right, blocks)

	if inner_left is None or c1 is None or c2 is None:
		return None

	owned = get_exclusive_input_subtree(
		outer_left, blocks, outer_id, graph.incoming, graph=graph
	)

	if owned is None:
		return None

	survivor_owned = get_exclusive_input_subtree(
		inner_left, blocks, inner_id, graph.incoming, graph=graph
	)

	if survivor_owned is None or inner_id not in owned or not survivor_owned <= owned:
		return None

	removed = owned - survivor_owned

	if removed & commented:
		return None

	# fold
	if op == "operator_join":
		combined = _constant(
			"string",
			_constant_to_string(c1[0]) + _constant_to_string(c2[0]),
		)

	elif op in ("operator_add", "operator_multiply"):
		a = constant_to_number(c1[0])
		b = constant_to_number(c2[0])

		if a is None or b is None:
			return None

		if not math.isfinite(a) or not math.isfinite(b):
			return None

		value = a + b if op == "operator_add" else a * b

		if not math.isfinite(value):
			return None

		combined = _constant("number", value)

	elif op in ("operator_and", "operator_or"):
		a = _constant_to_bool(c1[0])
		b = _constant_to_bool(c2[0])

		value = a and b if op == "operator_and" else a or b
		combined = _constant("bool", value)

	new_const, _ = constant_to_input(combined, blocks, outer_id)

	if new_const is None:
		return None

	return {
		"inner_id": inner_id,
		"base": copy.deepcopy(inner_left),
		"new_const": new_const,
		"removed": removed,
	}


def merge_associative_constants(project, stats, opts: Options):
	script_prepare_maps(opts, project.get("targets", []))
	count: int = 0

	for ti, target in enumerate(project.get("targets", [])):
		blocks: dict = target.get("blocks") or {}
		graph = ProjectGraph(target)
		commented: set = get_comment_ids(target)

		while True:
			changed: bool = False

			for bid in list(blocks):
				plan: dict[str, Any] | None = plan_associations(
					blocks, bid, graph, commented
				)
				if plan is None:
					continue

				outer = blocks[bid]
				opcode = outer.get("opcode", "default")
				name = OPERATOR_INPUTS[opcode] + "1"
				name2 = OPERATOR_INPUTS[opcode] + "2"

				for ref in direct_input_block_refs(plan["base"], blocks):
					if isinstance(blocks.get(ref), dict):
						blocks[ref]["parent"] = bid
						opts.script_rewrite_link_edits[(ti, ref, "parent")] = bid

				outer.setdefault("inputs", {})[name] = plan["base"]
				outer["inputs"][name2] = plan["new_const"]
				opts.script_rewrite_input_edits[(ti, bid, name)] = copy.deepcopy(
					plan["base"]
				)
				opts.script_rewrite_input_edits[(ti, bid, name2)] = copy.deepcopy(
					plan["new_const"]
				)

				for remove_id in plan["removed"]:
					blocks.pop(remove_id, None)

				record_removed_blocks(opts, ti, plan["removed"])
				graph.refresh_after_mutation(
					changed={bid} | set(direct_input_block_refs(plan["base"], blocks)),
					removed=set(plan["removed"]),
				)
				stats["associative_constant_merges"] += 1
				count += 1
				changed = True
				break

			if not changed:
				break

	return count


def get_var_id(block: Any):
	field = (
		(block.get("fields") or {}).get("VARIABLE") if isinstance(block, dict) else None
	)
	return (
		field[1]
		if isinstance(field, list) and len(field) > 1 and isinstance(field[1], str)
		else None
	)


def replace_known_reads(
	value,
	env,
	blocks,
	owner_id,
	ti,
	opts,
	graph,
	allow_literal: bool = True,
	visiting=frozenset(),
) -> tuple[list, bool, set]:
	if not isinstance(value, list) or not value:
		return value, False, set()

	if (
		len(value) >= 3
		and scratch_numeric_tag(value[0]) == PRIMITIVE_VARIABLE
		and isinstance(value[2], str)
		and value[2] in env
	):
		if not allow_literal:
			return value, False, set()
		entry = env[value[2]]
		return [INPUT_SAME_BLOCK_SHADOW, copy.deepcopy(entry)], True, set()

	tag = scratch_numeric_tag(value[0])

	if tag in INPUT_TAGS and len(value) > 1 and isinstance(value[1], str):
		ref = value[1]
		child = blocks.get(ref)

		if isinstance(child, dict) and child.get("opcode") == "data_variable":
			vid = get_var_id(child)
			if allow_literal and vid in env and child.get("parent") == owner_id:
				owned = get_exclusive_input_subtree(
					value, blocks, owner_id, graph.incoming, graph=graph
				)
				if owned == {ref}:
					return (
						[INPUT_SAME_BLOCK_SHADOW, copy.deepcopy(env[vid])],
						True,
						{ref},
					)

		if (
			isinstance(child, dict)
			and ref not in visiting
			and child.get("next") is None
			and not child.get("topLevel", False)
			and child.get("opcode")
			not in {
				"procedures_prototype",
				"procedures_definition",
			}
		):
			owned = get_exclusive_input_subtree(
				value,
				blocks,
				owner_id,
				graph.incoming,
				graph=graph,
			)

			if owned is not None and ref in owned:
				child_changed = False
				removed = set()
				child_visiting = visiting | {ref}

				for child_input_name, child_raw in list(
					(child.get("inputs") or {}).items()
				):
					new, c, dead = replace_known_reads(
						child_raw,
						env,
						blocks,
						ref,
						ti,
						opts,
						graph,
						allow_literal=not is_boolean_slot(child, child_input_name),
						visiting=child_visiting,
					)

					if not c:
						continue

					child.setdefault("inputs", {})[child_input_name] = new
					opts.script_rewrite_input_edits[(ti, ref, child_input_name)] = (
						copy.deepcopy(new)
					)

					for dead_id in dead:
						blocks.pop(dead_id, None)

					record_removed_blocks(opts, ti, dead)
					graph.refresh_after_mutation(
						changed={ref},
						removed=set(dead),
					)
					child_changed = True
					removed.update(dead)

				if child_changed:
					return value, True, removed

		return value, False, set()

	if tag == INPUT_DIFF_BLOCK_SHADOW:
		changed = False
		removed = set()

		for i in (1, 2):
			if i >= len(value) or not isinstance(value[i], list):
				continue
			new, c, dead = replace_known_reads(
				value[i],
				env,
				blocks,
				owner_id,
				ti,
				opts,
				graph,
				allow_literal=True,
				visiting=visiting,
			)
			if c:
				if i == 1 and is_literal_input(new):
					return new, True, removed | dead

				value[i] = new
				changed = True
			removed |= dead

		return value, changed, removed
	return value, False, set()


def get_literal_payload(value) -> list | None:
	literal = literal_from_input(value)
	if literal is None:
		return None
	return literal


def get_cfg_statement_roots(blocks) -> list:
	roots = []

	for bid, block in blocks.items():
		if not isinstance(block, dict):
			continue
		if block.get("topLevel") is True:
			roots.append(bid)
		elif block.get("parent") is None and is_hat_block(block):
			roots.append(bid)

	return roots


def get_cfg_tail(blocks, root) -> str | None:
	if not isinstance(root, str) or root not in blocks:
		return None

	current = root
	seen = set()

	while current in blocks and current not in seen:
		seen.add(current)
		block = blocks.get(current)
		if not isinstance(block, dict):
			return None

		nxt = block.get("next")
		if not isinstance(nxt, str) or nxt not in blocks:
			return current

		current = nxt

	return None


def cfg_may_fall_through(blocks, block_id, memo=None, active=None) -> bool:
	if memo is None:
		memo = {}

	if active is None:
		active = set()

	if block_id in memo:
		return memo[block_id]

	if block_id in active:
		return True

	block = blocks.get(block_id)
	if not isinstance(block, dict):
		memo[block_id] = False
		return False

	op = block.get("opcode", "")
	if op == "control_stop":
		field = (block.get("fields") or {}).get("STOP_OPTION")
		option = (
			field[0]
			if isinstance(field, list) and field and isinstance(field[0], str)
			else None
		)
		result = option == "other scripts in sprite"
		memo[block_id] = result
		return result

	if op in {
		"control_forever",
		"control_stop_all",
		"control_stop_other_scripts",
		"control_delete_this_clone",
	}:
		memo[block_id] = False
		return False

	active.add(block_id)
	try:
		if op == "control_if":
			inputs = block.get("inputs") or {}
			root = get_control_substack(inputs.get("SUBSTACK"), blocks)
			result = (
				True
				if root is None
				else cfg_substack_may_fall_through(blocks, root, memo, active)
			)
		elif op == "control_if_else":
			inputs = block.get("inputs") or {}
			then_root = get_control_substack(inputs.get("SUBSTACK"), blocks)
			else_root = get_control_substack(inputs.get("SUBSTACK2"), blocks)
			then_fall = (
				True
				if then_root is None
				else cfg_substack_may_fall_through(blocks, then_root, memo, active)
			)
			else_fall = (
				True
				if else_root is None
				else cfg_substack_may_fall_through(blocks, else_root, memo, active)
			)
			result = then_fall or else_fall
		else:
			result = True
	finally:
		active.discard(block_id)

	memo[block_id] = result
	return result


def cfg_substack_may_fall_through(blocks, root, memo=None, active=None) -> bool:
	if root is None:
		return True

	tail = get_cfg_tail(blocks, root)
	if tail is None:
		return True

	return cfg_may_fall_through(blocks, tail, memo, active)


def build_cfg(target) -> tuple:
	blocks = target.get("blocks") or {}
	roots = get_cfg_statement_roots(blocks)
	successors = {bid: set() for bid in roots}
	predecessors = {bid: set() for bid in roots}
	seen_by_root = {root: set() for root in roots}
	scheduler_edges = set()

	def add_node(root, bid) -> bool:
		if (
			not isinstance(bid, str)
			or bid not in blocks
			or not isinstance(blocks.get(bid), dict)
		):
			return False

		seen_by_root[root].add(bid)
		successors.setdefault(bid, set())
		predecessors.setdefault(bid, set())
		return True

	def add_edge(a, b, root, *, scheduler=False) -> None:
		if not add_node(root, a):
			return

		if (
			not isinstance(b, str)
			or b not in blocks
			or not isinstance(blocks.get(b), dict)
		):
			return

		add_node(root, b)
		successors[a].add(b)
		predecessors[b].add(a)

		if scheduler:
			scheduler_edges.add((a, b))

	def process_chain(
		first,
		fallthrough,
		root,
		active=None,
		scheduler_fallthrough=None,
	):
		if active is None:
			active = set()
		current = first
		local_seen = set()

		while (
			isinstance(current, str) and current in blocks and current not in local_seen
		):
			local_seen.add(current)
			if current in active:
				return

			active.add(current)
			block = blocks.get(current)
			if not isinstance(block, dict):
				active.discard(current)
				return

			add_node(root, current)
			op = block.get("opcode", "")
			inputs = block.get("inputs") or {}
			nxt = block.get("next")
			continuation = (
				nxt if isinstance(nxt, str) and nxt in blocks else fallthrough
			)

			def add_continuation_edge(*, force_scheduler=False):
				if continuation is None:
					return

				is_scheduler = force_scheduler or op in _CFG_SCHEDULER_YIELD_OPCODES
				if (
					scheduler_fallthrough is not None
					and continuation == scheduler_fallthrough
				):
					is_scheduler = True
				add_edge(current, continuation, root, scheduler=is_scheduler)

			match op:
				case "control_if":
					body_root = get_control_substack(inputs.get("SUBSTACK"), blocks)
					if body_root is not None:
						add_edge(current, body_root, root)
						process_chain(
							body_root,
							continuation,
							root,
							active,
							scheduler_fallthrough=scheduler_fallthrough,
						)

					add_continuation_edge()

				case "control_if_else":
					then_root = get_control_substack(inputs.get("SUBSTACK"), blocks)
					else_root = get_control_substack(inputs.get("SUBSTACK2"), blocks)
					if then_root is not None:
						add_edge(current, then_root, root)
						process_chain(
							then_root,
							continuation,
							root,
							active,
							scheduler_fallthrough=scheduler_fallthrough,
						)
					else:
						add_continuation_edge()

					if else_root is not None:
						add_edge(current, else_root, root)
						process_chain(
							else_root,
							continuation,
							root,
							active,
							scheduler_fallthrough=scheduler_fallthrough,
						)
					else:
						add_continuation_edge()

				case "control_repeat" | "control_repeat_until" | "control_while":
					body_root = get_control_substack(inputs.get("SUBSTACK"), blocks)
					add_continuation_edge()
					if body_root is not None:
						add_edge(current, body_root, root)
						process_chain(
							body_root,
							current,
							root,
							active,
							scheduler_fallthrough=current,
						)

				case "control_forever":
					body_root = get_control_substack(inputs.get("SUBSTACK"), blocks)
					if body_root is not None:
						add_edge(current, body_root, root)
						process_chain(
							body_root,
							current,
							root,
							active,
							scheduler_fallthrough=current,
						)

				case (
					"control_stop"
					| "control_stop_all"
					| "control_stop_other_scripts"
					| "control_delete_this_clone"
				):
					if op == "control_stop":
						field = (block.get("fields") or {}).get("STOP_OPTION")
						option = (
							field[0]
							if isinstance(field, list)
							and field
							and isinstance(field[0], str)
							else None
						)
						if (
							option == "other scripts in sprite"
							and continuation is not None
						):
							add_continuation_edge()

				case _:
					add_continuation_edge()

			active.discard(current)
			if not isinstance(nxt, str) or nxt not in blocks:
				return

			current = nxt

	for root in roots:
		if not add_node(root, root):
			continue

		process_chain(root, None, root)

	for root, nodes in seen_by_root.items():
		for bid in nodes:
			successors.setdefault(bid, set()).intersection_update(nodes)
			predecessors.setdefault(bid, set()).intersection_update(nodes)

	scheduler_edges.intersection_update(
		{
			(a, b)
			for nodes in seen_by_root.values()
			for a in nodes
			for b in successors.get(a, ())
		}
	)

	return roots, seen_by_root, successors, predecessors, scheduler_edges


def cfg_meet_constant_envs(envs):
	if not envs:
		return {}

	common = dict(envs[0])

	for env in envs[1:]:
		for vid in list(common):
			if vid not in env or env[vid] != common[vid]:
				del common[vid]

	return common


def _cfg_is_hard_constant_barrier(block: Any) -> bool:
	if not isinstance(block, dict):
		return True

	op = block.get("opcode", "")
	return op in {
		"procedures_call",
		"control_wait_until",
		"sensing_askandwait",
		"sound_playuntildone",
		"event_broadcastandwait",
		"control_create_clone_of",
		"control_delete_this_clone",
	}


def _cfg_transfer_edge_constants(env, pred, succ, scheduler_edges):
	if (pred, succ) in scheduler_edges:
		return {}
	return env


def _cfg_transfer_constants(block, env):
	out = dict(env)
	if not isinstance(block, dict):
		return out
	op = block.get("opcode", "")
	if op == "data_setvariableto":
		vid = get_var_id(block)
		lit = get_literal_payload((block.get("inputs") or {}).get("VALUE"))
		if vid and lit is not None:
			out[vid] = lit
		elif vid:
			out.pop(vid, None)
	elif op == "data_changevariableby":
		vid = get_var_id(block)
		if vid:
			out.pop(vid, None)
	elif op == "control_for_each":
		vid = get_var_id(block)
		if vid:
			out.pop(vid, None)
	if _cfg_is_hard_constant_barrier(block):
		out.clear()
	return out


def _cfg_propagate_root_constants(
	blocks,
	root,
	nodes,
	successors,
	predecessors,
	scheduler_edges,
	ti,
	opts,
	graph,
):

	in_env = {bid: {} for bid in nodes}
	out_env = {bid: None for bid in nodes}
	worklist = [root]
	queued = {root}
	changes = 0

	while worklist:
		bid = worklist.pop()
		queued.discard(bid)
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue

		if bid == root:
			new_in = {}
		else:
			pred_envs = []
			for pred in predecessors.get(bid, ()):
				env = out_env.get(pred)
				if env is None:
					continue
				env = _cfg_transfer_edge_constants(
					env,
					pred,
					bid,
					scheduler_edges,
				)
				pred_envs.append(env)
			new_in = cfg_meet_constant_envs(pred_envs)

		node_changed = new_in != in_env.get(bid, {})
		if node_changed:
			in_env[bid] = new_in
			changes += 1

		new_out = _cfg_transfer_constants(block, new_in)
		if new_out != out_env.get(bid):
			out_env[bid] = new_out
			node_changed = True
			changes += 1

		if node_changed:
			for succ in successors.get(bid, set()):
				if succ not in queued:
					worklist.append(succ)
					queued.add(succ)

	propagated = 0
	for bid in nodes:
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		env = in_env.get(bid, {})
		for input_name, raw in list((block.get("inputs") or {}).items()):
			new, changed, dead = replace_known_reads(
				raw,
				env,
				blocks,
				bid,
				ti,
				opts,
				graph,
				allow_literal=not is_boolean_slot(block, input_name),
			)
			if not changed:
				continue
			block.setdefault("inputs", {})[input_name] = new
			opts.script_rewrite_input_edits[(ti, bid, input_name)] = copy.deepcopy(new)
			for dead_id in dead:
				blocks.pop(dead_id, None)
			record_removed_blocks(opts, ti, dead)
			graph.refresh_after_mutation(changed={bid}, removed=set(dead))
			propagated += 1

	return propagated, changes


def constant_propagation(project, stats, opts):
	script_prepare_maps(opts, project.get("targets", []))
	total = 0
	analysis_changes = 0

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		if not blocks:
			continue
		roots, nodes_by_root, successors, predecessors, scheduler_edges = build_cfg(
			target
		)
		graph = ProjectGraph(target)
		for root in roots:
			nodes = nodes_by_root.get(root, set())
			if not nodes:
				continue
			count, iterations = _cfg_propagate_root_constants(
				blocks,
				root,
				nodes,
				successors,
				predecessors,
				scheduler_edges,
				ti,
				opts,
				graph,
			)
			total += count
			analysis_changes += iterations

	stats["script_constants_propagated"] += total
	stats["script_constant_cfg_changes"] += analysis_changes
	return total


def _numeric_data_literal(value):
	if not isinstance(value, str) or not value:
		return None
	try:
		iv = int(value)
		if str(iv) == value:
			return iv
	except (ValueError, OverflowError):
		...
	try:
		fv = float(value)
	except (ValueError, OverflowError):
		return None
	if (
		not math.isfinite(fv)
		or "." not in value
		or "e" in value.lower()
		or str(fv) != value
	):
		return None
	return fv


def compact_data_literals(project, stats):
	count = 0
	for target in project.get("targets", []):
		for entry in (target.get("variables") or {}).values():
			if isinstance(entry, list) and len(entry) >= 2:
				new = _numeric_data_literal(entry[1])
				if new is not None:
					entry[1] = new
					count += 1
		for entry in (target.get("lists") or {}).values():
			if not (
				isinstance(entry, list)
				and len(entry) >= 2
				and isinstance(entry[1], list)
			):
				continue
			for i, value in enumerate(entry[1]):
				new = _numeric_data_literal(value)
				if new is not None:
					entry[1][i] = new
					count += 1
	stats["data_literals_compacted"] += count
	return count


def strip_reference_names(project, stats):
	scopes = get_ref_scopes(project)
	count = 0
	for path, container, index, ident, kind in get_ref_name_slots(project):
		name = container[index]
		if (
			type(name) is str
			and name
			and type(ident) is str
			and ident
			and ident not in ("__proto__", "constructor", "prototype")
			and scopes[path[1]].get(ident) == (kind, name)
		):
			container[index] = ""
			count += 1
	stats["reference_names_stripped"] += count
	return count


def remove_unused_extensions(project, stats, opts):
	extensions = project.get("extensions")
	if not isinstance(extensions, list):
		return 0
	used = {
		block.get("opcode")
		for target in project.get("targets", [])
		for block in (target.get("blocks") or {}).values()
		if isinstance(block, dict) and isinstance(block.get("opcode"), str)
	}
	removed = []
	for ext in list(extensions):
		prefix = _EXTENSION_PREFIXES.get(ext)
		if prefix is not None and not any(op.startswith(prefix) for op in used):
			extensions.remove(ext)
			removed.append(ext)
	opts.removed_extensions.update(removed)
	stats["unused_extensions_removed"] += len(removed)
	return len(removed)


def _compact_data_value_equal(original, minified):
	if original == minified:
		return True
	converted = _numeric_data_literal(original) if isinstance(original, str) else None
	return converted is not None and converted == minified


def _compact_data_entry_equal(original, minified):
	if not (
		isinstance(original, list)
		and isinstance(minified, list)
		and len(original) == len(minified)
		and original[0] == minified[0]
	):
		return False
	if len(original) < 2:
		return original == minified
	if isinstance(original[1], list) and isinstance(minified[1], list):
		return len(original[1]) == len(minified[1]) and all(
			_compact_data_value_equal(a, b) for a, b in zip(original[1], minified[1])
		)
	return _compact_data_value_equal(original[1], minified[1])


def fold_constant_expressions(
	project: dict[str, Any], stats: dict[str, int], opts: Options
):
	targets = project.get("targets", [])
	if opts.folded_constant_expression_link_edits is None:
		opts.folded_constant_expression_link_edits = {}

	if opts.folded_constant_expression_inputs is None:
		opts.folded_constant_expression_inputs = {}

	if opts.folded_constant_expression_opcode_edits is None:
		opts.folded_constant_expression_opcode_edits = {}

	if opts.folded_constant_expression_blocks is None or len(
		opts.folded_constant_expression_blocks
	) != len(targets):
		opts.folded_constant_expression_blocks = [set() for _ in targets]

	if opts.folded_constant_expression_new_blocks is None or len(
		opts.folded_constant_expression_new_blocks
	) != len(targets):
		opts.folded_constant_expression_new_blocks = [set() for _ in targets]

	folded = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = ProjectGraph(target)
		incoming_refs = graph.incoming
		candidate_inputs = [
			(block_id, input_name)
			for block_id, block in blocks.items()
			if isinstance(block, dict)
			for input_name, input_val in (block.get("inputs") or {}).items()
			if (
				isinstance(input_val, list)
				and len(input_val) > 1
				and input_val[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)
				and isinstance(input_val[1], str)
				and isinstance(blocks.get(input_val[1]), dict)
				and str(blocks[input_val[1]].get("opcode", "")).startswith("operator_")
			)
		]
		commented_ids = {
			c.get("blockId")
			for c in (target.get("comments") or {}).values()
			if isinstance(c, dict) and c.get("blockId") is not None
		}

		while True:
			changed = False
			for block_id, input_name in candidate_inputs:
				block = blocks.get(block_id)
				if not isinstance(block, dict):
					continue

				inputs_now = block.get("inputs") or {}
				input_val = inputs_now.get(input_name)

				if input_val is None:
					continue

				if not (
					isinstance(input_val, list)
					and len(input_val) > 1
					and input_val[0]
					in (
						INPUT_SAME_BLOCK_SHADOW,
						INPUT_BLOCK_NO_SHADOW,
						INPUT_DIFF_BLOCK_SHADOW,
					)
				):
					continue

				if input_val[0] in (INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW):
					root_id = input_val[1]
					root = blocks.get(root_id) if isinstance(root_id, str) else None

					if not isinstance(root, dict) or not str(
						root.get("opcode", "")
					).startswith("operator_"):
						continue

					abs_pattern = simplify_abs_patterns(
						input_val, blocks, block_id, incoming_refs, graph
					)
					if abs_pattern is not None:
						removed_ids = abs_pattern["removed"]
						outer_id = abs_pattern["outer_id"]
						abs_id = abs_pattern["abs_id"]

						if (
							not fold_blocked_by_comment(
								removed_ids | {outer_id}, blocks, commented_ids
							)
						):
							outer = blocks[outer_id]
							outer_inputs = outer.setdefault("inputs", {})
							outer_inputs["OPERAND1"] = copy.deepcopy(
								abs_pattern["outer_left"]
							)
							opts.folded_constant_expression_inputs[
								(ti, outer_id, "OPERAND1")
							] = copy.deepcopy(abs_pattern["outer_left"])

							owner_input = copy.deepcopy(abs_pattern["owner_input"])
							block.setdefault("inputs", {})[input_name] = owner_input
							opts.folded_constant_expression_inputs[
								(ti, block_id, input_name)
							] = copy.deepcopy(owner_input)

							blocks[abs_id] = abs_pattern["abs_block"]
							opts.folded_constant_expression_new_blocks[ti].add(abs_id)

							for child_id in direct_input_block_refs(
								abs_pattern["abs_block"]["inputs"]["NUM"], blocks
							):
								child = blocks.get(child_id)

								if isinstance(child, dict):
									child["parent"] = abs_id
									opts.folded_constant_expression_link_edits[
										(ti, child_id, "parent")
									] = abs_id

							outer["parent"] = block_id
							opts.folded_constant_expression_link_edits[
								(ti, outer_id, "parent")
							] = block_id

							for remove_id in removed_ids:
								blocks.pop(remove_id, None)

							opts.folded_constant_expression_blocks[ti].update(
								removed_ids
							)
							graph.refresh_blocks(
								(block_id, outer_id, abs_id, *removed_ids)
							)
							incoming_refs = graph.incoming
							stats["algebraic_simplifications"] += 1
							folded += 1
							changed = True
							break

					if changed:
						break

					boolean_negation = simplify_double_boolean_negation(
						input_val, blocks, block_id, input_name, incoming_refs, graph
					)
					if boolean_negation is not None:
						replacement, removed_ids = boolean_negation
						if not fold_blocked_by_comment(
							removed_ids, blocks, commented_ids
						) and not fold_ids_have_external_refs(
							removed_ids, blocks, block_id, graph=graph
						):
							new_input = copy.deepcopy(replacement)
							for child_id in _input_block_ids(new_input, blocks):
								child = blocks.get(child_id)
								if (
									isinstance(child, dict)
									and child.get("parent") != block_id
								):
									opts.folded_constant_expression_link_edits[
										(ti, child_id, "parent")
									] = block_id
							_reparent_serialized_input(new_input, blocks, block_id)
							block.setdefault("inputs", {})[input_name] = new_input
							opts.folded_constant_expression_inputs[
								(ti, block_id, input_name)
							] = copy.deepcopy(new_input)
							opts.folded_constant_expression_blocks[ti].update(
								removed_ids
							)
							for remove_id in removed_ids:
								blocks.pop(remove_id, None)
							graph.refresh_blocks((block_id, *removed_ids))
							incoming_refs = graph.incoming
							stats["boolean_constants_propagated"] += 1
							folded += 1
							changed = True
							break

					if changed:
						break

					algebraic = simplify_algebraic_input(
						input_val, blocks, block_id, incoming_refs, graph
					)
					if algebraic is not None:
						replacement, removed_ids = algebraic
						if not fold_blocked_by_comment(
							removed_ids, blocks, commented_ids
						) and not fold_ids_have_external_refs(
							removed_ids, blocks, block_id, graph=graph
						):
							new_input = copy.deepcopy(replacement)
							if not (
								is_boolean_slot(block, input_name)
								and is_literal_input(new_input)
							):
								for child_id in _input_block_ids(new_input, blocks):
									child = blocks.get(child_id)
									if (
										isinstance(child, dict)
										and child.get("parent") != block_id
									):
										opts.folded_constant_expression_link_edits[
											(ti, child_id, "parent")
										] = block_id

								_reparent_serialized_input(new_input, blocks, block_id)
								block.setdefault("inputs", {})[input_name] = new_input
								opts_key = (ti, block_id, input_name)
								opts.folded_constant_expression_inputs[opts_key] = (
									copy.deepcopy(new_input)
								)
								opts.folded_constant_expression_blocks[ti].update(
									removed_ids
								)
								for remove_id in removed_ids:
									blocks.pop(remove_id, None)
								graph.refresh_blocks((block_id, *removed_ids))
								incoming_refs = graph.incoming
								stats["algebraic_simplifications"] += 1
								folded += 1
								changed = True
								break

					if changed:
						break

					demorgan = demorgan_boolean_input(
						input_val,
						blocks,
						owner_id=block_id,
						incoming_refs=incoming_refs,
						graph=graph,
					)
					if demorgan is not None and not fold_blocked_by_comment(
						demorgan["changed_ids"], blocks, commented_ids
					):
						logic_id = demorgan["logic_id"]
						left_not_id = demorgan["left_not_id"]
						right_not_id = demorgan["right_not_id"]
						logic = blocks[logic_id]
						left_not = blocks[left_not_id]

						logic["opcode"] = demorgan["new_inner_opcode"]
						logic_inputs = logic.get("inputs") or {}
						logic_inputs["OPERAND1"] = demorgan["new_inner_left"]
						logic_inputs["OPERAND2"] = demorgan["new_inner_right"]
						logic["inputs"] = logic_inputs

						left_not_inputs = left_not.get("inputs") or {}
						left_not_inputs["OPERAND"] = demorgan["new_outer_input"]
						left_not["inputs"] = left_not_inputs
						left_not["parent"] = demorgan["old_logic_parent"]
						logic["parent"] = left_not_id

						block_inputs = block.get("inputs") or {}
						block_inputs[input_name] = copy.deepcopy(
							demorgan["new_owner_input"]
						)
						block["inputs"] = block_inputs

						for raw, old_parent in (
							(demorgan["new_inner_left"], left_not_id),
							(demorgan["new_inner_right"], right_not_id),
						):
							for ref in direct_input_block_refs(raw, blocks):
								blocks[ref]["parent"] = logic_id
								opts.folded_constant_expression_link_edits[
									(ti, ref, "parent")
								] = logic_id

						opts.folded_constant_expression_link_edits[
							(ti, left_not_id, "parent")
						] = demorgan["old_logic_parent"]
						opts.folded_constant_expression_link_edits[
							(ti, logic_id, "parent")
						] = left_not_id
						opts.folded_constant_expression_opcode_edits[(ti, logic_id)] = (
							demorgan["new_inner_opcode"]
						)
						opts.folded_constant_expression_inputs[
							(ti, block_id, input_name)
						] = copy.deepcopy(demorgan["new_owner_input"])
						opts.folded_constant_expression_inputs[
							(ti, logic_id, "OPERAND1")
						] = copy.deepcopy(demorgan["new_inner_left"])
						opts.folded_constant_expression_inputs[
							(ti, logic_id, "OPERAND2")
						] = copy.deepcopy(demorgan["new_inner_right"])
						opts.folded_constant_expression_inputs[
							(ti, left_not_id, "OPERAND")
						] = copy.deepcopy(demorgan["new_outer_input"])
						opts.folded_constant_expression_blocks[ti].add(right_not_id)
						blocks.pop(right_not_id, None)
						graph.refresh_blocks(
							(block_id, logic_id, left_not_id, right_not_id)
						)
						incoming_refs = graph.incoming
						changed = True
						stats["demorgan_rewrites"] += 1
						break

					if changed:
						break

					short_circuited = short_circuit(
						input_val,
						blocks,
						parent_id=block_id,
						incoming_refs=incoming_refs,
						graph=graph,
					)
					if short_circuited is not None:
						replacement, folded_ids = short_circuited
						if fold_blocked_by_comment(
							folded_ids, blocks, commented_ids
						) or fold_ids_have_external_refs(
							folded_ids, blocks, block_id, graph=graph
						):
							continue

						if block_id not in blocks:
							continue

						inputs = blocks[block_id].get("inputs") or {}

						if input_name not in inputs:
							continue

						if isinstance(replacement, list):
							new_input = copy.deepcopy(replacement)
							new_ids = set()
						else:
							new_input, new_ids = constant_to_input(
								replacement, blocks, block_id
							)

						if new_input is None:
							continue

						if is_boolean_slot(block, input_name) and is_literal_input(
							new_input
						):
							continue  # a literal can't live in a boolean slot

						if isinstance(replacement, list):
							for child_id in _input_block_ids(new_input, blocks):
								child = blocks.get(child_id)
								if (
									isinstance(child, dict)
									and child.get("parent") != block_id
								):
									opts.folded_constant_expression_link_edits[
										(ti, child_id, "parent")
									] = block_id
							_reparent_serialized_input(new_input, blocks, block_id)

						inputs[input_name] = new_input
						stats["boolean_constants_propagated"] += 1
						opts.folded_constant_expression_inputs[
							(ti, block_id, input_name)
						] = copy.deepcopy(new_input)
						opts.folded_constant_expression_blocks[ti].update(folded_ids)
						opts.folded_constant_expression_new_blocks[ti].update(new_ids)

						for remove_id in folded_ids:
							blocks.pop(remove_id, None)

						graph.refresh_blocks((block_id, *owned_ids))
						incoming_refs = graph.incoming
						folded += 1
						changed = True
						break

					laws = simplify_boolean_identities(
						input_val,
						blocks,
						parent_id=block_id,
						incoming_refs=incoming_refs,
						graph=graph,
						slot_is_boolean=is_boolean_slot(block, input_name),
					)
					law_new_ids = set()
					if laws is not None and laws.get("law") == "complement":
						replacement = laws.get("replacement")
						if isinstance(replacement, dict) and isinstance(
							replacement.get("boolean"), bool
						):
							new_input, law_new_ids = constant_to_input(
								replacement["boolean"], blocks, block_id
							)
							if new_input is None or (
								is_boolean_slot(block, input_name)
								and is_literal_input(new_input)
							):
								# roll back if result cannot occupy this input
								for new_id in law_new_ids:
									blocks.pop(new_id, None)
								laws = None
							else:
								laws["replacement"] = new_input
								laws["touched"] = set(laws.get("touched", ())) | set(
									law_new_ids
								)
						else:
							laws = None

					applied_law = laws is not None and apply_plan(
						laws,
						blocks,
						graph,
						block_id,
						input_name,
						ti,
						opts,
						stats,
						commented_ids,
						"boolean"
					)
					if applied_law:
						if law_new_ids:
							opts.folded_constant_expression_new_blocks[ti].update(
								law_new_ids
							)
							for new_id in law_new_ids:
								new_block = blocks.get(new_id)
								if (
									isinstance(new_block, dict)
									and new_block.get("parent") != block_id
								):
									new_block["parent"] = block_id
									opts.folded_constant_expression_link_edits[
										(ti, new_id, "parent")
									] = block_id
							graph.refresh_blocks(law_new_ids)
						incoming_refs = graph.incoming
						folded += 1
						changed = True
						break
					elif law_new_ids:
						# there is an unapplied plan
						for new_id in law_new_ids:
							blocks.pop(new_id, None)
						graph.refresh_blocks(law_new_ids)
						incoming_refs = graph.incoming

					res = constexpr_from_input(input_val, blocks)
					if res is None:
						continue
					constant, folded_ids = res
					if not folded_ids:
						continue

					owned_ids = get_exclusive_input_subtree(
						input_val, blocks, block_id, incoming_refs, graph=graph
					)
					if owned_ids is None or not folded_ids <= owned_ids:
						continue
					if fold_blocked_by_comment(
						owned_ids, blocks, commented_ids
					) or fold_ids_have_external_refs(
						owned_ids, blocks, block_id, graph=graph
					):
						continue
					if block_id not in blocks:
						continue
					inputs = blocks[block_id].get("inputs") or {}
					if input_name not in inputs:
						continue
					new_input, new_ids = constant_to_input(constant, blocks, block_id)
					if new_input is None:
						continue
					if is_boolean_slot(block, input_name) and is_literal_input(
						new_input
					):
						continue  # a literal can't live in a boolean slot
					inputs[input_name] = new_input
					opts.folded_constant_expression_inputs[
						(ti, block_id, input_name)
					] = copy.deepcopy(new_input)
					opts.folded_constant_expression_blocks[ti].update(owned_ids)
					opts.folded_constant_expression_new_blocks[ti].update(new_ids)
					for remove_id in owned_ids:
						blocks.pop(remove_id, None)
					graph.refresh_blocks((block_id, *owned_ids))
					incoming_refs = graph.incoming
					folded += 1
					changed = True
					break

				if changed:
					break

			if not changed:
				break

	stats["constant_expressions_folded"] += folded
	return folded


def prompt_for_constant_variables(project, candidates) -> dict:
	if not candidates:
		print(Ansi.muted("\nNo constant-variable candidates found."))
		return {}

	positive = [c for c in candidates if c["bytes"] > 0]
	negative = [c for c in candidates if c["bytes"] < 0]
	total_positive = sum(c["bytes"] for c in positive)
	w_scope = max(len(c["scope"]) for c in candidates)
	w_name = min(34, max(len(c["name"]) for c in candidates))

	print()
	print(
		Ansi.heading(
			f"Constant variables found: {len(candidates)}  "
			f"({sum(c['reporters'] for c in candidates):,} reporter uses)"
		)
	)
	print(
		Ansi.muted(
			f"  {len(positive)} candidates when folded can save about {total_positive:,} bytes"
		)
	)
	print(
		Ansi.muted(
			f"  {len(negative)} candidates when folded would increase project.json"
		)
	)
	print()
	print(
		f"  {'#':>4}  {'scope':<{w_scope}}  {'variable':<{w_name}}  {'uses':>5}  {'bytes':>8}  notes"
	)

	for i, c in enumerate(candidates, 1):
		nm = (
			c["name"]
			if len(c["name"]) <= w_name
			else c["name"][: w_name - 1] + "..."
		)
		notes = []
		if c["bytes"] < 0:
			notes.append("!")
		if c["changes"]:
			notes.append(f"-{c['changes']} change")
		print(
			f" {i:>4}  {c['scope']:<{w_scope}}  {nm:<{w_name}}  {c['reporters']:>5,}  {-c['bytes']:>+8,}  {'; '.join(notes)}"
		)

	print()
	print(
		Ansi.warning(
			"  This replaces variable reporters with the variable's unchanged initial value "
			"from project.json. Only variables whose setter also restores that same initial value are offered."
		)
	)
	print(
		Ansi.muted(
			"  Positive byte estimates are marked '!' and are not selected by 'p'."
		)
	)
	print()
	print(Ansi.muted("  Enter / n       keep every variable"))
	print(Ansi.muted("  p               fold only candidates that reduce project.json"))
	print(Ansi.muted("  a               fold all candidates"))
	print(
		Ansi.muted("  u               fold only candidates without change-variable-by")
	)
	print(Ansi.muted("  1,3,5-7         fold those numbers"))
	print()

	while True:
		try:
			answer = (
				input(Ansi.prompt("Fold which variables? [Enter = keep all] > "))
				.strip()
				.lower()
			)
		except EOFError:
			print(Ansi.muted("\n(no input available, keeping all variables)"))
			return {}
		except KeyboardInterrupt:
			print(Ansi.warning("\nAborted."))
			return None

		if answer in ("", "n", "no", "none", "keep"):
			print(Ansi.success("Keeping all constant variables."))
			return {}

		if answer in ("p", "positive", "safe", "s"):
			picked = {i for i, c in enumerate(candidates, 1) if c["bytes"] > 0}
		elif answer in ("u", "unchanged"):
			picked = {
				i
				for i, c in enumerate(candidates, 1)
				if c["changes"] == 0 and c["bytes"] > 0
			}
		elif answer in ("a", "all"):
			picked = set(range(1, len(candidates) + 1))
		else:
			try:
				picked = parse_selection(answer, len(candidates))
			except ValueError as e:
				print(Ansi.error(f"  {e}. Try again."))
				continue

		chosen = [candidates[i - 1] for i in sorted(picked)]

		if not chosen:
			print(Ansi.warning("  Nothing selected. Try again."))
			continue

		estimate = sum(c["bytes"] for c in chosen)
		risky = [c for c in chosen if c["changes"] or c["bytes"] < 0]
		print(
			Ansi.success(
				f"  Selected {len(chosen)} variable(s), estimated project.json change: {estimate:+,} bytes."
			)
		)

		if risky:
			print(
				Ansi.warning(
					f"  WARNING: {len(risky)} selected variable(s) need extra care:"
				)
			)

			for c in risky[:20]:
				reasons = []
				if c["changes"]:
					reasons.append(f"{c['changes']} change-variable-by")
				if c["bytes"] < 0:
					reasons.append("increases JSON size")
				print(
					f"    - [{Ansi.error(c['scope'])}] {c['name']!r}: {Ansi.muted(', '.join(reasons))}"
				)

			if len(risky) > 20:
				print(f"    ... {len(risky) - 20:,} more")

			try:
				confirm = (
					input(
						"  Type 'yes' to fold them anyway, anything else to go back > "
					)
					.strip()
					.lower()
				)
			except EOFError:
				print(Ansi.muted("\n(no input available, keeping all variables)"))
				return {}
			except KeyboardInterrupt:
				print(Ansi.warning("\nAborted."))
				return None

			if confirm != "yes":
				print(Ansi.warning("  Not confirmed. Nothing was folded... yet >:)."))
				continue

		return {(c["ti"], c["id"]): c["literal"] for c in chosen}


def remove_empty_fields(project: dict[str, Any], stats: dict[str, int]):
	count = 0
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and block.get("fields") == {}:
				del block["fields"]
				count += 1
	stats["empty_fields_removed"] += count
	return count


def remove_empty_inputs(project: dict[str, Any], stats: dict[str, int]):
	count = 0
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and block.get("inputs") == {}:
				del block["inputs"]
				count += 1
	stats["empty_inputs_removed"] += count
	return count


def _target_default_properties_for_removal(target):
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


def repaired_sprite_layer_orders(targets) -> dict:
	sprites = [
		(index, target)
		for index, target in enumerate(targets)
		if not target.get("isStage")
	]

	if not any(
		type(target.get("layerOrder")) in (int, float)
		and target["layerOrder"] < 1
		for _, target in sprites
	):
		return {}

	def order_key(sprite):
		index, target = sprite
		order = target.get("layerOrder")
		if type(order) in (int, float) and (
			type(order) is int or math.isfinite(order)
		):
			return order, index

		return math.inf, index

	return {
		index: order
		for order, (index, _) in enumerate(sorted(sprites, key=order_key), 1)
	}


def repair_invalid_sprite_layer_orders(project):
	targets = project.get("targets", [])
	orders = repaired_sprite_layer_orders(targets)
	changed = 0
	for index, order in orders.items():
		if targets[index].get("layerOrder") != order:
			targets[index]["layerOrder"] = order
			changed += 1
	return changed


def remove_default_target_properties(project, stats):
	count = 0
	for target in project.get("targets", []):
		for key, default in _target_default_properties_for_removal(target).items():
			if (
				key in target
				and target[key] == default
				and type(target[key]) is type(default)
			):
				del target[key]
				count += 1

	stats["default_target_properties_removed"] += count
	return count


def remove_costume_metadata(project, stats, assets=None):
	count = 0
	for target in project.get("targets", []):
		for costume in target.get("costumes", []):
			if not isinstance(costume, dict):
				continue

			aid = costume.get("assetId")
			fmt = costume.get("dataFormat")
			canonical = (
				f"{aid}.{fmt}"
				if isinstance(aid, str) and isinstance(fmt, str)
				else None
			)

			if (
				canonical
				and costume.get("md5ext") == canonical
				and (assets is None or canonical in assets)
			):
				del costume["md5ext"]
				count += 1

			if fmt == "svg" and costume.get("bitmapResolution") == 1:
				del costume["bitmapResolution"]
				count += 1

	stats["costume_metadata_removed"] += count
	return count


def remove_empty_target_containers(project, stats):
	count = 0
	for target in project.get("targets", []):
		for key in ("lists", "broadcasts", "comments"):
			if target.get(key) == {}:
				del target[key]
				count += 1

	stats["empty_containers_removed"] += count
	return count


def remove_project_meta(project, stats):
	count = 0
	meta = project.get("meta")
	if isinstance(meta, dict):
		for key in ("agent", "platform"):
			if key in meta:
				del meta[key]
				count += 1

	stats["project_meta_cleaned"] += count
	return count


def compact_numeric_inputs(project, stats):
	count = 0
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue

			for ival in (block.get("inputs") or {}).values():
				if not isinstance(ival, list):
					continue

				for item in ival[1:]:
					if (
						isinstance(item, list)
						and len(item) > 1
						and item[0] in NUMERIC_TAGS
					):
						v = item[1]
						if isinstance(v, str):
							try:
								iv = int(v)
								if str(iv) == v:
									item[1] = iv
									count += 1
									continue
							except ValueError:
								...

							try:
								fv = float(v)
								if (
									"." in v
									and "e" not in v.lower()
									and str(fv) == v
									and not (
										fv != fv or fv in (float("inf"), float("-inf"))
									)
								):
									item[1] = fv
									count += 1
							except ValueError:
								...

	stats["numeric_inputs_compacted"] += count
	return count


def get_substack_tail(blocks, closure):
	if not closure:
		return None
	
	roots = [
		bid
		for bid in closure
		if isinstance(blocks.get(bid), dict)
		and blocks[bid].get("parent") not in closure
	]

	if len(roots) != 1:
		return None
	
	current = roots[0]
	seen = set()

	while True:
		if current in seen or current not in closure:
			return None
		
		seen.add(current)
		block = blocks.get(current)

		if not isinstance(block, dict):
			return None
		
		nxt = block.get("next")

		if nxt is None:
			return current
		
		if not isinstance(nxt, str) or nxt not in closure:
			return None
		
		current = nxt
