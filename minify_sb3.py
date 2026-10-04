import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import pickle
import re
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
import subprocess
import shutil
import sys
import tempfile
import time
import zipfile
import zlib
import uuid
from collections import Counter

from functools import lru_cache
from itertools import permutations, product


class JsonNumber(str):
	"""A JSON number token, kept separate from JSON strings and binary floats."""


def _unique_object(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError(f"duplicate JSON object key: {key!r}")
		result[key] = value
	return result


def _invalid_constant(value):
	raise ValueError(f"non-finite JSON number: {value}")


def loads_exact(data):
	if isinstance(data, (bytes, bytearray)):
		data = data.decode("utf-8-sig")
	return json.loads(
		data,
		parse_int=JsonNumber,
		parse_float=JsonNumber,
		parse_constant=_invalid_constant,
		object_pairs_hook=_unique_object,
	)


@lru_cache(maxsize=8192)
def _number_components(token):
	sign = token.startswith("-")
	mantissa, _, power = (token[1:] if sign else token).lower().partition("e")
	whole, _, fraction = mantissa.partition(".")
	digits = (whole + fraction).lstrip("0")
	if not digits:
		return sign, "0", 0
	coefficient = digits.rstrip("0")
	return sign, coefficient, int(power or "0") - len(fraction) + len(digits) - len(coefficient)


@lru_cache(maxsize=8192)
def _short_number(token):
	sign, coefficient, exponent = _number_components(token)
	prefix = "-" if sign else ""
	if coefficient == "0":
		return prefix + "0"
	n = len(coefficient)
	best_length, best_point = len(token), None
	# Every irredundant decimal mantissa is a placement of its decimal point
	# relative to these significant digits. Longer mantissas cannot beat token.
	lower = min(n, n + 2 + len(prefix) - best_length)
	upper = max(n, best_length - len(prefix))
	for point in range(lower, upper + 1):
		mantissa_length = (n if point == n else n + 1 if 0 < point < n
			else point if point > n else 2 - point + n)
		power = exponent + n - point
		length = len(prefix) + mantissa_length + (1 + len(str(power)) if power else 0)
		if length < best_length:
			best_length, best_point = length, point
	if best_point is None:
		return token
	point = best_point
	if point <= 0:
		mantissa = "0." + "0" * -point + coefficient
	elif point < n:
		mantissa = coefficient[:point] + "." + coefficient[point:]
	else:
		mantissa = coefficient + "0" * (point - n)
	power = exponent + n - point
	return prefix + mantissa + (f"e{power}" if power else "")


@lru_cache(maxsize=4096)
def _quote(value):
	return json.dumps(value, ensure_ascii=False)


def _encode_parts(value, short_numbers):
	if isinstance(value, JsonNumber):
		yield _short_number(value) if short_numbers else value
	elif isinstance(value, str):
		yield _quote(value)
	elif value is None:
		yield "null"
	elif value is True:
		yield "true"
	elif value is False:
		yield "false"
	elif isinstance(value, dict):
		yield "{"
		for index, (key, item) in enumerate(value.items()):
			if index:
				yield ","
			yield _quote(key)
			yield ":"
			yield from _encode_parts(item, short_numbers)
		yield "}"
	elif isinstance(value, list):
		yield "["
		for index, item in enumerate(value):
			if index:
				yield ","
			yield from _encode_parts(item, short_numbers)
		yield "]"
	else:
		raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")


def dumps_exact(project, short_numbers=False):
	"""Serialize exact JSON values with a list-backed emitter.

	The original generator-based encoder creates a Python generator frame for
	every recursive descent. Large Scratch projects contain millions of scalar
	values, so using one shared output list substantially reduces allocator and
	generator overhead while preserving byte-for-byte output.
	"""
	parts = []
	quote = _quote
	short = _short_number
	def emit(value):
		if isinstance(value, JsonNumber):
			parts.append(short(value) if short_numbers else value)
		elif isinstance(value, str):
			parts.append(quote(value))
		elif value is None:
			parts.append("null")
		elif value is True:
			parts.append("true")
		elif value is False:
			parts.append("false")
		elif isinstance(value, dict):
			parts.append("{")
			first = True
			for key, item in value.items():
				if not first:
					parts.append(",")
				first = False
				parts.append(quote(key))
				parts.append(":")
				emit(item)
			parts.append("}")
		elif isinstance(value, list):
			parts.append("[")
			for index, item in enumerate(value):
				if index:
					parts.append(",")
				emit(item)
			parts.append("]")
		else:
			raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")
	emit(project)
	return "".join(parts).encode("utf-8", "backslashreplace")


def dumps_exact_layout(project, order, short_numbers=False):
	"""Serialize while changing only the property order of block records.

	Unlike ``_layout`` this does not clone every target/block dictionary for
	each candidate. It emits the requested order directly, which is much
	cheaper for multi-megabyte project.json candidates.
	"""
	parts = []
	quote = _quote
	short = _short_number
	order_tuple = tuple(order or ())

	def emit(value, is_block=False):
		if isinstance(value, JsonNumber):
			parts.append(short(value) if short_numbers else value)
		elif isinstance(value, str):
			parts.append(quote(value))
		elif value is None:
			parts.append("null")
		elif value is True:
			parts.append("true")
		elif value is False:
			parts.append("false")
		elif isinstance(value, dict):
			parts.append("{")
			if is_block and order_tuple:
				keys = dict.fromkeys((*order_tuple, *value))
				items = ((key, value[key]) for key in keys if key in value)
			else:
				items = value.items()
			first = True
			for key, item in items:
				if not first:
					parts.append(",")
				first = False
				parts.append(quote(key))
				parts.append(":")
				emit(item, False)
			parts.append("}")
		elif isinstance(value, list):
			parts.append("[")
			for index, item in enumerate(value):
				if index:
					parts.append(",")
				emit(item, False)
			parts.append("]")
		else:
			raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")

	# Special-case only the targets[*].blocks mapping so ordinary dictionaries
	# retain their original property order.
	parts.append("{")
	first_target_key = True
	for pkey, pvalue in project.items():
		if not first_target_key:
			parts.append(",")
		first_target_key = False
		parts.append(quote(pkey)); parts.append(":")
		if pkey != "targets" or not isinstance(pvalue, list):
			emit(pvalue); continue
		parts.append("[")
		for ti, target in enumerate(pvalue):
			if ti: parts.append(",")
			if not isinstance(target, dict):
				emit(target); continue
			parts.append("{")
			first_key = True
			for key, item in target.items():
				if not first_key: parts.append(",")
				first_key = False
				parts.append(quote(key)); parts.append(":")
				if key == "blocks" and isinstance(item, dict):
					parts.append("{")
					first_block = True
					for bid, block in item.items():
						if not first_block: parts.append(",")
						first_block = False
						parts.append(quote(bid)); parts.append(":")
						emit(block, isinstance(block, dict))
					parts.append("}")
				else:
					emit(item)
			parts.append("}")
		parts.append("]")
	parts.append("}")
	return "".join(parts).encode("utf-8", "backslashreplace")


# Performance guardrails for very large Scratch project.json files.
# The semantic transforms remain unchanged; only the optional compression/layout
# search is made progressively cheaper as the JSON becomes expensive to re-compress.
FAST_JSON_RAW_THRESHOLD = 1_500_000
FAST_JSON_BLOCK_THRESHOLD = 8_000
FAST_JSON_ORDER_COUNT = 2
FAST_JSON_SCREEN_LEVEL = 1
FAST_JSON_SEARCH_LEVEL = 3

BLOCK_KEY_ORDERS = (
	("opcode", "next", "parent", "inputs", "fields"),
	("opcode", "fields", "inputs", "next", "parent"),
	("next", "parent", "opcode", "fields", "inputs"),
	("inputs", "fields", "opcode", "next", "parent"),
	("fields", "inputs", "opcode", "next", "parent"),
	("parent", "next", "opcode", "inputs", "fields"),
	("fields", "inputs", "mutation", "next", "opcode", "parent", "shadow", "topLevel", "x", "y"),
)


def _record_order_is_irrelevant(path):
	return len(path) == 4 and path[0] == "targets" and path[2] == "blocks"


def exact_difference(original, result, compact_defaults=False, compact_costume_references=False, compact_block_flags=False, relabel_block_ids=False):
	"""Compare exact numbers, strings, keys and all ordered collections."""
	if relabel_block_ids:
		try:
			result = restore_block_labels(original, result)
		except (ValueError, KeyError, TypeError) as error:
			return f"block ID correspondence failed: {error}"
	stack = [((), original, result)]
	while stack:
		path, left, right = stack.pop()
		where = "/".join(map(str, path)) or "project"
		if type(left) is not type(right):
			return f"{where}: JSON value type changed"
		if isinstance(left, JsonNumber):
			if _number_components(left) != _number_components(right):
				return f"{where}: exact numeric value changed"
		elif isinstance(left, dict):
			if compact_costume_references and len(path) == 4 and path[0] == "targets" and path[2] == "costumes":
				removed = {}
				canonical = _canonical_costume_filename(left)
				if canonical is not None and left.get("md5ext") == canonical and "md5ext" not in right:
					removed["md5ext"] = canonical
				if removed:
					if list(right) != [key for key in left if key not in removed]:
						return f"{where}: costume keys or their order changed"
					right = {key: removed[key] if key in removed else right[key] for key in left}
			if compact_defaults and _record_order_is_irrelevant(path):
				right = dict(right)
				for key in ("inputs", "fields"):
					if left.get(key) == {} and key not in right:
						right[key] = {}
			if compact_block_flags and _record_order_is_irrelevant(path):
				right = dict(right)
				for key in ("topLevel", "shadow"):
					if left.get(key) is False and key not in right:
						right[key] = False
			if set(left) != set(right):
				return f"{where}: object keys changed"
			if not _record_order_is_irrelevant(path) and list(left) != list(right):
				return f"{where}: collection order changed"
			stack.extend((path + (key,), left[key], right[key]) for key in left)
		elif isinstance(left, list):
			if len(left) != len(right):
				return f"{where}: array length changed"
			stack.extend((path + (index,), a, b) for index, (a, b) in enumerate(zip(left, right)))
		elif left != right:
			return f"{where}: JSON value changed"
	return None


def _ordered_blocks(target, order):
	result = dict(target)
	blocks = target.get("blocks")
	if isinstance(blocks, dict):
		result["blocks"] = {
			bid: ({key: block[key] for key in dict.fromkeys((*order, *block)) if key in block}
				if isinstance(block, dict) else block)
			for bid, block in blocks.items()
		}
	return result


def _layout(project, order):
	result = dict(project)
	if isinstance(project.get("targets"), list):
		result["targets"] = [_ordered_blocks(target, order) for target in project["targets"]]
	return result


def _compact_block_defaults(project):
	result = dict(project)
	result["targets"] = []
	for target in project.get("targets", []):
		target = dict(target)
		if isinstance(target.get("blocks"), dict):
			target["blocks"] = {
				bid: ({key: value for key, value in block.items() if key not in ("fields", "inputs") or value != {}}
					if isinstance(block, dict) else block)
				for bid, block in target["blocks"].items()
			}
		result["targets"].append(target)
	return result


def _canonical_costume_filename(costume):
	aid, fmt = costume.get("assetId"), costume.get("dataFormat")
	if isinstance(aid, str) and not isinstance(aid, JsonNumber) and isinstance(fmt, str) and not isinstance(fmt, JsonNumber) and aid and fmt:
		return f"{aid}.{fmt}"
	return None


def compact_representation(project, compact_defaults=False, compact_costume_references=False, compact_block_flags=False):
	result = dict(project)
	result["targets"] = []
	for source in project.get("targets", []):
		target = dict(source)
		if isinstance(source.get("blocks"), dict) and (compact_defaults or compact_block_flags):
			target["blocks"] = {}
			for bid, block in source["blocks"].items():
				if isinstance(block, dict):
					block = {key: value for key, value in block.items()
						if not (compact_defaults and key in ("inputs", "fields") and value == {})
						and not (compact_block_flags and key in ("topLevel", "shadow") and value is False)}
				target["blocks"][bid] = block
		if compact_costume_references and isinstance(source.get("costumes"), list):
			target["costumes"] = []
			for costume in source["costumes"]:
				if isinstance(costume, dict):
					removed = set()
					canonical = _canonical_costume_filename(costume)
					if canonical is not None and costume.get("md5ext") == canonical: removed.add("md5ext")
					costume = {key: value for key, value in costume.items() if key not in removed}
				target["costumes"].append(costume)
		result["targets"].append(target)
	return result


def _minimum_json_size_prepared(project, identifier_cost=None, relabel_block_ids=False):
	"""Compute the same lower bound for an already compacted/relabelled tree."""
	counts = dict(structure=0, keys=0, strings=0, numbers=0, literals=0)
	quote_size = lambda value: len(_quote(value).encode("utf-8", "backslashreplace"))
	def visit(value):
		if isinstance(value, JsonNumber):
			counts["numbers"] += len(_short_number(value))
		elif isinstance(value, str):
			counts["strings"] += quote_size(value)
		elif isinstance(value, dict):
			counts["structure"] += 2 + len(value) + max(0, len(value) - 1)
			for key, item in value.items():
				counts["keys"] += quote_size(key)
				visit(item)
		elif isinstance(value, list):
			counts["structure"] += 2 + max(0, len(value) - 1)
			for item in value:
				visit(item)
		elif value is None or isinstance(value, bool):
			counts["literals"] += 4 if value is None or value is True else 5
		else:
			raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")
	visit(project)
	return {"model": "fixed-graph-block-ids-v3" if relabel_block_ids else "fixed-tree-v2",
		"minimum_bytes": sum(counts.values()), "components": counts,
		"compact_block_defaults": False, "compact_costume_references": False,
		"compact_block_flags": False, "identifier_cost": identifier_cost,
		"global_scratch_minimum_proven": False}


def minimum_json_size(project, compact_defaults=False, compact_costume_references=False, compact_block_flags=False, relabel_block_ids=False):
	"""Exact lower bound for the selected fixed-tree representation model."""
	project = compact_representation(project, compact_defaults, compact_costume_references, compact_block_flags)
	identifier_cost = None
	if relabel_block_ids:
		project, _, identifier_cost = relabel_blocks(project)
	counts = dict(structure=0, keys=0, strings=0, numbers=0, literals=0)
	def string_size(value):
		return 2 + sum(2 if char in '\"\\\b\f\n\r\t' else 6 if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
			else len(char.encode("utf-8")) for char in value)
	def visit(value):
		if isinstance(value, JsonNumber):
			counts["numbers"] += len(_short_number(value))
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
	visit(project)
	return {"model": "fixed-graph-block-ids-v3" if relabel_block_ids else "fixed-tree-v2", "minimum_bytes": sum(counts.values()), "components": counts,
		"compact_block_defaults": compact_defaults, "compact_costume_references": compact_costume_references,
		"compact_block_flags": compact_block_flags, "identifier_cost": identifier_cost, "global_scratch_minimum_proven": False}


def _index_key(key):
	return len(key) <= 10 and key.isascii() and key.isdigit() and str(int(key)) == key and int(key) < 4294967295


def javascript_keys(mapping):
	return sorted((key for key in mapping if _index_key(key)), key=int) + [key for key in mapping if not _index_key(key)]


def _input_tag(value):
	if isinstance(value, JsonNumber):
		components = _number_components(value)
		return int(components[1]) if not components[0] and components[2] == 0 and components[1] in ("1", "2", "3") else None
	return value if type(value) in (int, float) and value in (1, 2, 3) else None


def _block_slots(target):
	for block in (target.get("blocks") or {}).values():
		if not isinstance(block, dict): continue
		for key in ("next", "parent"):
			if isinstance(block.get(key), str) and not isinstance(block[key], JsonNumber): yield block, key
		for desc in (block.get("inputs") or {}).values():
			if not isinstance(desc, list) or not desc: continue
			tag = _input_tag(desc[0])
			positions = (1,) if tag in (1, 2) else (1, 2) if tag == 3 else ()
			for position in positions:
				if len(desc) > position and isinstance(desc[position], str) and not isinstance(desc[position], JsonNumber): yield desc, position
	for comment in (target.get("comments") or {}).values():
		if isinstance(comment, dict) and isinstance(comment.get("blockId"), str) and not isinstance(comment["blockId"], JsonNumber): yield comment, "blockId"


def _identifier_names(reserved):
	# All admissible names of JSON payload cost 1 or 2 are enumerated. Cost 3+
	# ties use printable ASCII; see the identifier assignment proof.
	chars = tuple(chr(value) for value in range(32, 128) if chr(value) not in '\"\\<&')
	def allowed(name):
		return name not in reserved and name not in ("__proto__", "constructor", "prototype") and not _index_key(name)
	for name in chars:
		if allowed(name): yield name
	for pair in product(chars, repeat=2):
		name = "".join(pair)
		if allowed(name): yield name
	for name in ('\\', *(chr(value) for value in range(128, 2048))):
		if allowed(name): yield name
	length = 3
	while True:
		for digits in product(chars, repeat=length):
			name = "".join(digits)
			if allowed(name): yield name
		length += 1


def relabel_blocks(project):
	"""Minimize identifier bytes for a fixed graph; preserve dangling edges."""
	result = copy.deepcopy(project)
	maps, cost = {}, dict(before=0, after=0, identifiers=0, occurrences=0)
	for ti, target in enumerate(result.get("targets", [])):
		blocks = target.get("blocks")
		if not isinstance(blocks, dict) or not blocks: continue
		for block in blocks.values():
			if not isinstance(block, dict): continue
			for key in ("next", "parent"):
				value = block.get(key)
				if value is not None and (not isinstance(value, str) or isinstance(value, JsonNumber)):
					raise ValueError("verified block relabeling requires string or null next/parent links")
			for desc in (block.get("inputs") or {}).values():
				if not isinstance(desc, list) or not desc or _input_tag(desc[0]) is None:
					raise ValueError("verified block relabeling requires serialized SB3 input descriptors")
				for position in ((1, 2) if _input_tag(desc[0]) == 3 else (1,)):
					if len(desc) <= position:
						raise ValueError("verified block relabeling rejects truncated input descriptors")
					value = desc[position]
					if value is not None and (not isinstance(value, (str, list)) or isinstance(value, JsonNumber)):
						raise ValueError("verified block relabeling rejects numeric or opaque input references")
		for comment in (target.get("comments") or {}).values():
			if isinstance(comment, dict):
				value = comment.get("blockId")
				if value is not None and (not isinstance(value, str) or isinstance(value, JsonNumber)):
					raise ValueError("verified block relabeling requires string or null comment block IDs")
		ids = javascript_keys(blocks)
		if any(key in blocks for key in ("__proto__", "constructor", "prototype")):
			raise ValueError("cannot relabel prototype-sensitive block IDs")
		frequencies = {bid: 1 for bid in ids}
		slots = list(_block_slots(target))
		reserved = set()
		for container, key in slots:
			value = container[key]
			if value in frequencies: frequencies[value] += 1
			else: reserved.add(value)
		names = _identifier_names(reserved)
		ordered = sorted(ids, key=lambda bid: -frequencies[bid])
		mapping = {bid: next(names) for bid in ordered}
		# Beyond all cost-3 names this generator omits some UTF-8 ties. Do not
		# advertise a complete lower-bound certificate beyond that domain.
		if any(len(_quote(name).encode("utf-8")) - 2 > 3 for name in mapping.values()):
			raise ValueError("block ID minimum certificate supports names of at most 3 payload bytes")
		maps[ti] = mapping
		for container, key in slots:
			container[key] = mapping.get(container[key], container[key])
		target["blocks"] = {mapping[bid]: blocks[bid] for bid in ids}
		for bid in ids:
			frequency = frequencies[bid]
			cost["before"] += frequency * (len(_quote(bid).encode("utf-8", "backslashreplace")) - 2)
			cost["after"] += frequency * (len(_quote(mapping[bid]).encode("utf-8")) - 2)
			cost["occurrences"] += frequency
		cost["identifiers"] += len(ids)
	return result, maps, cost


def restore_block_labels(original, result):
	"""Infer bijections independently from semantic record order, not optimizer maps."""
	if len(original.get("targets", [])) != len(result.get("targets", [])):
		raise ValueError("target count changed")
	result = copy.deepcopy(result)
	for left, right in zip(original.get("targets", []), result.get("targets", [])):
		if "blocks" not in left and "blocks" not in right: continue
		if "blocks" not in left or "blocks" not in right: raise ValueError("block table presence changed")
		a, b = left.get("blocks", {}), right.get("blocks", {})
		if len(a) != len(b): raise ValueError("block count changed")
		if javascript_keys(a) != javascript_keys(b) and any(not bid or _index_key(bid) or bid in ("__proto__", "constructor", "prototype")
			or any(char in '<&"\t\r\n' or not (0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF) for char in bid) for bid in b):
			raise ValueError("output block IDs violate editor XML or property-order contracts")
		inverse = dict(zip(javascript_keys(b), javascript_keys(a)))
		for container, key in _block_slots(right):
			container[key] = inverse.get(container[key], container[key])
		restored = {inverse[bid]: block for bid, block in b.items()}
		right["blocks"] = {bid: restored[bid] for bid in a}
	return result


class _LayoutRecord:
	def __init__(self, prefix, block):
		self.prefix = prefix
		self.fields = {key: _quote(key).encode("utf-8", "backslashreplace") + b":" + dumps_exact(value, True) for key, value in block.items()}
		self.shape = tuple(block)

	def render(self, order, shapes):
		key = (self.shape, order)
		if key not in shapes:
			shapes[key] = tuple(name for name in dict.fromkeys((*(order or ()), *self.shape)) if name in self.fields)
		return self.prefix + b"{" + b",".join(self.fields[name] for name in shapes[key]) + b"}"


def _layout_parts(project):
	parts, pending = [], bytearray()
	def emit(data):
		pending.extend(data)
	def visit(value, path):
		if isinstance(value, dict):
			emit(b"{")
			for index, (key, item) in enumerate(value.items()):
				prefix = (b"," if index else b"") + _quote(key).encode("utf-8", "backslashreplace") + b":"
				if len(path) == 3 and path[0] == "targets" and path[2] == "blocks" and isinstance(item, dict):
					if pending:
						parts.append(bytes(pending)); pending.clear()
					parts.append(_LayoutRecord(prefix, item))
				else:
					emit(prefix); visit(item, path + (key,))
			emit(b"}")
		elif isinstance(value, list):
			emit(b"[")
			for index, item in enumerate(value):
				if index: emit(b",")
				visit(item, path + (index,))
			emit(b"]")
		else:
			emit(dumps_exact(value, True))
	visit(project, ())
	if pending: parts.append(bytes(pending))
	return parts


def _window_layout(project, orders, level, width=128):
	"""Search contiguous record windows against their actual preceding dictionary."""
	parts, output, shapes = _layout_parts(project), [], {}
	history, trials, index = b"", 0, 0
	while index < len(parts):
		part = parts[index]
		if isinstance(part, bytes):
			output.append(part); history = (history + part)[-32768:]; index += 1
			continue
		end = index
		while end < len(parts) and isinstance(parts[end], _LayoutRecord) and end - index < width:
			end += 1
		records = parts[index:end]
		lookahead = b"".join(piece if isinstance(piece, bytes) else piece.render(None, shapes) for piece in parts[end:end + 16])[:4096]
		best = b"".join(record.render(None, shapes) for record in records)
		best_size = len(deflate(history + best + lookahead, level))
		for order in orders:
			candidate = b"".join(record.render(order, shapes) for record in records)
			if candidate == best: continue
			size = len(deflate(history + candidate + lookahead, level)); trials += 1
			if size < best_size:
				best, best_size = candidate, size
		output.append(best); history = (history + best)[-32768:]; index = end
	return b"".join(output), trials


def _sample_blocks(project, width=64):
	result = {"targets": []}
	for target in project.get("targets", []):
		blocks = target.get("blocks") or {}
		items = list(blocks.items())
		if len(items) > width * 3:
			starts = (0, len(items) // 2 - width // 2, len(items) - width)
			items = [item for start in starts for item in items[start:start + width]]
		result["targets"].append({"blocks": dict(items)})
	return result


def _screen_record_orders(project, level):
	"""Screen all core-property permutations on bounded, contiguous samples."""
	probe = _sample_blocks(project)
	scores = []
	for order in permutations(("opcode", "next", "parent", "inputs", "fields")):
		size = len(deflate(dumps_exact(_layout(probe, order), True), level))
		scores.append((size, order))
	return [order for _, order in sorted(scores)[:4]], len(scores)


def _specialized_layout(project, orders, level, grouping, max_members=96):
	groups = {}
	def group_key(ti, block):
		shape = frozenset(block)
		if grouping == "shape":
			return shape
		key = (shape, block.get("opcode"))
		return (ti, key) if grouping == "target-opcode" else key
	for ti, target in enumerate(project.get("targets", [])):
		for bid, block in (target.get("blocks") or {}).items():
			if isinstance(block, dict):
				groups.setdefault(group_key(ti, block), []).append((ti, bid, block))
	chosen = {}
	trials = 0
	for key, members in groups.items():
		if len(members) > max_members:
			half = max(1, max_members // 2)
			members = members[:half] + members[-(max_members - half):]
		probe_targets = {}
		for ti, bid, block in members:
			probe_targets.setdefault(ti, {"blocks": {}})["blocks"][bid] = block
		probe = {"targets": list(probe_targets.values())}
		best_order = None
		best_size = len(deflate(dumps_exact(probe), level))
		for order in orders:
			size = len(deflate(dumps_exact(_layout(probe, order)), level))
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
				order = chosen.get(group_key(ti, block)) if isinstance(block, dict) else None
				new_blocks[bid] = ({name: block[name] for name in dict.fromkeys((*order, *block)) if name in block}
					if order is not None else block)
			target["blocks"] = new_blocks
		result["targets"].append(target)
	return result, trials


def deflate(data, level=9, memory=8, strategy=zlib.Z_DEFAULT_STRATEGY):
	compressor = zlib.compressobj(level, zlib.DEFLATED, -15, memory, strategy)
	return compressor.compress(data) + compressor.flush()


def _compressions(data, level):
	yield "zlib", deflate(data, level)
	if level:
		yield "zlib-mem9", deflate(data, level, 9)
		yield "zlib-filtered", deflate(data, level, 8, zlib.Z_FILTERED)


def get_zopfli(required=False):
	try:
		import zopfli.zlib
	except ImportError:
		if required:
			raise ValueError("Zopfli is unavailable; install it with: python -m pip install zopfli") from None
		return None
	return zopfli.zlib.compress


def zopfli_deflate(data, compress, iterations=15):
	candidates = [
		compress(data, numiterations=iterations)[2:-4],
		compress(data, numiterations=iterations, blocksplittingmax=0)[2:-4],
	]
	return min(candidates, key=len)


def optimize_project_json(project, level=9, use_zopfli=False, iterations=15, zopfli_required=True, compact_defaults=False,
	compact_costume_references=False, compact_block_flags=False, minimum_json=False, search_rounds=0, relabel_block_ids=False,
	verify_encoding=True, already_relabelled=False):
	"""Search useful encodings while keeping compression search bounded on large projects."""
	baseline = dumps_exact(project)
	search_project = compact_representation(project, compact_defaults, compact_costume_references, compact_block_flags)
	identifier_cost = None
	if relabel_block_ids and not already_relabelled:
		search_project, _, identifier_cost = relabel_blocks(search_project)
	prepared_relabelled = bool(relabel_block_ids or already_relabelled)
	short_minimum_raw = dumps_exact(search_project, True)
	short_minimum_bytes = len(short_minimum_raw)
	best_raw = short_minimum_raw if minimum_json else baseline
	large_project = len(baseline) >= FAST_JSON_RAW_THRESHOLD or sum(
		len(t.get("blocks") or {}) for t in project.get("targets", []) if isinstance(t, dict)
	) >= FAST_JSON_BLOCK_THRESHOLD
	search_level = FAST_JSON_SEARCH_LEVEL if large_project else level
	best_compressed = deflate(best_raw, search_level)
	# Always initialize the encoding label from the baseline candidate. Some valid
	# projects have no candidate that beats the baseline, but the later Zopfli/
	# DEFLATE checks still need a defined label.
	best_label = "minimum/raw-zlib" if minimum_json else "original-order/zlib"
	rank = (lambda encoded, raw: (len(raw), len(encoded))) if minimum_json else (lambda encoded, raw: (len(encoded), len(raw)))
	pool = []
	def remember(raw, compressed):
		if not search_rounds: return
		score = rank(compressed, raw)
		for index, (previous, candidate) in enumerate(pool):
			if raw == candidate:
				if score < previous: pool[index] = (score, raw)
				break
		else:
			pool.append((score, raw))
		pool.sort(key=lambda item: item[0]); del pool[4:]

	trials = 1
	seen = set()

	# Large projects do not need the 120-permutation screen. Screen only the
	# established core layouts, then carry the best few into full-project tests.
	if large_project:
		screened_orders = []
		probe = _sample_blocks(search_project)
		for order in BLOCK_KEY_ORDERS:
			probe_bytes = dumps_exact_layout(probe, order, True)
			screened_orders.append((len(deflate(probe_bytes, FAST_JSON_SCREEN_LEVEL)), order))
		screened_orders = [order for _, order in sorted(screened_orders)[:FAST_JSON_ORDER_COUNT]]
		screen_trials = len(BLOCK_KEY_ORDERS)
		orders = tuple(dict.fromkeys(screened_orders[:FAST_JSON_ORDER_COUNT]))
	else:
		screened_orders, screen_trials = _screen_record_orders(search_project, level)
		orders = tuple(dict.fromkeys((*BLOCK_KEY_ORDERS, *screened_orders)))
	trials += screen_trials

	# For large projects use a single fast compressor while ranking candidate layouts.
	# The final winner is still recompressed with every enabled strategy below.
	search_compressions = (("zlib", lambda data: deflate(data, search_level)),)
	if not large_project:
		search_compressions = tuple((name, lambda data, memory=memory, strategy=strategy: deflate(data, level, memory, strategy))
			for name, memory, strategy in (("zlib", 8, zlib.Z_DEFAULT_STRATEGY), ("zlib-mem9", 9, zlib.Z_DEFAULT_STRATEGY), ("zlib-filtered", 8, zlib.Z_FILTERED))
		)

	def consider(candidate, label_prefix):
		nonlocal best_raw, best_compressed, best_label, trials
		if len(candidate) > len(baseline):
			return
		fingerprint = hashlib.sha256(candidate).digest()
		if fingerprint in seen:
			return
		seen.add(fingerprint)
		for compressor, compress_candidate in search_compressions:
			compressed = compress_candidate(candidate)
			trials += 1
			remember(candidate, compressed)
			if rank(compressed, candidate) < rank(best_compressed, best_raw):
				best_raw, best_compressed = candidate, compressed
				best_label = f"{label_prefix}/{compressor}"

	# Large projects only search the compact numeric representation; its final
	# compression is compared against the best baseline encoding before return.
	short_modes = (True,) if large_project else (False, True)
	for short_numbers in short_modes:
		if minimum_json and not short_numbers: continue
		for order_index, order in enumerate((None, *orders)):
			if order is None and short_numbers:
				candidate = short_minimum_raw
			else:
				candidate = dumps_exact(search_project, short_numbers) if order is None else dumps_exact_layout(search_project, order, short_numbers)
			consider(candidate, f"layout-{order_index}/{'short-numbers' if short_numbers else 'original-numbers'}")

	# Different sprites can benefit from distinct orders. This is valuable on smaller
	# projects, but its per-target compression cost is disproportionate on large ones.
	if isinstance(search_project.get("targets"), list) and not large_project:
		adaptive = dict(search_project)
		adaptive["targets"] = []
		for target in search_project["targets"]:
			chosen = target
			chosen_size = len(deflate(dumps_exact(target, True), level))
			for order in orders:
				candidate = _ordered_blocks(target, order)
				size = len(deflate(dumps_exact(candidate, True), level))
				trials += 1
				if size < chosen_size:
					chosen, chosen_size = candidate, size
			adaptive["targets"].append(chosen)
		candidate = dumps_exact(adaptive, True)
		consider(candidate, "per-target/short-numbers")

	# Specialized grouping is another compression-quality refinement. Keep it on small
	# projects; on large projects the global screen already captures the useful ordering.
	if not large_project:
		for grouping in ("shape", "opcode", "target-opcode"):
			specialized, specialized_trials = _specialized_layout(search_project, orders, level, grouping)
			trials += specialized_trials
			for short_numbers in (False, True):
				if minimum_json and not short_numbers: continue
				candidate = dumps_exact(specialized, short_numbers)
				consider(candidate, f"per-{grouping}/{'short-numbers' if short_numbers else 'original-numbers'}")

	for round_index in range(0 if large_project else search_rounds):
		candidate, window_trials = _window_layout(loads_exact(best_raw), orders, level, 128 if round_index % 2 == 0 else 64)
		trials += window_trials
		consider(candidate, f"window-{round_index + 1}")

	# Candidate layouts were ranked with a cheaper compressor on large projects.
	# From this point onward, compare and emit using the requested compression level.
	best_compressed = deflate(best_raw, level)
	zopfli = get_zopfli(zopfli_required) if use_zopfli else None
	if zopfli:
		# Auto-Zopfli (--all-optimizations/--all-flags) gets a bounded effort on large JSON.
		# Explicit --zopfli keeps the requested iteration count.
		effective_iterations = min(iterations, 2) if (large_project and not zopfli_required) else iterations
		compressed = zopfli_deflate(best_raw, zopfli, effective_iterations)
		trials += 1
		if len(compressed) < len(best_compressed):
			best_compressed = compressed
			best_label += "/zopfli"

	# Re-evaluate all normal DEFLATE strategies only for the final winner.
	for compressor, compressed in _compressions(best_raw, level):
		trials += 1
		if len(compressed) < len(best_compressed):
			best_compressed = compressed
			best_label += f"/{compressor}"

	if verify_encoding:
		difference = exact_difference(project, loads_exact(best_raw), compact_defaults, compact_costume_references, compact_block_flags, relabel_block_ids)
		if difference:
			raise ValueError(f"JSON encoding verification failed: {difference}")
	if zlib.decompress(best_compressed, -15) != best_raw:
		raise ValueError("JSON encoding verification failed: DEFLATE mismatch")
	if minimum_json:
		json_minimum = {
			"model": "fixed-graph-block-ids-v3" if prepared_relabelled else "fixed-tree-v2",
			"minimum_bytes": short_minimum_bytes,
			"components": None,
			"compact_block_defaults": compact_defaults,
			"compact_costume_references": compact_costume_references,
			"compact_block_flags": compact_block_flags,
			"identifier_cost": identifier_cost,
			"global_scratch_minimum_proven": False,
		}
	else:
		json_minimum = _minimum_json_size_prepared(search_project, identifier_cost, prepared_relabelled)
	return best_raw, best_compressed, {
		"json_encoding_trials": trials,
		"json_encoding_bytes_saved": len(baseline) - len(best_raw),
		"json_deflate_bytes_saved": len(deflate(baseline, level)) - len(best_compressed),
		"json_encoding": best_label,
		"zopfli_available": zopfli is not None,
		"json_minimum": json_minimum,
	}


def read_compressed_entry(archive, info):
	archive.fp.seek(info.header_offset)
	header = archive.fp.read(30)
	if len(header) != 30 or header[:4] != b"PK\x03\x04":
		raise ValueError(f"invalid ZIP local header: {info.filename!r}")
	offset = int.from_bytes(header[26:28], "little") + int.from_bytes(header[28:30], "little")
	archive.fp.seek(offset, 1)
	compressed = archive.fp.read(info.compress_size)
	if len(compressed) != info.compress_size:
		raise ValueError(f"truncated ZIP entry: {info.filename!r}")
	return compressed


def write_compressed_entry(archive, info, compressed):
	"""Write a checked raw stream using the same ZIP bookkeeping as ZipFile."""
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


BLOCK_ID_ALPHABET = "!@#$%^*()+_-={}|[]:;?,./~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class ProgressBar:
	def __init__(self, total, enabled=None, width=30):
		self.total = max(1, int(total))
		self.enabled = sys.stdout.isatty() if enabled is None else enabled
		self.width = max(10, int(width))
		self.completed = 0
		self.current = ""
		self.detail_text = ""
		self.started = time.monotonic()
		self.last_render = 0.0

	def _render(self, force=False):
		if not self.enabled:
			return
		now = time.monotonic()
		if not force and now - self.last_render < 0.10:
			return
		self.last_render = now
		fraction = min(1.0, self.completed / self.total)
		filled = int(self.width * fraction)
		bar = Ansi.success("#" * filled) + Ansi.muted("." * (self.width - filled))
		percent = fraction * 100.0
		elapsed = now - self.started
		detail = f" | {self.detail_text}" if self.detail_text else ""
		line = f"\r[{bar}] {self.completed:>2}/{self.total:<2} {percent:5.1f}% | {self.current}{detail} | {elapsed:6.1f}s"
		try:
			terminal_width = shutil.get_terminal_size((120, 20)).columns
		except OSError:
			terminal_width = 120
		width = max(40, terminal_width)
		print(line[:width - 1].ljust(width - 1), end="", flush=True)

	def next(self, label):
		if self.current:
			self.completed = min(self.total, self.completed + 1)
		self.current = label
		self.detail_text = ""
		self._render(force=True)

	def detail(self, text):
		self.detail_text = str(text)
		self._render()

	def finish(self, label="Complete"):
		self.completed = self.total
		self.current = label
		self.detail_text = ""
		self._render(force=True)
		if self.enabled:
			print()

	def close(self):
		if self.enabled:
			print()


def _progress_stage_count(opts):
	count = 1  # normalize block defaults
	count += bool(opts.convert_wav_to_mp3)
	count += bool(opts.deduplicate_assets)
	count += bool(opts.comments)
	count += bool(opts.positions)
	count += bool(opts.covered)
	count += bool(opts.monitors)
	count += bool(opts.cleared_lists)
	count += bool(opts.remove_unreachable)
	count += bool(opts.remove_unused_procedures)
	count += bool(opts.remove_unreachable or opts.remove_unused_procedures)
	count += bool(opts.folded_constant_variables)
	count += bool(opts.script_constant_propagation)
	count += bool(opts.branch_swapping or opts.trivial_loops or opts.nested_conditionals)
	count += bool(opts.associative_constant_merging)
	count += bool(opts.fold_constant_expressions)
	count += bool(opts.simplify_boolean_control or opts.trivial_loops)
	count += bool(opts.simplify_blocks)
	count += bool(opts.group_similar_sequences)
	count += bool(opts.optimize_procedure_arguments)
	count += bool(opts.merge_duplicate_procedures)
	count += bool(opts.inline_single_use_procedures)
	count += bool(opts.remove_unused_procedures and (opts.optimize_procedure_arguments or opts.merge_duplicate_procedures))
	count += bool(opts.remove_unused_variables or opts.remove_unused_lists)
	count += True
	count += bool(opts.remove_unused_broadcasts)
	count += bool(opts.rename_identifiers or opts.rename_variable_names or opts.rename_list_names or opts.rename_broadcast_names or opts.rename_argument_names or opts.rename_procedure_names)
	count += bool(opts.rename_variable_ids or opts.rename_list_ids)
	count += bool(opts.rename_broadcast_ids)
	count += bool(opts.rename_argument_ids)
	count += bool(opts.rename_block_ids)
	count += bool(opts.compact_numeric_inputs)
	count += bool(opts.compact_field_ids)
	count += bool(opts.compact_mutation_hasnext)
	count += bool(opts.compact_mutation_metadata)
	count += bool(opts.normalize_numbers)
	count += bool(not opts.keep_sound_metadata)
	count += bool(opts.remove_empty_fields)
	count += bool(opts.remove_empty_inputs)
	count += bool(opts.remove_costume_metadata)
	count += bool(opts.remove_default_target_properties)
	count += bool(opts.remove_empty_target_containers)
	count += bool(opts.remove_project_meta)
	count += bool(opts.compact_data_literals)
	count += bool(opts.strip_reference_names)
	count += bool(opts.remove_unused_extensions)
	count += True
	count += bool(opts.relabel_block_ids)
	count += bool(opts.optimize_json)
	count += True
	count += True
	return count


class AnsiPrint:
	RESET = "\033[0m"
	BOLD = "\033[1m"
	DIM = "\033[2m"
	RED = "\033[31m"
	GREEN = "\033[32m"
	YELLOW = "\033[33m"
	PURPLE = "\033[35m"
	CYAN = "\033[36m"

	def __init__(self, enabled=None):
		self.enabled = sys.stdout.isatty() if enabled is None else enabled

	def paint(self, text, *codes):
		if not self.enabled:
			return text
		return f"{''.join(codes)}{text}{self.RESET}"

	def heading(self, text):
		return self.paint(text, self.BOLD, self.CYAN)

	def subheading(self, text):
		return self.paint(text, self.PURPLE)

	def success(self, text):
		return self.paint(text, self.GREEN)

	def warning(self, text):
		return self.paint(text, self.YELLOW)

	def error(self, text):
		return self.paint(text, self.RED)

	def muted(self, text):
		return self.paint(text, self.DIM)

	def prompt(self, text):
		return self.paint(text, self.BOLD)


Ansi = AnsiPrint()


def _short_id(index):
	base = len(BLOCK_ID_ALPHABET)
	length = 1
	while index >= base**length:
		index -= base**length
		length += 1
	digits = []
	for _ in range(length):
		digits.append(BLOCK_ID_ALPHABET[index % base])
		index //= base
	return "".join(reversed(digits))


def _mapped_block_id(value, block_ids):
	return block_ids.get(value, value) if isinstance(value, str) else value


def _replace_input_block_ids(value, block_ids):
	for node in _iter_scratch_input_nodes(value):
		tag = node[0]
		if tag in (1, 2) and len(node) > 1:
			node[1] = _mapped_block_id(node[1], block_ids)
		elif tag == 3:
			if len(node) > 1:
				node[1] = _mapped_block_id(node[1], block_ids)
			if len(node) > 2:
				node[2] = _mapped_block_id(node[2], block_ids)


def _count_input_block_refs(value, blocks, refs):
	for ref in _iter_input_block_refs(value):
		if ref in blocks:
			refs[ref] += 1


def _count_block_references(target):
	blocks = target.get("blocks", {})
	refs = Counter()
	for bid, block in blocks.items():
		refs[bid] += 1
		if isinstance(block, dict):
			nxt = block.get("next")
			if isinstance(nxt, str) and nxt in blocks:
				refs[nxt] += 1
			par = block.get("parent")
			if isinstance(par, str) and par in blocks:
				refs[par] += 1
			for value in (block.get("inputs") or {}).values():
				_count_input_block_refs(value, blocks, refs)
	for comment in (target.get("comments") or {}).values():
		if isinstance(comment, dict):
			cbid = comment.get("blockId")
			if isinstance(cbid, str) and cbid in blocks:
				refs[cbid] += 1
	return refs


def _analyze_block_ids(target):
	"""Collect reference frequencies and dangling IDs in a single graph scan."""
	blocks = target.get("blocks", {})
	ids = set(blocks)
	refs = Counter({bid: 1 for bid in blocks})
	dangling = set()
	for block in blocks.values():
		if not isinstance(block, dict):
			continue
		for key in ("next", "parent"):
			v = block.get(key)
			if not isinstance(v, str):
				continue
			if v in ids:
				refs[v] += 1
			else:
				dangling.add(v)
		for value in (block.get("inputs") or {}).values():
			for ref in _iter_input_block_refs(value):
				if ref in ids:
					refs[ref] += 1
				else:
					dangling.add(ref)
	for comment in (target.get("comments") or {}).values():
		if isinstance(comment, dict):
			cbid = comment.get("blockId")
			if isinstance(cbid, str):
				if cbid in ids:
					refs[cbid] += 1
				else:
					dangling.add(cbid)
	return refs, dangling


def rename_block_ids(project, stats, frequency_order=False):
	total_stats = 0
	all_maps = {}
	dangling_skipped = 0

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		old_ids = list(blocks.keys())
		if not old_ids:
			continue

		if frequency_order:
			refs, dangling = _analyze_block_ids(target)
			old_ids = sorted(old_ids, key=lambda x: (-refs[x], x))
		else:
			_, dangling = _analyze_block_ids(target)
		block_ids = _rename_id_map(old_ids, existing_ids=dangling)
		all_maps[ti] = block_ids

		new_blocks = {
			block_ids.get(old_id, old_id): block for old_id, block in blocks.items()
		}
		for block in new_blocks.values():
			if isinstance(block, dict):
				for key in ("next", "parent"):
					if block.get(key) in block_ids:
						block[key] = block_ids[block[key]]
				for value in (block.get("inputs") or {}).values():
					_replace_input_block_ids(value, block_ids)
		for comment in (target.get("comments") or {}).values():
			if (
				isinstance(comment, dict)
				and "blockId" in comment
				and comment["blockId"] is not None
			):
				comment["blockId"] = block_ids.get(
					comment["blockId"], comment["blockId"]
				)
		target["blocks"] = new_blocks
		total_stats += len(block_ids)

	stats["block_ids"] += total_stats
	stats["dangling_refs_skipped"] += dangling_skipped
	return all_maps


def _collect_dangling_in_input(value, ids, dangling):
	for ref in _iter_input_block_refs(value):
		if ref not in ids:
			dangling.add(ref)


def strip_sprite_comments(project, stats):
	for target in project.get("targets", []):
		if target.get("isStage"):
			continue
		comments = target.get("comments")
		if comments:
			stats["comments"] += len(comments)
		target["comments"] = {}  # sb3fix expects an object
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and "comment" in block:
				del block["comment"]  # otherwise, the block links to a missing comment
				stats["comment_links"] += 1


def minify_blocks(project):
	stats = Counter()
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):  # primitive [12, name, id, x, y]
				continue
			if block.get("topLevel") is False:
				del block["topLevel"]
				stats["topLevel"] += 1
			if block.get("shadow") is False:
				del block["shadow"]
				stats["shadow"] += 1
			mut = block.get("mutation")
			if isinstance(mut, dict):
				w = mut.get("warp")
				if w == "true":
					mut["warp"] = True
					stats["warp"] += 1
				elif w == "false":
					mut["warp"] = False
					stats["warp"] += 1
	return stats


def _round_num(v):
	if isinstance(v, bool) or not isinstance(v, (int, float)):
		return v
	if isinstance(v, float):
		if v != v or v in (float("inf"), float("-inf")):  # NaN / inf: leave untouched
			return v
		return int(round(v))
	return v


def round_positions(project, stats):
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict):
				for k in ("x", "y"):
					if k in block:
						n = _round_num(block[k])
						if n != block[k] or type(n) is not type(block[k]):
							block[k] = n
							stats["rounded"] += 1
			elif isinstance(block, list) and len(block) >= 5:  # [12, name, id, x, y]
				for i in (3, 4):
					n = _round_num(block[i])
					if n != block[i] or type(n) is not type(block[i]):
						block[i] = n
						stats["rounded"] += 1
		for comment in (target.get("comments") or {}).values():
			if not isinstance(comment, dict):
				continue
			for k in ("x", "y", "width", "height"):
				if k in comment:
					n = _round_num(comment[k])
					if n != comment[k] or type(n) is not type(comment[k]):
						comment[k] = n
						stats["rounded"] += 1
		for costume in target.get("costumes", []):
			if not isinstance(costume, dict):
				continue
			for k in ("rotationCenterX", "rotationCenterY"):
				if k in costume:
					n = _round_num(costume[k])
					if n != costume[k] or type(n) is not type(costume[k]):
						costume[k] = n
						stats["rounded"] += 1


_BOOLEAN_INPUTS = {
	"operator_and": ("OPERAND1", "OPERAND2"),
	"operator_or": ("OPERAND1", "OPERAND2"),
	"operator_not": ("OPERAND",),
	"control_if": ("CONDITION",),
	"control_if_else": ("CONDITION",),
	"control_repeat_until": ("CONDITION",),
	"control_wait_until": ("CONDITION",),
	"control_while": ("CONDITION",),
}


def _boolean_input_names(block):
	if not isinstance(block, dict):
		return frozenset()
	op = block.get("opcode")
	names = set(_BOOLEAN_INPUTS.get(op, ()))
	if op == "procedures_call":
		mut = block.get("mutation")
		if isinstance(mut, dict):
			try:
				ids = json.loads(mut.get("argumentids") or "[]")
			except (TypeError, ValueError):
				ids = []
			proccode = mut.get("proccode")
			if isinstance(ids, list) and isinstance(proccode, str):
				kinds = [t for t in proccode.split(" ") if t in ("%s", "%b")]
				if len(kinds) == len(ids):
					names.update(
						i for i, k in zip(ids, kinds) if k == "%b" and isinstance(i, str)
					)
	return frozenset(names)


def _is_boolean_slot(block, input_name):
	return input_name in _boolean_input_names(block)


def _is_bare_literal_input(value):
	if not (isinstance(value, list) and len(value) > 1):
		return False
	if value[0] in (1, 3):
		return isinstance(value[1], list)
	return False


_NUMERIC_TAGS = (4, 5, 6, 7, 8)  # number, positive, whole, integer, angle
_TEXT_TAG = 10


def clear_covered_values(project, stats):
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue
			for iv in (block.get("inputs") or {}).values():
				if not (isinstance(iv, list) and len(iv) == 3 and iv[0] == 3):
					continue
				shadow = iv[2]
				if not (isinstance(shadow, list) and len(shadow) == 2):
					continue  # block-id reference or 3-long broadcast: leave alone
				tag = shadow[0]
				if tag in _NUMERIC_TAGS:
					empty = 0
				elif tag == _TEXT_TAG:
					empty = ""
				else:
					continue  # color (9) etc.: validated by sb3fix, keep
				if shadow[1] != empty or type(shadow[1]) is not type(empty):
					shadow[1] = empty
					stats["covered"] += 1


def _variable_ids_used_by_blocks(project):
	"""
	(scope, id) -> True for every variable/list a block could reach.
	Used to decide if a hidden monitor could ever be shown.
	"""
	show_ops = {
		"data_showvariable",
		"data_showlist",
		"data_hidevariable",
		"data_hidelist",
	}
	shown = set()  # ids referenced by a show/hide block
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and block.get("opcode") in show_ops:
				for fk in ("VARIABLE", "LIST"):
					f = (block.get("fields") or {}).get(fk)
					if isinstance(f, list) and len(f) > 1:
						shown.add(f[1])
	return shown


def _is_default_layout(m):
	return (m.get("x", 5), m.get("y", 5)) == (5, 5) and (
		m.get("width", 0),
		m.get("height", 0),
	) == (0, 0)


def clean_monitors(project, stats):
	targets = project.get("targets", [])
	stage = next((t for t in targets if t.get("isStage")), None)
	by_name = {t.get("name"): t for t in targets if not t.get("isStage")}
	shown_ids = _variable_ids_used_by_blocks(project)

	kept = []
	for m in project.get("monitors", []):
		if not isinstance(m, dict) or m.get("opcode") not in (
			"data_variable",
			"data_listcontents",
		):
			kept.append(m)
			continue
		is_list = m["opcode"] == "data_listcontents"
		owner = by_name.get(m.get("spriteName")) if m.get("spriteName") else stage
		store_key = "lists" if is_list else "variables"

		if owner is None or m.get("id") not in owner.get(store_key, {}):
			if m.get("visible") is False:
				stats["monitors_orphan"] += 1
				continue
			kept.append(m)
			continue

		if (
			m.get("visible") is False
			and m.get("id") not in shown_ids
			and _is_default_layout(m)
		):
			stats["monitors_unused"] += 1
			continue

		# redundant data on the survivors
		if is_list and m.get("params"):
			m["params"] = {}
			stats["monitor_params"] += 1

		want = [] if is_list else 0
		if "value" in m and m["value"] != want:
			m["value"] = want
			stats["monitor_value"] += 1
		kept.append(m)

	project["monitors"] = kept


def dumps_compact(obj, sort_keys=False):
	return json.dumps(
		obj, separators=(",", ":"), ensure_ascii=False, sort_keys=sort_keys
	)


LIST_READ_OPS = {
	"data_itemoflist",
	"data_lengthoflist",
	"data_listcontainsitem",
	"data_itemnumoflist",
	"data_listcontents",
}
LIST_ADD_OPS = {"data_addtolist", "data_insertatlist"}
LIST_REPLACE_OPS = {"data_replaceitemoflist"}
LIST_DELETE_OPS = {"data_deleteoflist", "data_deletealloflist"}
LIST_SHOWHIDE_OPS = {"data_showlist", "data_hidelist"}

DEFAULT_LIST_BYTES = 4096
DEFAULT_LIST_ITEMS = 1000
DEFAULT_EPSILON = 1e-8

WAV_TO_MP3_BITRATE = "128k"
WAV_TO_MP3_SAMPLE_RATE = 44100
WAV_TO_MP3_CHANNELS = 2


def _json_len(obj):
	return len(
		json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
	)


def _list_usage(project, ti, list_id):
	targets = project.get("targets", [])
	owner = targets[ti]
	if owner.get("isStage"):
		scan = [
			i
			for i, t in enumerate(targets)
			if i == ti or list_id not in (t.get("lists") or {})
		]
	else:
		scan = [ti]

	use = Counter()

	def scan_prim(e):
		if isinstance(e, list):
			if len(e) >= 3 and e[0] == 13 and e[2] == list_id:
				use["read"] += 1
			else:
				for sub in e:
					scan_prim(sub)
		elif isinstance(e, dict):
			for sub in e.values():
				scan_prim(sub)

	for i in scan:
		for block in targets[i].get("blocks", {}).values():
			if isinstance(block, list):  # [13, name, id, x, y]
				scan_prim(block)
				continue
			if not isinstance(block, dict):
				continue
			f = (block.get("fields") or {}).get("LIST")
			if isinstance(f, list) and len(f) > 1 and f[1] == list_id:
				op = block.get("opcode")
				if op in LIST_READ_OPS:
					use["read"] += 1
				elif op in LIST_ADD_OPS:
					use["add"] += 1
				elif op in LIST_REPLACE_OPS:
					use["replace"] += 1
				elif op in LIST_DELETE_OPS:
					use["delete"] += 1
				elif op in LIST_SHOWHIDE_OPS:
					use["showhide"] += 1
				else:
					use["other"] += 1
			for iv in (block.get("inputs") or {}).values():
				scan_prim(iv)

	for m in project.get("monitors", []):
		if (
			isinstance(m, dict)
			and m.get("opcode") == "data_listcontents"
			and m.get("id") == list_id
			and m.get("visible")
			and bool(m.get("spriteName")) == (not owner.get("isStage"))
			and (owner.get("isStage") or m.get("spriteName") == owner.get("name"))
		):
			use["shown"] += 1
	return use


def describe_list_usage(use) -> tuple[str, bool]:
	"""Returns `(label: str, risky: bool)`. `risky=True` indicates that clearing this list may change behavior."""
	reads = use["read"]
	writes = use["add"] + use["replace"] + use["delete"]
	bits = []
	if writes:
		parts = [
			f"{n} {k}"
			for k, n in (
				("add", use["add"]),
				("replace", use["replace"]),
				("delete", use["delete"]),
			)
			if n
		]
		text = "modified at runtime (" + ", ".join(parts) + ")"
		if use["replace"] and not use["add"]:
			text += ", looks like a fixed-size array"
		bits.append(text)
	if reads:
		bits.append(
			f"read by {reads} block{'s' if reads != 1 else ''}"
			+ ("" if writes else ", never modified")
		)
	if use["shown"] or use["showhide"]:
		bits.append("shown on stage" if use["shown"] else "show/hide blocks")
	if not bits and use["other"]:
		bits.append("referenced by other blocks")
	if not bits:
		return "not referenced by any block", False
	return "; ".join(bits), True


def find_large_lists(
	project, min_bytes=DEFAULT_LIST_BYTES, min_items=DEFAULT_LIST_ITEMS
) -> list[dict]:
	"""
	Return the lists at/above either threshold, largest first:
	  {ti, id, scope, name, items, bytes, label, risky}
	"""
	found = []
	for ti, target in enumerate(project.get("targets", [])):
		for lid, entry in (target.get("lists") or {}).items():
			if not (
				isinstance(entry, list)
				and len(entry) >= 2
				and isinstance(entry[1], list)
			):
				continue
			items = entry[1]
			size = _json_len(items)
			if not items or (size < min_bytes and len(items) < min_items):
				continue
			label, risky = describe_list_usage(_list_usage(project, ti, lid))
			found.append(
				{
					"ti": ti,
					"id": lid,
					"scope": (
						"GLOBAL"
						if target.get("isStage")
						else f"local:{target.get('name')}"
					),
					"name": str(entry[0]),
					"items": len(items),
					"bytes": size,
					"label": label,
					"risky": risky,
				}
			)
	found.sort(key=lambda c: (-c["bytes"], c["scope"], c["name"]))
	return found


def _parse_selection(text, n) -> set[str]:
	chosen = set()
	for tok in text.replace(",", " ").split():
		try:
			if "-" in tok:
				a, _, b = tok.partition("-")
				lo, hi = int(a), int(b)
				if lo > hi:
					raise ValueError(f"'{tok}' is a backwards range")
				chosen.update(range(lo, hi + 1))
			else:
				chosen.add(int(tok))
		except ValueError as e:
			if "range" in str(e):
				raise
			raise ValueError(f"didn't understand '{tok}'") from None
	if not chosen:
		raise ValueError("no list numbers given")
	bad = sorted(i for i in chosen if not 1 <= i <= n)
	if bad:
		raise ValueError(f"no such list number(s): {bad} (valid: 1-{n})")
	return chosen


def prompt_for_lists(project, candidates, project_bytes) -> set | None:
	"""
	Interactively decide which large lists to clear.
	Returns a set of (target_index, list_id) to clear, or None if the user aborted.
	Default (Enter / EOF / closed stdin) = clear nothing.
	"""
	if not candidates:
		return set()
	total = sum(c["bytes"] for c in candidates)
	w_scope = max(len(c["scope"]) for c in candidates)
	w_name = min(34, max(len(c["name"]) for c in candidates))

	print("")
	print(
		Ansi.heading(
			f"Large lists found: {len(candidates)}  "
			f"({total:,} bytes = {total / max(1, project_bytes) * 100:.1f}% of project.json)"
		)
	)
	print("")
	print(
		f"  {'#':>3}  {'scope':<{w_scope}}  {'list':<{w_name}}  {'items':>6}  {'bytes':>8}  usage"
	)
	for i, c in enumerate(candidates, 1):
		nm = c["name"] if len(c["name"]) <= w_name else c["name"][: w_name - 1] + "…"
		mark = Ansi.warning("!") if c["risky"] else " "
		print(
			f" {mark}{i:>3}  {c['scope']:<{w_scope}}  {nm:<{w_name}}  {c['items']:>6,}  {c['bytes']:>8,}  {c['label']}"
		)
	print("")
	print(
		Ansi.warning(
			"  '!' = clearing this list can change how the project behaves. Blocks that read a"
		)
	)
	print(
		"  cleared list get empty values, and 'replace item N' on an emptied list does nothing."
	)
	print("  Cleared lists stay defined, only their contents are removed.")
	print("")
	print(Ansi.muted("  Enter / n   keep every list"))
	print(Ansi.muted("  a           clear all large lists"))
	print(Ansi.muted("  u           clear only lists nothing references"))
	print(Ansi.muted("  1,3,5-7     clear those numbers"))
	print(Ansi.muted("  s N         show a sample of list N"))
	print("")

	while True:
		try:
			answer = (
				input(Ansi.prompt("Clear which lists? [Enter = keep all] > "))
				.strip()
				.lower()
			)
		except EOFError:
			print(Ansi.muted("\n(no input available, keeping all lists)"))
			return set()
		except KeyboardInterrupt:
			print(Ansi.warning("\nAborted."))
			return None

		if answer in ("", "n", "no", "none", "keep"):
			print(Ansi.success("Keeping all lists."))
			return set()

		if answer.startswith("s") and answer[1:].strip().isdigit():
			n = int(answer[1:].strip())
			if not 1 <= n <= len(candidates):
				print(Ansi.error(f"  no such list number: {n}"))
				continue
			c = candidates[n - 1]
			items = project["targets"][c["ti"]]["lists"][c["id"]][1]
			print(
				f"  [{c['scope']}] {c['name']!r}: {len(items):,} items, showing {min(8, len(items))}"
			)
			for k, v in enumerate(items[:8], 1):
				text = str(v).replace("\n", "\\n")
				print(f"    {k:>3}: {text[:70]}{'...' if len(text) > 70 else ''}")
			if len(items) > 8:
				print(f"  ... {len(items) - 8:,} more")
			continue

		if answer in ("a", "all"):
			picked = set(range(1, len(candidates) + 1))
		elif answer in ("u", "unreferenced", "safe"):
			picked = {i for i, c in enumerate(candidates, 1) if not c["risky"]}
			if not picked:
				print(
					Ansi.warning(
						"  every listed list is referenced by the project; nothing is safe to clear."
					)
				)
				continue
		else:
			try:
				picked = _parse_selection(answer, len(candidates))
			except ValueError as e:
				print(Ansi.error(f"  {e}. Try again."))
				continue

		chosen = [candidates[i - 1] for i in sorted(picked)]
		risky = [c for c in chosen if c["risky"]]
		saved = sum(c["bytes"] - 2 for c in chosen)
		print(
			Ansi.success(
				f"  Selected {len(chosen)} list(s), saving about {saved:,} bytes of project.json."
			)
		)
		if risky:
			print(
				Ansi.warning(
					f"  WARNING: {len(risky)} of them are used by the project:"
				)
			)
			for c in risky:
				print(f"    - [{c['scope']}] {c['name']!r}: {c['label']}")
			try:
				confirm = (
					input(
						"  Type 'yes' to clear them anyway, anything else to go back > "
					)
					.strip()
					.lower()
				)
			except EOFError:
				print(Ansi.muted("\n(no input available, keeping all lists)"))
				return set()
			except KeyboardInterrupt:
				print(Ansi.warning("\nAborted."))
				return None
			if confirm != "yes":
				print(Ansi.warning("  Not confirmed. Nothing was cleared... yet >:)."))
				continue
		return {(c["ti"], c["id"]) for c in chosen}


def clear_large_lists(project, to_clear, stats):
	for ti, lid in to_clear:
		entry = project["targets"][ti]["lists"].get(lid)
		if entry is None or not entry[1]:
			continue
		stats["lists_cleared"] += 1
		stats["list_items_cleared"] += len(entry[1])
		entry[1] = []


def _iter_scratch_input_nodes(value):
	"""Iterate serialized Scratch input nodes without walking arbitrary JSON.

	Scratch input values have a small tagged grammar: tags 1/2 contain one
	primary value, tag 3 contains primary+shadow, and other primitive tags are
	flat. Unknown nested lists are still followed defensively, but dictionaries
	are not recursively traversed because they are not part of the input grammar.
	"""
	stack = [value]
	while stack:
		node = stack.pop()
		if not isinstance(node, list) or not node:
			continue
		yield node
		tag = node[0]
		if tag in (1, 2):
			if len(node) > 1 and isinstance(node[1], list):
				stack.append(node[1])
		elif tag == 3:
			if len(node) > 2 and isinstance(node[2], list):
				stack.append(node[2])
			if len(node) > 1 and isinstance(node[1], list):
				stack.append(node[1])
		else:
			for child in node[1:]:
				if isinstance(child, list):
					stack.append(child)


def _field_id(block, names):
	if not isinstance(block, dict):
		return None
	fields = block.get("fields") or {}
	for name in names:
		f = fields.get(name)
		if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str) and f[1]:
			return f[1]
	return None


def _collect_data_references(project, var_owners=None, list_owners=None):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	stage_var_ids = (
		set(targets[stage_index].get("variables", {}))
		if stage_index is not None
		else set()
	)
	stage_list_ids = (
		set(targets[stage_index].get("lists", {})) if stage_index is not None else set()
	)

	var_refs = {}
	list_refs = {}
	broadcast_refs = {}

	def add_var_ref(ti, i, desc):
		if not isinstance(i, str):
			return
		target_ids = targets[ti].get("variables", {})
		if i in target_ids:
			var_refs.setdefault((ti, i), []).append(desc)
		elif stage_index is not None and i in stage_var_ids:
			var_refs.setdefault((stage_index, i), []).append(desc)
		elif var_owners and i in var_owners:
			var_refs.setdefault((var_owners[i], i), []).append(desc)
		else:
			var_refs.setdefault((ti, i), []).append(desc)

	def add_list_ref(ti, i, desc):
		if not isinstance(i, str):
			return
		target_ids = targets[ti].get("lists", {})
		if i in target_ids:
			list_refs.setdefault((ti, i), []).append(desc)
		elif stage_index is not None and i in stage_list_ids:
			list_refs.setdefault((stage_index, i), []).append(desc)
		elif list_owners and i in list_owners:
			list_refs.setdefault((list_owners[i], i), []).append(desc)
		else:
			list_refs.setdefault((ti, i), []).append(desc)

	for ti, target in enumerate(targets):
		tname = target.get("name", f"target_{ti}")
		for bid, block in target.get("blocks", {}).items():
			b_op = block.get("opcode") if isinstance(block, dict) else "primitive"
			desc = f"block {bid!r} ({b_op}) in {tname!r}"

			if isinstance(block, list) and len(block) > 2 and isinstance(block[2], str):
				if block[0] == 12:
					add_var_ref(ti, block[2], desc)
				elif block[0] == 13:
					add_list_ref(ti, block[2], desc)
				elif block[0] == 11:
					broadcast_refs.setdefault(block[2], []).append(desc)

			for v in ((block.get("inputs") or {}).values() if isinstance(block, dict) else ()):
				for node in _iter_scratch_input_nodes(v):
					if len(node) <= 2 or not isinstance(node[2], str):
						continue
					tag = node[0]
					if tag == 12:
						add_var_ref(ti, node[2], desc)
					elif tag == 13:
						add_list_ref(ti, node[2], desc)
					elif tag == 11:
						broadcast_refs.setdefault(node[2], []).append(desc)

			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			for name, f in fields.items():
				if not (isinstance(f, list) and len(f) > 1 and isinstance(f[1], str)):
					continue
				if name == "VARIABLE":
					add_var_ref(ti, f[1], desc)
				elif name == "LIST":
					add_list_ref(ti, f[1], desc)
				elif name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
					broadcast_refs.setdefault(f[1], []).append(desc)

			if block.get("opcode") == "sensing_of_property_menu":
				prop_field = fields.get("PROPERTY")
				if (
					isinstance(prop_field, list)
					and len(prop_field) > 0
					and isinstance(prop_field[0], str)
				):
					prop_name = prop_field[0]
					for target_idx, t in enumerate(targets):
						for vid, vdata in (t.get("variables") or {}).items():
							if (
								isinstance(vdata, list)
								and len(vdata) > 0
								and vdata[0] == prop_name
							):
								add_var_ref(
									target_idx,
									vid,
									f"sensing_of property {prop_name!r} in {desc}",
								)

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
		op = m.get("opcode")
		mid = m.get("id")
		desc = f"monitor {mid!r} ({op})"
		if op == "data_variable" and isinstance(mid, str):
			add_var_ref(ti, mid, desc)
		elif op == "data_listcontents" and isinstance(mid, str):
			add_list_ref(ti, mid, desc)

	return var_refs, list_refs, broadcast_refs


def _collect_data_ids(project):
	var_refs, list_refs, broadcast_refs = _collect_data_references(project)
	return set(var_refs.keys()), set(list_refs.keys()), set(broadcast_refs.keys())


def _rename_id_map(ids, existing_ids=(), prefix=""):
	used = set(existing_ids)
	result = {}
	n = 0
	for old in ids:
		new = f"{prefix}{_short_id(n)}"
		while new in used:
			n += 1
			new = f"{prefix}{_short_id(n)}"
		result[old] = new
		used.add(new)
		n += 1
	return result


def _frequency_optimal_id_map(ids, frequencies=None, existing_ids=(), prefix=""):
	"""Assign the shortest available identifiers to the most frequent symbols.

	Generated identifiers have strictly increasing JSON payload length under
	_short_id(), so descending reference frequency is the byte-optimal assignment
	for this identifier alphabet. Ties are deterministic by the original name.
	"""
	ids = list(ids)
	if not ids:
		return {}
	frequencies = frequencies or {}
	ordered = sorted(ids, key=lambda value: (-frequencies.get(value, 0), value))
	return _rename_id_map(ordered, existing_ids=existing_ids, prefix=prefix)


def _replace_field_id(block, field_names, mapping):
	if not isinstance(block, dict):
		return 0
	changed = 0
	fields = block.get("fields") or {}
	for name in field_names:
		f = fields.get(name)
		if isinstance(f, list) and len(f) > 1 and f[1] in mapping:
			f[1] = mapping[f[1]]
			changed += 1
	return changed


def rename_variable_list_ids(
	project, stats, rename_variables=True, rename_lists=True, frequency_order=False
):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	stage_var_ids = (
		set(targets[stage_index].get("variables", {}))
		if stage_index is not None
		else set()
	)
	stage_list_ids = (
		set(targets[stage_index].get("lists", {})) if stage_index is not None else set()
	)

	var_refs, list_refs, _ = (
		_collect_data_references(project) if frequency_order else ({}, {}, {})
	)

	var_map = {}
	list_map = {}

	def sprites_referencing_stage_ids(stage_ids, tag):
		hits = set()
		if stage_index is None or not stage_ids:
			return hits
		for ti, target in enumerate(targets):
			if ti == stage_index:
				continue
			local_ids = set(
				(target.get("variables") if tag == 12 else target.get("lists")) or {}
			)

			def scan(value):
				if isinstance(value, list):
					if len(value) > 2 and value[0] == tag:
						if value[2] in stage_ids and value[2] not in local_ids:
							hits.add(ti)
					else:
						for v in value:
							scan(v)
				elif isinstance(value, dict):
					for v in value.values():
						scan(v)

			for block in target.get("blocks", {}).values():
				if isinstance(block, list):
					scan(block)
					continue
				if not isinstance(block, dict):
					continue
				field_key = "VARIABLE" if tag == 12 else "LIST"
				f = (block.get("fields") or {}).get(field_key)
				if (
					isinstance(f, list)
					and len(f) > 1
					and f[1] in stage_ids
					and f[1] not in local_ids
				):
					hits.add(ti)
				for value in (block.get("inputs") or {}).values():
					scan(value)
			if ti in hits:
				continue
			for m in project.get("monitors", []):
				if not isinstance(m, dict) or m.get("spriteName") != target.get("name"):
					continue
				op = "data_variable" if tag == 12 else "data_listcontents"
				if (
					m.get("opcode") == op
					and m.get("id") in stage_ids
					and m.get("id") not in local_ids
				):
					hits.add(ti)
		return hits

	allocated_ids = set()
	if rename_variables:
		stage_ids = (
			list(targets[stage_index].get("variables", {}).keys())
			if stage_index is not None
			else []
		)
		if frequency_order and stage_index is not None:
			stage_ids = sorted(
				stage_ids,
				key=lambda vid: (-len(var_refs.get((stage_index, vid), [])), vid),
			)
		stage_new_ids = (
			_rename_id_map(stage_ids, existing_ids=allocated_ids) if stage_ids else {}
		)
		allocated_ids.update(stage_new_ids.values())
		if stage_index is not None:
			for old, new in stage_new_ids.items():
				var_map[(stage_index, old)] = new
		for ti, target in enumerate(targets):
			if ti == stage_index:
				continue
			ids = list((target.get("variables") or {}).keys())
			if not ids:
				continue
			if frequency_order:
				ids = sorted(
					ids, key=lambda vid: (-len(var_refs.get((ti, vid), [])), vid)
				)
			new_ids = _rename_id_map(ids, existing_ids=allocated_ids)
			allocated_ids.update(new_ids.values())
			for old, new in new_ids.items():
				var_map[(ti, old)] = new
	if rename_lists:
		stage_ids = (
			list(targets[stage_index].get("lists", {}).keys())
			if stage_index is not None
			else []
		)
		if frequency_order and stage_index is not None:
			stage_ids = sorted(
				stage_ids,
				key=lambda lid: (-len(list_refs.get((stage_index, lid), [])), lid),
			)
		stage_new_ids = (
			_rename_id_map(stage_ids, existing_ids=allocated_ids) if stage_ids else {}
		)
		allocated_ids.update(stage_new_ids.values())
		if stage_index is not None:
			for old, new in stage_new_ids.items():
				list_map[(stage_index, old)] = new
		for ti, target in enumerate(targets):
			if ti == stage_index:
				continue
			ids = list((target.get("lists") or {}).keys())
			if not ids:
				continue
			if frequency_order:
				ids = sorted(
					ids, key=lambda lid: (-len(list_refs.get((ti, lid), [])), lid)
				)
			new_ids = _rename_id_map(ids, existing_ids=allocated_ids)
			allocated_ids.update(new_ids.values())
			for old, new in new_ids.items():
				list_map[(ti, old)] = new

	original_var_ids = [set((t.get("variables") or {}).keys()) for t in targets]
	original_list_ids = [set((t.get("lists") or {}).keys()) for t in targets]

	def resolve_var_owner(ti, i):
		if i in original_var_ids[ti]:
			return ti
		if stage_index is not None and i in stage_var_ids:
			return stage_index
		return None

	def resolve_list_owner(ti, i):
		if i in original_list_ids[ti]:
			return ti
		if stage_index is not None and i in stage_list_ids:
			return stage_index
		return None

	def rewrite_var(ti, i):
		owner = resolve_var_owner(ti, i)
		return var_map.get((owner, i), i) if owner is not None else i

	def rewrite_list(ti, i):
		owner = resolve_list_owner(ti, i)
		return list_map.get((owner, i), i) if owner is not None else i

	def rewrite_nested(ti, value):
		if isinstance(value, list):
			if len(value) > 2 and value[0] == 12:
				value[2] = rewrite_var(ti, value[2])
			elif len(value) > 2 and value[0] == 13:
				value[2] = rewrite_list(ti, value[2])
			else:
				for v in value:
					rewrite_nested(ti, v)
		elif isinstance(value, dict):
			for v in value.values():
				rewrite_nested(ti, v)

	for ti, target in enumerate(targets):
		if rename_variables:
			target["variables"] = {
				var_map.get((ti, old), old): value
				for old, value in (target.get("variables") or {}).items()
			}
		if rename_lists:
			target["lists"] = {
				list_map.get((ti, old), old): value
				for old, value in (target.get("lists") or {}).items()
			}

		for block in target.get("blocks", {}).values():
			if isinstance(block, list):  # [12/13, name, id, x, y]
				if len(block) > 2 and block[0] == 12:
					block[2] = rewrite_var(ti, block[2])
				elif len(block) > 2 and block[0] == 13:
					block[2] = rewrite_list(ti, block[2])
				continue
			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			f = fields.get("VARIABLE")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = rewrite_var(ti, f[1])
			f = fields.get("LIST")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = rewrite_list(ti, f[1])
			for value in (block.get("inputs") or {}).values():
				rewrite_nested(ti, value)

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
			m["id"] = rewrite_var(ti, m.get("id"))
		elif m.get("opcode") == "data_listcontents":
			m["id"] = rewrite_list(ti, m.get("id"))

	stats["variable_ids"] += len(var_map)
	stats["list_ids"] += len(list_map)
	return var_map, list_map


def _replace_nested_primitive_ids(value, var_map, list_map):
	for node in _iter_scratch_input_nodes(value):
		if len(node) > 2 and node[0] == 12 and node[2] in var_map:
			node[2] = var_map[node[2]]
		elif len(node) > 2 and node[0] == 13 and node[2] in list_map:
			node[2] = list_map[node[2]]


def _replace_broadcast_ids_in_value(value, mapping):
	for node in _iter_scratch_input_nodes(value):
		if (
			len(node) > 2
			and node[0] == 11
			and isinstance(node[2], str)
			and node[2] in mapping
		):
			node[2] = mapping[node[2]]


def _collect_broadcast_references(project):
	refs = {}

	def add(bid, name=None, desc=None):
		if not isinstance(bid, str) or not bid:
			return
		entry = refs.setdefault(bid, {"names": set(), "refs": []})
		if isinstance(name, str) and name:
			entry["names"].add(name)
		if desc:
			entry["refs"].append(desc)

	for ti, target in enumerate(project.get("targets", [])):
		tname = target.get("name", f"target_{ti}")
		for bid, block in target.get("blocks", {}).items():
			if not isinstance(block, dict):
				continue
			for field_name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
				f = (block.get("fields") or {}).get(field_name)
				if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
					add(
						f[1],
						f[0] if isinstance(f[0], str) else None,
						f"block {bid!r} ({block.get('opcode')}) in {tname!r}",
					)
			for value in (block.get("inputs") or {}).values():
				_collect_broadcast_refs_in_value(
					value, add, f"block {bid!r} ({block.get('opcode')}) in {tname!r}"
				)
	return refs


def _collect_broadcast_refs_in_value(value, add, desc=None):
	# Broadcast menus are Scratch primitive tag 11; walk only the serialized
	# input grammar rather than recursively scanning arbitrary JSON containers.
	for node in _iter_scratch_input_nodes(value):
		if len(node) > 2 and node[0] == 11 and isinstance(node[2], str):
			name = node[1] if len(node) > 1 and isinstance(node[1], str) else None
			add(node[2], name, desc)


def _all_broadcast_ids(project):
	ids = set()
	for target in project.get("targets", []):
		ids.update((target.get("broadcasts") or {}).keys())
	return ids


def _repair_dangling_broadcast_refs(project, stats):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	if stage_index is None:
		return set(), {}
	stage = targets[stage_index]
	broadcasts = stage.setdefault("broadcasts", {})
	defined = _all_broadcast_ids(project)
	repaired = set()
	conflicts = {}
	for bid, info in _collect_broadcast_references(project).items():
		if bid in defined:
			continue
		names = sorted(info["names"])
		if len(names) > 1:
			conflicts[bid] = names
			continue
		broadcasts[bid] = names[0] if names else "message"
		defined.add(bid)
		repaired.add(bid)
	if repaired:
		stats["broadcast_refs_repaired"] += len(repaired)
	if conflicts:
		stats["broadcast_ref_conflicts"] += len(conflicts)
	return repaired, conflicts


def rename_broadcast_ids(project, stats, existing_ids=(), frequency_order=False):
	ids = set()
	for target in project.get("targets", []):
		for old_id in target.get("broadcasts") or {}:
			ids.add(old_id)
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict):
				for name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
					f = (block.get("fields") or {}).get(name)
					if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
						ids.add(f[1])
				for value in (block.get("inputs") or {}).values():
					_collect_broadcast_ids_in_value(value, ids)
	if frequency_order:
		_, _, bcast_refs = _collect_data_references(project)
		sorted_ids = sorted(ids, key=lambda bid: (-len(bcast_refs.get(bid, [])), bid))
	else:
		sorted_ids = sorted(ids)
	mapping = _rename_id_map(sorted_ids, existing_ids=existing_ids)
	for target in project.get("targets", []):
		if target.get("broadcasts"):
			target["broadcasts"] = {
				mapping.get(old, old): name
				for old, name in target["broadcasts"].items()
			}
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue
			_replace_field_id(block, ("BROADCAST_OPTION", "BROADCAST_INPUT"), mapping)
			for value in (block.get("inputs") or {}).values():
				_replace_broadcast_ids_in_value(value, mapping)
	stats["broadcast_ids"] += len(mapping)
	return mapping


def _collect_broadcast_ids_in_value(value, ids):
	if not isinstance(value, list) or not value:
		return
	if len(value) > 2 and value[0] == 11 and isinstance(value[2], str):
		ids.add(value[2])
	else:
		for v in value:
			_collect_broadcast_ids_in_value(v, ids)


def _parse_argumentids(mut):
	if not isinstance(mut, dict):
		return None
	raw = mut.get("argumentids")
	if not isinstance(raw, str):
		return None
	try:
		value = json.loads(raw)
	except (TypeError, ValueError):
		return None
	if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
		return None
	return value


def rename_argument_ids(project, stats):
	mapping = {}  # {(target_index, proccode, old_id): new_id} -- for the verifier
	total = 0
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		by_proccode = {}
		for bid, b in blocks.items():
			if not isinstance(b, dict) or b.get("opcode") not in (
				"procedures_prototype",
				"procedures_call",
			):
				continue
			vals = _parse_argumentids(b.get("mutation"))
			if vals is None:
				continue
			proc = b["mutation"].get("proccode")
			by_proccode.setdefault(proc, []).append((bid, vals))

		for proc, entries in by_proccode.items():
			id_lists = {tuple(v) for _, v in entries}
			if len(id_lists) != 1:
				continue
			ids = next(iter(id_lists))
			local_map = _rename_id_map(list(ids))
			if not local_map:
				continue
			for bid, vals in entries:
				b = blocks[bid]
				b["mutation"]["argumentids"] = json.dumps(
					[local_map.get(x, x) for x in vals],
					separators=(",", ":"),
					ensure_ascii=False,
				)
				inputs = b.get("inputs") or {}
				b["inputs"] = {local_map.get(k, k): v for k, v in inputs.items()}
			for old, new in local_map.items():
				mapping[(ti, proc, old)] = new
			total += len(local_map)

	stats["argument_ids"] += total
	return mapping


def rename_identifiers(
	project,
	stats,
	rename_variable_names=True,
	rename_list_names=True,
	rename_broadcast_names=True,
	rename_argument_names=True,
	rename_procedure_names=True,
):
	targets = project.get("targets", [])
	stage_index = next(
		(i for i, target in enumerate(targets) if target.get("isStage")), None
	)
	variable_names = {}
	list_names = {}
	broadcast_names = {}
	argument_names = {}
	argument_metadata = {}
	procedure_names = {}
	changed = 0

	variable_new_names = {}
	list_new_names = {}

	# Count semantic uses before any names are rewritten. Definitions count as one
	# serialized occurrence, while _collect_data_references() captures references
	# through fields, inputs, sensing_of, and monitors. This lets the shortest
	# generated names go to the identifiers that actually dominate project.json.
	var_refs, list_refs, broadcast_refs = _collect_data_references(project)
	variable_frequencies = Counter()
	list_frequencies = Counter()
	broadcast_frequencies = Counter()
	for key, refs in var_refs.items():
		variable_frequencies[key] += 1 + len(refs)
	for key, refs in list_refs.items():
		list_frequencies[key] += 1 + len(refs)
	for key, refs in broadcast_refs.items():
		broadcast_frequencies[key] += len(refs)

	stage_variable_names = set()
	stage_list_names = set()

	# rename the Stage first so its post-rename names can be reserved to avoid name collisions
	if stage_index is not None:
		stage = targets[stage_index]
		variables = stage.get("variables") or {}
		lists = stage.get("lists") or {}
		valid_variables = {
			vid: entry
			for vid, entry in variables.items()
			if isinstance(entry, list) and entry and isinstance(entry[0], str)
		}
		valid_lists = {
			lid: entry
			for lid, entry in lists.items()
			if isinstance(entry, list) and entry and isinstance(entry[0], str)
		}

		reserved_variables = {
			entry[0]
			for vid, entry in variables.items()
			if (
				vid not in valid_variables
				or not rename_variable_names
			)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		}
		reserved_lists = {
			entry[0]
			for lid, entry in lists.items()
			if (
				lid not in valid_lists
				or not rename_list_names
			)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		}

		stage_var_map = (
			_frequency_optimal_id_map(
				valid_variables, variable_frequencies, reserved_variables
			)
			if rename_variable_names
			else {}
		)
		stage_list_map = (
			_frequency_optimal_id_map(
				valid_lists, list_frequencies, reserved_lists
			)
			if rename_list_names
			else {}
		)

		if rename_variable_names:
			variable_new_names.update(
				{
					(stage_index, vid): stage_var_map[vid]
					for vid in valid_variables
				}
			)
		for vid, entry in valid_variables.items():
			new_name = variable_new_names.get((stage_index, vid), entry[0])
			stage_variable_names.add(new_name)
			if rename_variable_names and entry[0] != new_name:
				variable_names[(stage_index, vid)] = entry[0]
				entry[0] = new_name
				changed += 1

		if rename_list_names:
			list_new_names.update(
				{
					(stage_index, lid): stage_list_map[lid]
					for lid in valid_lists
				}
			)
		for lid, entry in valid_lists.items():
			new_name = list_new_names.get((stage_index, lid), entry[0])
			stage_list_names.add(new_name)
			if rename_list_names and entry[0] != new_name:
				list_names[(stage_index, lid)] = entry[0]
				entry[0] = new_name
				changed += 1

	for ti, target in enumerate(targets):
		if ti == stage_index:
			continue
		variables = target.get("variables") or {}
		lists = target.get("lists") or {}
		valid_variables = {
			vid: entry
			for vid, entry in variables.items()
			if isinstance(entry, list) and entry and isinstance(entry[0], str)
		}
		valid_lists = {
			lid: entry
			for lid, entry in lists.items()
			if isinstance(entry, list) and entry and isinstance(entry[0], str)
		}

		reserved_variables = set(stage_variable_names)
		reserved_variables.update(
			entry[0]
			for vid, entry in variables.items()
			if (
				vid not in valid_variables
				or not rename_variable_names
			)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		)
		reserved_lists = set(stage_list_names)
		reserved_lists.update(
			entry[0]
			for lid, entry in lists.items()
			if (
				lid not in valid_lists
				or not rename_list_names
			)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		)

		var_map = (
			_frequency_optimal_id_map(
				valid_variables, variable_frequencies, reserved_variables
			)
			if rename_variable_names
			else {}
		)
		list_map = (
			_frequency_optimal_id_map(
				valid_lists, list_frequencies, reserved_lists
			)
			if rename_list_names
			else {}
		)

		if rename_variable_names:
			variable_new_names.update(
				{
					(ti, vid): var_map[vid]
					for vid in valid_variables
				}
			)
		for vid, entry in valid_variables.items():
			new_name = variable_new_names.get((ti, vid), entry[0])
			if rename_variable_names and entry[0] != new_name:
				variable_names[(ti, vid)] = entry[0]
				entry[0] = new_name
				changed += 1

		if rename_list_names:
			list_new_names.update(
				{
					(ti, lid): list_map[lid]
					for lid in valid_lists
				}
			)
		for lid, entry in valid_lists.items():
			new_name = list_new_names.get((ti, lid), entry[0])
			if rename_list_names and entry[0] != new_name:
				list_names[(ti, lid)] = entry[0]
				entry[0] = new_name
				changed += 1

		if rename_variable_names:
			variable_new_names.update(
				{
					(ti, vid): var_map[vid]
					for vid in valid_variables
				}
			)
		for vid, entry in valid_variables.items():
			if rename_variable_names and entry[0] != variable_new_names[(ti, vid)]:
				variable_names[(ti, vid)] = entry[0]
				entry[0] = variable_new_names[(ti, vid)]
				changed += 1

		if rename_list_names:
			list_new_names.update(
				{
					(ti, lid): list_map[lid]
					for lid in valid_lists
				}
			)
		for lid, entry in valid_lists.items():
			if rename_list_names and entry[0] != list_new_names[(ti, lid)]:
				list_names[(ti, lid)] = entry[0]
				entry[0] = list_new_names[(ti, lid)]
				changed += 1

	stage_broadcast_names = {}
	broadcast_definition_frequencies = Counter()
	conflicting_broadcast_ids = set()
	for target in targets:
		for bid, name in (target.get("broadcasts") or {}).items():
			broadcast_definition_frequencies[bid] += 1
			if not isinstance(bid, str) or not isinstance(name, str):
				continue
			previous = stage_broadcast_names.get(bid)
			if previous is not None and previous != name:
				conflicting_broadcast_ids.add(bid)
			else:
				stage_broadcast_names[bid] = name
	valid_broadcast_ids = (
		sorted(set(stage_broadcast_names) - conflicting_broadcast_ids)
		if rename_broadcast_names
		else []
	)
	reserved_broadcast_names = {
		name
		for bid, name in stage_broadcast_names.items()
		if bid in conflicting_broadcast_ids
	}
	for bid, count in broadcast_definition_frequencies.items():
		broadcast_frequencies[bid] += count
	broadcast_new_names = _frequency_optimal_id_map(
		valid_broadcast_ids, broadcast_frequencies, reserved_broadcast_names
	)
	for bid, new_name in broadcast_new_names.items():
		old_name = stage_broadcast_names[bid]
		if old_name != new_name:
			broadcast_names[bid] = old_name
			changed += 1

	procedure_new_names = {}
	if rename_procedure_names:
		for ti, target in enumerate(targets):
			blocks = target.get("blocks") or {}
			procedure_frequencies = Counter()
			for block in blocks.values():
				if not isinstance(block, dict) or block.get("opcode") not in (
					"procedures_prototype",
					"procedures_call",
				):
					continue
				mutation = block.get("mutation")
				proccode = mutation.get("proccode") if isinstance(mutation, dict) else None
				if isinstance(proccode, str) and proccode:
					procedure_frequencies[proccode] += 1

			old_proccodes = list(procedure_frequencies)
			# Procedure names use a smaller alphabet so generated names cannot
			# accidentally introduce Scratch block/icon syntax. The name portion
			# is optimized by frequency; the %... suffix is preserved exactly.
			procedure_alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
			def short_procedure_name(index):
				base = len(procedure_alphabet)
				name = ""
				n = index
				while True:
					name = procedure_alphabet[n % base] + name
					n = n // base
					if n == 0:
						return name

			for index, old_proccode in enumerate(
				sorted(old_proccodes, key=lambda p: (-procedure_frequencies[p], p))
			):
				new_name = short_procedure_name(index)
				percent = old_proccode.find("%")
				if percent >= 0:
					suffix_start = percent
					while suffix_start > 0 and old_proccode[suffix_start - 1].isspace():
						suffix_start -= 1
					new_proccode = new_name + old_proccode[suffix_start:]
				else:
					new_proccode = new_name
				procedure_new_names[(ti, old_proccode)] = new_proccode
				if old_proccode != new_proccode:
					procedure_names[(ti, new_proccode)] = old_proccode

		for ti, target in enumerate(targets):
			blocks = target.get("blocks") or {}
			for block in blocks.values():
				if not isinstance(block, dict) or block.get("opcode") not in (
					"procedures_prototype",
					"procedures_call",
				):
					continue
				mutation = block.get("mutation")
				if not isinstance(mutation, dict):
					continue
				old_proccode = mutation.get("proccode")
				new_proccode = procedure_new_names.get((ti, old_proccode))
				if isinstance(new_proccode, str) and old_proccode != new_proccode:
					mutation["proccode"] = new_proccode
					changed += 1

	argument_new_names = {}
	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		argument_frequencies = Counter()
		for block in blocks.values() if rename_argument_names else ():
			if not isinstance(block, dict):
				continue
			mutation = block.get("mutation")
			if isinstance(mutation, dict) and isinstance(
				mutation.get("argumentnames"), str
			):
				try:
					decoded = json.loads(mutation["argumentnames"])
				except (TypeError, ValueError):
					decoded = None
				if isinstance(decoded, list) and all(
					isinstance(name, str) for name in decoded
				):
					argument_frequencies.update(decoded)
			if block.get("opcode", "").startswith("argument_reporter_"):
				field = (block.get("fields") or {}).get("VALUE")
				if isinstance(field, list) and field and isinstance(field[0], str):
					argument_frequencies[field[0]] += 1
		old_argument_names = list(argument_frequencies)
		new_names = _frequency_optimal_id_map(
			old_argument_names, argument_frequencies
		)
		argument_new_names[ti] = new_names
		argument_names.update(
			{(ti, new): old for old, new in new_names.items() if old != new}
		)

	def variable_owner(ti, variable_id):
		if variable_id in (targets[ti].get("variables") or {}):
			return (ti, variable_id)
		if stage_index is not None and variable_id in (
			targets[stage_index].get("variables") or {}
		):
			return (stage_index, variable_id)
		return None

	def list_owner(ti, list_id):
		if list_id in (targets[ti].get("lists") or {}):
			return (ti, list_id)
		if stage_index is not None and list_id in (
			targets[stage_index].get("lists") or {}
		):
			return (stage_index, list_id)
		return None

	def rename_nested_names(ti, value):
		nonlocal changed
		if isinstance(value, list):
			if len(value) > 2 and value[0] == 12 and isinstance(value[2], str):
				owner = variable_owner(ti, value[2])
				if (
					owner in variable_new_names
					and value[1] != variable_new_names[owner]
				):
					value[1] = variable_new_names[owner]
					changed += 1
				return
			if len(value) > 2 and value[0] == 13 and isinstance(value[2], str):
				owner = list_owner(ti, value[2])
				if owner in list_new_names and value[1] != list_new_names[owner]:
					value[1] = list_new_names[owner]
					changed += 1
				return
			if len(value) > 2 and value[0] == 11 and value[2] in broadcast_new_names:
				if value[1] != broadcast_new_names[value[2]]:
					value[1] = broadcast_new_names[value[2]]
					changed += 1
				return
			for child in value:
				if isinstance(child, (list, dict)):
					rename_nested_names(ti, child)
		elif isinstance(value, dict):
			for child in value.values():
				if isinstance(child, (list, dict)):
					rename_nested_names(ti, child)

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		broadcasts = target.get("broadcasts") or {}
		for bid, name in list(broadcasts.items()):
			if bid in broadcast_new_names:
				broadcasts[bid] = broadcast_new_names[bid]
		for block_id, block in blocks.items():
			if isinstance(block, list):
				rename_nested_names(ti, block)
				continue
			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			for field_name, owner_fn, new_names in (
				("VARIABLE", variable_owner, variable_new_names),
				("LIST", list_owner, list_new_names),
			):
				field = fields.get(field_name)
				if (
					isinstance(field, list)
					and len(field) > 1
					and isinstance(field[1], str)
				):
					owner = owner_fn(ti, field[1])
					if owner in new_names and field[0] != new_names[owner]:
						field[0] = new_names[owner]
						changed += 1
			for field_name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
				field = fields.get(field_name)
				if (
					isinstance(field, list)
					and len(field) > 1
					and field[1] in broadcast_new_names
				):
					if field[0] != broadcast_new_names[field[1]]:
						field[0] = broadcast_new_names[field[1]]
						changed += 1
			# sensing_of stores the variable/list name in PROPERTY
			if block.get("opcode") in (
				"sensing_of",
				"sensing_of_property_menu",
			):
				field = fields.get("PROPERTY")
				if (
					isinstance(field, list)
					and field
					and isinstance(field[0], str)
				):
					property_ti = _resolve_sensing_of_target(
						project, ti, block, stage_index
					)
					new_prop = _resolve_renamed_property_name(
						ti,
						field[0],
						variable_names,
						list_names,
						variable_new_names,
						list_new_names,
						stage_index,
						property_ti,
					)
					if new_prop is not None and field[0] != new_prop:
						field[0] = new_prop
						changed += 1
			if block.get("opcode", "").startswith("argument_reporter_"):
				field = fields.get("VALUE")
				new_names = argument_new_names.get(ti, {})
				if isinstance(field, list) and field and field[0] in new_names:
					if field[0] != new_names[field[0]]:
						old_name = field[0]
						field[0] = new_names[old_name]
						changed += 1
			mutation = block.get("mutation")
			if isinstance(mutation, dict) and isinstance(
				mutation.get("argumentnames"), str
			):
				raw_names = mutation["argumentnames"]
				try:
					decoded = json.loads(raw_names)
				except (TypeError, ValueError):
					decoded = None
				if isinstance(decoded, list) and all(
					isinstance(name, str) for name in decoded
				):
					new_names = argument_new_names.get(ti, {})
					renamed = [new_names.get(name, name) for name in decoded]
					if renamed != decoded:
						argument_metadata[(ti, block_id)] = raw_names
						mutation["argumentnames"] = json.dumps(
							renamed, separators=(",", ":"), ensure_ascii=False
						)
						changed += sum(a != b for a, b in zip(decoded, renamed))
			for value in (block.get("inputs") or {}).values():
				rename_nested_names(ti, value)

	name_to_index = {
		t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")
	}
	for mon in project.get("monitors", []):
		if not isinstance(mon, dict):
			continue
		op = mon.get("opcode")
		mid = mon.get("id")
		if not isinstance(mid, str):
			continue
		sprite = mon.get("spriteName")
		mon_ti = name_to_index.get(sprite, stage_index) if sprite else stage_index
		if mon_ti is None:
			continue
		params = mon.get("params")
		if not isinstance(params, dict):
			continue
		if op == "data_variable":
			owner = (
				(mon_ti, mid)
				if mid in (targets[mon_ti].get("variables") or {})
				else (
					(stage_index, mid)
					if stage_index is not None
					and mid in (targets[stage_index].get("variables") or {})
					else None
				)
			)
			if owner is not None and owner in variable_new_names:
				new_name = variable_new_names[owner]
				if params.get("VARIABLE") != new_name:
					params["VARIABLE"] = new_name
					changed += 1
		elif op == "data_listcontents":
			owner = (
				(mon_ti, mid)
				if mid in (targets[mon_ti].get("lists") or {})
				else (
					(stage_index, mid)
					if stage_index is not None
					and mid in (targets[stage_index].get("lists") or {})
					else None
				)
			)
			if owner is not None and owner in list_new_names:
				new_name = list_new_names[owner]
				if params.get("LIST") != new_name:
					params["LIST"] = new_name
					changed += 1

	stats["identifier_names"] += changed
	return {
		"variables": variable_names,
		"lists": list_names,
		"broadcasts": broadcast_names,
		"arguments": argument_names,
		"procedures": procedure_names,
		"argument_metadata": argument_metadata,
		# new display name -> original, per target, for sensing_of / monitor restore
		"variable_name_rev": {
			(ti, variable_new_names[(ti, vid)]): old
			for (ti, vid), old in variable_names.items()
		},
		"list_name_rev": {
			(ti, list_new_names[(ti, lid)]): old
			for (ti, lid), old in list_names.items()
		},
	}


def _target_index_for_object_name(targets, object_name, stage_index):
	if not isinstance(object_name, str):
		return None
	if object_name == "_stage_":
		return stage_index
	for target_index, target in enumerate(targets):
		if target.get("name") == object_name:
			return target_index
	return None


def _resolve_sensing_of_target(project, ti, block, stage_index):
	targets = project.get("targets", [])
	if not isinstance(block, dict):
		return ti

	blocks = targets[ti].get("blocks", {}) if 0 <= ti < len(targets) else {}
	object_block = block
	if block.get("opcode") == "sensing_of_property_menu":
		parent_id = block.get("parent")
		parent = blocks.get(parent_id) if isinstance(parent_id, str) else None
		if isinstance(parent, dict) and parent.get("opcode") == "sensing_of":
			object_block = parent
	inputs = object_block.get("inputs") or {}
	value = inputs.get("OBJECT")

	seen = set()

	def resolve(value):
		if isinstance(value, list):
			if len(value) > 1:
				ref = value[1]
				if isinstance(ref, str):
					if ref in blocks and ref not in seen:
						seen.add(ref)
						menu = blocks[ref]
						if isinstance(menu, dict):
							fields = menu.get("fields") or {}
							field = fields.get("OBJECT")
							if isinstance(field, list) and field:
								index = _target_index_for_object_name(
									targets, field[0], stage_index
								)
								if index is not None:
									return index
						index = _target_index_for_object_name(
							targets, ref, stage_index
						)
						if index is not None:
							return index
				else:
					index = resolve(ref)
					if index is not None:
						return index

			if value[0] == 10 and isinstance(value[1], str):
				index = _target_index_for_object_name(
					targets, value[1], stage_index
				)
				if index is not None:
					return index

			for child in value[2:] if len(value) > 2 else ():
				if isinstance(child, (list, dict)):
					index = resolve(child)
					if index is not None:
						return index
		elif isinstance(value, dict):
			fields = value.get("fields") or {}
			field = fields.get("OBJECT")
			if isinstance(field, list) and field:
				index = _target_index_for_object_name(
					targets, field[0], stage_index
				)
				if index is not None:
					return index
			for child in value.values():
				if isinstance(child, (list, dict)):
					index = resolve(child)
					if index is not None:
						return index
		return None

	selected = resolve(value)
	return selected if selected is not None else ti


def _resolve_renamed_property_name(
	ti,
	old_name,
	variable_names,
	list_names,
	variable_new_names,
	list_new_names,
	stage_index,
	object_ti=None,
):
	search_order = []
	if object_ti is not None:
		search_order.append(object_ti)
	else:
		if ti is not None:
			search_order.append(ti)
		if stage_index is not None and stage_index not in search_order:
			search_order.append(stage_index)

	for owner_ti in search_order:
		for (oti, vid), oname in variable_names.items():
			if oti == owner_ti and oname == old_name:
				return variable_new_names.get((oti, vid))
		for (oti, lid), oname in list_names.items():
			if oti == owner_ti and oname == old_name:
				return list_new_names.get((oti, lid))
	return None


def _restore_identifier_names(project, renamed_names):
	if not renamed_names:
		return
	targets = project.get("targets", [])
	variable_names = renamed_names.get("variables", {})
	list_names = renamed_names.get("lists", {})
	broadcast_names = renamed_names.get("broadcasts", {})
	argument_names = renamed_names.get("arguments", {})
	argument_metadata = renamed_names.get("argument_metadata", {})
	procedure_names = renamed_names.get("procedures", {})
	variable_name_rev = renamed_names.get("variable_name_rev", {})
	list_name_rev = renamed_names.get("list_name_rev", {})
	stage_index = next(
		(i for i, target in enumerate(targets) if target.get("isStage")), None
	)

	def restore_variable_owner(ti, variable_id):
		if variable_id in (targets[ti].get("variables") or {}):
			return (ti, variable_id)
		if stage_index is not None and variable_id in (
			targets[stage_index].get("variables") or {}
		):
			return (stage_index, variable_id)
		return None

	def restore_list_owner(ti, list_id):
		if list_id in (targets[ti].get("lists") or {}):
			return (ti, list_id)
		if stage_index is not None and list_id in (
			targets[stage_index].get("lists") or {}
		):
			return (stage_index, list_id)
		return None

	def restore_nested_names(ti, value):
		if isinstance(value, list):
			if len(value) > 2 and value[0] == 12 and isinstance(value[2], str):
				owner = restore_variable_owner(ti, value[2])
				if owner in variable_names:
					value[1] = variable_names[owner]
				return
			if len(value) > 2 and value[0] == 13 and isinstance(value[2], str):
				owner = restore_list_owner(ti, value[2])
				if owner in list_names:
					value[1] = list_names[owner]
				return
			if len(value) > 2 and value[0] == 11 and value[2] in broadcast_names:
				value[1] = broadcast_names[value[2]]
				return
			for child in value:
				if isinstance(child, (list, dict)):
					restore_nested_names(ti, child)
		elif isinstance(value, dict):
			for child in value.values():
				if isinstance(child, (list, dict)):
					restore_nested_names(ti, child)

	for ti, target in enumerate(targets):
		for variable_id, original_name in variable_names.items():
			if variable_id[0] == ti:
				entry = (target.get("variables") or {}).get(variable_id[1])
				if isinstance(entry, list) and entry:
					entry[0] = original_name
		for list_id, original_name in list_names.items():
			if list_id[0] == ti:
				entry = (target.get("lists") or {}).get(list_id[1])
				if isinstance(entry, list) and entry:
					entry[0] = original_name
		broadcasts = target.get("broadcasts") or {}
		for broadcast_id, original_name in broadcast_names.items():
			if broadcast_id in broadcasts:
				broadcasts[broadcast_id] = original_name
		blocks = target.get("blocks") or {}
		for block_id, block in blocks.items():
			original_argument_names = argument_metadata.get((ti, block_id))
			mutation = block.get("mutation") if isinstance(block, dict) else None
			if isinstance(mutation, dict) and block.get("opcode") in (
				"procedures_prototype",
				"procedures_call",
			):
				current_proccode = mutation.get("proccode")
				original_proccode = procedure_names.get((ti, current_proccode))
				if original_proccode is not None:
					mutation["proccode"] = original_proccode
			if original_argument_names is not None and isinstance(mutation, dict):
				mutation["argumentnames"] = original_argument_names
			if isinstance(block, list):
				restore_nested_names(ti, block)
				continue
			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			for field_name, owner_fn, old_names in (
				("VARIABLE", restore_variable_owner, variable_names),
				("LIST", restore_list_owner, list_names),
			):
				field = fields.get(field_name)
				if (
					isinstance(field, list)
					and len(field) > 1
					and isinstance(field[1], str)
				):
					owner = owner_fn(ti, field[1])
					if owner in old_names:
						field[0] = old_names[owner]
			for field_name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
				field = fields.get(field_name)
				if (
					isinstance(field, list)
					and len(field) > 1
					and field[1] in broadcast_names
				):
					field[0] = broadcast_names[field[1]]
			if block.get("opcode") in (
				"sensing_of",
				"sensing_of_property_menu",
			):
				field = fields.get("PROPERTY")
				if (
					isinstance(field, list)
					and field
					and isinstance(field[0], str)
				):
					property_ti = _resolve_sensing_of_target(
						project, ti, block, stage_index
					)
					restored = _resolve_original_property_name(
						ti, field[0], variable_name_rev, list_name_rev, stage_index,
						property_ti,
					)
					if restored is not None:
						field[0] = restored
			if block.get("opcode", "").startswith("argument_reporter_"):
				field = fields.get("VALUE")
				if isinstance(field, list) and field:
					original_name = argument_names.get((ti, field[0]))
					if original_name is not None:
						field[0] = original_name
			for value in (block.get("inputs") or {}).values():
				restore_nested_names(ti, value)

	name_to_index = {
		t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")
	}
	for mon in project.get("monitors", []):
		if not isinstance(mon, dict):
			continue
		op = mon.get("opcode")
		mid = mon.get("id")
		if not isinstance(mid, str):
			continue
		sprite = mon.get("spriteName")
		mon_ti = name_to_index.get(sprite, stage_index) if sprite else stage_index
		if mon_ti is None:
			continue
		params = mon.get("params")
		if not isinstance(params, dict):
			continue
		if op == "data_variable":
			owner = restore_variable_owner(mon_ti, mid)
			if owner in variable_names:
				params["VARIABLE"] = variable_names[owner]
		elif op == "data_listcontents":
			owner = restore_list_owner(mon_ti, mid)
			if owner in list_names:
				params["LIST"] = list_names[owner]


def _resolve_original_property_name(
	ti, current_name, variable_name_rev, list_name_rev, stage_index,
	object_ti=None,
):
	search_order = []
	if object_ti is not None:
		search_order.append(object_ti)
	else:
		if ti is not None:
			search_order.append(ti)
		if stage_index is not None and stage_index not in search_order:
			search_order.append(stage_index)
	for owner_ti in search_order:
		key = (owner_ti, current_name)
		if key in variable_name_rev:
			return variable_name_rev[key]
		if key in list_name_rev:
			return list_name_rev[key]
	return None


def _all_referenced_ids(project):
	vars_, lists_, broadcasts = _collect_data_ids(project)
	return vars_, lists_, broadcasts


def remove_unused_data(project, stats, remove_variables=True, remove_lists=True):
	used_vars, used_lists, _ = _all_referenced_ids(project)
	removed_vars = removed_lists = 0
	for ti, target in enumerate(project.get("targets", [])):
		if remove_variables:
			variables = target.get("variables") or {}
			for vid in list(variables):
				entry = variables[vid]
				is_cloud = (
					isinstance(entry, list) and len(entry) > 2 and entry[2] is True
				)
				if (ti, vid) not in used_vars and not is_cloud:
					del variables[vid]
					removed_vars += 1
		if remove_lists:
			lists = target.get("lists") or {}
			for lid in list(lists):
				if (ti, lid) not in used_lists:
					del lists[lid]
					removed_lists += 1
	stats["variables_removed"] += removed_vars
	stats["lists_removed"] += removed_lists
	return removed_vars, removed_lists


def remove_unused_broadcasts(project, stats):
	used = _collect_data_ids(project)[2]
	removed = 0
	for target in project.get("targets", []):
		bcasts = target.get("broadcasts")
		if isinstance(bcasts, dict):
			for bid in list(bcasts):
				if bid not in used:
					del bcasts[bid]
					removed += 1
	stats["broadcasts_removed"] += removed
	return removed


def _is_hat(block):
	if not isinstance(block, dict):
		return False
	op = block.get("opcode", "")
	return (
		op.endswith("_whenflagclicked")
		or op.endswith("_whenkeypressed")
		or (op.startswith("event_when") or op.startswith("control_start_as_clone"))
		or op
		in {
			"event_whenthisspriteclicked",
			"event_whenbackdropswitchesto",
			"event_whenbroadcastreceived",
			"event_whengreaterthan",
		}
	)


class _ScratchGraphIndex:
	"""Cached block graph and reverse edges for a single Scratch target."""
	__slots__ = ("target", "edges", "parents", "incoming")

	def __init__(self, target):
		self.target = target
		self.edges = {}
		self.parents = {}
		self.incoming = Counter()
		self.rebuild()

	def rebuild(self):
		blocks = self.target.get("blocks", {})
		edges = {}
		parents = {}
		incoming = Counter()
		for bid, block in blocks.items():
			out = set()
			if isinstance(block, dict):
				nxt = block.get("next")
				if isinstance(nxt, str) and nxt in blocks:
					out.add(nxt)
					incoming[nxt] += 1
					parents.setdefault(nxt, set()).add(bid)
				for value in (block.get("inputs") or {}).values():
					# `_input_block_ids` historically de-duplicates references within
					# one serialized input, so preserve that exact multiplicity rule.
					for ref in set(_iter_input_block_refs(value)):
						if ref in blocks:
							out.add(ref)
						incoming[ref] += 1
						parents.setdefault(ref, set()).add(bid)
			edges[bid] = out
		self.edges = edges
		self.parents = parents
		self.incoming = incoming
		return self

	def refresh_blocks(self, block_ids):
		"""Refresh only the outgoing reference edges of changed/removed blocks."""
		blocks = self.target.get("blocks", {})
		for bid in set(block_ids):
			old_out = self.edges.get(bid, ())
			for ref in old_out:
				owners = self.parents.get(ref)
				if owners is not None:
					owners.discard(bid)
					if not owners:
						del self.parents[ref]
				count = self.incoming.get(ref, 0) - 1
				if count > 0:
					self.incoming[ref] = count
				else:
					del self.incoming[ref]

			block = blocks.get(bid)
			if not isinstance(block, dict):
				self.edges.pop(bid, None)
				continue

			new_out = set()
			nxt = block.get("next")
			if isinstance(nxt, str) and nxt in blocks:
				new_out.add(nxt)
			for value in (block.get("inputs") or {}).values():
				for ref in set(_iter_input_block_refs(value)):
					if ref in blocks:
						new_out.add(ref)
			self.edges[bid] = new_out
			for ref in new_out:
				self.parents.setdefault(ref, set()).add(bid)
				self.incoming[ref] += 1
		return self


def _build_block_graph(target, graph=None):
	if graph is None:
		graph = _ScratchGraphIndex(target)
	return graph.edges


def _collect_block_refs(value, blocks, out):
	for ref in _iter_input_block_refs(value):
		if ref in blocks:
			out.add(ref)


def _repair_dangling_block_ref(value, blocks):
	"""
	Repair a serialized Scratch input whose block/shadow reference no longer
	resolves.
	Returns (replacement, changed).
	`None` means the whole input should be removed because there is no safe value to preserve.
	"""
	if not isinstance(value, list) or not value:
		return value, False

	tag = value[0]
	if tag in (1, 2):
		if len(value) <= 1:
			return value, False
		ref = value[1]
		if isinstance(ref, str):
			if ref in blocks:
				return value, False
			return None, True
		if isinstance(ref, (list, dict)):
			fixed, changed = _repair_dangling_block_ref(ref, blocks)
			if fixed is None:
				return None, True
			if changed:
				value[1] = fixed
			return value, changed
		return value, False

	if tag == 3:
		changed = False
		if len(value) > 1 and isinstance(value[1], str):
			if value[1] not in blocks:
				if len(value) > 2:
					shadow = value[2]
					if isinstance(shadow, str) and shadow not in blocks:
						return None, True
					# preserve shadow value
					return [1, shadow], True
				return None, True

		if len(value) > 2:
			shadow = value[2]
			if isinstance(shadow, str) and shadow not in blocks:
				return [value[0], value[1]], True
			if isinstance(shadow, (list, dict)):
				fixed, shadow_changed = _repair_dangling_block_ref(shadow, blocks)
				if fixed is None:
					return [value[0], value[1]], True
				if shadow_changed:
					value[2] = fixed
					changed = True
		return value, changed

	changed = False
	for i in range(1, len(value)):
		v = value[i]
		if isinstance(v, (list, dict)):
			fixed, sub_changed = _repair_dangling_block_ref(v, blocks)
			if fixed is None:
				continue
			if sub_changed:
				value[i] = fixed
				changed = True
	return value, changed


def _is_known_orphan_argument_reporter(block, blocks):
	if not isinstance(block, dict):
		return False
	return (
		block.get("opcode", "").startswith("argument_reporter_")
		and block.get("shadow") is True
		and isinstance(block.get("parent"), (type(None), str))
		and (block.get("parent") is None or block.get("parent") not in blocks)
	)


def _iter_input_block_refs(value):
	if not isinstance(value, list) or not value:
		return
	tag = value[0]
	if tag in (1, 2):
		if len(value) > 1:
			ref = value[1]
			if isinstance(ref, str):
				yield ref
			elif isinstance(ref, (list, dict)):
				yield from _iter_input_block_refs(ref)
		return
	if tag == 3:
		for ref in value[1:3]:
			if isinstance(ref, str):
				yield ref
			elif isinstance(ref, (list, dict)):
				yield from _iter_input_block_refs(ref)
		return
	for item in value[1:]:
		if isinstance(item, (list, dict)):
			yield from _iter_input_block_refs(item)


def _repair_dangling_block_refs(target):
	blocks = target.get("blocks", {})
	fixed = 0

	# remove unresolved refs
	for block in blocks.values():
		if not isinstance(block, dict):
			continue
		for key in ("next", "parent"):
			ref = block.get(key)
			if isinstance(ref, str) and ref not in blocks:
				block[key] = None
				fixed += 1
		inputs = block.get("inputs")
		if not isinstance(inputs, dict):
			continue
		for name in list(inputs):
			fixed_value, changed = _repair_dangling_block_ref(inputs[name], blocks)
			if not changed:
				continue
			if fixed_value is None:
				del inputs[name]
			else:
				inputs[name] = fixed_value
			fixed += 1

	# when an existing next/input edge unambiguously identifies the owner,
	# repair the child's reverse parent link too
	input_owners = {}
	for owner_id, block in blocks.items():
		if not isinstance(block, dict):
			continue
		for value in (block.get("inputs") or {}).values():
			for child_id in _iter_input_block_refs(value):
				if child_id in blocks:
					input_owners.setdefault(child_id, set()).add(owner_id)

	for owner_id, block in blocks.items():
		if not isinstance(block, dict):
			continue
		nxt = block.get("next")
		if isinstance(nxt, str) and nxt in blocks:
			child = blocks[nxt]
			if (
				isinstance(child, dict)
				and child.get("parent") is None
				and child.get("topLevel") is not True
			):
				child["parent"] = owner_id
				fixed += 1

	for child_id, owners in input_owners.items():
		if len(owners) != 1:
			continue
		child = blocks[child_id]
		if (
			isinstance(child, dict)
			and child.get("parent") is None
			and child.get("topLevel") is not True
		):
			child["parent"] = next(iter(owners))
			fixed += 1

	comments = target.get("comments") or {}
	for cid in list(comments):
		c = comments[cid]
		if (
			isinstance(c, dict)
			and c.get("blockId") is not None
			and c["blockId"] not in blocks
		):
			del comments[cid]
	return fixed


def remove_unreachable_blocks(project, stats, stat_key="unreachable_blocks_removed"):
	removed = 0
	dangling_fixed = 0
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		roots = {
			bid
			for bid, b in blocks.items()
			if isinstance(b, list) or (isinstance(b, dict) and b.get("topLevel"))
		}
		graph = _ScratchGraphIndex(target)
		edges = graph.edges
		reachable = set()
		stack = list(roots)

		while stack:
			bid = stack.pop()
			if bid in reachable or bid not in blocks:
				continue
			reachable.add(bid)
			stack.extend(edges.get(bid, ()))

		for bid in list(blocks):
			if bid not in reachable:
				del blocks[bid]
				removed += 1

	stats[stat_key] += removed
	return removed


def _create_scratch_id():
	return str(uuid.uuid4()).replace("-", "")[:20]


def _procedure_key(block):
	mut = block.get("mutation") if isinstance(block, dict) else None
	if not isinstance(mut, dict):
		return None
	return mut.get("proccode")


SEQUENCE_MAX_PARAMETERS = 8


def _sequence_reporter_block(block):
	if not isinstance(block, dict):
		return False
	op = block.get("opcode")
	if not isinstance(op, str) or not op or block.get("next") is not None:
		return False
	if op.startswith(("event_", "procedures_", "control_")):
		return False
	if op.startswith(("operator_", "argument_reporter_")):
		return True
	if op.startswith("sensing_"):
		return op not in {"sensing_askandwait"}
	if op in {
		"data_variable",
		"data_itemoflist",
		"data_lengthoflist",
		"data_listcontainsitem",
		"looks_costumenumbername",
		"looks_backdropnumbername",
		"looks_size",
		"sound_volume",
		"motion_xposition",
		"motion_yposition",
		"motion_direction",
	}:
		return True
	return False


def _sequence_input_refs(value, blocks):
	for ref in _iter_input_block_refs(value):
		if ref in blocks:
			yield ref


def _sequence_inline_parameterizable(item):
	if item is None:
		return True
	if not isinstance(item, list):
		return False
	if len(item) == 2 and item[0] in _NUMERIC_TAGS + (_TEXT_TAG,):
		return True
	if len(item) >= 3 and item[0] in (11, 12, 13):
		return all(isinstance(x, (str, int, float, bool, type(None))) for x in item[1:])
	return False


def _sequence_input_parameterizable(value, blocks):
	if not isinstance(value, list) or not value:
		return False
	refs = list(_sequence_input_refs(value, blocks))
	if refs:
		return all(_sequence_reporter_block(blocks.get(ref)) for ref in refs)
	if value[0] in (1, 2):
		return len(value) >= 2 and _sequence_inline_parameterizable(value[1])
	if value[0] == 3:
		if len(value) < 2:
			return False
		return all(
			item is None
			or _sequence_inline_parameterizable(item)
		or (isinstance(item, str) and item in blocks)
			for item in value[1:]
		)
	return False


def _sequence_has_unsafe_nested_inputs(block, blocks):
	for value in (block.get("inputs") or {}).values():
		for ref in _sequence_input_refs(value, blocks):
			if not _sequence_reporter_block(blocks.get(ref)):
				return True
	return False


def _sequence_block_signature(block, blocks):
	if not isinstance(block, dict):
		return None
	op = block.get("opcode")
	if not isinstance(op, str) or not op:
		return None
	if op.startswith(("event_", "procedures_")):
		return None
	if op in {"control_stop", "control_delete_this_clone"}:
		return None
	if _sequence_has_unsafe_nested_inputs(block, blocks):
		return None
	fields = dumps_compact(block.get("fields") or {})
	mutation = dumps_compact(block.get("mutation")) if block.get("mutation") is not None else None
	inputs_sig = []
	for name in sorted((block.get("inputs") or {}).keys()):
		value = block["inputs"][name]
		if not _is_boolean_slot(block, name) and _sequence_input_parameterizable(value, blocks):
			inputs_sig.append((name, "PARAM"))
		else:
			inputs_sig.append((name, "FIXED", dumps_compact(value)))
	return (op, fields, mutation, tuple(inputs_sig))


def _sequence_signature(sequence, blocks):
	parts = []
	for bid in sequence:
		sig = _sequence_block_signature(blocks.get(bid), blocks)
		if sig is None:
			return None
		parts.append(sig)
	return tuple(parts)


def _collect_linear_sequence_runs(blocks):
	incoming_next = set()
	for block in blocks.values():
		if isinstance(block, dict):
			nxt = block.get("next")
			if isinstance(nxt, str) and nxt in blocks:
				incoming_next.add(nxt)

	runs = []
	seen_starts = set()
	for bid, block in blocks.items():
		if bid in incoming_next or not isinstance(block, dict):
			continue
		op = block.get("opcode", "")
		if op.startswith("argument_reporter_") or op == "procedures_prototype":
			continue
		start = bid
		if op.startswith("event_") or op == "procedures_definition":
			start = block.get("next")
		if not isinstance(start, str) or start not in blocks or start in seen_starts:
			continue

		sequence = []
		seen = set()
		current = start
		while isinstance(current, str) and current in blocks and current not in seen:
			b = blocks.get(current)
			if not isinstance(b, dict):
				break
			seen.add(current)
			sequence.append(current)
			current = b.get("next")
		if sequence:
			runs.append(sequence)
			seen_starts.add(start)
	return runs


def _sequence_compare(base, other, blocks):
	if len(base) != len(other):
		return None
	diff_positions = []
	for a_id, b_id in zip(base, other):
		a = blocks.get(a_id)
		b = blocks.get(b_id)
		if not isinstance(a, dict) or not isinstance(b, dict):
			return None
		if a.get("opcode") != b.get("opcode"):
			return None
		if (a.get("fields") or {}) != (b.get("fields") or {}):
			return None
		if a.get("mutation") != b.get("mutation"):
			return None
		a_inputs = a.get("inputs") or {}
		b_inputs = b.get("inputs") or {}
		if set(a_inputs) != set(b_inputs):
			return None
		for name in a_inputs:
			a_value, b_value = a_inputs[name], b_inputs[name]
			if a_value == b_value:
				continue
			if _is_boolean_slot(a, name) or not (
				_sequence_input_parameterizable(a_value, blocks)
				and _sequence_input_parameterizable(b_value, blocks)
			):
				return None
			diff_positions.append((base.index(a_id), name))
	return diff_positions


def _sequence_collect_closure(sequence, blocks, graph=None):
	if graph is None:
		graph = _ScratchGraphIndex({"blocks": blocks})
	closure = set(sequence)
	stack = list(sequence)
	while stack:
		bid = stack.pop()
		for ref in graph.edges.get(bid, ()):
			if ref not in closure:
				closure.add(ref)
				stack.append(ref)
	return closure


def _sequence_has_external_owner(closure, sequence, blocks, comments, graph=None):
	if graph is None:
		graph = _ScratchGraphIndex({"blocks": blocks})
	root = sequence[0]
	root_parent = blocks.get(root, {}).get("parent") if isinstance(blocks.get(root), dict) else None
	for child_id in closure:
		for owner_id in graph.parents.get(child_id, ()):
			if owner_id in closure:
				continue
			if child_id == root and owner_id == root_parent:
				continue
			return True
	for comment in comments.values():
		if isinstance(comment, dict) and comment.get("blockId") in closure:
			return True
	return False


def _sequence_replace_direct_ref(value, old_id, new_id):
	value = copy.deepcopy(value)

	def replace(node):
		if not isinstance(node, list) or not node:
			return False
		tag = node[0]
		if tag in (1, 2):
			if len(node) > 1 and node[1] == old_id:
				node[1] = new_id
				return True
			return False
		if tag == 3:
			# [3, primary, shadow] may reference either block directly
			for i in (1, 2):
				if len(node) > i and node[i] == old_id:
					node[i] = new_id
					return True
			for i in (1, 2):
				if len(node) > i and isinstance(node[i], (list, dict)) and replace(node[i]):
					return True
			return False
		for i in range(1, len(node)):
			child = node[i]
			if isinstance(child, (list, dict)) and replace(child):
				return True
		return False

	if not replace(value):
		return None
	return value


def _sequence_new_id(blocks, generated):
	bid = _create_scratch_id()
	while bid in blocks or bid in generated:
		bid = _create_scratch_id()
	return bid


def _sequence_clone_input(value, blocks, generated, parent_id):
	memo = {}

	def clone_block(old_id, parent):
		if old_id in memo:
			new_id = memo[old_id]
			generated[new_id]["parent"] = parent
			return new_id
		old = blocks.get(old_id)
		if not isinstance(old, dict):
			return old_id
		new_id = _sequence_new_id(blocks, generated)
		memo[old_id] = new_id
		new = copy.deepcopy(old)
		new["parent"] = parent
		new.pop("topLevel", None)
		new.pop("x", None)
		new.pop("y", None)
		if new.get("next") is not None:
			return None
		new["next"] = None
		generated[new_id] = new
		for name, child in list((new.get("inputs") or {}).items()):
			fixed = clone_value(child, new_id)
			if fixed is None:
				return None
			new["inputs"][name] = fixed
		return new_id

	def clone_value(v, parent):
		out = copy.deepcopy(v)
		if not isinstance(out, list) or not out:
			return out
		tag = out[0]
		if tag in (1, 2) and len(out) > 1 and isinstance(out[1], str) and out[1] in blocks:
			new_ref = clone_block(out[1], parent)
			if new_ref is None:
				return None
			out[1] = new_ref
		elif tag == 3:
			for i in (1, 2):
				if len(out) <= i:
					continue
				if isinstance(out[i], str) and out[i] in blocks:
					new_ref = clone_block(out[i], parent)
					if new_ref is None:
						return None
					out[i] = new_ref
				elif isinstance(out[i], (list, dict)):
					out[i] = clone_value(out[i], parent)
					if out[i] is None:
						return None
		return out

	return clone_value(value, parent_id)


def _sequence_default_value(value, blocks):
	if not isinstance(value, list) or not value:
		return ""
	child = value[1] if len(value) > 1 else None
	if isinstance(child, list):
		if len(child) == 2 and child[0] in _NUMERIC_TAGS + (_TEXT_TAG,):
			return str(child[1]) if child[1] is not None else ""
		if len(child) >= 3 and child[0] in (11, 12, 13):
			return str(child[1]) if child[1] is not None else ""
	if isinstance(child, str) and child in blocks:
		return ""
	return ""


def _sequence_make_argument_reporter(generated, parent_id, arg_id, name, shadow=False, blocks=None):
	bid = _create_scratch_id()
	while bid in generated or (blocks is not None and bid in blocks):
		bid = _create_scratch_id()
	block = {
		"opcode": "argument_reporter_string_number",
		"next": None,
		"parent": parent_id,
		"inputs": {},
		"fields": {"VALUE": [name, None]},
	}
	if shadow:
		block["shadow"] = True
	generated[bid] = block
	return bid


def _sequence_make_call_input(value, blocks, generated, parent_id):
	return _sequence_clone_input(value, blocks, generated, parent_id)


def group_similar_sequences(project, stats, threshold=3, opts=None):
	threshold = max(1, int(threshold))
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		comments = target.get("comments") or {}
		graph = _ScratchGraphIndex(target)
		runs = _collect_linear_sequence_runs(blocks)
		buckets = {}
		for sequence in runs:
			if len(sequence) < threshold:
				continue
			if any(
				not isinstance(blocks.get(bid), dict)
				or "comment" in blocks[bid]
				for bid in sequence
			):
				continue
			sig = _sequence_signature(sequence, blocks)
			if sig is not None:
				buckets.setdefault(sig, []).append(sequence)

		if not buckets:
			continue

		proc_counter = 0
		existing_procs = {
			_procedure_key(block)
			for block in blocks.values()
			if isinstance(block, dict)
			and block.get("opcode") in ("procedures_prototype", "procedures_call")
			and isinstance(_procedure_key(block), str)
		}

		for sequences in sorted(
			buckets.values(), key=lambda group: (-len(group), -len(group[0]))
		):
			sequences = [
				seq for seq in sequences
				if all(bid in blocks for bid in seq) and len(seq) >= threshold
			]
			if len(sequences) < 2:
				continue
			base = sequences[0]
			if any(
				_sequence_compare(base, seq, blocks) is None
				for seq in sequences[1:]
			):
				continue

			closures = {
				tuple(seq): _sequence_collect_closure(seq, blocks, graph=graph) for seq in sequences
			}
			if any(
				_sequence_has_external_owner(closure, seq, blocks, comments, graph=graph)
				for seq, closure in ((seq, closures[tuple(seq)]) for seq in sequences)
			):
				continue
			all_closure_ids = list(closures.values())
			if any(
				all_closure_ids[i] & all_closure_ids[j]
				for i in range(len(all_closure_ids))
				for j in range(i + 1, len(all_closure_ids))
			):
				continue

			param_slots = []
			for index, block_id in enumerate(base):
				base_block = blocks[block_id]
				for input_name, base_value in (base_block.get("inputs") or {}).items():
					values = [
						(blocks[seq[index]].get("inputs") or {}).get(input_name)
						for seq in sequences
					]
					if len({dumps_compact(v) for v in values}) <= 1:
						continue
					if _is_boolean_slot(base_block, input_name) or not all(
						_sequence_input_parameterizable(v, blocks) for v in values
					):
						param_slots = None
						break
					param_slots.append((index, input_name, values))
				if param_slots is None:
					break
			if param_slots is None:
				continue

			param_vectors = {}
			param_for_slot = {}
			for index, input_name, values in param_slots:
				vector = tuple(dumps_compact(v) for v in values)
				if vector not in param_vectors:
					param_vectors[vector] = len(param_vectors)
				param_for_slot[(index, input_name)] = param_vectors[vector]
			parameter_count = len(param_vectors)
			if parameter_count > SEQUENCE_MAX_PARAMETERS:
				continue

			proc_counter_start = proc_counter
			while True:
				name = f"group{proc_counter}"
				proc_counter += 1
				candidate = name + (
					" " + " ".join("%s" for _ in range(parameter_count))
					if parameter_count
					else ""
				)
				if candidate not in existing_procs:
					proc_name = candidate
					existing_procs.add(candidate)
					break
			if proc_counter_start == proc_counter and proc_name in existing_procs:
				continue

			generated = {}
			removed = set()
			link_changes = []
			input_changes = []
			owner_previews = {}

			argument_ids = []
			argument_names = []
			argument_defaults = []
			for pidx in range(parameter_count):
				arg_id = _create_scratch_id()
				while arg_id in generated or arg_id in blocks or arg_id in argument_ids:
					arg_id = _create_scratch_id()
				argument_ids.append(arg_id)
				arg_name = f"p{pidx}"
				argument_names.append(arg_name)
				first_values = next(
					values
					for index, input_name, values in param_slots
					if param_for_slot[(index, input_name)] == pidx
				)
				argument_defaults.append(_sequence_default_value(first_values[0], blocks))

			proto_id = _sequence_new_id(blocks, generated)
			def_id = _sequence_new_id(blocks, generated)
			generated[proto_id] = {
				"opcode": "procedures_prototype",
				"next": None,
				"parent": def_id,
				"inputs": {},
				"fields": {},
				"shadow": True,
				"mutation": {
					"tagName": "mutation",
					"children": [],
					"proccode": proc_name,
					"argumentids": json.dumps(argument_ids, separators=(",", ":")),
					"argumentnames": json.dumps(argument_names, separators=(",", ":")),
					"argumentdefaults": json.dumps(argument_defaults, separators=(",", ":")),
					"warp": False,
				},
			}
			for arg_id, arg_name in zip(argument_ids, argument_names):
				reporter_id = _sequence_make_argument_reporter(
					generated, proto_id, arg_id, arg_name, shadow=True, blocks=blocks
				)
				generated[proto_id]["inputs"][arg_id] = [1, reporter_id]

			body_new_ids = []
			body_map = {old_id: _sequence_new_id(blocks, generated) for old_id in base}
			for idx, old_id in enumerate(base):
				old_original = blocks[old_id]
				old = copy.deepcopy(old_original)
				new_id = body_map[old_id]
				old["parent"] = def_id if idx == 0 else body_map[base[idx - 1]]
				old["next"] = body_map.get(old_original.get("next"))
				old.pop("topLevel", None)
				old.pop("x", None)
				old.pop("y", None)
				new_inputs = {}
				for input_name, original_value in (old_original.get("inputs") or {}).items():
					param_index = param_for_slot.get((idx, input_name))
					if param_index is not None:
						arg_reporter_id = _sequence_make_argument_reporter(
							generated,
							new_id,
							argument_ids[param_index],
							argument_names[param_index],
							shadow=False,
							blocks=blocks,
						)
						if (
							isinstance(original_value, list)
							and original_value
							and original_value[0] == 3
							and len(original_value) > 2
						):
							shadow = copy.deepcopy(original_value[2])
							if isinstance(shadow, str) and shadow in blocks:
								shadow_value = _sequence_clone_input(
									[1, shadow], blocks, generated, new_id
								)
								if isinstance(shadow_value, list) and len(shadow_value) == 2:
									shadow = shadow_value[1]
								else:
									continue
							new_inputs[input_name] = [3, arg_reporter_id, shadow]
						else:
							new_inputs[input_name] = [2, arg_reporter_id]
					else:
						cloned = _sequence_clone_input(
							original_value, blocks, generated, new_id
						)
						if cloned is None:
							new_inputs = None
							break
						new_inputs[input_name] = cloned
				if new_inputs is None:
					break
				old["inputs"] = new_inputs
				generated[new_id] = old
				body_new_ids.append(new_id)
			if len(body_new_ids) != len(base):
				continue

			generated[def_id] = {
				"opcode": "procedures_definition",
				"next": body_new_ids[0],
				"parent": None,
				"inputs": {"custom_block": [1, proto_id]},
				"fields": {},
				"topLevel": True,
				"x": 5,
				"y": 5,
			}

			construction_ok = True
			for seq in sequences:
				root = seq[0]
				root_block = blocks[root]
				call_id = _sequence_new_id(blocks, generated)
				call_inputs = {}
				for idx, old_id in enumerate(seq):
					for input_name, value in (blocks[old_id].get("inputs") or {}).items():
						param_index = param_for_slot.get((idx, input_name))
						if param_index is None:
							continue
						cloned_arg = _sequence_make_call_input(
							value, blocks, generated, call_id
						)
						if cloned_arg is None:
							construction_ok = False
							break
						call_inputs[argument_ids[param_index]] = cloned_arg
					if not construction_ok:
						break
				if not construction_ok:
					break

				call = {
					"opcode": "procedures_call",
					"next": None,
					"parent": root_block.get("parent"),
					"inputs": call_inputs,
					"fields": {},
					"mutation": {
						"tagName": "mutation",
						"children": [],
						"proccode": proc_name,
						"argumentids": json.dumps(argument_ids, separators=(",", ":")),
						"warp": False,
					},
				}
				if root_block.get("topLevel") is True or root_block.get("parent") is None:
					call["topLevel"] = True
					call["x"] = root_block.get("x", 5)
					call["y"] = root_block.get("y", 5)
				generated[call_id] = call

				parent_id = root_block.get("parent")
				if isinstance(parent_id, str) and parent_id in blocks:
					parent_block = owner_previews.get(parent_id, blocks[parent_id])
					if not isinstance(parent_block, dict):
						construction_ok = False
						break
					if parent_block.get("next") == root:
						preview = copy.deepcopy(parent_block)
						preview["next"] = call_id
						owner_previews[parent_id] = preview
						link_changes.append(
							(parent_id, preview, root, call_id)
						)
					else:
						found = False
						for input_name, input_value in (parent_block.get("inputs") or {}).items():
							replaced = _sequence_replace_direct_ref(
								input_value, root, call_id
							)
							if replaced is None:
								continue
							preview = copy.deepcopy(parent_block)
							preview["inputs"][input_name] = replaced
							owner_previews[parent_id] = preview
							input_changes.append(
								(parent_id, input_name, preview, replaced, root, call_id)
							)
							found = True
							break
						if not found:
							construction_ok = False
							break
			if not construction_ok:
				continue

			for seq in sequences:
				removed.update(closures[tuple(seq)])

			old_target_bytes = _json_len(target)
			candidate_blocks = dict(blocks)
			for owner_id, preview in owner_previews.items():
				candidate_blocks[owner_id] = preview
			for new_id, block in generated.items():
				candidate_blocks[new_id] = block
			for old_id in removed:
				candidate_blocks.pop(old_id, None)
			candidate_target = copy.copy(target)
			candidate_target["blocks"] = candidate_blocks
			new_target_bytes = _json_len(candidate_target)
			byte_delta = old_target_bytes - new_target_bytes

			if byte_delta <= 0:
				if opts is not None:
					opts.grouped_sequence_rejected_size += 1
				stats["sequence_groups_rejected_size"] += 1
				continue

			for owner_id, preview in owner_previews.items():
				blocks[owner_id] = preview
			for new_id, block in generated.items():
				blocks[new_id] = block
			for old_id in removed:
				blocks.pop(old_id, None)

			if opts is not None:
				while len(opts.grouped_sequence_removed_blocks) <= ti:
					opts.grouped_sequence_removed_blocks.append(set())
				while len(opts.grouped_sequence_new_blocks) <= ti:
					opts.grouped_sequence_new_blocks.append(set())
				opts.grouped_sequence_removed_blocks[ti].update(removed)
				opts.grouped_sequence_new_blocks[ti].update(generated)
				for owner_id, _preview, _old, new_value in link_changes:
					opts.grouped_sequence_link_edits[(ti, owner_id, "next")] = new_value
				for owner_id, input_name, _preview, new_value, _old, _call in input_changes:
					opts.grouped_sequence_input_edits[(ti, owner_id, input_name)] = copy.deepcopy(
						new_value
					)
			stats["sequence_groups_created"] += 1
			stats["sequences_grouped"] += len(sequences)
			stats["sequence_procedures_created"] += 1
			stats["sequence_parameters"] += parameter_count
			stats["sequence_blocks_removed"] += len(removed)
			stats["sequence_blocks_added"] += len(generated)
			stats["sequence_bytes_saved"] += byte_delta
			graph.rebuild()



_PROCEDURE_EFFECTFUL_ARGUMENT_OPCODES = {
	"operator_random",
	"sensing_askandwait",
	"procedures_call",
}


def _procedure_argument_kinds(proccode, count):
	if not isinstance(proccode, str):
		return None
	kinds = [token for token in proccode.split(" ") if token in ("%s", "%b")]
	return kinds if len(kinds) == count else None


def _remove_procedure_placeholder(proccode, argument_index):
	if not isinstance(proccode, str):
		return None
	matches = list(re.finditer(r"(?<!\S)(%s|%b)(?!\S)", proccode))
	if argument_index < 0 or argument_index >= len(matches):
		return None
	start, end = matches[argument_index].span()
	if start > 0 and proccode[start - 1] == " ":
		start -= 1
	elif end < len(proccode) and proccode[end] == " ":
		end += 1
	return proccode[:start] + proccode[end:]


def _procedure_definition_closure(target, definition_id, graph=None):
	blocks = target.get("blocks") or {}
	if graph is None:
		graph = _ScratchGraphIndex(target)
	if definition_id not in blocks or not isinstance(blocks.get(definition_id), dict):
		return set()
	closure = set()
	stack = [definition_id]
	while stack:
		bid = stack.pop()
		if bid in closure or bid not in blocks:
			continue
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		closure.add(bid)
		stack.extend(graph.edges.get(bid, ()))
	# A definition closure must be wholly owned by its definition.
	for bid in closure:
		for parent in graph.parents.get(bid, set()):
			if bid == definition_id:
				if parent not in closure:
					continue
			elif parent not in closure:
				return None
	# The definition itself must be a top-level root or parentless.
	def_block = blocks[definition_id]
	if def_block.get("parent") is not None:
		return None
	return closure


def _procedure_infos(target):
	blocks = target.get("blocks") or {}
	graph = _ScratchGraphIndex(target)
	by_proc = {}
	for definition_id, definition in blocks.items():
		if not isinstance(definition, dict) or definition.get("opcode") != "procedures_definition":
			continue
		custom = (definition.get("inputs") or {}).get("custom_block")
		proto_id = (
			custom[1] if isinstance(custom, list) and len(custom) > 1
			and isinstance(custom[1], str) else None
		)
		proto = blocks.get(proto_id)
		if not isinstance(proto, dict) or proto.get("opcode") != "procedures_prototype":
			continue
		mut = proto.get("mutation")
		proc = _procedure_key(proto)
		argids = _parse_argumentids(mut)
		if not isinstance(mut, dict) or proc is None or argids is None:
			continue
		closure = _procedure_definition_closure(target, definition_id, graph)
		if closure is None:
			continue
		by_proc.setdefault(proc, []).append({
			"definition_id": definition_id,
			"prototype_id": proto_id,
			"prototype": proto,
			"closure": closure,
			"argument_ids": list(argids),
			"mutation": mut,
		})
	return by_proc, graph


def _procedure_body_argument_uses(info, blocks):
	arg_names = []
	raw_names = info["mutation"].get("argumentnames")
	if isinstance(raw_names, str):
		try:
			arg_names = json.loads(raw_names)
		except (TypeError, ValueError):
			arg_names = []
	if not isinstance(arg_names, list) or len(arg_names) != len(info["argument_ids"]):
		return None
	if len(set(x for x in arg_names if isinstance(x, str))) != len(arg_names):
		return None
	name_to_index = {
		name: i for i, name in enumerate(arg_names) if isinstance(name, str)
	}
	used = set()
	reporter_ids = {}
	for bid in info["closure"]:
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		op = block.get("opcode", "")
		if not op.startswith("argument_reporter_"):
			continue
		field = (block.get("fields") or {}).get("VALUE")
		name = field[0] if isinstance(field, list) and field and isinstance(field[0], str) else None
		if name not in name_to_index:
			continue
		idx = name_to_index[name]
		used.add(idx)
		reporter_ids.setdefault(idx, set()).add(bid)
	return used, reporter_ids, arg_names


def _procedure_input_is_discardable(value, blocks, graph):
	"""Return True when evaluating an argument cannot have a protected effect."""
	roots = _input_block_ids(value, blocks)
	if not roots:
		return True
	seen = set()
	stack = list(roots)
	while stack:
		bid = stack.pop()
		if bid in seen or bid not in blocks:
			continue
		seen.add(bid)
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		op = block.get("opcode")
		if op in _PROCEDURE_EFFECTFUL_ARGUMENT_OPCODES:
			return False
		# Unknown command-ish blocks are conservatively treated as effectful.
		if isinstance(op, str):
			if op.startswith(("event_", "control_")):
				return False
			if op.startswith(("looks_", "sound_", "motion_", "data_")) and op not in {
				"data_variable",
				"data_itemoflist",
				"data_itemnumoflist",
				"data_listcontents",
				"data_lengthoflist",
				"data_listcontainsitem",
				"motion_xposition",
				"motion_yposition",
				"motion_direction",
				"looks_costumenumbername",
				"looks_backdropnumbername",
				"looks_size",
				"sound_volume",
			}:
				return False
		stack.extend(graph.edges.get(bid, ()))
	return True


def _replace_argument_reporter_refs(value, reporter_ids, replacement):
	"""Replace argument-reporter references with a complete Scratch input literal."""
	if not isinstance(value, list) or not value:
		return value, False

	tag = value[0]
	if tag in (1, 2, 3):
		primary = value[1] if len(value) > 1 else None
		if isinstance(primary, str) and primary in reporter_ids:
			# A [2, blockId] or [3, blockId, shadowId] input becomes a
			# complete literal [1, [tag, value]]. The obsolete shadow is no
			# longer reachable and is deliberately discarded.
			return copy.deepcopy(replacement), True
		if isinstance(primary, list):
			value[1], changed = _replace_argument_reporter_refs(
				primary, reporter_ids, replacement
			)
			if changed:
				return value, True
		if tag == 3 and len(value) > 2:
			shadow = value[2]
			if isinstance(shadow, str) and shadow in reporter_ids:
				value[2] = copy.deepcopy(replacement)
				return value, True
			if isinstance(shadow, list):
				value[2], changed = _replace_argument_reporter_refs(
					shadow, reporter_ids, replacement
				)
				if changed:
					return value, True
		return value, False

	for index in range(1, len(value)):
		if isinstance(value[index], list):
			value[index], changed = _replace_argument_reporter_refs(
				value[index], reporter_ids, replacement
			)
			if changed:
				return value, True
	return value, False


def _procedure_input_literal(value):
	constant = _folded_literal_input(value)
	if constant is None:
		return None
	return constant


def _procedure_update_mutation(mut, arg_ids, arg_names, removed_indices, new_proccode):
	out = copy.deepcopy(mut)
	if "argumentids" in out:
		out["argumentids"] = json.dumps(
			arg_ids, separators=(",", ":"), ensure_ascii=False
		)
	if "argumentnames" in out and isinstance(arg_names, list):
		out["argumentnames"] = json.dumps(
			arg_names, separators=(",", ":"), ensure_ascii=False
		)
	raw_defaults = out.get("argumentdefaults")
	if isinstance(raw_defaults, str):
		try:
			defaults = json.loads(raw_defaults)
		except (TypeError, ValueError):
			defaults = None
		if isinstance(defaults, list) and len(defaults) >= len(arg_ids) + len(removed_indices):
			kept_defaults = [
				v for i, v in enumerate(defaults) if i not in removed_indices
			]
			out["argumentdefaults"] = json.dumps(
				kept_defaults, separators=(",", ":"), ensure_ascii=False
			)
	out["proccode"] = new_proccode
	return out


def _procedure_warp_enabled(value):
    """Return whether a procedure is explicitly configured to run without screen refresh."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _procedure_recursive_set(by_proc):
    """Find procedures that participate in recursive call cycles."""
    call_graph = {proc: set() for proc in by_proc}
    for proc, infos in by_proc.items():
        for info in infos:
            blocks = info.get("blocks", {})
            for bid in info["closure"]:
                block = blocks.get(bid)
                if not isinstance(block, dict) or block.get("opcode") != "procedures_call":
                    continue
                called = _procedure_key(block)
                if called in by_proc:
                    call_graph[proc].add(called)

    recursive = set()
    for start in call_graph:
        stack = list(call_graph[start])
        seen = set()
        while stack:
            current = stack.pop()
            if current == start:
                recursive.add(start)
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(call_graph.get(current, ()))
    return recursive


def _inline_argument_reporter_refs(value, reporter_ids, replacement):
    """Replace references to formal-argument reporters with a safe literal."""
    if not isinstance(value, list) or not value:
        return value, False
    tag = value[0]
    if tag in (1, 2, 3):
        positions = (1, 2) if tag == 3 else (1,)
        for index in positions:
            if index >= len(value):
                continue
            item = value[index]
            if isinstance(item, str) and item in reporter_ids:
                return copy.deepcopy(replacement), True
            if isinstance(item, (list, dict)):
                new_item, changed = _inline_argument_reporter_refs(item, reporter_ids, replacement)
                if changed:
                    value[index] = new_item
                    if index == 1 and _is_bare_literal_input(new_item):
                        return new_item, True
                    return value, True
        return value, False
    for index in range(1, len(value)):
        item = value[index]
        if isinstance(item, (list, dict)):
            new_item, changed = _inline_argument_reporter_refs(item, reporter_ids, replacement)
            if changed:
                value[index] = new_item
                return value, True
    return value, False


def inline_single_use_procedures(project, stats, opts):
    """Inline single-use custom procedures only when both sides are warp/no-refresh."""
    targets = project.get("targets", [])
    opts.inlined_procedure_removed_blocks = [set() for _ in targets]
    opts.inlined_procedure_link_edits = {}
    opts.inlined_procedure_input_edits = {}

    max_passes = max(1, int(getattr(opts, "procedure_inline_passes", 8)))
    total = 0
    total_removed = 0
    total_reporters = 0
    completed_passes = 0

    for _pass in range(max_passes):
        changed_this_pass = False

        for ti, target in enumerate(targets):
            blocks = target.get("blocks") or {}
            if not blocks:
                continue
            by_proc, graph = _procedure_infos(target)
            if not by_proc:
                continue
            for infos in by_proc.values():
                for info in infos:
                    info["blocks"] = blocks

            recursive = _procedure_recursive_set(by_proc)
            warp_owners = {}
            for proc, infos in by_proc.items():
                if len(infos) != 1:
                    continue
                info = infos[0]
                if not _procedure_warp_enabled(info["mutation"].get("warp")):
                    continue
                for owned_id in info["closure"]:
                    warp_owners.setdefault(owned_id, set()).add(proc)

            calls_by_proc = {}
            for call_id, block in blocks.items():
                if not isinstance(block, dict) or block.get("opcode") != "procedures_call":
                    continue
                proc = _procedure_key(block)
                if proc is not None:
                    calls_by_proc.setdefault(proc, []).append((call_id, block))

            for proc, infos in list(by_proc.items()):
                if len(infos) != 1 or len(calls_by_proc.get(proc, ())) != 1:
                    continue
                if proc in recursive:
                    continue
                info = infos[0]
                if not _procedure_warp_enabled(info["mutation"].get("warp")):
                    continue

                call_id, call = calls_by_proc[proc][0]
                if len(warp_owners.get(call_id, set())) != 1:
                    continue

                definition_id = info["definition_id"]
                prototype_id = info["prototype_id"]
                closure = set(info["closure"])
                definition = blocks.get(definition_id)
                prototype = blocks.get(prototype_id)
                if not isinstance(definition, dict) or not isinstance(prototype, dict):
                    continue

                body_root = definition.get("next")
                if not isinstance(body_root, str) or body_root not in blocks or body_root not in closure:
                    continue

                body_sequence = []
                seen_body = set()
                current = body_root
                valid_body_chain = False
                while isinstance(current, str):
                    if current in seen_body or current not in closure:
                        break
                    block = blocks.get(current)
                    if not isinstance(block, dict):
                        break
                    body_sequence.append(current)
                    seen_body.add(current)
                    nxt = block.get("next")
                    if nxt is None:
                        valid_body_chain = True
                        break
                    if not isinstance(nxt, str):
                        break
                    current = nxt
                if not valid_body_chain:
                    continue
                tail_id = body_sequence[-1]

                parent_id = call.get("parent")
                if not isinstance(parent_id, str) or parent_id not in blocks:
                    continue
                if graph.parents.get(call_id, set()) != {parent_id}:
                    continue
                call_next = call.get("next")
                if call_next is not None and (not isinstance(call_next, str) or call_next not in blocks):
                    continue

                parent_block = blocks.get(parent_id)
                if not isinstance(parent_block, dict):
                    continue
                parent_mode = None
                parent_input_name = None
                parent_input_replacement = None
                if parent_block.get("next") == call_id:
                    parent_mode = "next"
                else:
                    for input_name, raw in (parent_block.get("inputs") or {}).items():
                        replaced = _sequence_replace_direct_ref(raw, call_id, body_root)
                        if replaced is not None:
                            parent_mode = "input"
                            parent_input_name = input_name
                            parent_input_replacement = replaced
                            break
                if parent_mode is None:
                    continue

                # Argument reporter shadows attached to the prototype are part of
                # the procedure scaffold, not actual body uses. Exclude them from
                # the usage analysis while still removing them with the prototype.
                prototype_owned = {prototype_id}
                prototype_stack = [prototype_id]
                while prototype_stack:
                    owned_id = prototype_stack.pop()
                    for ref in graph.edges.get(owned_id, ()):
                        if ref in closure and ref not in prototype_owned:
                            prototype_owned.add(ref)
                            prototype_stack.append(ref)
                body_info = dict(info)
                body_info["closure"] = closure - prototype_owned - {definition_id}
                used, reporter_map, _arg_names = _procedure_body_argument_uses(body_info, blocks)
                if used is None:
                    continue
                formal_ids = list(info["argument_ids"])
                call_ids = _parse_argumentids(call.get("mutation"))
                if call_ids is None or len(call_ids) != len(formal_ids):
                    continue

                replacements = {}
                can_inline = True
                for idx, formal_id in enumerate(formal_ids):
                    raw = (call.get("inputs") or {}).get(call_ids[idx])
                    if raw is None:
                        can_inline = False
                        break
                    if idx in used:
                        literal = _procedure_input_literal(raw)
                        if literal is None:
                            can_inline = False
                            break
                        replacement, _ = _constant_to_scratch_input(literal, blocks, None)
                        if replacement is None:
                            can_inline = False
                            break
                        replacements[formal_id] = (set(reporter_map.get(idx, ())), replacement)
                    elif not _procedure_input_is_discardable(raw, blocks, graph):
                        can_inline = False
                        break
                if not can_inline:
                    continue

                all_reporters = set().union(*(set(v) for v in reporter_map.values())) if reporter_map else set()
                removed = {call_id, definition_id} | prototype_owned | all_reporters
                comments = target.get("comments") or {}
                if any(
                    isinstance(comment, dict) and comment.get("blockId") in removed
                    for comment in comments.values()
                ):
                    continue

                planned_inputs = {}
                for survivor_id in closure - removed:
                    block = blocks.get(survivor_id)
                    if not isinstance(block, dict):
                        continue
                    for input_name, raw in (block.get("inputs") or {}).items():
                        new_raw = copy.deepcopy(raw)
                        changed_input = False
                        for reporter_ids, replacement in replacements.values():
                            if not reporter_ids:
                                continue
                            new_raw, replaced = _inline_argument_reporter_refs(
                                new_raw, reporter_ids, replacement
                            )
                            changed_input |= replaced
                        if changed_input:
                            planned_inputs[(survivor_id, input_name)] = new_raw

                # Ensure no surviving block will continue to reference the removed
                # procedure scaffolding or argument reporters.
                for survivor_id in closure - removed:
                    block = blocks.get(survivor_id)
                    if not isinstance(block, dict):
                        continue
                    for key in ("next", "parent"):
                        ref = block.get(key)
                        if ref in removed:
                            if survivor_id == body_root and key == "parent" and ref == definition_id:
                                continue
                            can_inline = False
                            break
                    if not can_inline:
                        break
                    for input_name, raw in (block.get("inputs") or {}).items():
                        check_value = planned_inputs.get((survivor_id, input_name), raw)
                        if any(ref in removed for ref in _iter_input_block_refs(check_value)):
                            can_inline = False
                            break
                    if not can_inline:
                        break
                if not can_inline:
                    continue

                root_block = blocks.get(body_root)
                tail_block = blocks.get(tail_id)
                next_block = blocks.get(call_next) if call_next is not None else None
                if (
                    not isinstance(root_block, dict)
                    or not isinstance(tail_block, dict)
                    or (call_next is not None and not isinstance(next_block, dict))
                ):
                    continue

                for (survivor_id, input_name), new_raw in planned_inputs.items():
                    blocks[survivor_id].setdefault("inputs", {})[input_name] = new_raw
                    opts.inlined_procedure_input_edits[(ti, survivor_id, input_name)] = copy.deepcopy(new_raw)

                root_block["parent"] = parent_id
                opts.inlined_procedure_link_edits[(ti, body_root, "parent")] = parent_id
                tail_block["next"] = call_next
                opts.inlined_procedure_link_edits[(ti, tail_id, "next")] = call_next
                if call_next is not None:
                    next_block["parent"] = tail_id
                    opts.inlined_procedure_link_edits[(ti, call_next, "parent")] = tail_id

                if parent_mode == "next":
                    parent_block["next"] = body_root
                    opts.inlined_procedure_link_edits[(ti, parent_id, "next")] = body_root
                else:
                    parent_block["inputs"][parent_input_name] = parent_input_replacement
                    opts.inlined_procedure_input_edits[(ti, parent_id, parent_input_name)] = copy.deepcopy(parent_input_replacement)

                for remove_id in removed:
                    blocks.pop(remove_id, None)
                opts.inlined_procedure_removed_blocks[ti].update(removed)

                total += 1
                total_removed += len(removed)
                total_reporters += len(all_reporters)
                changed_this_pass = True

        if not changed_this_pass:
            break
        completed_passes += 1

    stats["procedure_inlined"] += total
    stats["procedure_inline_blocks_removed"] += total_removed
    stats["procedure_inline_argument_reporters_removed"] += total_reporters
    stats["procedure_inline_passes"] += completed_passes
    return total


def optimize_procedure_arguments(project, stats, opts):
	"""
	Remove formal arguments which are never read, and fold non-boolean
	arguments whose every call supplies the same literal.

	An unused argument is removed only when discarding its actual call input is
	known to be effect-free. A constant argument is folded only from literal
	call inputs, so it cannot discard random/procedure-call evaluation.
	"""
	opts.procedure_argument_mutation_edits = {}
	opts.procedure_argument_removed_inputs = {}
	opts.procedure_argument_input_edits = {}
	opts.procedure_argument_removed_blocks = [
		set() for _ in project.get("targets", [])
	]
	total_removed = 0
	total_constant = 0
	total_reporters_removed = 0

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		by_proc, graph = _procedure_infos(target)
		if not by_proc:
			continue

		calls_by_proc = {}
		for bid, block in blocks.items():
			if isinstance(block, dict) and block.get("opcode") == "procedures_call":
				proc = _procedure_key(block)
				if proc is not None:
					calls_by_proc.setdefault(proc, []).append((bid, block))

		for proc, infos in by_proc.items():
			if not infos:
				continue
			# Multiple definitions sharing a proccode must agree on the formal
			# signature before any argument can be optimized globally.
			base_ids = infos[0]["argument_ids"]
			base_mut = infos[0]["mutation"]
			kinds = _procedure_argument_kinds(proc, len(base_ids))
			if kinds is None:
				continue
			if any(info["argument_ids"] != base_ids for info in infos):
				# Different argument IDs are okay, but the positions/count must agree.
				if any(len(info["argument_ids"]) != len(base_ids) for info in infos):
					continue

			body_uses = []
			invalid = False
			for info in infos:
				usage = _procedure_body_argument_uses(info, blocks)
				if usage is None:
					invalid = True
					break
				body_uses.append((info, usage))
			if invalid:
				continue

			used_any = (
				set().union(*(usage[0] for _, usage in body_uses))
				if body_uses
				else set()
			)
			calls = calls_by_proc.get(proc, [])

			removable = []
			constants = {}
			for idx, kind in enumerate(kinds):
				used = idx in used_any
				if not used:
					# Removing a call argument also removes its evaluation.
					safe = True
					for call_id, call in calls:
						call_ids = _parse_argumentids(call.get("mutation"))
						if call_ids is None or idx >= len(call_ids):
							safe = False
							break
						raw = (call.get("inputs") or {}).get(call_ids[idx])
						if not _procedure_input_is_discardable(raw, blocks, graph):
							safe = False
							break
					if safe:
						removable.append(idx)
						continue

				# Constant folding is deliberately restricted to %s/%n-like
				# arguments; boolean literals are not serialized uniformly enough
				# for a safe direct insertion into a boolean slot.
				if used and kind == "%b":
					continue
				if not calls:
					continue
				observed = None
				ok = True
				for call_id, call in calls:
					call_ids = _parse_argumentids(call.get("mutation"))
					if call_ids is None or idx >= len(call_ids):
						ok = False
						break
					call_value = (call.get("inputs") or {}).get(call_ids[idx])
					literal = _procedure_input_literal(call_value)
					if literal is None:
						ok = False
						break
					if observed is None:
						observed = literal
					elif observed != literal:
						ok = False
						break
				if ok and observed is not None and used:
					constants[idx] = observed
					removable.append(idx)

			if not removable:
				continue

			removed_set = set(removable)
			remaining_indices = [
				i for i in range(len(base_ids)) if i not in removed_set
			]

			# Build per-definition reporter replacement plans for constant args.
			constant_reporters = {}
			can_constant_fold = True
			for info, usage in body_uses:
				_, reporter_map, arg_names = usage
				for idx, literal in constants.items():
					reporters = set(reporter_map.get(idx, ()))
					if not reporters:
						continue
					if any(
						bid in {
							cid
							for c in (target.get("comments") or {}).values()
							if isinstance(c, dict)
							for cid in [c.get("blockId")]
							if cid is not None
						}
						for bid in reporters
					):
						can_constant_fold = False
						break
					for reporter_id in reporters:
						if any(
							parent not in info["closure"]
							for parent in graph.parents.get(reporter_id, set())
						):
							can_constant_fold = False
							break
					if not can_constant_fold:
						break
					constant_reporters[(info["definition_id"], idx)] = reporters
				if not can_constant_fold:
					break
			if not can_constant_fold:
				# Do not fold constants, but an unused argument may still be safe
				# to remove independently.
				only_unused = [
					idx for idx in removable if idx not in constants
				]
				removed_set = set(only_unused)
				remaining_indices = [
					i for i in range(len(base_ids)) if i not in removed_set
				]
				constants = {}
				if not removed_set:
					continue

			for info, usage in body_uses:
				proto_id = info["prototype_id"]
				proto = blocks[proto_id]
				old_ids = list(info["argument_ids"])
				old_mut = copy.deepcopy(proto.get("mutation") or {})
				arg_names = json.loads(old_mut.get("argumentnames") or "[]")
				if not isinstance(arg_names, list) or len(arg_names) != len(old_ids):
					continue
				new_proc = proc
				for idx in sorted(removed_set, reverse=True):
					new_proc = _remove_procedure_placeholder(new_proc, idx)
					if new_proc is None:
						break
				if new_proc is None:
					continue

				new_ids = [
					old_ids[i] for i in remaining_indices
				]
				new_names = [arg_names[i] for i in remaining_indices]
				new_mut = _procedure_update_mutation(
					old_mut, new_ids, new_names, removed_set, new_proc
				)
				proto["mutation"] = new_mut
				opts.procedure_argument_mutation_edits[(ti, proto_id)] = copy.deepcopy(
					new_mut
				)
				opts.procedure_argument_removed_inputs[(ti, proto_id)] = {
					old_ids[i] for i in removed_set
				}
				old_inputs = proto.get("inputs") or {}
				for idx in removed_set:
					old_inputs.pop(old_ids[idx], None)
				if not old_inputs:
					# Keep the dict: Scratch blocks are verifier-reinflated to
					# the canonical shape later.
					proto["inputs"] = {}
				else:
					proto["inputs"] = old_inputs

				# Replace constant formal reporters inside this procedure body.
				for idx, literal in constants.items():
					reporters = constant_reporters.get((info["definition_id"], idx), set())
					if not reporters:
						continue
					replacement, _ = _constant_to_scratch_input(
						literal, blocks, None
					)
					if replacement is None:
						continue
					for parent_id in info["closure"]:
						parent = blocks.get(parent_id)
						if not isinstance(parent, dict):
							continue
						for input_name, raw in list((parent.get("inputs") or {}).items()):
							new_raw = copy.deepcopy(raw)
							new_raw, changed = _replace_argument_reporter_refs(
								new_raw, reporters, replacement
							)
							if changed:
								parent.setdefault("inputs", {})[input_name] = new_raw
								opts.procedure_argument_input_edits[
									(ti, parent_id, input_name)
								] = copy.deepcopy(new_raw)
					for reporter_id in reporters:
						blocks.pop(reporter_id, None)
						opts.procedure_argument_removed_blocks[ti].add(reporter_id)
						total_reporters_removed += 1

			# Rewrite every call with the same signature.
			for call_id, call in calls:
				call_mut = call.get("mutation")
				call_ids = _parse_argumentids(call_mut)
				if not isinstance(call_mut, dict) or call_ids is None or len(call_ids) != len(base_ids):
					continue
				old_ids = list(call_ids)
				new_ids = [old_ids[i] for i in remaining_indices]
				new_inputs = dict(call.get("inputs") or {})
				for idx in removed_set:
					new_inputs.pop(old_ids[idx], None)
				new_proc = proc
				for idx in sorted(removed_set, reverse=True):
					new_proc = _remove_procedure_placeholder(new_proc, idx)
					if new_proc is None:
						break
				if new_proc is None:
					continue
				new_mut = _procedure_update_mutation(
					call_mut, new_ids, None, removed_set, new_proc
				)
				call["mutation"] = new_mut
				call["inputs"] = new_inputs
				opts.procedure_argument_mutation_edits[(ti, call_id)] = copy.deepcopy(
					new_mut
				)
				opts.procedure_argument_removed_inputs[(ti, call_id)] = {
					old_ids[i] for i in removed_set
				}

			total_removed += len(removed_set)
			total_constant += len(constants)

	stats["procedure_arguments_removed"] += total_removed
	stats["procedure_constant_arguments_folded"] += total_constant
	stats["procedure_argument_reporters_removed"] += total_reporters_removed
	return total_removed


def _canonicalize_procedure(target, info, graph=None):
	"""Return a stable body/signature representation for duplicate detection."""
	blocks = target.get("blocks") or {}
	closure = info["closure"]
	if graph is None:
		graph = _ScratchGraphIndex(target)

	# Deterministically discover nodes from the definition root. Inputs are
	# traversed in key order; next is visited first to preserve stack order.
	order = []
	seen = set()
	stack = [info["definition_id"]]
	while stack:
		bid = stack.pop()
		if bid in seen or bid not in closure:
			continue
		seen.add(bid)
		order.append(bid)
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		inputs = block.get("inputs") or {}
		for name in sorted(inputs, reverse=True):
			for ref in reversed(list(_iter_input_block_refs(inputs[name]))):
				if ref in closure and ref not in seen:
					stack.append(ref)
		nxt = block.get("next")
		if isinstance(nxt, str) and nxt in closure and nxt not in seen:
			stack.append(nxt)

	labels = {bid: f"$B{index}" for index, bid in enumerate(order)}
	arg_positions = {
		"@arg" + str(i): i for i in range(len(info["argument_ids"]))
	}

	def canonical_input(value, arg_id_map=None):
		if not isinstance(value, list) or not value:
			return value
		tag = value[0]
		out = copy.deepcopy(value)
		if tag in (1, 2):
			if len(out) > 1:
				item = out[1]
				if isinstance(item, str) and item in labels:
					out[1] = labels[item]
				elif isinstance(item, list):
					out[1] = canonical_input(item, arg_id_map)
		elif tag == 3:
			for i in (1, 2):
				if len(out) <= i:
					continue
				item = out[i]
				if isinstance(item, str) and item in labels:
					out[i] = labels[item]
				elif isinstance(item, list):
					out[i] = canonical_input(item, arg_id_map)
		return out

	def canonical_mutation(block):
		mut = block.get("mutation")
		if not isinstance(mut, dict):
			return None
		out = {}
		for key in sorted(mut):
			value = mut[key]
			if key == "argumentids":
				ids = _parse_argumentids(mut)
				if ids is not None:
					out[key] = [
						f"@arg{i}" for i in range(len(ids))
					]
					continue
			out[key] = copy.deepcopy(value)
		return out

	result = {
		"proccode": info["mutation"].get("proccode"),
		"prototype_mutation": canonical_mutation(info["prototype"]),
		"blocks": [],
	}
	for bid in order:
		block = blocks.get(bid)
		if not isinstance(block, dict):
			continue
		item = {}
		for key in sorted(block):
			if key in ("parent", "next", "topLevel", "x", "y"):
				continue
			if key == "inputs":
				inputs = {}
				ids = _parse_argumentids(block.get("mutation"))
				arg_map = {
					old: f"@arg{i}" for i, old in enumerate(ids or [])
				}
				for name in sorted(block["inputs"]):
					cname = arg_map.get(name, name)
					inputs[cname] = canonical_input(block["inputs"][name], arg_map)
				item[key] = inputs
			elif key == "mutation":
				item[key] = canonical_mutation(block)
			else:
				item[key] = copy.deepcopy(block[key])
		item["next"] = labels.get(block.get("next"))
		item["parent"] = labels.get(block.get("parent"))
		result["blocks"].append(item)
	return json.dumps(result, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def merge_duplicate_procedures(project, stats, opts):
	"""Merge whole custom procedures whose signatures and bodies are identical."""
	opts.merged_procedure_removed_blocks = [
		set() for _ in project.get("targets", [])
	]
	total_procedures = 0
	total_blocks = 0

	for ti, target in enumerate(project.get("targets", [])):
		by_proc, graph = _procedure_infos(target)
		if not by_proc:
			continue
		blocks = target.get("blocks") or {}
		comments = target.get("comments") or {}
		commented = {
			c.get("blockId")
			for c in comments.values()
			if isinstance(c, dict) and c.get("blockId") is not None
		}
		for proc, infos in by_proc.items():
			if len(infos) < 2:
				continue
			buckets = {}
			for info in infos:
				closure = info["closure"]
				if closure & commented:
					continue
				# No member of a duplicate procedure may be externally owned.
				if any(
					any(parent not in closure for parent in graph.parents.get(bid, set()))
					for bid in closure
				):
					continue
				signature = _canonicalize_procedure(target, info, graph)
				buckets.setdefault(signature, []).append(info)

			for duplicates in buckets.values():
				if len(duplicates) < 2:
					continue
				duplicates = sorted(
					duplicates, key=lambda info: info["definition_id"]
				)
				canonical = duplicates[0]
				canonical_def = canonical["definition_id"]
				for duplicate in duplicates[1:]:
					closure = set(duplicate["closure"])
					# Never delete a block that the canonical procedure needs.
					if closure & canonical["closure"]:
						continue
					for bid in closure:
						blocks.pop(bid, None)
					opts.merged_procedure_removed_blocks[ti].update(closure)
					total_blocks += len(closure)
					total_procedures += 1

	stats["duplicate_procedures_merged"] += total_procedures
	stats["duplicate_procedure_blocks_removed"] += total_blocks
	return total_procedures

def remove_unused_procedures(project, stats):
	removed_blocks = 0
	removed_procedures = 0

	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		if not blocks:
			continue

		graph = _ScratchGraphIndex(target)
		children = {}
		for parent_id, child_ids in graph.edges.items():
			if child_ids:
				children[parent_id] = set(child_ids)

		definition_blocks = (
			{}
		)  # (proccode, definition_id) -> complete definition closure
		for bid, b in blocks.items():
			if not isinstance(b, dict) or b.get("opcode") != "procedures_definition":
				continue
			custom = (b.get("inputs") or {}).get("custom_block") or [None, None]
			proto_id = (
				custom[1] if len(custom) > 1 and isinstance(custom[1], str) else None
			)
			proto = blocks.get(proto_id)
			proc = _procedure_key(proto) if isinstance(proto, dict) else None
			if proc is None:
				continue

			todo, seen = [bid], set()
			while todo:
				x = todo.pop()
				if x in seen or x not in blocks:
					continue
				seen.add(x)
				todo.extend(children.get(x, ()))
			definition_blocks[(proc, bid)] = seen

		if not definition_blocks:
			continue

		owned = set().union(*definition_blocks.values())
		live_procs = set()
		queue = []

		def seed_calls(ids):
			for x in ids:
				b = blocks.get(x)
				if isinstance(b, dict) and b.get("opcode") == "procedures_call":
					proc = _procedure_key(b)
					if proc is not None and proc not in live_procs:
						queue.append(proc)

		seed_calls(set(blocks) - owned)
		while queue:
			proc = queue.pop()
			if proc in live_procs:
				continue
			live_procs.add(proc)
			for (defined_proc, _definition_id), closure in definition_blocks.items():
				if defined_proc == proc:
					seed_calls(closure)

		all_procs = {proc for proc, _ in definition_blocks}
		dead_procs = all_procs - live_procs
		if not dead_procs:
			continue

		to_delete = set()
		for (proc, _definition_id), closure in definition_blocks.items():
			if proc in dead_procs:
				to_delete.update(closure)

		for bid in to_delete:
			if bid in blocks:
				del blocks[bid]
				removed_blocks += 1
		removed_procedures += len(dead_procs)

		stats["procedure_dangling_block_refs_fixed"] += _repair_dangling_block_refs(target)

	stats["procedures_removed"] += removed_procedures
	stats["procedure_blocks_removed"] += removed_blocks
	return removed_procedures


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
	"""Losslessly compact JSON-encoded custom-block mutation arrays.

	TurboWarp's editor expects mutation.tagName and mutation.children to exist
	when it converts blocks back to XML, so those properties must not be removed.
	This instead canonicalizes the JSON strings used by
	argumentids, argumentnames, and argumentdefaults without changing their
	decoded values.
	"""
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
		return [4, value]
	if isinstance(value, str):
		return [10, value]
	return None


def _is_literal_value(value):
	if not (isinstance(value, list) and len(value) == 2 and value[0] == 1):
		return False
	literal = value[1]
	if not isinstance(literal, list) or len(literal) != 2:
		return False
	tag, raw = literal
	if tag in _NUMERIC_TAGS:
		return (
			not isinstance(raw, bool)
			and isinstance(raw, (str, int, float))
			and (not isinstance(raw, float) or math.isfinite(raw))
		)
	return tag == _TEXT_TAG and isinstance(raw, str)


def _primitive_json_len(value):
	return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _find_constant_variables(project):
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
		if value[0] == 12 and len(value) > 2 and isinstance(value[2], str):
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

		old = [12, entry[0], vid]
		per_use = _primitive_json_len(old) - _primitive_json_len(literal)
		total = per_use * reporter_counts[owner]
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


def _replace_constant_variable_reporters(value, ti, selected, project, stats):
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
	if value[0] == 12 and len(value) > 2 and isinstance(value[2], str):
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


def fold_constant_variables(project, selected, stats):
	if not selected:
		return 0
	selected_map = {(ti, vid): literal for (ti, vid), literal in selected.items()}
	changed = 0
	for ti, target in enumerate(project.get("targets", [])):
		for block in (target.get("blocks") or {}).values():
			if not isinstance(block, dict):
				continue
			for input_name, value in (block.get("inputs") or {}).items():
				if _is_boolean_slot(block, input_name):
					continue  # would leave a literal in a boolean slot
				changed += _replace_constant_variable_reporters(
					value, ti, selected_map, project, stats
				)
	return changed


def _literal_storage_equal(literal, initial):
	if not (isinstance(literal, list) and len(literal) == 2):
		return False
	tag, raw = literal
	if tag in _NUMERIC_TAGS:
		return (
			not isinstance(raw, bool)
			and isinstance(raw, (int, float))
			and not isinstance(initial, bool)
			and isinstance(initial, (int, float))
			and math.isfinite(float(raw))
			and math.isfinite(float(initial))
			and float(raw) == float(initial)
		)
	if tag == _TEXT_TAG:
		return isinstance(raw, str) and isinstance(initial, str) and raw == initial
	return False


def _setter_matches_initial(block, initial):
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
						and value[0] in (1, 2, 3)
						and value[1] == bid
					):
						found = (name, value)
						break
				if found is None:
					kept += 1
					continue
				name, value = found
				if nxt is None:
					new_value = [2, None]  # empty substack
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


def _to_scratch_number(val):
	"""Scratch Cast.toNumber implementation"""
	if isinstance(val, bool):
		return 1.0 if val else 0.0
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
		if math.isnan(num):
			# Cast.toNumber maps NaN to 0
			return 0.0
		return num
	return 0.0


def _to_scratch_bool(val):
	"""Scratch Cast.toBoolean implementation; whitespace is not stripped first."""
	if isinstance(val, bool):
		return val
	if isinstance(val, str):
		if val == "" or val == "0" or val.lower() == "false":
			return False
		return True
	if isinstance(val, (int, float)):
		return val != 0 and not (isinstance(val, float) and math.isnan(val))
	return bool(val)


def _scratch_string(val):
	"""Scratch Cast.toString implementation"""
	if isinstance(val, bool):
		return "true" if val else "false"
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


def _constant(kind, value):
	return (kind, value)


def _constant_to_number(value):
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
	n1 = _constant_to_number(left)
	n2 = _constant_to_number(right)
	if (
		n1 == 0
		and isinstance(left_raw, str)
		and left_raw.strip() == ""
	):
		n1 = float("nan")
	elif (
		n2 == 0
		and isinstance(right_raw, str)
		and right_raw.strip() == ""
	):
		n2 = float("nan")

	if math.isnan(n1) or math.isnan(n2):
		s1 = _scratch_string(left).lower()
		s2 = _scratch_string(right).lower()
		return (s1 > s2) - (s1 < s2)
	if (
		(math.isinf(n1) or math.isinf(n2))
		and n1 == n2
	):
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
	# JavaScript Math.round(-0.x) returns -0; JSON/Scratch serialization does
	# not preserve that distinction -> 0 is sufficient here
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
	if not (isinstance(value, list) and len(value) > 1 and value[0] in (1, 2, 3)):
		return None
	child = value[1]
	if value[0] == 3 and isinstance(child, str):
		# block reference
		return None
	if isinstance(child, list) and len(child) == 2:
		tag, raw = child
		if tag in _NUMERIC_TAGS and isinstance(raw, (int, float, str)) and not isinstance(raw, bool):
			num = _to_scratch_number(raw)
			if isinstance(raw, str) and _scratch_string(num) != raw:
				# Scratch keeps the raw text, so compare/join must see the string
				return _constant("string", raw)
			return _constant("number", num)
		if tag == _TEXT_TAG and isinstance(raw, str):
			return _constant("string", raw)
	return None


def _constant_expression_from_input(value, blocks, visiting=frozenset()):
	if not (isinstance(value, list) and len(value) > 1):
		return None
	if value[0] in (1, 2, 3):
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
		return _constant_expression_from_input(
			inputs.get(name), blocks, current_visiting
		)

	if operation == "operator_not":
		operand = get_in("OPERAND")
		if operand is None:
			return None
		res = not _constant_to_bool(operand[0])
		return _constant("bool", res), operand[1] | {block_id}

	if operation in ("operator_and", "operator_or"):
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
		result = left_bool and _constant_to_bool(right[0]) if operation == "operator_and" else left_bool or _constant_to_bool(right[0])
		return _constant("bool", result), left[1] | right[1] | {block_id}

	if operation in ("operator_gt", "operator_lt", "operator_equals"):
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

	if operation == "operator_join":
		left = get_in("STRING1")
		right = get_in("STRING2")
		if left is None or right is None:
			return None
		return _constant(
			"string", _constant_to_string(left[0]) + _constant_to_string(right[0])
		), left[1] | right[1] | {block_id}

	if operation == "operator_letter_of":
		letter = get_in("LETTER")
		string = get_in("STRING")
		if letter is None or string is None:
			return None
		n = _constant_to_number(letter[0])
		if n is None or not math.isfinite(n):
			return None
		index = math.trunc(n) - 1
		text = _constant_to_string(string[0])
		return _constant("string", _utf16_char_at(text, index)), letter[1] | string[1] | {block_id}

	if operation == "operator_length":
		operand = get_in("STRING")
		if operand is None:
			return None
		text = _constant_to_string(operand[0])
		return _constant("number", _utf16_length(text)), operand[1] | {block_id}

	if operation == "operator_contains":
		left = get_in("STRING1")
		right = get_in("STRING2")
		if left is None or right is None:
			return None
		return _constant(
			"bool",
			_constant_to_string(right[0]).lower() in _constant_to_string(left[0]).lower(),
		), left[1] | right[1] | {block_id}

	if operation == "operator_round":
		operand = get_in("NUM")
		if operand is None:
			return None
		n = _constant_to_number(operand[0])
		if n is None:
			return None
		result = _scratch_round(n)
		if result is None:
			return None
		return _constant("number", result), operand[1] | {block_id}

	if operation == "operator_mathop":
		operand = get_in("NUM")
		if operand is None:
			return None
		num = _constant_to_number(operand[0])
		if num is None:
			return None
		operator_field = (block.get("fields") or {}).get("OPERATOR")
		op_name = (
			operator_field[0]
			if isinstance(operator_field, list) and operator_field
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
						return None
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
					res = 10 ** num
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
	left_num = _constant_to_number(left[0])
	right_num = _constant_to_number(right[0])
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
	return {ref for ref in _iter_input_block_refs(value) if ref in blocks}


def _exclusive_input_block_subtree(value, blocks, owner_id, graph=None):
	roots = _direct_input_block_refs(value, blocks)
	if not roots:
		return set()

	if graph is None:
		# Keep the legacy API usable for callers outside hot paths.
		graph = _ScratchGraphIndex({"blocks": blocks})

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
	for bid in _direct_input_block_refs(value, blocks):
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
	if tag == 12:
		if len(value) > 2 and isinstance(value[2], str) and value[2]:
			return value[2]
		return None

	if tag not in (1, 2, 3) or len(value) <= 1:
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
	if isinstance(field, list) and len(field) > 1 and isinstance(field[1], str) and field[1]:
		return field[1]
	return None


def _simplify_setter_rhs_blocks(project, stats, opts):
	"""
	Rewrite:
	  set [x] to ([x] + a) -> change [x] by a
	  set [x] to ([x] - a) -> change [x] by -a              (literal a)
	  set [x] to ([x] - a) -> change [x] by (0 - a)         (non-literal a)

	The transformation is conservative around shared/commented reporter blocks.
	"""
	targets = project.get("targets", [])
	opts.simplified_block_removed_blocks = [set() for _ in targets]
	opts.simplified_block_link_edits = {}
	opts.simplified_block_input_edits = {}
	opts.simplified_block_opcode_edits = {}

	total = add_count = subtract_count = removed_count = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = _ScratchGraphIndex(target)
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

			rhs_closure = _exclusive_input_block_subtree(rhs_value, blocks, set_id, graph=graph)
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
						opts.simplified_block_link_edits[(ti, child_id, "parent")] = set_id
						child["parent"] = set_id
				_reparent_serialized_input(new_value, blocks, set_id)

				removed = {rhs_id} | left_to_remove
				set_block["opcode"] = "data_changevariableby"
				opts.simplified_block_opcode_edits[(ti, set_id)] = "data_changevariableby"
				inputs["VALUE"] = new_value
				opts.simplified_block_input_edits[(ti, set_id, "VALUE")] = (
					copy.deepcopy(new_value)
				)

				for bid in removed:
					blocks.pop(bid, None)
				opts.simplified_block_removed_blocks[ti].update(removed)
				graph.rebuild()

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

				new_value = [1, [4, number]]
				removed = {rhs_id} | left_to_remove
				set_block["opcode"] = "data_changevariableby"
				opts.simplified_block_opcode_edits[(ti, set_id)] = "data_changevariableby"
				inputs["VALUE"] = new_value
				opts.simplified_block_input_edits[(ti, set_id, "VALUE")] = (
					copy.deepcopy(new_value)
				)

				for bid in removed:
					blocks.pop(bid, None)
				opts.simplified_block_removed_blocks[ti].update(removed)
				graph.rebuild()

				total += 1
				subtract_count += 1
				removed_count += len(removed)
				continue

			# set x to (x - a) -> change x by (0 - a)
			new_left = [1, [4, 0]]
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
			graph.rebuild()

			total += 1
			subtract_count += 1
			removed_count += len(removed)

	stats["blocks_simplified"] += total
	stats["set_to_change_add"] += add_count
	stats["set_to_change_subtract"] += subtract_count
	stats["simplify_blocks_removed"] += removed_count
	return total


def _subtree_block_ids(raw, blocks):
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


def _simplify_boolean_identity_input(value, blocks, parent_id=None, incoming_refs=None, graph=None):
	"""
	Simplify AND/OR inputs when one operand is a compile-time boolean.

	The other operand may be an irreducible reporter expression.
	If that operand is preserved as an alias, its block(s) are re-parented to the new owner.
	If the operand is discarded, its exclusively-owned subtree is
	removed as well, so no surviving block is left with a dangling parent.

	Returns `(replacement_input, removed_block_ids)`.
	"""
	if not (isinstance(value, list) and len(value) > 1 and value[0] in (2, 3)):
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

	left = _constant_expression_from_input(left_raw, blocks)
	right = _constant_expression_from_input(right_raw, blocks)
	left_bool = _constant_to_bool(left[0]) if left is not None else None
	right_bool = _constant_to_bool(right[0]) if right is not None else None

	removed_base = {block_id}
	operator = block.get("opcode")

	def discard(raw, constant_result, constant_ids):
		dead_ids = (
			_exclusive_input_block_subtree_fast(raw, blocks, block_id, incoming_refs, graph=graph)
			if graph is not None and incoming_refs is not None
			else _exclusive_input_block_subtree(raw, blocks, block_id)
		)
		if dead_ids is None:
			return None
		replacement, _ = _constant_to_scratch_input(
			_constant("bool", constant_result), blocks, parent_id or block_id
		)
		return replacement, _expand_removed_closure(
			removed_base | constant_ids | dead_ids, blocks
		)

	def preserve(raw, constant_ids):
		owned_ids = (
			_exclusive_input_block_subtree_fast(raw, blocks, block_id, incoming_refs, graph=graph)
			if graph is not None and incoming_refs is not None
			else _exclusive_input_block_subtree(raw, blocks, block_id)
		)
		if owned_ids is None:
			return None
		return copy.deepcopy(raw), _expand_removed_closure(
			removed_base | constant_ids, blocks, keep=_subtree_block_ids(raw, blocks)
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


def _fold_blocked_by_comment(folded_ids, blocks, commented_ids):
	return any(
		bid in commented_ids
		or (isinstance(blocks.get(bid), dict) and "comment" in blocks[bid])
		for bid in folded_ids
	)


def _direct_input_block_refs(value, blocks):
	if not isinstance(value, list) or not value:
		return ()
	refs = []
	if value[0] in (2, 3) and len(value) > 1:
		ref = value[1]
		if isinstance(ref, str) and ref in blocks:
			refs.append(ref)
	if value[0] == 3 and len(value) > 2:
		ref = value[2]
		if isinstance(ref, str) and ref in blocks:
			refs.append(ref)
	return tuple(refs)


def _incoming_block_ref_counts(target, graph=None):
	if graph is None:
		graph = _ScratchGraphIndex(target)
	return graph.incoming


def _exclusive_input_block_subtree_fast(value, blocks, owner_id, incoming_refs, graph=None):
	if graph is None:
		graph = _ScratchGraphIndex({"blocks": blocks})
	roots = _direct_input_block_refs(value, blocks)
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


def _constant_to_scratch_input(constant, blocks, parent_id):
	kind, value = constant
	if kind == "number":
		number = int(value) if isinstance(value, float) and value.is_integer() else value
		return [1, [4, number]], set()
	if kind == "string":
		return [1, [10, value]], set()
	if kind == "bool":
		return [1, [10, "true" if value else "false"]], set()
	return None, set()


def _fold_ids_have_external_refs(folded_ids, blocks, owner_id, graph=None):
	folded_ids = set(folded_ids)
	if graph is None:
		graph = _ScratchGraphIndex({"blocks": blocks})
	for bid in folded_ids:
		for owner in graph.parents.get(bid, ()):
			if owner != owner_id and owner not in folded_ids:
				return True
	return False


def _demorgan_boolean_input(value, blocks, owner_id=None, incoming_refs=None, graph=None):
	"""
	Apply De Morgan's law in exactly one direction:

		(not a) and (not b) -> not (a or b)
		(not a) or  (not b) -> not (a and b)

	The reverse direction is deliberately never attempted.
	"""
	if not (isinstance(value, list) and len(value) > 1 and value[0] in (2, 3)):
		return None

	logic_id = value[1]
	if not isinstance(logic_id, str) or logic_id not in blocks:
		return None
	logic = blocks[logic_id]
	if not isinstance(logic, dict) or logic.get("opcode") not in ("operator_and", "operator_or"):
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
			and raw[0] in (2, 3)
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
	if incoming_refs.get(left_not_id, 0) != 1 or incoming_refs.get(right_not_id, 0) != 1:
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

	left_owned = _exclusive_input_block_subtree_fast(
		left_operand, blocks, left_not_id, incoming_refs, graph=graph
	)
	right_owned = _exclusive_input_block_subtree_fast(
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

	new_owner_input = [2, left_not_id]
	new_outer_input = [2, logic_id]

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
			"operator_or"
			if logic.get("opcode") == "operator_and"
			else "operator_and"
		),
		"changed_ids": {logic_id, left_not_id, right_not_id} | left_owned | right_owned,
	}


_NUMERIC_REPORTER_OPCODES = {
	"operator_add", "operator_subtract", "operator_multiply",
	"operator_divide", "operator_mod", "operator_round",
	"operator_mathop", "operator_length", "motion_xposition",
	"motion_yposition", "motion_direction", "looks_size",
	"looks_costumenumbername", "looks_backdropnumbername",
	"sound_volume", "sensing_timer", "sensing_dayssince2000",
}

_BOOLEAN_REPORTER_OPCODES = {
	"operator_not", "operator_and", "operator_or", "operator_gt",
	"operator_lt", "operator_equals", "operator_contains",
	"sensing_touchingobject", "sensing_touchingcolor",
	"sensing_keypressed", "sensing_mousedown", "sensing_loud",
	"sensing_askandwait", "video_sensing_on",
}


def _value_is_definitely_numeric(value, blocks, visiting=frozenset()):
	constant = _constant_expression_from_input(value, blocks, visiting)
	if constant is not None:
		return constant[0][0] in ("number", "bool")
	if not (isinstance(value, list) and len(value) > 1):
		return False
	child = value[1]
	if not isinstance(child, str) or child in visiting:
		return False
	block = blocks.get(child)
	return isinstance(block, dict) and block.get("opcode") in _NUMERIC_REPORTER_OPCODES


def _value_is_definitely_boolean(value, blocks, visiting=frozenset()):
	constant = _constant_expression_from_input(value, blocks, visiting)
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
	return op in _BOOLEAN_REPORTER_OPCODES or op == "argument_reporter_boolean"


def _simplify_double_boolean_negation(value, blocks, owner_id, input_name, incoming_refs, graph):
	if not (isinstance(value, list) and len(value) > 1 and value[0] in (2, 3)):
		return None
	outer_id = value[1]
	if not isinstance(outer_id, str) or outer_id == owner_id:
		return None
	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("opcode") != "operator_not":
		return None
	inner_raw = (outer.get("inputs") or {}).get("OPERAND")
	if not (isinstance(inner_raw, list) and len(inner_raw) > 1 and inner_raw[0] in (2, 3)):
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
	if not (_is_boolean_slot(blocks.get(owner_id), input_name) or _value_is_definitely_boolean(operand, blocks)):
		return None
	closure = _exclusive_input_block_subtree_fast(value, blocks, owner_id, incoming_refs, graph=graph)
	if closure is None or outer_id not in closure or inner_id not in closure:
		return None
	preserved_ids = _subtree_block_ids(operand, blocks)
	removed = closure - preserved_ids
	if owner_id in removed or not removed:
		return None
	return copy.deepcopy(operand), removed


def _simplify_algebraic_input(value, blocks, owner_id, incoming_refs, graph):
	"""Apply safe numeric identities to definitely-numeric reporter expressions."""
	if not (isinstance(value, list) and len(value) > 1 and value[0] in (2, 3)):
		return None
	block_id = value[1]
	if not isinstance(block_id, str) or block_id == owner_id:
		return None
	block = blocks.get(block_id)
	if not isinstance(block, dict) or block.get("next") is not None:
		return None
	op = block.get("opcode")
	if op not in ("operator_add", "operator_subtract", "operator_multiply", "operator_divide"):
		return None
	inputs = block.get("inputs") or {}
	left_raw, right_raw = inputs.get("NUM1"), inputs.get("NUM2")
	if left_raw is None or right_raw is None:
		return None
	left = _constant_expression_from_input(left_raw, blocks)
	right = _constant_expression_from_input(right_raw, blocks)
	left_num = _constant_to_number(left[0]) if left is not None else None
	right_num = _constant_to_number(right[0]) if right is not None else None
	preserve = None
	dead_side = None
	if op == "operator_add":
		if left_num == 0 and _value_is_definitely_numeric(right_raw, blocks):
			preserve, dead_side = right_raw, left
		elif right_num == 0 and _value_is_definitely_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_subtract":
		if right_num == 0 and _value_is_definitely_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_multiply":
		if left_num == 1 and _value_is_definitely_numeric(right_raw, blocks):
			preserve, dead_side = right_raw, left
		elif right_num == 1 and _value_is_definitely_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	elif op == "operator_divide":
		if right_num == 1 and _value_is_definitely_numeric(left_raw, blocks):
			preserve, dead_side = left_raw, right
	if preserve is None:
		return None
	closure = _exclusive_input_block_subtree_fast(value, blocks, owner_id, incoming_refs, graph=graph)
	if closure is None or block_id not in closure:
		return None
	removed = closure - _subtree_block_ids(preserve, blocks)
	if owner_id in removed or not removed:
		return None
	if dead_side is not None and not (_subtree_block_ids(dead_side, blocks) <= removed):
		return None
	return copy.deepcopy(preserve), removed


def _replace_owner_block_ref(parent_block, old_id, new_id):
	if not isinstance(parent_block, dict):
		return None
	if parent_block.get("next") == old_id:
		parent_block["next"] = new_id
		return ("next", None)
	for name, value in (parent_block.get("inputs") or {}).items():
		if not isinstance(value, list) or not value:
			continue
		if value[0] in (1, 2, 3) and len(value) > 1 and value[1] == old_id:
			value[1] = new_id
			return ("input", name)
	return None


def _control_substack(value, blocks):
	if not isinstance(value, list) or not value or value[0] not in (1, 2, 3):
		return None
	if len(value) <= 1:
		return None
	ref = value[1]
	return ref if isinstance(ref, str) and ref in blocks else None


def _substack_tail(blocks, closure):
	if not closure:
		return None
	roots = [
		bid for bid in closure
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


def _control_owner_edge(blocks, control_id):
	"""Return (owner_id, edge_kind, edge_name) for a unique direct owner."""
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
			and value[0] in (1, 2, 3)
			and value[1] == control_id
		):
			return parent_id, "input", name
	return None


def _control_input_replacement(owner, edge_kind, edge_name, old_id, new_id):
	"""Return a copied owner reference with one direct control edge replaced."""
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
		or value[0] not in (1, 2, 3)
		or value[1] != old_id
	):
		return None
	value[1] = new_id
	return owner


def _substack_closure(value, blocks, owner_id, incoming_refs, graph):
	if value is None:
		return set(), None
	root = _control_substack(value, blocks)
	if root is None:
		return set(), None
	closure = _exclusive_input_block_subtree_fast(
		value, blocks, owner_id, incoming_refs, graph=graph
	)
	if closure is None or root not in closure:
		return None, None
	tail = _substack_tail(blocks, closure)
	if tail is None:
		return None, None
	return closure, tail


def _constant_control_plan(blocks, control_id, incoming_refs, graph):
	"""
	Build a side-effect-free plan for eliminating a constant control block.

	Supported transformations:
	  if false             -> delete the control + false substack
	  if true              -> splice the true substack
	  if/else              -> splice only the selected branch
	  repeat until true    -> delete loop + body
	  wait until true      -> delete wait
	  repeat 0             -> delete loop + body
	  repeat 1             -> splice the body once

	The plan does not mutate `blocks`; callers can therefore reject a plan due
	to comments or other invariants without having to roll it back.
	"""
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

	owner_info = _control_owner_edge(blocks, control_id)
	if owner_info is None:
		return None
	owner_id, owner_edge_kind, owner_edge_name = owner_info
	owner = blocks.get(owner_id)
	if not isinstance(owner, dict) or control.get("parent") != owner_id:
		return None

	inputs = control.get("inputs") or {}
	condition_name = (
		"TIMES" if opcode == "control_repeat" else "CONDITION"
	)
	condition_raw = inputs.get(condition_name)
	constant = _constant_expression_from_input(condition_raw, blocks)
	if constant is None:
		return None

	if opcode == "control_repeat":
		n = _constant_to_number(constant[0])
		if n is None or not math.isfinite(n) or n not in (0, 1):
			return None
		condition_closure = _exclusive_input_block_subtree_fast(
			condition_raw, blocks, control_id, incoming_refs, graph=graph
		) if condition_raw is not None else set()
		if condition_closure is None:
			return None
		selected_raw = inputs.get("SUBSTACK") if n == 1 else None
		dead_raws = [] if n == 1 else [inputs.get("SUBSTACK")]
	elif opcode == "control_wait_until":
		if not _constant_to_bool(constant[0]):
			return None
		condition_closure = _exclusive_input_block_subtree_fast(
			condition_raw, blocks, control_id, incoming_refs, graph=graph
		) if condition_raw is not None else set()
		if condition_closure is None:
			return None
		selected_raw = None
		dead_raws = []
	elif opcode == "control_repeat_until":
		if not _constant_to_bool(constant[0]):
			return None
		condition_closure = _exclusive_input_block_subtree_fast(
			condition_raw, blocks, control_id, incoming_refs, graph=graph
		) if condition_raw is not None else set()
		if condition_closure is None:
			return None
		selected_raw = None
		dead_raws = [inputs.get("SUBSTACK")]
	else:
		condition_closure = _exclusive_input_block_subtree_fast(
			condition_raw, blocks, control_id, incoming_refs, graph=graph
		) if condition_raw is not None else set()
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

	selected_closure, selected_tail = _substack_closure(
		selected_raw, blocks, control_id, incoming_refs, graph
	)
	if selected_closure is None:
		return None

	removed = {control_id}
	removed.update(condition_closure)
	for raw in dead_raws:
		if raw is None:
			continue
		dead_closure = _exclusive_input_block_subtree_fast(
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

	# Every removed block must belong to this control. No outside edge may
	# enter the removed closure.
	for bid in removed:
		if bid == control_id:
			continue
		parents = graph.parents.get(bid, set())
		if any(parent not in removed and parent != control_id for parent in parents):
			return None

	replacement = selected_closure and _control_substack(selected_raw, blocks) or continuation

	# For a surviving branch, the branch root and continuation get new parents.
	# For a deletion-only transformation, continuation takes the control's old owner.
	parent_updates = []
	next_updates = []
	if selected_closure:
		selected_id = _control_substack(selected_raw, blocks)
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
		"selected_id": _control_substack(selected_raw, blocks),
		"selected_tail": selected_tail,
		"continuation": continuation,
		"removed": removed,
		"parent_updates": parent_updates,
		"next_updates": next_updates,
	}


def simplify_boolean_controls(project, stats, opts, only_repeat_one=False):
	"""Structurally eliminate constant control-flow constructs."""
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
		graph = _ScratchGraphIndex(target)

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

				plan = _constant_control_plan(
					blocks, control_id, graph.incoming, graph
				)
				if plan is None:
					continue
				removed = set(plan["removed"])
				if _fold_blocked_by_comment(removed, blocks, commented_ids):
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
				replaced_owner = _control_input_replacement(
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

				# Record the exact structural differences expected by the verifier.
				if plan["owner_edge_kind"] == "next":
					link_edits[(ti, owner_id, "next")] = plan["replacement"]
				else:
					input_edits[(ti, owner_id, plan["owner_edge_name"])] = copy.deepcopy(
						owner["inputs"][plan["owner_edge_name"]]
					)
				for bid, parent_id in plan["parent_updates"]:
					link_edits[(ti, bid, "parent")] = parent_id
				for bid, next_id in plan["next_updates"]:
					link_edits[(ti, bid, "next")] = next_id

				removed_sets[ti].update(removed)
				for bid in removed:
					blocks.pop(bid, None)

				graph.rebuild()
				total += 1
				changed = True
				stats["boolean_control_simplifications"] += 1
				stats["control_blocks_removed"] += 1
				stats["control_branch_blocks_removed"] += len(removed) - 1
				break

			if not changed:
				break

	return total


_SCRIPT_PURE_REPORTERS = {
	"data_variable", "data_itemoflist", "data_itemnumoflist",
	"data_lengthoflist", "data_listcontainsitem", "data_listcontents",
	"looks_costumenumbername", "looks_backdropnumbername", "looks_size",
	"sound_volume", "motion_xposition", "motion_yposition", "motion_direction",
	"sensing_answer", "sensing_timer", "sensing_dayssince2000", "sensing_username",
	"sensing_current", "sensing_mousedown", "sensing_mousex", "sensing_mousey",
	"sensing_loudness",
}


def _script_input_root(value, blocks):
	if not isinstance(value, list) or len(value) <= 1 or value[0] not in (1, 2, 3):
		return None
	ref = value[1]
	return ref if isinstance(ref, str) and ref in blocks else None


def _script_condition_is_pure(value, blocks, graph):
	"""Conservatively prove that a condition has no observable side effects."""
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
		if op == "operator_random" or op.startswith(("event_", "control_", "procedures_")):
			return False
		if op.startswith("operator_") or op.startswith("argument_reporter_"):
			if op == "operator_random":
				return False
		elif op not in _SCRIPT_PURE_REPORTERS:
			return False
		stack.extend(graph.edges.get(bid, ()))
	return True


def _script_comment_ids(target):
	ids = {
		c.get("blockId")
		for c in (target.get("comments") or {}).values()
		if isinstance(c, dict) and isinstance(c.get("blockId"), str)
	}
	ids.update(
		bid for bid, block in (target.get("blocks") or {}).items()
		if isinstance(block, dict) and "comment" in block
	)
	return ids


def _script_prepare_maps(opts, targets):
	for name in ("script_rewrite_removed_blocks", "script_rewrite_new_blocks"):
		value = getattr(opts, name, None)
		if value is None or len(value) != len(targets):
			setattr(opts, name, [set() for _ in targets])
	for name in ("script_rewrite_link_edits", "script_rewrite_input_edits", "script_rewrite_opcode_edits"):
		if getattr(opts, name, None) is None:
			setattr(opts, name, {})
	if getattr(opts, "script_rewrite_removed_inputs", None) is None:
		opts.script_rewrite_removed_inputs = {}


def _script_record_removed(opts, ti, removed):
	while len(opts.script_rewrite_removed_blocks) <= ti:
		opts.script_rewrite_removed_blocks.append(set())
	opts.script_rewrite_removed_blocks[ti].update(removed)


def _script_record_new(opts, ti, created):
	while len(opts.script_rewrite_new_blocks) <= ti:
		opts.script_rewrite_new_blocks.append(set())
	opts.script_rewrite_new_blocks[ti].update(created)


def _script_literal_from_input(value):
	if not (isinstance(value, list) and len(value) == 2 and value[0] == 1):
		return None
	literal = value[1]
	if not (isinstance(literal, list) and len(literal) == 2):
		return None
	tag, raw = literal
	if tag in _NUMERIC_TAGS and not isinstance(raw, bool) and isinstance(raw, (str, int, float)):
		if isinstance(raw, float) and not math.isfinite(raw):
			return None
		return copy.deepcopy(literal)
	if tag == _TEXT_TAG and isinstance(raw, str):
		return copy.deepcopy(literal)
	return None


def _script_plan_unwrap_not(raw, blocks, owner_id, incoming_refs, graph):
	if not (isinstance(raw, list) and len(raw) == 2 and raw[0] == 2):
		return None
	not_id = raw[1]
	not_block = blocks.get(not_id)
	if not isinstance(not_id, str) or not isinstance(not_block, dict):
		return None
	if not_block.get("opcode") != "operator_not" or not_block.get("next") is not None:
		return None
	operand = (not_block.get("inputs") or {}).get("OPERAND")
	if operand is None or _is_bare_literal_input(operand):
		return None
	owned = _exclusive_input_block_subtree_fast(raw, blocks, owner_id, incoming_refs, graph=graph)
	if owned is None or not_id not in owned:
		return None
	operand_owned = _exclusive_input_block_subtree_fast(operand, blocks, not_id, incoming_refs, graph=graph)
	if operand_owned is None:
		return None
	moved_roots = _direct_input_block_refs(operand, blocks)
	if any((not isinstance(blocks.get(ref), dict)) or blocks[ref].get("parent") != not_id for ref in moved_roots):
		return None
	removed_ids = owned - operand_owned
	return not_id, copy.deepcopy(operand), removed_ids, moved_roots


def _script_plan_branch_swap(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_if_else":
		return None
	condition = (control.get("inputs") or {}).get("CONDITION")
	plan = _script_plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if plan is None:
		return None
	not_id, operand, remove_ids, moved_ids = plan
	removed = set(remove_ids) | {not_id}
	if removed & commented:
		return None
	return {"condition": operand, "removed": removed, "moved_ids": moved_ids}


def _script_plan_empty_then(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_if_else":
		return None
	inputs = control.get("inputs") or {}
	# We only regard a missing SUBSTACK as an empty then-branch. A malformed
	# non-null descriptor is left alone for the verifier/repair machinery.
	if inputs.get("SUBSTACK") is not None:
		return None
	else_raw = inputs.get("SUBSTACK2")
	if _control_substack(else_raw, blocks) is None:
		return None
	condition = inputs.get("CONDITION")
	if condition is None:
		return None
	unwrap = _script_plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if unwrap is not None:
		not_id, operand, remove_ids, moved_ids = unwrap
		removed = set(remove_ids) | {not_id}
		if removed & commented:
			return None
		return {"mode": "unwrap", "condition": operand, "removed": removed,
			"moved_ids": moved_ids, "else_raw": copy.deepcopy(else_raw)}
	owned = _exclusive_input_block_subtree_fast(condition, blocks, control_id, incoming_refs, graph=graph)
	if owned is None or owned & commented:
		return None
	new_id = _create_scratch_id()
	while new_id in blocks:
		new_id = _create_scratch_id()
	new_block = {
		"opcode": "operator_not", "next": None, "parent": control_id,
		"inputs": {"OPERAND": copy.deepcopy(condition)}, "fields": {},
	}
	return {"mode": "wrap", "new_id": new_id, "new_block": new_block,
		"condition_owned": owned, "else_raw": copy.deepcopy(else_raw), "removed": set()}


def _script_plan_repeat_until_not(blocks, control_id, incoming_refs, graph, commented):
	control = blocks.get(control_id)
	if not isinstance(control, dict) or control.get("opcode") != "control_repeat_until":
		return None
	condition = (control.get("inputs") or {}).get("CONDITION")
	unwrap = _script_plan_unwrap_not(condition, blocks, control_id, incoming_refs, graph)
	if unwrap is None:
		return None
	not_id, operand, remove_ids, moved_ids = unwrap
	removed = set(remove_ids) | {not_id}
	if removed & commented:
		return None
	return {"condition": operand, "removed": removed, "moved_ids": moved_ids}


def _script_plan_nested_if(blocks, outer_id, incoming_refs, graph, commented):
	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("opcode") != "control_if":
		return None
	outer_inputs = outer.get("inputs") or {}
	inner_id = _control_substack(outer_inputs.get("SUBSTACK"), blocks)
	if inner_id is None or inner_id == outer_id:
		return None
	inner = blocks.get(inner_id)
	if not isinstance(inner, dict) or inner.get("opcode") != "control_if":
		return None
	if inner.get("next") is not None or inner.get("parent") != outer_id:
		return None
	inner_inputs = inner.get("inputs") or {}
	body_raw = inner_inputs.get("SUBSTACK")
	body_root = _control_substack(body_raw, blocks)
	if body_root is None or "SUBSTACK2" in inner_inputs:
		return None
	body_closure, _ = _substack_closure(body_raw, blocks, inner_id, incoming_refs, graph)
	if body_closure is None:
		return None
	condition_b = inner_inputs.get("CONDITION")
	if not _script_condition_is_pure(condition_b, blocks, graph):
		return None
	condition_a = outer_inputs.get("CONDITION")
	outer_owned = _exclusive_input_block_subtree_fast(condition_a, blocks, outer_id, incoming_refs, graph=graph)
	inner_owned = _exclusive_input_block_subtree_fast(condition_b, blocks, inner_id, incoming_refs, graph=graph)
	if outer_owned is None or inner_owned is None:
		return None
	if ({inner_id} | outer_owned | inner_owned) & commented:
		return None
	new_id = _create_scratch_id()
	while new_id in blocks:
		new_id = _create_scratch_id()
	new_block = {
		"opcode": "operator_and", "next": None, "parent": outer_id,
		"inputs": {"OPERAND1": copy.deepcopy(condition_a), "OPERAND2": copy.deepcopy(condition_b)},
		"fields": {},
	}
	return {"outer_id": outer_id, "inner_id": inner_id, "new_id": new_id,
		"new_block": new_block, "body_raw": copy.deepcopy(body_raw), "body_root": body_root}


def simplify_script_structures(project, stats, opts):
	"""Apply local control rewrites with explicit verifier bookkeeping."""
	targets = project.get("targets", [])
	_script_prepare_maps(opts, targets)
	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = _ScratchGraphIndex(target)
		commented = _script_comment_ids(target)
		candidate_ids = [
			bid for bid, block in blocks.items()
			if isinstance(block, dict) and block.get("opcode") in (
				"control_if", "control_if_else", "control_repeat_until",
				"control_repeat",
			)
		]
		while True:
			changed = False
			for bid in candidate_ids:
				block = blocks.get(bid)
				if not isinstance(block, dict) or bid not in blocks:
					continue
				incoming = graph.incoming

				if opts.branch_swapping:
					plan = _script_plan_branch_swap(blocks, bid, incoming, graph, commented)
					if plan is not None:
						inputs = blocks[bid].setdefault("inputs", {})
						then_raw = copy.deepcopy(inputs.get("SUBSTACK"))
						else_raw = copy.deepcopy(inputs.get("SUBSTACK2"))
						inputs["CONDITION"] = plan["condition"]
						inputs["SUBSTACK"], inputs["SUBSTACK2"] = else_raw, then_raw
						for moved in plan["moved_ids"]:
							if isinstance(blocks.get(moved), dict):
								blocks[moved]["parent"] = bid
								opts.script_rewrite_link_edits[(ti, moved, "parent")] = bid
						for n in ("CONDITION", "SUBSTACK", "SUBSTACK2"):
							opts.script_rewrite_input_edits[(ti, bid, n)] = copy.deepcopy(inputs[n])
						_script_record_removed(opts, ti, plan["removed"])
						for dead in plan["removed"]: blocks.pop(dead, None)
						graph.refresh_blocks((bid, *plan["removed"])); stats["branch_swaps"] += 1; changed = True; break

				if opts.branch_swapping:
					plan = _script_plan_empty_then(blocks, bid, incoming, graph, commented)
					if plan is not None:
						control = blocks[bid]
						inputs = control.setdefault("inputs", {})
						inputs["CONDITION"] = copy.deepcopy(
							plan["condition"] if plan["mode"] == "unwrap" else [2, plan["new_id"]]
						)
						inputs["SUBSTACK"] = plan["else_raw"]
						inputs.pop("SUBSTACK2", None)
						control["opcode"] = "control_if"
						opts.script_rewrite_opcode_edits[(ti, bid)] = "control_if"
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = copy.deepcopy(inputs["CONDITION"])
						opts.script_rewrite_input_edits[(ti, bid, "SUBSTACK")] = copy.deepcopy(inputs["SUBSTACK"])
						opts.script_rewrite_removed_inputs[(ti, bid)] = {"SUBSTACK2"}
						if plan["mode"] == "unwrap":
							for moved in plan["moved_ids"]:
								if isinstance(blocks.get(moved), dict):
									blocks[moved]["parent"] = bid
									opts.script_rewrite_link_edits[(ti, moved, "parent")] = bid
						else:
							for ref in _direct_input_block_refs(plan["new_block"]["inputs"]["OPERAND"], blocks):
								if isinstance(blocks.get(ref), dict):
									blocks[ref]["parent"] = plan["new_id"]
									opts.script_rewrite_link_edits[(ti, ref, "parent")] = plan["new_id"]
							blocks[plan["new_id"]] = plan["new_block"]
							_script_record_new(opts, ti, {plan["new_id"]})
						_script_record_removed(opts, ti, plan["removed"])
						for dead in plan["removed"]: blocks.pop(dead, None)
						graph.refresh_blocks((bid, plan.get("new_id"), *plan["removed"])); stats["empty_then_rewrites"] += 1; changed = True; break

				if opts.trivial_loops:
					plan = _script_plan_repeat_until_not(blocks, bid, incoming, graph, commented)
					if plan is not None:
						control = blocks[bid]
						control["opcode"] = "control_while"
						control.setdefault("inputs", {})["CONDITION"] = plan["condition"]
						opts.script_rewrite_opcode_edits[(ti, bid)] = "control_while"
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = copy.deepcopy(control["inputs"]["CONDITION"])
						for moved in plan["moved_ids"]:
							if isinstance(blocks.get(moved), dict):
								blocks[moved]["parent"] = bid
								opts.script_rewrite_link_edits[(ti, moved, "parent")] = bid
						_script_record_removed(opts, ti, plan["removed"])
						for dead in plan["removed"]: blocks.pop(dead, None)
						graph.refresh_blocks((bid, *plan["removed"])); stats["repeat_until_not_rewrites"] += 1; changed = True; break

				if opts.nested_conditionals:
					plan = _script_plan_nested_if(blocks, bid, incoming, graph, commented)
					if plan is not None:
						outer = blocks[bid]
						new_id = plan["new_id"]
						for input_name in ("OPERAND1", "OPERAND2"):
							for ref in _direct_input_block_refs(plan["new_block"]["inputs"][input_name], blocks):
								if isinstance(blocks.get(ref), dict):
									blocks[ref]["parent"] = new_id
									opts.script_rewrite_link_edits[(ti, ref, "parent")] = new_id
						blocks[new_id] = plan["new_block"]
						outer.setdefault("inputs", {})["CONDITION"] = [2, new_id]
						outer["inputs"]["SUBSTACK"] = plan["body_raw"]
						opts.script_rewrite_input_edits[(ti, bid, "CONDITION")] = [2, new_id]
						opts.script_rewrite_input_edits[(ti, bid, "SUBSTACK")] = copy.deepcopy(plan["body_raw"])
						if isinstance(blocks.get(plan["body_root"]), dict):
							blocks[plan["body_root"]]["parent"] = bid
							opts.script_rewrite_link_edits[(ti, plan["body_root"], "parent")] = bid
						_script_record_new(opts, ti, {new_id})
						_script_record_removed(opts, ti, {plan["inner_id"]})
						blocks.pop(plan["inner_id"], None)
						graph.refresh_blocks((bid, new_id, plan["inner_id"])); stats["nested_if_merges"] += 1; changed = True; break
			if not changed: break
	return sum(stats.get(k,0) for k in ("branch_swaps","empty_then_rewrites","repeat_until_not_rewrites","nested_if_merges"))


def _script_associative_plan(blocks, outer_id, graph, commented):
	outer = blocks.get(outer_id)
	if not isinstance(outer, dict) or outer.get("next") is not None:
		return None
	op = outer.get("opcode")
	if op not in ("operator_add", "operator_join"):
		return None
	left_name, right_name = (("NUM1", "NUM2") if op == "operator_add" else ("STRING1", "STRING2"))
	outer_left = (outer.get("inputs") or {}).get(left_name)
	outer_right = (outer.get("inputs") or {}).get(right_name)
	inner_id = _script_input_root(outer_left, blocks)
	inner = blocks.get(inner_id) if inner_id else None
	if not isinstance(inner, dict) or inner.get("opcode") != op or inner.get("next") is not None:
		return None
	inner_inputs = inner.get("inputs") or {}
	inner_left = inner_inputs.get(left_name)
	inner_right = inner_inputs.get(right_name)
	c1 = _constant_expression_from_input(inner_right, blocks)
	c2 = _constant_expression_from_input(outer_right, blocks)
	if inner_left is None or c1 is None or c2 is None:
		return None
	owned = _exclusive_input_block_subtree_fast(outer_left, blocks, outer_id, graph.incoming, graph=graph)
	if owned is None:
		return None
	survivor_owned = _exclusive_input_block_subtree_fast(inner_left, blocks, inner_id, graph.incoming, graph=graph)
	if survivor_owned is None or inner_id not in owned or not survivor_owned <= owned:
		return None
	removed = owned - survivor_owned
	if removed & commented:
		return None
	if op == "operator_join":
		combined = _constant("string", _constant_to_string(c1[0]) + _constant_to_string(c2[0]))
	else:
		a = _constant_to_number(c1[0]); b = _constant_to_number(c2[0])
		if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
			return None
		combined = _constant("number", a + b)
	new_const, _ = _constant_to_scratch_input(combined, blocks, outer_id)
	if new_const is None:
		return None
	# Numeric reassociation is intentionally gated by its dedicated opt-in flag.
	return {"inner_id": inner_id, "base": copy.deepcopy(inner_left), "new_const": new_const, "removed": removed}


def merge_associative_constants(project, stats, opts):
	_script_prepare_maps(opts, project.get("targets", []))
	count = 0
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		graph = _ScratchGraphIndex(target)
		commented = _script_comment_ids(target)
		while True:
			changed = False
			for bid in list(blocks):
				plan = _script_associative_plan(blocks, bid, graph, commented)
				if plan is None:
					continue
				outer = blocks[bid]
				name = "NUM1" if outer.get("opcode") == "operator_add" else "STRING1"
				name2 = "NUM2" if outer.get("opcode") == "operator_add" else "STRING2"
				for ref in _direct_input_block_refs(plan["base"], blocks):
					if isinstance(blocks.get(ref), dict):
						blocks[ref]["parent"] = bid
						opts.script_rewrite_link_edits[(ti, ref, "parent")] = bid
				outer.setdefault("inputs", {})[name] = plan["base"]
				outer["inputs"][name2] = plan["new_const"]
				opts.script_rewrite_input_edits[(ti, bid, name)] = copy.deepcopy(plan["base"])
				opts.script_rewrite_input_edits[(ti, bid, name2)] = copy.deepcopy(plan["new_const"])
				for remove_id in plan["removed"]:
					blocks.pop(remove_id, None)
				_script_record_removed(opts, ti, plan["removed"])
				graph.rebuild(); stats["associative_constant_merges"] += 1; count += 1; changed = True; break
			if not changed: break
	return count


def _script_var_id(block):
	field = (block.get("fields") or {}).get("VARIABLE") if isinstance(block, dict) else None
	return field[1] if isinstance(field, list) and len(field) > 1 and isinstance(field[1], str) else None


def _script_replace_known_reads(value, env, blocks, owner_id, ti, opts, graph, allow_literal=True):
	if not isinstance(value, list) or not value:
		return value, False, set()
	if len(value) >= 3 and value[0] == 12 and isinstance(value[2], str) and value[2] in env:
		if not allow_literal:
			return value, False, set()
		entry = env[value[2]]
		return [1, copy.deepcopy(entry)], True, set()
	if value[0] in (1,2,3) and len(value) > 1 and isinstance(value[1], str):
		ref = value[1]
		child = blocks.get(ref)
		if isinstance(child, dict) and child.get("opcode") == "data_variable":
			vid = _script_var_id(child)
			if allow_literal and vid in env and child.get("parent") == owner_id:
				owned = _exclusive_input_block_subtree_fast(value, blocks, owner_id, graph.incoming, graph=graph)
				if owned == {ref}:
					return [1, copy.deepcopy(env[vid])], True, {ref}
		return value, False, set()
	if value[0] == 3:
		changed = False; removed = set()
		for i in (1,2):
			if i >= len(value) or not isinstance(value[i], list): continue
			new, c, dead = _script_replace_known_reads(value[i], env, blocks, owner_id, ti, opts, graph, allow_literal=True)
			if c:
				# A [3, actual, shadow] input cannot contain an input wrapper
				# as its actual value.  When constant propagation turns the
				# primary value into [1, literal], collapse the entire covered
				# input to that literal and discard the now-redundant shadow.
				if i == 1 and _is_bare_literal_input(new):
					return new, True, removed | dead
				value[i] = new
				changed = True
			removed |= dead
		return value, changed, removed
	return value, False, set()


def _script_literal_payload(value):
	literal = _script_literal_from_input(value)
	if literal is None:
		return None
	return literal


def propagate_script_constants(project, stats, opts):
	_script_prepare_maps(opts, project.get("targets", []))
	count = 0
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		graph = _ScratchGraphIndex(target)
		roots = [
			bid for bid, block in blocks.items()
			if isinstance(block, dict) and (block.get("topLevel") is True or (block.get("parent") is None and _is_hat(block)))
		]
		for root in roots:
			env = {}; seen = set(); current = root
			while isinstance(current, str) and current in blocks and current not in seen:
				seen.add(current)
				block = blocks[current]
				if not isinstance(block, dict): break
				for input_name, raw in list((block.get("inputs") or {}).items()):
					new, changed, dead = _script_replace_known_reads(raw, env, blocks, current, ti, opts, graph, allow_literal=not _is_boolean_slot(block, input_name))
					if changed:
						block.setdefault("inputs", {})[input_name] = new
						opts.script_rewrite_input_edits[(ti, current, input_name)] = copy.deepcopy(new)
						for dead_id in dead: blocks.pop(dead_id, None)
						_script_record_removed(opts, ti, dead)
						graph.rebuild(); count += 1
				op = block.get("opcode")
				if op == "data_setvariableto":
					vid = _script_var_id(block)
					lit = _script_literal_payload((block.get("inputs") or {}).get("VALUE"))
					if vid and lit is not None: env[vid] = lit
					else: env.pop(vid, None)
				elif op == "data_changevariableby":
					env.pop(_script_var_id(block), None)
				if op.startswith(("event_", "control_", "procedures_")) or op in {"sensing_askandwait", "sound_playuntildone", "event_broadcastandwait"}:
					env.clear()
				current = block.get("next") if isinstance(block.get("next"), str) else None
	stats["script_constants_propagated"] += count
	return count


def _numeric_data_literal(value):
	if not isinstance(value, str) or not value:
		return None
	try:
		iv = int(value)
		if str(iv) == value: return iv
	except (ValueError, OverflowError):
		pass
	try: fv = float(value)
	except (ValueError, OverflowError): return None
	if not math.isfinite(fv) or "." not in value or "e" in value.lower() or str(fv) != value: return None
	return fv


def compact_data_literals(project, stats):
	count = 0
	for target in project.get("targets", []):
		for entry in (target.get("variables") or {}).values():
			if isinstance(entry, list) and len(entry) >= 2:
				new = _numeric_data_literal(entry[1])
				if new is not None: entry[1] = new; count += 1
		for entry in (target.get("lists") or {}).values():
			if not (isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], list)): continue
			for i, value in enumerate(entry[1]):
				new = _numeric_data_literal(value)
				if new is not None: entry[1][i] = new; count += 1
	stats["data_literals_compacted"] += count
	return count


def strip_reference_names(project, stats):
	count = 0
	for target in project.get("targets", []):
		for block in (target.get("blocks") or {}).values():
			if isinstance(block, list) and len(block) >= 3 and block[0] in (12,13) and block[1] != "":
				block[1] = ""; count += 1
			elif isinstance(block, dict):
				fields = block.get("fields") or {}
				for key in ("VARIABLE", "LIST"):
					field = fields.get(key)
					if isinstance(field, list) and len(field) >= 2 and field[0] != "": field[0] = ""; count += 1
				for raw in (block.get("inputs") or {}).values():
					for node in _iter_scratch_input_nodes(raw):
						if len(node) >= 3 and node[0] in (12,13) and node[1] != "": node[1] = ""; count += 1
	stats["reference_names_stripped"] += count
	return count

_EXTENSION_PREFIXES = {
	"pen":"pen_", "music":"music_", "videoSensing":"videoSensing_", "text2speech":"text2speech_",
	"translate":"translate_", "makeymakey":"makeymakey_", "microbit":"microbit_", "microbit_more":"microbit_more_",
	"boost":"boost_", "ev3":"ev3_", "wedo2":"wedo2_", "gdxfor":"gdxfor_",
}


def remove_unused_extensions(project, stats, opts):
	extensions = project.get("extensions")
	if not isinstance(extensions, list): return 0
	used = {block.get("opcode") for target in project.get("targets", []) for block in (target.get("blocks") or {}).values() if isinstance(block, dict) and isinstance(block.get("opcode"), str)}
	removed = []
	for ext in list(extensions):
		prefix = _EXTENSION_PREFIXES.get(ext)
		if prefix is not None and not any(op.startswith(prefix) for op in used):
			extensions.remove(ext); removed.append(ext)
	opts.removed_extensions.update(removed)
	stats["unused_extensions_removed"] += len(removed)
	return len(removed)


def _compact_data_value_equal(original, minified):
	if original == minified: return True
	converted = _numeric_data_literal(original) if isinstance(original, str) else None
	return converted is not None and converted == minified


def _compact_data_entry_equal(original, minified):
	if not (isinstance(original, list) and isinstance(minified, list) and len(original) == len(minified) and original[0] == minified[0]): return False
	if len(original) < 2: return original == minified
	if isinstance(original[1], list) and isinstance(minified[1], list):
		return len(original[1]) == len(minified[1]) and all(_compact_data_value_equal(a,b) for a,b in zip(original[1],minified[1]))
	return _compact_data_value_equal(original[1], minified[1])

def fold_constant_expressions(project, stats, opts):
	targets = project.get("targets", [])
	opts.folded_constant_expression_link_edits = {}
	opts.folded_constant_expression_inputs = {}
	opts.folded_constant_expression_opcode_edits = {}
	opts.folded_constant_expression_blocks = [set() for _ in targets]
	opts.folded_constant_expression_new_blocks = [set() for _ in targets]
	folded = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		graph = _ScratchGraphIndex(target)
		incoming_refs = graph.incoming
		candidate_inputs = [
			(block_id, input_name)
			for block_id, block in blocks.items()
			if isinstance(block, dict)
			for input_name, input_val in (block.get("inputs") or {}).items()
			if (isinstance(input_val, list) and len(input_val) > 1 and input_val[0] in (2, 3)
				and isinstance(input_val[1], str) and isinstance(blocks.get(input_val[1]), dict)
				and str(blocks[input_val[1]].get("opcode", "")).startswith("operator_"))
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
				if not (isinstance(input_val, list) and len(input_val) > 1 and input_val[0] in (1, 2, 3)):
					continue
				if input_val[0] in (2, 3):
					root_id = input_val[1]
					root = blocks.get(root_id) if isinstance(root_id, str) else None
					if not isinstance(root, dict) or not str(root.get("opcode", "")).startswith("operator_"):
						continue

					boolean_negation = _simplify_double_boolean_negation(
						input_val, blocks, block_id, input_name, incoming_refs, graph
					)
					if boolean_negation is not None:
						replacement, removed_ids = boolean_negation
						if not _fold_blocked_by_comment(removed_ids, blocks, commented_ids) and not _fold_ids_have_external_refs(removed_ids, blocks, block_id, graph=graph):
							new_input = copy.deepcopy(replacement)
							for child_id in _input_block_ids(new_input, blocks):
								child = blocks.get(child_id)
								if isinstance(child, dict) and child.get("parent") != block_id:
									opts.folded_constant_expression_link_edits[(
									ti, child_id, "parent"
								)] = block_id
							_reparent_serialized_input(new_input, blocks, block_id)
							block.setdefault("inputs", {})[input_name] = new_input
							opts.folded_constant_expression_inputs[(ti, block_id, input_name)] = copy.deepcopy(new_input)
							opts.folded_constant_expression_blocks[ti].update(removed_ids)
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

					algebraic = _simplify_algebraic_input(
						input_val, blocks, block_id, incoming_refs, graph
					)
					if algebraic is not None:
						replacement, removed_ids = algebraic
						if not _fold_blocked_by_comment(removed_ids, blocks, commented_ids) and not _fold_ids_have_external_refs(removed_ids, blocks, block_id, graph=graph):
							new_input = copy.deepcopy(replacement)
							if not (_is_boolean_slot(block, input_name) and _is_bare_literal_input(new_input)):
								for child_id in _input_block_ids(new_input, blocks):
									child = blocks.get(child_id)
									if isinstance(child, dict) and child.get("parent") != block_id:
										opts.folded_constant_expression_link_edits[(
										ti, child_id, "parent"
									)] = block_id
									
								_reparent_serialized_input(new_input, blocks, block_id)
								block.setdefault("inputs", {})[input_name] = new_input
								opts_key = (ti, block_id, input_name)
								opts.folded_constant_expression_inputs[opts_key] = copy.deepcopy(new_input)
								opts.folded_constant_expression_blocks[ti].update(removed_ids)
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

					demorgan = _demorgan_boolean_input(
						input_val,
						blocks,
						owner_id=block_id,
						incoming_refs=incoming_refs,
						graph=graph,
					)
					if demorgan is not None and not _fold_blocked_by_comment(
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
							for ref in _direct_input_block_refs(raw, blocks):
								blocks[ref]["parent"] = logic_id
								opts.folded_constant_expression_link_edits[(
									ti, ref, "parent"
								)] = logic_id

						opts.folded_constant_expression_link_edits[(
							ti, left_not_id, "parent"
						)] = demorgan["old_logic_parent"]
						opts.folded_constant_expression_link_edits[(
							ti, logic_id, "parent"
						)] = left_not_id
						opts.folded_constant_expression_opcode_edits[(
							ti, logic_id
						)] = demorgan["new_inner_opcode"]
						opts.folded_constant_expression_inputs[(
							ti, block_id, input_name
						)] = copy.deepcopy(demorgan["new_owner_input"])
						opts.folded_constant_expression_inputs[(
							ti, logic_id, "OPERAND1"
						)] = copy.deepcopy(demorgan["new_inner_left"])
						opts.folded_constant_expression_inputs[(
							ti, logic_id, "OPERAND2"
						)] = copy.deepcopy(demorgan["new_inner_right"])
						opts.folded_constant_expression_inputs[(
							ti, left_not_id, "OPERAND"
						)] = copy.deepcopy(demorgan["new_outer_input"])
						opts.folded_constant_expression_blocks[ti].add(right_not_id)
						blocks.pop(right_not_id, None)
						graph.refresh_blocks((block_id, logic_id, left_not_id, right_not_id))
						incoming_refs = graph.incoming
						changed = True
						stats["demorgan_rewrites"] += 1
						break

					if changed:
						break

					identity = _simplify_boolean_identity_input(
						input_val,
						blocks,
						parent_id=block_id,
						incoming_refs=incoming_refs,
						graph=graph,
					)
					if identity is not None:
						replacement, folded_ids = identity
						if _fold_blocked_by_comment(
							folded_ids, blocks, commented_ids
						) or _fold_ids_have_external_refs(
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
							new_input, new_ids = _constant_to_scratch_input(
								replacement, blocks, block_id
							)
						if new_input is None:
							continue
						if _is_boolean_slot(block, input_name) and _is_bare_literal_input(new_input):
							continue  # a literal can't live in a boolean slot
						if isinstance(replacement, list):
							for child_id in _input_block_ids(new_input, blocks):
								child = blocks.get(child_id)
								if isinstance(child, dict) and child.get("parent") != block_id:
									opts.folded_constant_expression_link_edits[(
										ti, child_id, "parent"
									)] = block_id
							_reparent_serialized_input(new_input, blocks, block_id)
						inputs[input_name] = new_input
						stats["boolean_constants_propagated"] += 1
						opts.folded_constant_expression_inputs[(
							ti, block_id, input_name
						)] = copy.deepcopy(new_input)
						opts.folded_constant_expression_blocks[ti].update(folded_ids)
						opts.folded_constant_expression_new_blocks[ti].update(new_ids)
						for remove_id in folded_ids:
							blocks.pop(remove_id, None)
						graph.refresh_blocks((block_id, *owned_ids))
						incoming_refs = graph.incoming
						folded += 1
						changed = True
						break

					res = _constant_expression_from_input(input_val, blocks)
					if res is None:
						continue
					constant, folded_ids = res
					if not folded_ids:
						continue

					owned_ids = _exclusive_input_block_subtree_fast(
						input_val, blocks, block_id, incoming_refs, graph=graph
					)
					if owned_ids is None or not folded_ids <= owned_ids:
						continue
					if _fold_blocked_by_comment(
						owned_ids, blocks, commented_ids
					) or _fold_ids_have_external_refs(
						owned_ids, blocks, block_id, graph=graph
					):
						continue
					if block_id not in blocks:
						continue
					inputs = blocks[block_id].get("inputs") or {}
					if input_name not in inputs:
						continue
					new_input, new_ids = _constant_to_scratch_input(
						constant, blocks, block_id
					)
					if new_input is None:
						continue
					if _is_boolean_slot(block, input_name) and _is_bare_literal_input(new_input):
						continue  # a literal can't live in a boolean slot
					inputs[input_name] = new_input
					opts.folded_constant_expression_inputs[(
						ti, block_id, input_name
					)] = copy.deepcopy(new_input)
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


def prompt_for_constant_variables(project, candidates) -> dict:
	if not candidates:
		print(Ansi.muted("\nNo constant-variable candidates found."))
		return {}

	positive = [c for c in candidates if c["bytes"] > 0]
	negative = [c for c in candidates if c["bytes"] < 0]
	total_positive = sum(c["bytes"] for c in positive)
	w_scope = max(len(c["scope"]) for c in candidates)
	w_name = min(34, max(len(c["name"]) for c in candidates))

	print("")
	print(
		Ansi.heading(
			f"Constant variables found: {len(candidates)}  "
			f"({sum(c['reporters'] for c in candidates):,} reporter uses)"
		)
	)
	print(
		Ansi.muted(
			f"  Positive-size candidates: {len(positive)} (about {total_positive:,} bytes saved)"
		)
	)
	print(
		Ansi.muted(
			f"  Negative-size candidates: {len(negative)} (folding would increase project.json)"
		)
	)
	print("")
	print(
		f"  {'#':>4}  {'scope':<{w_scope}}  {'variable':<{w_name}}  {'uses':>5}  {'bytes':>8}  notes"
	)
	for i, c in enumerate(candidates, 1):
		nm = c["name"] if len(c["name"]) <= w_name else c["name"][: w_name - 1] + "…"
		notes = []
		if c["bytes"] < 0:
			notes.append("LOSS")
		if c["changes"]:
			notes.append(f"+{c['changes']} change")
		print(
			f" {i:>4}  {c['scope']:<{w_scope}}  {nm:<{w_name}}  {c['reporters']:>5,}  {c['bytes']:>+8,}  {'; '.join(notes)}"
		)
	print("")
	print(
		Ansi.warning(
			"  This replaces variable reporters with the variable's unchanged initial value "
			"from project.json. Only variables whose setter also restores that same initial value are offered."
		)
	)
	print(
		Ansi.muted(
			"  Negative byte estimates are marked LOSS and are not selected by 'p'."
		)
	)
	print("")
	print(Ansi.muted("  Enter / n       keep every variable"))
	print(Ansi.muted("  p               fold only candidates that reduce project.json"))
	print(Ansi.muted("  a               fold all candidates"))
	print(
		Ansi.muted("  u               fold only candidates without change-variable-by")
	)
	print(Ansi.muted("  1,3,5-7         fold those numbers"))
	print("")

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
				picked = _parse_selection(answer, len(candidates))
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
				print(f"    - [{c['scope']}] {c['name']!r}: {', '.join(reasons)}")
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


def remove_empty_fields(project, stats):
	count = 0
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if isinstance(block, dict) and block.get("fields") == {}:
				del block["fields"]
				count += 1
	stats["empty_fields_removed"] += count
	return count


def remove_empty_inputs(project, stats):
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
	"""Remove only costume metadata that is provably redundant.

	`md5ext` is removable only when it is exactly the canonical
	`assetId.dataFormat` filename and that asset exists. This avoids
	breaking projects which deliberately use a non-canonical extension.

	`bitmapResolution=1` is removable only for SVG costumes; Scratch's
	vector costume path treats the omitted value as the default.
	"""
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

			# DON'T remove md5ext unless we can prove it is derivable and the
			# referenced asset is actually present in the archive
			if (
				canonical
				and costume.get("md5ext") == canonical
				and (assets is None or canonical in assets)
			):
				del costume["md5ext"]
				count += 1

			# SVGs use vector geometry directly; bitmapResolution=1 is the default and is safe to omit
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


# for backwards compatibility
remove_empty_containers = remove_empty_target_containers


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
						and item[0] in _NUMERIC_TAGS
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
								pass
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
								pass
	stats["numeric_inputs_compacted"] += count
	return count


class Options:
	def __init__(
		self,
		comments=True,
		positions=True,
		covered=True,
		monitors=True,
		lists=True,
		rename_block_ids=False,
		rename_variable_ids=False,
		rename_list_ids=False,
		rename_broadcast_ids=False,
		rename_argument_ids=False,
		rename_identifiers=False,
		rename_variable_names=False,
		rename_list_names=False,
		rename_broadcast_names=False,
		rename_argument_names=False,
		rename_procedure_names=False,
		remove_unused_variables=False,
		remove_unused_lists=False,
		remove_unused_broadcasts=False,
		remove_unreachable=False,
		remove_unused_procedures=False,
		normalize_numbers=False,
		remove_empty_fields=False,
		remove_empty_inputs=False,
		remove_costume_metadata=False,
		remove_default_target_properties=False,
		remove_empty_containers=False,
		remove_empty_target_containers=None,
		remove_project_meta=False,
		convert_wav_to_mp3=False,
		compress_assets=False,
		sort_keys=False,
		compression_level=9,
		list_bytes=DEFAULT_LIST_BYTES,
		list_items=DEFAULT_LIST_ITEMS,
		normalize_epsilon=DEFAULT_EPSILON,
		keep_sound_metadata=False,
		preserve_asset_compression=False,
		frequency_block_ids=False,
		frequency_data_ids=False,
		compact_numeric_inputs=False,
		compact_field_ids=False,
		compact_mutation_hasnext=False,
		compact_mutation_metadata=False,
		fold_constant_variables=False,
		fold_constant_expressions=False,
		simplify_boolean_control=False,
		simplify_blocks=False,
		deduplicate_assets=False,
		optimize_procedure_arguments=False,
		merge_duplicate_procedures=False,
		inline_single_use_procedures=False,
		procedure_inline_passes=8,
		branch_swapping=False,
		trivial_loops=False,
		nested_conditionals=False,
		associative_constant_merging=False,
		script_constant_propagation=False,
		strip_reference_names=False,
		compact_data_literals=False,
		remove_unused_extensions=False,
		group_similar_sequences=False,
		sequence_threshold=3,
		lossless=False,
		all_lossless=False,
		optimize_json=False,
		optimize_assets=False,
		compact_block_defaults=False,
		zopfli=False,
		zopfli_assets=False,
		zopfli_iterations=5,
		compact_costume_references=False,
		compact_block_flags=False,
		minimum_json=False,
		json_search_rounds=0,
		relabel_block_ids=False,
		auto_zopfli=False,
	):
		self.lossless = lossless or all_lossless
		self.all_lossless = all_lossless
		self.optimize_json = optimize_json or self.lossless or zopfli or auto_zopfli or compact_block_defaults or compact_costume_references or compact_block_flags or minimum_json or bool(json_search_rounds) or relabel_block_ids
		self.optimize_assets = optimize_assets or all_lossless or zopfli_assets or auto_zopfli
		self.compact_block_defaults = compact_block_defaults or all_lossless
		self.zopfli = zopfli or all_lossless or auto_zopfli
		self.zopfli_assets = zopfli_assets or all_lossless or auto_zopfli
		self.auto_zopfli = auto_zopfli
		self.relabel_block_ids = relabel_block_ids
		self.zopfli_required = zopfli or zopfli_assets
		self.zopfli_iterations = int(zopfli_iterations)
		self.compact_costume_references = compact_costume_references or all_lossless
		self.compact_block_flags = compact_block_flags
		self.minimum_json = minimum_json or all_lossless
		self.json_search_rounds = int(json_search_rounds) or (1 if all_lossless else 0)
		if self.json_search_rounds < 0:
			raise ValueError("json_search_rounds must be nonnegative")
		if self.zopfli_iterations < 1:
			raise ValueError("zopfli_iterations must be positive")
		if self.lossless:
			conflicts = [
				name for name, enabled in locals().copy().items()
				if enabled is True and (
					name.startswith("rename_") or name.startswith("remove_")
					or name.startswith("fold_") or name.startswith("simplify_")
					or name in ("normalize_numbers", "convert_wav_to_mp3", "sort_keys",
						"compact_numeric_inputs", "compact_field_ids", "compact_mutation_hasnext",
						"compact_mutation_metadata", "deduplicate_assets", "group_similar_sequences",
						"optimize_procedure_arguments", "merge_duplicate_procedures", "inline_single_use_procedures",
						"branch_swapping", "trivial_loops", "nested_conditionals",
						"associative_constant_merging", "script_constant_propagation",
						"strip_reference_names", "compact_data_literals", "remove_unused_extensions",
						"frequency_block_ids", "frequency_data_ids")
				)
			]
			if conflicts:
				raise ValueError(f"lossless mode is incompatible with: {', '.join(conflicts)}")
			comments = positions = covered = monitors = lists = False
			keep_sound_metadata = preserve_asset_compression = True
		self.comments, self.positions, self.covered, self.monitors = (
			comments,
			positions,
			covered,
			monitors,
		)
		self.lists = lists

		self.rename_block_ids = rename_block_ids
		self.rename_variable_ids = rename_variable_ids
		self.rename_list_ids = rename_list_ids
		self.rename_broadcast_ids = rename_broadcast_ids
		self.rename_argument_ids = rename_argument_ids
		self.rename_identifiers = rename_identifiers
		self.rename_variable_names = rename_variable_names
		self.rename_list_names = rename_list_names
		self.rename_broadcast_names = rename_broadcast_names
		self.rename_argument_names = rename_argument_names
		self.rename_procedure_names = rename_procedure_names

		self.remove_unused_variables = remove_unused_variables
		self.remove_unused_lists = remove_unused_lists
		self.remove_unused_broadcasts = remove_unused_broadcasts
		self.remove_unreachable = remove_unreachable
		self.remove_unused_procedures = remove_unused_procedures

		self.normalize_numbers = normalize_numbers
		self.remove_empty_fields = remove_empty_fields
		self.remove_empty_inputs = remove_empty_inputs

		self.remove_costume_metadata = remove_costume_metadata
		self.remove_default_target_properties = remove_default_target_properties
		if remove_empty_target_containers is None:
			remove_empty_target_containers = remove_empty_containers
		self.remove_empty_target_containers = remove_empty_target_containers
		self.remove_empty_containers = remove_empty_target_containers

		self.remove_project_meta = remove_project_meta
		self.compress_assets = compress_assets
		self.convert_wav_to_mp3 = convert_wav_to_mp3
		self.sort_keys = sort_keys
		self.compression_level = compression_level
		self.preserve_asset_compression = preserve_asset_compression
		self.frequency_block_ids = frequency_block_ids
		self.frequency_data_ids = frequency_data_ids

		self.compact_numeric_inputs = compact_numeric_inputs
		self.compact_field_ids = compact_field_ids
		self.compact_mutation_hasnext = compact_mutation_hasnext
		self.compact_mutation_metadata = compact_mutation_metadata
		self.fold_constant_variables = fold_constant_variables
		self.fold_constant_expressions = fold_constant_expressions
		self.simplify_boolean_control = simplify_boolean_control
		self.simplify_blocks = simplify_blocks
		self.deduplicate_assets = deduplicate_assets
		self.optimize_procedure_arguments = optimize_procedure_arguments
		self.merge_duplicate_procedures = merge_duplicate_procedures
		self.inline_single_use_procedures = inline_single_use_procedures
		self.procedure_inline_passes = int(procedure_inline_passes)
		if self.procedure_inline_passes < 1:
			raise ValueError("procedure_inline_passes must be positive")
		self.branch_swapping = branch_swapping
		self.trivial_loops = trivial_loops
		self.nested_conditionals = nested_conditionals
		self.associative_constant_merging = associative_constant_merging
		self.script_constant_propagation = script_constant_propagation
		self.strip_reference_names = strip_reference_names
		self.compact_data_literals = compact_data_literals
		self.remove_unused_extensions = remove_unused_extensions
		self.group_similar_sequences = group_similar_sequences
		self.sequence_threshold = max(1, int(sequence_threshold))

		self.simplified_block_removed_blocks = []
		self.simplified_block_link_edits = {}
		self.simplified_block_input_edits = {}
		self.simplified_block_opcode_edits = {}
		self.renamed_block_ids = {}
		self.renamed_variable_ids = {}
		self.renamed_list_ids = {}
		self.renamed_broadcast_ids = {}
		self.renamed_argument_ids = {}
		self.renamed_identifiers = {}

		self.wav_conversions = {}
		self.asset_deduplications = {}

		self.repaired_broadcast_ids = set()
		self.broadcast_repair_conflicts = {}
		self.list_bytes, self.list_items = list_bytes, list_items
		self.cleared_lists = frozenset()

		self.normalize_epsilon = normalize_epsilon
		self.keep_sound_metadata = keep_sound_metadata

		self.folded_constant_variables = {}
		self.folded_constant_variable_literals = {}
		self.folded_constant_variable_setters = {}
		self.removed_variable_setter_blocks = []
		self.variable_setter_link_edits = {}
		self.variable_setter_input_edits = {}

		self.folded_constant_expression_link_edits = {}
		self.folded_constant_expression_inputs = {}
		self.folded_constant_expression_opcode_edits = {}
		self.folded_constant_expression_blocks = []
		self.folded_constant_expression_new_blocks = []
		self.constant_control_removed_blocks = []

		self.procedure_argument_mutation_edits = {}
		self.procedure_argument_removed_inputs = {}
		self.procedure_argument_input_edits = {}
		self.procedure_argument_removed_blocks = []
		self.merged_procedure_removed_blocks = []
		self.inlined_procedure_removed_blocks = []
		self.inlined_procedure_link_edits = {}
		self.inlined_procedure_input_edits = {}

		self.script_rewrite_removed_blocks = []
		self.script_rewrite_new_blocks = []
		self.script_rewrite_link_edits = {}
		self.script_rewrite_input_edits = {}
		self.script_rewrite_opcode_edits = {}
		self.script_rewrite_removed_inputs = {}
		self.removed_extensions = set()

		self.unreachable_removed_blocks_first = []
		self.unreachable_removed_blocks_second = []
		self.unused_procedure_removed_blocks = []

		self.grouped_sequence_removed_blocks = []
		self.grouped_sequence_new_blocks = []
		self.grouped_sequence_link_edits = {}
		self.grouped_sequence_input_edits = {}
		self.grouped_sequence_rejected_size = 0


def apply_transforms(project, opts: Options, assets=None, progress=None):
	if opts.lossless:
		return Counter()
	progress = progress or ProgressBar(_progress_stage_count(opts))
	stage = progress.next
	stage("Normalize block defaults")
	stats = minify_blocks(project)
	if opts.convert_wav_to_mp3 and assets is not None:
		stage('Convert WAV sounds to MP3')
		opts.wav_conversions = convert_wav_sounds_to_mp3(project, assets, stats)
	if opts.deduplicate_assets and assets is not None:
		stage('Deduplicate assets')
		opts.asset_deduplications = deduplicate_assets(project, assets, stats)
	if opts.comments:
		stage('Strip sprite comments')
		strip_sprite_comments(project, stats)
	if opts.positions:
		stage('Round positions')
		round_positions(project, stats)
	if opts.covered:
		stage('Reset covered shadow values')
		clear_covered_values(project, stats)
	if opts.monitors:
		stage('Clean monitors')
		clean_monitors(project, stats)
	if opts.cleared_lists:
		stage('Clear selected large lists')
		clear_large_lists(project, opts.cleared_lists, stats)
	if opts.remove_unreachable:
		stage('Remove unreachable blocks')
		before = [set((target.get("blocks") or {})) for target in project.get("targets", [])]
		remove_unreachable_blocks(project, stats, "unreachable_blocks_removed_first")
		opts.unreachable_removed_blocks_first = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.remove_unused_procedures:
		stage('Remove unused procedures')
		before = [set((target.get("blocks") or {})) for target in project.get("targets", [])]
		remove_unused_procedures(project, stats)
		opts.unused_procedure_removed_blocks = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.remove_unreachable or opts.remove_unused_procedures:
		stage('Re-scan unreachable blocks')
		before = [set((target.get("blocks") or {})) for target in project.get("targets", [])]
		remove_unreachable_blocks(project, stats, "unreachable_blocks_removed_second")
		opts.unreachable_removed_blocks_second = [
			before[ti] - set((target.get("blocks") or {}))
			for ti, target in enumerate(project.get("targets", []))
		]
	if opts.folded_constant_variables:
		stage('Fold constant variables')
		fold_constant_variables(project, opts.folded_constant_variables, stats)
		remove_constant_variable_setters(
			project, opts.folded_constant_variable_setters, stats, opts
		)
	if opts.script_constant_propagation:
		stage('Propagate script constants')
		propagate_script_constants(project, stats, opts)
	if opts.branch_swapping or opts.trivial_loops or opts.nested_conditionals:
		stage('Rewrite script control structures')
		simplify_script_structures(project, stats, opts)
	if opts.associative_constant_merging:
		stage('Merge associative constants')
		merge_associative_constants(project, stats, opts)
	if opts.fold_constant_expressions:
		stage('Fold constant expressions')
		fold_constant_expressions(project, stats, opts)
	if opts.simplify_boolean_control:
		stage('Simplify boolean control flow')
		simplify_boolean_controls(project, stats, opts)
	elif opts.trivial_loops:
		stage('Simplify trivial boolean loops')
		simplify_boolean_controls(project, stats, opts, only_repeat_one=True)
	if opts.simplify_blocks:
		stage('Simplify setter RHS blocks')
		_simplify_setter_rhs_blocks(project, stats, opts)
	if opts.group_similar_sequences:
		stage('Group similar sequences')
		group_similar_sequences(project, stats, opts.sequence_threshold, opts)
	if opts.optimize_procedure_arguments:
		stage('Optimize procedure arguments')
		optimize_procedure_arguments(project, stats, opts)
	if opts.merge_duplicate_procedures:
		stage('Merge duplicate procedures')
		merge_duplicate_procedures(project, stats, opts)
	if opts.inline_single_use_procedures:
		stage('Inline safe single-use procedures')
		inline_single_use_procedures(project, stats, opts)
	if opts.remove_unused_procedures and (
		opts.optimize_procedure_arguments or opts.merge_duplicate_procedures
	):
		stage("Remove procedures made unused")
		remove_unused_procedures(project, stats)
	if opts.remove_unused_variables or opts.remove_unused_lists:
		stage('Remove unused variables/lists')
		remove_unused_data(
			project, stats, opts.remove_unused_variables, opts.remove_unused_lists
		)
	stage("Repair dangling broadcast references")
	opts.repaired_broadcast_ids, opts.broadcast_repair_conflicts = (
		_repair_dangling_broadcast_refs(project, stats)
	)
	if opts.remove_unused_broadcasts:
		stage('Remove unused broadcasts')
		remove_unused_broadcasts(project, stats)
	if (
		opts.rename_identifiers
		or opts.rename_variable_names
		or opts.rename_list_names
		or opts.rename_broadcast_names
		or opts.rename_argument_names
		or opts.rename_procedure_names
	):
		stage('Rename identifiers')
		opts.renamed_identifiers = rename_identifiers(
			project,
			stats,
		rename_variable_names=(opts.rename_identifiers or opts.rename_variable_names),
		rename_list_names=(opts.rename_identifiers or opts.rename_list_names),
			rename_broadcast_names=(opts.rename_identifiers or opts.rename_broadcast_names),
			rename_argument_names=(opts.rename_identifiers or opts.rename_argument_names),
			rename_procedure_names=(opts.rename_identifiers or opts.rename_procedure_names),
		)
	used_data_ids = set()
	if opts.rename_variable_ids or opts.rename_list_ids:
		stage('Rename variable/list IDs')
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
		stage('Rename broadcast IDs')
		opts.renamed_broadcast_ids = rename_broadcast_ids(
			project,
			stats,
			existing_ids=used_data_ids,
			frequency_order=opts.frequency_data_ids,
		)
	if opts.rename_argument_ids:
		stage('Rename argument IDs')
		opts.renamed_argument_ids = rename_argument_ids(project, stats)
	if opts.rename_block_ids:
		stage('Rename block IDs')
		opts.renamed_block_ids = rename_block_ids(
			project, stats, frequency_order=opts.frequency_block_ids
		)
	if opts.compact_numeric_inputs:
		stage('Compact numeric inputs')
		compact_numeric_inputs(project, stats)
	if opts.compact_field_ids:
		stage('Compact redundant field IDs')
		compact_redundant_field_ids(project, stats)
	if opts.compact_mutation_hasnext:
		stage('Compact mutation hasnext')
		compact_mutation_hasnext(project, stats)
	if opts.compact_mutation_metadata:
		stage('Compact mutation metadata')
		compact_mutation_metadata(project, stats)
	if opts.normalize_numbers:
		stage('Normalize numbers')
		normalize_numbers(project, stats, opts.normalize_epsilon)
	if not opts.keep_sound_metadata:
		stage('Remove sound metadata')
		remove_sound_metadata(project, stats)
	if opts.remove_empty_fields:
		stage('Remove empty fields')
		remove_empty_fields(project, stats)
	if opts.remove_empty_inputs:
		stage('Remove empty inputs')
		remove_empty_inputs(project, stats)
	if opts.remove_costume_metadata:
		stage('Remove costume metadata')
		remove_costume_metadata(project, stats, assets)
	if opts.remove_default_target_properties:
		stage('Remove default target properties')
		remove_default_target_properties(project, stats)
	if opts.remove_empty_target_containers:
		stage('Remove empty target containers')
		remove_empty_target_containers(project, stats)
	if opts.remove_project_meta:
		stage('Remove project metadata')
		remove_project_meta(project, stats)
	if opts.compact_data_literals:
		stage('Compact data literals')
		compact_data_literals(project, stats)
	if opts.strip_reference_names:
		stage('Strip reference names')
		strip_reference_names(project, stats)
	if opts.remove_unused_extensions:
		stage('Remove unused extensions')
		remove_unused_extensions(project, stats, opts)
	stage("Repair dangling block links")
	for target in project.get("targets", []):
		stats["dangling_block_refs_fixed"] += _repair_dangling_block_refs(target)
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
					_replace_input_block_ids(value, block_ids)
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
			if len(value) > 2 and value[0] == 12:
				value[2] = restore_var(ti, value[2])
			elif len(value) > 2 and value[0] == 13:
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
				if len(block) > 2 and block[0] == 12:
					block[2] = restore_var(ti, block[2])
				elif len(block) > 2 and block[0] == 13:
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
					_replace_broadcast_ids_in_value(value, rev)


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
			vals = _parse_argumentids(mut)
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



def _restore_block_data_broadcast_ids(project, opts):
	"""Restore block, variable/list, and broadcast IDs in one graph traversal."""
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)

	block_rev = {
		ti: {new: old for old, new in (getattr(opts, "renamed_block_ids", {}) or {}).get(ti, {}).items()}
		for ti in range(len(targets))
	}
	var_rev, list_rev = {}, {}
	for (ti, old), new in (getattr(opts, "renamed_variable_ids", {}) or {}).items():
		var_rev.setdefault(ti, {})[new] = old
	for (ti, old), new in (getattr(opts, "renamed_list_ids", {}) or {}).items():
		list_rev.setdefault(ti, {})[new] = old
	broadcast_rev = {
		new: old for old, new in (getattr(opts, "renamed_broadcast_ids", {}) or {}).items()
	}

	current_var_ids = [set((t.get("variables") or {}).keys()) for t in targets]
	current_list_ids = [set((t.get("lists") or {}).keys()) for t in targets]

	def restore_var(ti, value):
		if not isinstance(value, str):
			return value
		if value in current_var_ids[ti]:
			return var_rev.get(ti, {}).get(value, value)
		if stage_index is not None and value in current_var_ids[stage_index]:
			return var_rev.get(stage_index, {}).get(value, value)
		return value

	def restore_list(ti, value):
		if not isinstance(value, str):
			return value
		if value in current_list_ids[ti]:
			return list_rev.get(ti, {}).get(value, value)
		if stage_index is not None and value in current_list_ids[stage_index]:
			return list_rev.get(stage_index, {}).get(value, value)
		return value

	def restore_nested(ti, value):
		if isinstance(value, list):
			if len(value) > 2 and value[0] == 12 and isinstance(value[2], str):
				value[2] = restore_var(ti, value[2])
				return
			if len(value) > 2 and value[0] == 13 and isinstance(value[2], str):
				value[2] = restore_list(ti, value[2])
				return
			if len(value) > 2 and value[0] == 11 and isinstance(value[2], str):
				value[2] = broadcast_rev.get(value[2], value[2])
				return
			if value and value[0] in (1, 2, 3):
				positions = (1, 2) if value[0] == 3 else (1,)
				for index in positions:
					if index >= len(value):
						continue
					item = value[index]
					if isinstance(item, str):
						value[index] = block_rev.get(ti, {}).get(item, item)
					elif isinstance(item, (list, dict)):
						restore_nested(ti, item)
				return
			for child in value:
				if isinstance(child, (list, dict)):
					restore_nested(ti, child)
		elif isinstance(value, dict):
			for child in value.values():
				if isinstance(child, (list, dict)):
					restore_nested(ti, child)

	for ti, target in enumerate(targets):
		vmap = var_rev.get(ti, {})
		lmap = list_rev.get(ti, {})
		if vmap:
			target["variables"] = {vmap.get(k, k): v for k, v in (target.get("variables") or {}).items()}
		if lmap:
			target["lists"] = {lmap.get(k, k): v for k, v in (target.get("lists") or {}).items()}
		if broadcast_rev and target.get("broadcasts"):
			target["broadcasts"] = {broadcast_rev.get(k, k): v for k, v in target["broadcasts"].items()}

		bmap = block_rev.get(ti, {})
		blocks = target.get("blocks") or {}
		if bmap:
			blocks = {bmap.get(current_id, current_id): block for current_id, block in blocks.items()}
			target["blocks"] = blocks
		for block in blocks.values():
			if isinstance(block, list):
				if len(block) > 2 and block[0] == 12 and isinstance(block[2], str):
					block[2] = restore_var(ti, block[2])
				elif len(block) > 2 and block[0] == 13 and isinstance(block[2], str):
					block[2] = restore_list(ti, block[2])
				continue
			if not isinstance(block, dict):
				continue
			for key in ("next", "parent"):
				if isinstance(block.get(key), str):
					block[key] = bmap.get(block[key], block[key])
			fields = block.get("fields") or {}
			f = fields.get("VARIABLE")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = restore_var(ti, f[1])
			f = fields.get("LIST")
			if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
				f[1] = restore_list(ti, f[1])
			for field_name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
				f = fields.get(field_name)
				if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str):
					f[1] = broadcast_rev.get(f[1], f[1])
			for value in (block.get("inputs") or {}).values():
				restore_nested(ti, value)

	name_to_index = {t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")}
	for monitor in project.get("monitors", []):
		if not isinstance(monitor, dict):
			continue
		sprite = monitor.get("spriteName")
		ti = name_to_index.get(sprite, stage_index) if sprite else stage_index
		if ti is None:
			continue
		if monitor.get("opcode") == "data_variable":
			monitor["id"] = restore_var(ti, monitor.get("id"))
		elif monitor.get("opcode") == "data_listcontents":
			monitor["id"] = restore_list(ti, monitor.get("id"))

def _num_eq(a, b, epsilon=0):
	"""Compare JSON numbers by decimal value, optionally allowing normalization epsilon.

	The JSON optimizer may rewrite a large integer into exponent notation.
	Python then parses the exponent form as a float, even when the decimal
	value is exactly the same integer.  Decimal(str(...)) preserves that exact
	decimal value.  When ``epsilon`` is nonzero, the comparison also mirrors
	``normalize_numbers``: values within the configured normalization epsilon
	are considered equivalent.
	"""
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
	"""Match the exact numeric transformation performed by ``round_positions``."""
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

	for bid, info in _collect_broadcast_references(project).items():
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
	valid = _all_broadcast_ids(project)
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
	if len(value) > 2 and value[0] == 11 and isinstance(value[2], str):
		return value[2] if value[2] not in valid else None
	for v in value:
		bad = _find_dangling_broadcast(v, valid)
		if bad is not None:
			return bad
	return None


def _check_block_references_resolve(project):
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			parent = block.get("parent")
			if (
				isinstance(parent, str)
				and parent not in blocks
				and not _is_known_orphan_argument_reporter(block, blocks)
			):
				return f"{target.get('name')!r}/{bid!r}: parent points to missing block {parent!r}"
			nxt = block.get("next")
			if isinstance(nxt, str) and nxt not in blocks:
				return f"{target.get('name')!r}/{bid!r}: next points to missing block {nxt!r}"
			for name, value in (block.get("inputs") or {}).items():
				for child_id in _iter_input_block_refs(value):
					if child_id not in blocks:
						return f"{target.get('name')!r}/{bid!r}: input {name!r} points to missing block {child_id!r}"
	return None


def _check_argument_id_consistency(target, where):
	for bid, b in target.get("blocks", {}).items():
		if not isinstance(b, dict) or b.get("opcode") not in (
			"procedures_prototype",
			"procedures_call",
		):
			continue
		vals = _parse_argumentids(b.get("mutation"))
		if vals is None:
			continue
		if set(vals) != set((b.get("inputs") or {}).keys()):
			return (
				f"{where}/{bid!r}: procedures argumentids {vals} does not match "
				f"inputs keys {sorted((b.get('inputs') or {}).keys())}"
			)
	return None


def _input_val_eq(vo, vm, opts):
	"""Compare the payload of a Scratch primitive literal.

	``compact_numeric_inputs`` can first turn a canonical numeric string such
	as ``"10000"`` into the integer ``10000``.  The later JSON encoder may
	serialize that numeric value as exponent notation, which ``json.loads``
	then exposes as ``10000.0``.  Compare the resulting numeric value rather
	than the incidental Python type, but only for string forms that the
	optimizer itself can actually compact.
	"""
	if vo == vm:
		return True

	if opts.normalize_numbers and (_num_eq(vo, vm, opts.normalize_epsilon) or vo == vm):
		return True

	# The JSON layout optimizer can spell an integral number using exponent
	# notation (for example 10000 -> 1e4), which changes only the Python
	# int/float type returned by json.loads. Accept that exact numeric
	# re-encoding when JSON optimization is enabled.
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
				pass

	if opts.compact_numeric_inputs:
		if isinstance(vo, str) and isinstance(vm, (int, float)) and not isinstance(vm, bool):
			# Mirror the integer branch of compact_numeric_inputs exactly.
			try:
				iv = int(vo)
			except (ValueError, OverflowError):
				iv = None
			if iv is not None and str(iv) == vo:
				if isinstance(vm, float) and not math.isfinite(vm):
					return False
				return Decimal(str(iv)) == Decimal(str(vm))

			# Mirror the float branch of compact_numeric_inputs exactly.
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
	if tag in (1, 2):
		if len(value) > 1 and isinstance(value[1], str):
			return value[1] not in blocks
		if len(value) > 1 and isinstance(value[1], (list, dict)):
			return _input_has_dangling_block_ref(value[1], blocks)
		return False
	if tag == 3:
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
	if value and value[0] == 12 and len(value) > 2 and isinstance(value[2], str):
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
		expected, _ = _repair_dangling_block_ref(expected, remaining_blocks)
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
	if not (isinstance(oi, list) and isinstance(mi, list) and len(oi) == len(mi)):
		return False
	if not oi or not mi or oi[0] != mi[0]:
		return False
	if oi[0] in (12, 13) and len(oi) >= 3 and len(mi) >= 3:
		if oi[2] != mi[2]: return False
		return oi[1] == mi[1] or bool(getattr(opts, "strip_reference_names", False) and mi[1] == "")
	if oi[0] in (1, 2):
		if len(oi) > 1 and len(mi) > 1:
			if (
				isinstance(oi[1], list)
				and isinstance(mi[1], list)
				and len(oi[1]) == len(mi[1])
			):
				if oi[1][0] == mi[1][0] and oi[1][0] in _NUMERIC_TAGS:
					return _input_val_eq(oi[1][1], mi[1][1], opts)
				return oi[1] == mi[1]
			return oi[1] == mi[1]
	elif oi[0] == 3:
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
				and oi[2][0] in (4, 5, 6, 7, 8, 10)
			):
				return True
			if (
				isinstance(oi[2], list)
				and isinstance(mi[2], list)
				and len(oi[2]) == len(mi[2])
			):
				if oi[2][0] == mi[2][0] and oi[2][0] in _NUMERIC_TAGS:
					return _input_val_eq(oi[2][1], mi[2][1], opts)
				return oi[2] == mi[2]
	return False


def _check_fields_match(original_fields, minified_fields, opts):
	if original_fields == minified_fields:
		return True
	if getattr(opts, "strip_reference_names", False) and isinstance(original_fields, dict) and isinstance(minified_fields, dict) and set(original_fields) == set(minified_fields):
		good = True
		for key, ov in original_fields.items():
			mv = minified_fields[key]
			if key in ("VARIABLE", "LIST") and isinstance(ov, list) and isinstance(mv, list) and len(ov) == len(mv) and len(ov) >= 2 and ov[1] == mv[1] and mv[0] == "":
				continue
			if ov != mv: good = False; break
		if good: return True
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


@lru_cache(maxsize=4096)
def _cached_json_loads(value):
	return json.loads(value)


def _check_mutation_match(original_mutation, minified_mutation, opts):
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
	for key, ov in original_mutation.items():
		if key not in minified_mutation:
			if key in allowed_missing:
				continue
			return False
		mv = minified_mutation[key]
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
				pass
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
	# Most surviving blocks are byte-for-byte/field-for-field unchanged after
	# the verifier's reinflation and ID restoration. Avoid the per-property
	# validation machinery in that common case. Equality is stronger than every
	# individual rule below, so this cannot mask an allowed-but-required change.
	if o == m:
		return None
	remaining_blocks = (
		remaining_blocks if remaining_blocks is not None else set(original_blocks or ())
	)
	if set(o) != set(m):
		gone = set(o) - set(m)
		extra = set(m) - set(o)
		if extra or not (opts.comments and gone <= {"comment"}):
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
			folded_opcodes = getattr(opts, "folded_constant_expression_opcode_edits", {}) or {}
			expected_opcode = folded_opcodes.get((target_index, block_id))
			if expected_opcode is not None and m[k] == expected_opcode:
				continue
			simplified_opcodes = getattr(opts, "simplified_block_opcode_edits", {}) or {}
			expected_opcode = simplified_opcodes.get((target_index, block_id))
			if expected_opcode is not None and m[k] == expected_opcode:
				continue
			if o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): property {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "fields":
			if not _check_fields_match(o[k], m[k], opts):
				return f"{where} (opcode: {o.get('opcode')}): fields changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "mutation":
			procedure_mutations = getattr(opts, "procedure_argument_mutation_edits", {}) or {}
			procedure_mutation = procedure_mutations.get((target_index, block_id))
			if procedure_mutation is not None:
				expected_mutation = copy.deepcopy(procedure_mutation)
				if isinstance(expected_mutation, dict):
					if expected_mutation.get("warp") is True:
						expected_mutation["warp"] = "true"
					elif expected_mutation.get("warp") is False:
						expected_mutation["warp"] = "false"
				if _check_mutation_match(expected_mutation, m[k], opts):
					continue
			if not _check_mutation_match(o[k], m[k], opts):
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
					fixed, changed = _repair_dangling_block_ref(
						copy.deepcopy(o["inputs"][name]), remaining_blocks
					)
					if changed and fixed is None:
						allowed_gone.add(name)
			procedure_removed = getattr(opts, "procedure_argument_removed_inputs", {}) or {}
			allowed_procedure_removed = procedure_removed.get((target_index, block_id), set())
			allowed_gone.update(gone_inputs & set(allowed_procedure_removed))
			script_removed_inputs = getattr(opts, "script_rewrite_removed_inputs", {}) or {}
			allowed_gone.update(gone_inputs & set(script_removed_inputs.get((target_index, block_id), set())))

			# Schema-changing script rewrites may rename an input slot along with the opcode.
			# In particular, control_if_else -> control_if moves the surviving else branch
			# from SUBSTACK2 to SUBSTACK. A later pass may legitimately update that same
			# surviving input, so accept either the final value as directly equivalent to the
			# original value, or the optimizer's recorded input edit as the provenance.
			script_opcodes = getattr(opts, "script_rewrite_opcode_edits", {}) or {}
			expected_script_opcode = script_opcodes.get((target_index, block_id))
			script_input_edits = getattr(opts, "script_rewrite_input_edits", {}) or {}
			recorded_substack = script_input_edits.get((target_index, block_id, "SUBSTACK"))
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
					opts, original_blocks, remaining_blocks, target_index
				)
				if not schema_match and recorded_substack is not None:
					schema_match = _check_inputs_match(
					recorded_substack,
					m["inputs"]["SUBSTACK"],
					opts, original_blocks, remaining_blocks, target_index
				)
				if not schema_match:
					inlined_substack = (
						getattr(opts, "inlined_procedure_input_edits", {}) or {}
					).get((target_index, block_id, "SUBSTACK"))
					schema_match = inlined_substack is not None and inlined_substack == m["inputs"]["SUBSTACK"]
			if schema_match:
				gone_inputs.clear()
				extra_inputs.clear()

			if extra_inputs or gone_inputs - allowed_gone:
				return f"{where} (opcode: {o.get('opcode')}): input names changed. Original inputs: {sorted(o['inputs'])}, minified inputs: {sorted(m['inputs'])}"
			for name in set(o["inputs"]) & set(m["inputs"]):
				oi, mi = o["inputs"][name], m["inputs"][name]
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
				procedure_arg_inputs = getattr(opts, "procedure_argument_input_edits", {}) or {}
				procedure_arg_input = procedure_arg_inputs.get((target_index, block_id, name))
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
				inlined_inputs = getattr(opts, "inlined_procedure_input_edits", {}) or {}
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
				if script_input is not None and (script_input == mi or _check_inputs_match(script_input, mi, opts, original_blocks, remaining_blocks, target_index)):
					continue
				simplified_inputs = getattr(opts, "simplified_block_input_edits", {}) or {}
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
			fold_link_edits = getattr(opts, "folded_constant_expression_link_edits", None) or {}
			script_link_edits = getattr(opts, "script_rewrite_link_edits", None) or {}
			group_link_edits = getattr(opts, "grouped_sequence_link_edits", None) or {}
			simplified_link_edits = getattr(opts, "simplified_block_link_edits", None) or {}
			if k in ("next", "parent"):
				link_key = (target_index, block_id, k)
				expected_link = link_edits.get(link_key)
				if expected_link is None:
					expected_link = fold_link_edits.get(link_key)
				if expected_link is None:
					inlined_link_edits = getattr(opts, "inlined_procedure_link_edits", {}) or {}
					expected_link = inlined_link_edits.get(link_key)
				if expected_link is None:
					expected_link = group_link_edits.get(link_key)
				if expected_link is None:
					expected_link = simplified_link_edits.get(link_key)
				if expected_link is None:
					expected_link = script_link_edits.get(link_key)
				if expected_link is not None and m[k] == expected_link:
					continue
			if (
				k == "parent"
				and m[k] is None
				and isinstance(o[k], str)
				and _is_known_orphan_argument_reporter(o, original_blocks or {})
				and o[k] not in (original_blocks or {})
			):
				# Scratch projects can contain shadow argument reporters whose parent
				# points at an already-missing prototype shadow block. The structural
				# validator explicitly permits this quirk; accept its canonicalization
				# here as well without opening generic parent-link repair.
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


_ASSET_EXTENSIONS = {
	"costume": {"png", "svg", "jpeg", "jpg", "bmp", "gif"},
	"sound": {"wav", "wave", "mp3"},
}


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
	out.update(_iter_input_block_refs(value))


def _validate_block_structure(project, label):
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		if not isinstance(blocks, dict):
			return f"{label}, target {ti} ({target.get('name')!r}): blocks is not an object"

		input_parents = {}
		for bid, block in blocks.items():
			where = f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}"
			if not isinstance(bid, str) or not bid:
				return f"{where}: invalid block ID"
			if isinstance(block, list):
				if len(block) < 3 or block[0] not in (12, 13):
					return f"{where}: malformed primitive block {block!r}"
				continue
			if not isinstance(block, dict):
				return f"{where}: block is neither object nor variable/list primitive"
			if not isinstance(block.get("opcode"), str) or not block.get("opcode"):
				return f"{where}: missing/invalid opcode"
			_orphan_parent_quirk = False
			for key in ("next", "parent"):
				ref = block.get(key)
				if ref is not None and not isinstance(ref, str):
					return f"{where}: {key} must be null or a string"
				if isinstance(ref, str) and ref not in blocks:
					if key == "parent" and _is_known_orphan_argument_reporter(
						block, blocks
					):
						_orphan_parent_quirk = True
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

			for name, value in block["inputs"].items():
				for child in _iter_input_block_refs(value):
					if child not in blocks:
						return f"{where}: input {name!r} points to missing block {child!r}"
					input_parents.setdefault(child, set()).add(bid)

		# next must be reciprocated by parent
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			nxt = block.get("next")
			if isinstance(nxt, str):
				child = blocks.get(nxt)
				if isinstance(child, dict) and child.get("parent") != bid:
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: next -> {nxt!r} but child.parent is {child.get('parent')!r}"
			parent = block.get("parent")
			if isinstance(parent, str) and not _orphan_parent_quirk:
				pb = blocks.get(parent)
				if not isinstance(pb, dict):
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: parent is not an object"
				if pb.get("next") != bid and parent not in input_parents.get(
					bid, set()
				):
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: parent {parent!r} does not reference this block"
			for child in input_parents.get(bid, set()):
				cb = blocks.get(bid)
				if isinstance(cb, dict) and cb.get("parent") != child:
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
	new_name = _final_asset_name(conv_name if old_name in conversions else old_name, opts)
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
	edges = _build_block_graph(target)
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
		custom = (block.get("inputs") or {}).get("custom_block") or [None, None]
		proto_id = custom[1] if len(custom) > 1 and isinstance(custom[1], str) else None
		proto = blocks.get(proto_id)
		proc = _procedure_key(proto) if isinstance(proto, dict) else None
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
				proc = _procedure_key(b)
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
		if (opts.remove_unreachable or opts.remove_unused_procedures) and ti < len(second_removed):
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
		and original[0] in (12, 13)
		and minified[0] == original[0]
		and minified[1] == ""
		and original[2] == minified[2]
	)


# Process-based verification is used only for large block tables. The block
# checker is mostly Python code, so threads cannot exploit multiple cores.
# Each worker receives one serialized target state and verifies a disjoint
# slice of that target's surviving blocks.
VERIFY_PARALLEL_MIN_BLOCKS = 3000
VERIFY_PARALLEL_MAX_WORKERS = 4
_VERIFY_WORKER_STATE = None
_VERIFY_WORKER_STATE_PATH = None

_VERIFY_BLOCK_OPT_ATTRS = (
    "comments", "positions", "covered", "remove_unreachable",
    "remove_unused_procedures", "compact_field_ids",
    "compact_mutation_hasnext", "compact_mutation_metadata",
    "rename_argument_ids", "strip_reference_names", "normalize_epsilon",
    "normalize_numbers", "optimize_json", "minimum_json", "compact_numeric_inputs",
    "inline_single_use_procedures",
)
_VERIFY_BLOCK_MAP_ATTRS = (
    "variable_setter_input_edits", "variable_setter_link_edits",
    "procedure_argument_input_edits", "procedure_argument_mutation_edits",
    "procedure_argument_removed_inputs",
    "folded_constant_expression_inputs", "folded_constant_expression_link_edits",
    "folded_constant_expression_opcode_edits",
    "grouped_sequence_input_edits", "grouped_sequence_link_edits",
    "inlined_procedure_input_edits", "inlined_procedure_link_edits",
    "script_rewrite_input_edits", "script_rewrite_link_edits",
    "script_rewrite_opcode_edits", "script_rewrite_removed_inputs",
    "simplified_block_input_edits", "simplified_block_link_edits",
    "simplified_block_opcode_edits", "folded_constant_variables",
    "folded_constant_variable_literals",
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
        # These two maps are indexed by the *owner* target of the folded
        # variable, not by the target containing the use. A stage/global
        # constant can therefore be folded inside another target, so filtering
        # them to target_index would make the verifier forget the legal rewrite.
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
            and all(_num_eq(x, y, opts.normalize_epsilon) or x == y for x, y in zip(bo[3:], bm[3:]))
        )
        if not (ok and (opts.positions or bo == bm)):
            return position, f"{where}, primitive block {bid!r} changed from {bo!r} to {bm!r}"
        return None

    err = _check_blocks(
        bo, bm, opts, f"{where}, block {bid!r}",
        None, remaining_blocks, target_index, bid
    )
    if err:
        return position, err
    if not isinstance(bm.get("inputs"), dict) or not isinstance(bm.get("fields"), dict):
        return position, f"{where}, block {bid!r} ({bm.get('opcode')}): inputs or fields is not a dict (violates Scratch VM format)"
    if "next" not in bm or "parent" not in bm:
        return position, f"{where}, block {bid!r} ({bm.get('opcode')}): missing required 'next' or 'parent' attribute"
    mu = bm.get("mutation")
    if mu is not None:
        if not isinstance(mu, dict):
            return position, f"{where}, block {bid!r} ({bm.get('opcode')}): mutation is not an object"
        if not getattr(opts, "compact_mutation_metadata", False) and ("tagName" not in mu or "children" not in mu):
            return position, f"{where}, block {bid!r} ({bm.get('opcode')}): lost required mutation tagName or children"
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
    remaining_blocks = set(minified_blocks)
    changed = []

    # The equality fast path is intentionally kept in the parent process. Most
    # Scratch projects contain far more unchanged blocks than changed blocks;
    # there is no reason to serialize and process those just to rediscover that
    # they are equal.
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
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_verify_worker, initargs=(state_path,)) as pool:
            for result in pool.map(_verify_block_chunk, bounds):
                if result is not None:
                    results.append(result)
        if results:
            return min(results, key=lambda item: item[0])[1]
    return None

def verify(original_path, minified_path, opts):
	if opts.lossless:
		return _verify_lossless(original_path, minified_path, opts.compact_block_defaults, opts.compact_costume_references, opts.compact_block_flags, opts.relabel_block_ids)
	with zipfile.ZipFile(original_path) as a, zipfile.ZipFile(minified_path) as b:
		# All project and asset members are read later; zipfile.read() validates their
		# CRCs, so a separate testzip() pass would decompress the same data again.
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
			_final_asset_name(conversions.get(name, name), opts)
			for name in orig_assets
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
						return (False, f"deduplicated asset byte mismatch: {name!r} -> {final_name!r}")
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
		orig = _reinflate(orig_raw)
		mini = _reinflate(mini_raw_project)
		opts._verify_project = orig

		err = _validate_asset_entries(mini, b, "minified project", minified_asset_cache)
		if err:
			return False, err
		err = _validate_asset_entries(orig, a, "original project", original_asset_cache)
		if err:
			return False, err

		conflicts = getattr(opts, "broadcast_repair_conflicts", {}) or {}
		if conflicts:
			first_id = sorted(conflicts)[0]
			return (
				False,
				f"broadcast {first_id!r} has conflicting reference names {conflicts[first_id]!r}; refusing to guess a definition",
			)

		if opts.rename_block_ids or opts.rename_variable_ids or opts.rename_list_ids or opts.rename_broadcast_ids:
			_restore_block_data_broadcast_ids(mini, opts)
		if opts.rename_argument_ids:
			_restore_argument_ids(mini, opts.renamed_argument_ids)
		if (
			opts.rename_identifiers
			or opts.rename_variable_names
			or opts.rename_list_names
			or opts.rename_broadcast_names
			or opts.rename_argument_names
			or opts.rename_procedure_names
		):
			_restore_identifier_names(mini, opts.renamed_identifiers)

		for key in set(orig) | set(mini):
			if key == "extensions" and getattr(opts, "remove_unused_extensions", False):
				original_ext = orig.get("extensions", [])
				minified_ext = mini.get("extensions", [])
				removed_ext = getattr(opts, "removed_extensions", set()) or set()
				expected_ext = [x for x in original_ext if x not in removed_ext]
				if minified_ext != expected_ext:
					return False, f"Top-level extensions changed unexpectedly: original {original_ext!r}, minified {minified_ext!r}, removed {sorted(removed_ext)!r}"
				continue
			if key not in ("targets", "monitors") and orig.get(key) != mini.get(key):
				if key == "meta" and opts.remove_project_meta:
					mo = orig.get("meta") or {}
					mm = mini.get("meta") or {}
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
		mini_var_refs, mini_list_refs, mini_bcast_refs = _collect_data_references(
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
					if opts.normalize_numbers and _num_eq(to[k], tm[k], opts.normalize_epsilon):
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
			if missing_vars and not opts.remove_unused_variables:
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
					if getattr(opts, "compact_data_literals", False) and _compact_data_entry_equal(vo, vm):
						continue
					if (
						opts.normalize_numbers
						and isinstance(vo, list)
						and isinstance(vm, list)
						and len(vo) == len(vm)
						and vo[0] == vm[0]
						and (_num_eq(vo[1], vm[1], opts.normalize_epsilon) or vo[1] == vm[1])
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
			if missing_lists and not opts.remove_unused_lists:
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
				if getattr(opts, "compact_data_literals", False) and _compact_data_entry_equal(lo, lm):
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
					and all(x == y or _num_eq(x, y, opts.normalize_epsilon) for x, y in zip(lo[1], lm[1]))
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
				allowed_new_blocks = set(allowed_new_blocks) | set(group_new_block_sets[ti])
			script_new_block_sets = getattr(opts, "script_rewrite_new_blocks", None) or []
			if ti < len(script_new_block_sets):
				allowed_new_blocks = set(allowed_new_blocks) | set(script_new_block_sets[ti])
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
				_mini_blocks = tm.get("blocks") or {}
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

			if _check_argument_id_consistency(to, name) is None:
				err = _check_argument_id_consistency(tm, name)
				if err:
					return False, err

		err = _check_monitors(orig, mini, opts)
		if err:
			return False, err
		err = _check_broadcast_consistency(mini)
		if err:
			return False, err
		err = _validate_block_structure(mini, "minified project (final)")
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


def humanize(n):
	return f"{n/1048576:.4f} MiB" if n >= 1048576 else f"{n/1024:.4f} KiB"


STAT_GROUPS = [
	(
		"1.",
		"Drop topLevel:false / shadow:false; normalize mutation.warp",
		[
			("topLevel", "topLevel:false dropped"),
			("shadow", "shadow:false dropped"),
			("warp", "mutation.warp normalized to boolean"),
		],
	),
	(
		"2.",
		"WAV -> MP3 conversion",
		[
			("wav_converted", "sounds converted"),
			("wav_bytes_saved", "asset bytes saved"),
			("wav_conversion_failed", "conversions failed"),
			("wav_ffmpeg_unavailable", "ffmpeg unavailable"),
		],
	),
	(
		"2a.",
		"Deduplicate identical assets",
		[
			("assets_deduplicated", "duplicate asset files removed"),
			("asset_bytes_saved", "raw asset bytes removed"),
		],
	),
	(
		"3.",
		"Strip sprite comments",
		[
			("comments", "sprite comments removed"),
			("comment_links", "block comment links removed"),
		],
	),
	(
		"4.",
		"Round positions",
		[("rounded", "position values rounded")],
	),
	(
		"5.",
		"Reset covered shadow values",
		[("covered", "covered values reset")],
	),
	(
		"6.",
		"Clean monitors",
		[
			("monitors_orphan", "orphaned monitors removed"),
			("monitors_unused", "unused monitors removed"),
			("monitor_params", "list-monitor params cleared"),
			("monitor_value", "monitor values normalized"),
		],
	),
	(
		"7.",
		"Clear lists selected at the prompt",
		[
			("lists_cleared", "lists cleared"),
			("list_items_cleared", "list items removed"),
		],
	),
	(
		"8.",
		"Remove unreachable blocks (1st pass)",
		[("unreachable_blocks_removed_first", "blocks removed")],
	),
	(
		"9.",
		"Remove unused procedures",
		[
			("procedures_removed", "procedures removed"),
			("procedure_blocks_removed", "procedure blocks removed"),
			("procedure_dangling_block_refs_fixed", "block references repaired"),
		],
	),
	(
		"10.",
		"Remove unreachable blocks (2nd pass)",
		[("unreachable_blocks_removed_second", "blocks removed")],
	),
	(
		"11.",
		"Fold selected constant variable reporters",
		[
			("constant_variable_reporters", "constant variable reporters folded"),
			("constant_variable_bytes_saved", "variable-reporter JSON bytes saved"),
			("constant_variable_setters_removed", "safe matching setters removed"),
			("constant_variable_setters_kept", "setters kept"),
		],
	),
	(
		"12.",
		"Fold constant expressions",
		[
			("constant_expressions_folded", "constant expressions folded"),
			("boolean_constants_propagated", "boolean constants propagated"),
			("algebraic_simplifications", "algebraic simplifications"),
			("demorgan_rewrites", "De Morgan rewrites"),
		],
	),
	(
		"13.",
		"Remove unused variables and lists",
		[
			("variables_removed", "unused variables removed"),
			("lists_removed", "unused lists removed"),
		],
	),
	(
		"14.",
		"Repair dangling broadcast references",
		[
			("broadcast_refs_repaired", "missing broadcast definitions restored"),
			("broadcast_ref_conflicts", "conflicting broadcast references"),
		],
	),
	(
		"15.",
		"Remove unused broadcasts",
		[("broadcasts_removed", "unused broadcasts removed")],
	),
	(
		"16.",
		"Rename IDs",
		[
			("variable_ids", "variable IDs renamed"),
			("list_ids", "list IDs renamed"),
			("broadcast_ids", "broadcast IDs renamed"),
			("argument_ids", "argument IDs renamed"),
			("block_ids", "block IDs renamed"),
		],
	),
	(
		"17.",
		"Compact numeric inputs, field IDs, mutation hasnext/metadata",
		[
			("numeric_inputs_compacted", "numeric string inputs compacted"),
			("field_ids_compacted", "redundant null field IDs removed"),
			("mutation_hasnext_compacted", "mutation hasnext=false removed"),
			("mutation_metadata_compacted", "mutation metadata JSON compacted"),
		],
	),
	(
		"18.",
		"Normalize numbers",
		[("numbers_normalized", "integral numbers normalized")],
	),
	(
		"19.",
		"Remove sound rate/sampleCount",
		[("sound_metadata_removed", "sound metadata fields removed")],
	),
	(
		"20.",
		"Remove empty fields/inputs, costume metadata, default properties, empty containers, project meta",
		[
			("empty_fields_removed", "empty block fields removed"),
			("empty_inputs_removed", "empty block inputs removed"),
			("costume_metadata_removed", "redundant costume metadata removed"),
			("default_target_properties_removed", "default target properties removed"),
			("empty_containers_removed", "empty target containers removed"),
			("project_meta_cleaned", "project meta fields cleaned"),
		],
	),
	(
		"21.",
		"Repair dangling block links",
		[("dangling_block_refs_fixed", "dangling block references repaired")],
	),
]


def _print_transform_stats(stats, opts):
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
					("sequence_groups_rejected_size", "candidate groups rejected for size"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.simplify_boolean_control:
				print(Ansi.subheading("  12c. Simplify constant boolean control flow"))
				for key, label in (
					("boolean_control_simplifications", "constant control blocks simplified"),
					("control_blocks_removed", "control blocks removed"),
					("control_branch_blocks_removed", "branch/condition blocks removed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.optimize_procedure_arguments:
				print(Ansi.subheading("  12d. Optimize custom procedure arguments"))
				for key, label in (
					("procedure_arguments_removed", "procedure arguments removed"),
					("procedure_constant_arguments_folded", "constant procedure arguments folded"),
					("procedure_argument_reporters_removed", "argument reporter blocks removed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.merge_duplicate_procedures:
				print(Ansi.subheading("  12e. Merge duplicate custom procedures"))
				for key, label in (
					("duplicate_procedures_merged", "duplicate procedures merged"),
					("duplicate_procedure_blocks_removed", "duplicate procedure blocks removed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			if opts.inline_single_use_procedures:
				print(Ansi.subheading("  12f. Inline safe single-use procedures"))
				for key, label in (
					("procedure_inlined", "single-use procedures inlined"),
					("procedure_inline_blocks_removed", "procedure scaffolding blocks removed"),
					("procedure_inline_argument_reporters_removed", "argument reporters removed during inlining"),
					("procedure_inline_passes", "inlining passes completed"),
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


def _verify_lossless(original_path, minified_path, compact_defaults=False, compact_costume_references=False, compact_block_flags=False, relabel_block_ids=False):
	try:
		with zipfile.ZipFile(original_path) as original, zipfile.ZipFile(minified_path) as result:
			for archive in (original, result):
				_, error = _zip_entry_names(archive)
				if error:
					return False, error
			if original.namelist() != result.namelist():
				return False, "archive entries or their order changed"
			if original.comment != result.comment:
				return False, "archive comment changed"
			left = loads_exact(original.read("project.json"))
			right = loads_exact(result.read("project.json"))
			difference = exact_difference(left, right, compact_defaults, compact_costume_references, compact_block_flags, relabel_block_ids)
			if difference:
				return False, difference
			for info in original.infolist():
				other = result.getinfo(info.filename)
				for field in ("date_time", "comment", "extra", "external_attr", "internal_attr", "create_system"):
					if getattr(info, field) != getattr(other, field):
						return False, f"ZIP metadata {field} changed: {info.filename!r}"
				if info.filename != "project.json" and original.read(info) != result.read(other):
					return False, f"asset byte-for-byte mismatch: {info.filename!r}"
		if compact_block_flags or relabel_block_ids:
			return True, "approved block representation changes verified; every other JSON value, execution order, ZIP metadata and asset byte preserved"
		return True, "every JSON value, collection order, ZIP metadata and asset byte preserved"
	except (ValueError, OSError, KeyError, zipfile.BadZipFile, zlib.error) as error:
		return False, str(error)


def _minify_lossless_sb3(src, dst, opts):
	temporary = None
	try:
		zopfli = get_zopfli(opts.zopfli_required) if (opts.zopfli or opts.zopfli_assets) else None
		with zipfile.ZipFile(src) as source:
			_, error = _zip_entry_names(source)
			if error:
				raise ValueError(error)
			raw = source.read("project.json")
			project = loads_exact(raw)
			if not isinstance(project, dict) or not isinstance(project.get("targets"), list):
				raise ValueError("project.json must contain a project object with a targets array")
			print(Ansi.heading("Optimizing project.json (exact values and collection order preserved)..."), flush=True)
			out_json, compressed, stats = optimize_project_json(
				project, opts.compression_level, opts.zopfli, opts.zopfli_iterations, opts.zopfli_required,
				opts.compact_block_defaults, opts.compact_costume_references, opts.compact_block_flags,
				opts.minimum_json, opts.json_search_rounds, opts.relabel_block_ids
			)
			json_info = source.getinfo("project.json")
			original_wins = ((len(raw), json_info.compress_size) <= (len(out_json), len(compressed))) if opts.minimum_json else json_info.compress_size <= len(compressed)
			if json_info.compress_type == zipfile.ZIP_DEFLATED and original_wins:
				out_json = raw
				compressed = read_compressed_entry(source, json_info)
				stats["json_encoding"] = "original entry (already smaller)"
			new_json_info = copy.copy(json_info)
			new_json_info.compress_type = zipfile.ZIP_DEFLATED
			new_json_info.file_size = len(out_json)
			new_json_info.CRC = zlib.crc32(out_json)
			with tempfile.NamedTemporaryFile(prefix=".minify-", suffix=".sb3", dir=os.path.dirname(os.path.abspath(dst)), delete=False) as handle:
				temporary = handle.name
			with zipfile.ZipFile(temporary, "w") as output:
				output.comment = source.comment
				print(Ansi.heading("Optimizing assets without changing their bytes..."), flush=True)
				entries = source.infolist()
				last_progress = time.monotonic()
				for index, info in enumerate(entries, 1):
					if info.filename == "project.json":
						write_compressed_entry(output, new_json_info, compressed)
						continue
					data = source.read(info)
					previous = read_compressed_entry(source, info)
					method, encoded = info.compress_type, previous
					if opts.optimize_assets and method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
						method, encoded = optimize_asset(
							data, opts.compression_level, (method, previous),
							zopfli if opts.zopfli_assets else False, opts.zopfli_iterations
						)
					if len(encoded) < len(previous):
						stats["assets_recompressed"] = stats.get("assets_recompressed", 0) + 1
						stats["asset_deflate_bytes_saved"] = stats.get("asset_deflate_bytes_saved", 0) + len(previous) - len(encoded)
					new_info = copy.copy(info)
					new_info.compress_type = method
					write_compressed_entry(output, new_info, encoded)
					if time.monotonic() - last_progress >= 20:
						print(f"  Entries checked: {index:,}/{len(entries):,}; asset bytes saved: {stats.get('asset_deflate_bytes_saved', 0):,}", flush=True)
						last_progress = time.monotonic()
			print(Ansi.heading("Verifying exact project data and every asset byte..."), flush=True)
			ok, message = _verify_lossless(src, temporary, opts.compact_block_defaults, opts.compact_costume_references, opts.compact_block_flags, opts.relabel_block_ids)
			if not ok:
				raise ValueError(f"lossless verification failed: {message}")
		os.replace(temporary, dst)
		temporary = None
		print(f'Input : "{src}"\nOutput: "{dst}"')
		print(f"JSON encoding: {stats['json_encoding']} ({stats['json_encoding_trials']} measured trials)")
		for line in format_size_report(len(raw), len(out_json), os.path.getsize(src), os.path.getsize(dst)):
			print(line)
		print(f"JSON DEFLATE : {json_info.compress_size:,} -> {len(compressed):,} bytes")
		floor = stats["json_minimum"]["minimum_bytes"]
		print(f"JSON lower bound under selected model: {floor:,} bytes; gap: {len(out_json) - floor:,} bytes (fixed representation model)")
		print(f"asset DEFLATE: {stats.get('asset_deflate_bytes_saved', 0):,} bytes saved across {stats.get('assets_recompressed', 0):,} assets")
		if opts.all_lossless and not zopfli:
			print("Optional Zopfli was unavailable; all built-in lossless methods ran. Install zopfli for stronger compression.")
		print(Ansi.success(f"Verified successfully: {message}."))
		return 0
	except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, zlib.error) as error:
		print(Ansi.error(f"Error: {error}"))
		return 1
	finally:
		if temporary and os.path.exists(temporary):
			os.remove(temporary)


def minify_sb3(src, dst, opts=None):
	opts = opts or Options()
	if os.path.normcase(os.path.realpath(src)) == os.path.normcase(os.path.realpath(dst)) or (
		os.path.exists(src) and os.path.exists(dst) and os.path.samefile(src, dst)
	):
		print(Ansi.error("Error: output path must differ from input path."))
		return 1
	if not os.path.isfile(src):
		print(Ansi.error(f'Error: file not found: "{src}"'))
		return 1
	if opts.lossless:
		return _minify_lossless_sb3(src, dst, opts)
	try:
		zopfli = get_zopfli(opts.zopfli_required) if (opts.zopfli or opts.zopfli_assets) else None
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
			candidates = _find_constant_variables(project)
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
		progress = ProgressBar(_progress_stage_count(opts))
		stats = apply_transforms(project, opts, assets, progress)
		if opts.relabel_block_ids:
			progress.next("Minimize block IDs")
			project, label_maps, label_cost = relabel_blocks(project)
			previous_maps = getattr(opts, "renamed_block_ids", {})
			opts.renamed_block_ids = {
				ti: {old: label_maps.get(ti, {}).get(new, new) for old, new in previous_maps[ti].items()}
				if ti in previous_maps else mapping for ti, mapping in label_maps.items()
			}
			opts.rename_block_ids = True
			stats["block_id_bytes_saved"] = label_cost["before"] - label_cost["after"]
		out_json = dumps_compact(project, opts.sort_keys).encode("utf-8")
		compressed_json = None
		if opts.optimize_json:
			progress.next("Optimize project.json")
			out_json, compressed_json, encoding_stats = optimize_project_json(
				loads_exact(out_json), opts.compression_level, opts.zopfli,
				opts.zopfli_iterations, opts.zopfli_required, opts.compact_block_defaults,
			opts.compact_costume_references, opts.compact_block_flags, opts.minimum_json, opts.json_search_rounds,
			False, verify_encoding=False, already_relabelled=opts.relabel_block_ids
		)
			stats.update({key: value for key, value in encoding_stats.items() if isinstance(value, int)})

		progress.next("Process assets and write archive" if opts.optimize_assets else "Write archive")
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
				if opts.optimize_assets and time.monotonic() - last_progress_render >= 0.15:
					progress.detail(f"{asset_index:,}/{asset_count:,} assets")
					last_progress_render = time.monotonic()
				if opts.optimize_assets:
					# PNG/JPEG/GIF/WebP/MP3/OGG containers are already internally compressed.
					# Preserve their existing ZIP stream instead of spending CPU on DEFLATE/Zopfli.
					_ext = os.path.splitext(name)[1].lower()
					if _ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".ogg", ".m4a", ".aac") and name in asset_infos:
						info = copy.copy(asset_infos[name])
						previous = read_compressed_entry(zin, info)
						write_compressed_entry(zout, info, previous)
						continue
					info = copy.copy(asset_infos[name]) if name in asset_infos else zipfile.ZipInfo(name)
					original = None
					if name in asset_infos and zin.read(name) == data:
						original = (info.compress_type, read_compressed_entry(zin, info))
					asset_zopfli = (
						zopfli
						if opts.zopfli_assets and (not opts.auto_zopfli or opts.zopfli_required)
						else False
					)
					method, compressed = optimize_asset(
						data, opts.compression_level, original,
						asset_zopfli, opts.zopfli_iterations
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
	_print_transform_stats(stats, opts)

	b, a = len(raw), len(out_json)
	print("")
	for line in format_size_report(b, a, os.path.getsize(src), os.path.getsize(dst)):
		print(line)
	return 0


if __name__ == "__main__":
	from minify_flags import toggles, valued

	flags = [a for a in sys.argv[1:] if a.startswith("--")]
	args = [a for a in sys.argv[1:] if not a.startswith("--")]
	values, bad = {}, []

	for f in flags:
		key, eq, val = f.partition("=")
		if key in toggles and not eq:
			continue
		if key in valued and eq:
			if key == "--normalize-epsilon":
				try:
					n = float(val)
				except ValueError:
					n = 0.0
				if math.isfinite(n) and n > 0:
					values[key] = n
					continue
			else:
				if not val.isdigit():
					bad.append(f)
					continue
				n = int(val)
				if key == "--compression-level" and 0 <= n <= 9:
					values[key] = n
					continue
				if key != "--compression-level" and n > 0:
					values[key] = n
					continue
		bad.append(f)
	if bad:
		print(Ansi.error(f"Unknown or malformed option(s): {bad}"))
		print(
			Ansi.muted(
				f"Valid: {sorted(toggles)} and --list-bytes=N --list-items=N --sequence-threshold=N --zopfli-iterations=N --json-search-rounds=N --procedure-inline-passes=N (positive), --compression-level=N (0-9), --normalize-epsilon=N (positive)"
			)
		)
		sys.exit(1)
	if not args:
		print(Ansi.error("Path and flags are not specified."))
		print(Ansi.error("Usage: ") + Ansi.warning("python minify_sb3.py path/to/project.sb3 --flags"))
		sys.exit(1)
	
	all_flags = "--all-flags" in flags
	all_optimizations = "--all-optimizations" in flags or all_flags
	all_safe = any(flag in flags for flag in ("--all-lossless", "--all-safe", "--all-safe-flags"))
	lossless = "--lossless" in flags or all_safe
	if lossless:
		allowed = {
			"--lossless", "--all-lossless", "--optimize-json", "--optimize-assets",
			"--zopfli", "--zopfli-assets", "--zopfli-iterations", "--compression-level",
			"--compact-block-defaults", "--compact-costume-references", "--compact-block-flags",
			"--all-safe", "--all-safe-flags", "--minimum-json", "--json-search-rounds", "--relabel-block-ids",
			"--keep-comments", "--keep-positions", "--keep-covered", "--keep-monitors",
			"--keep-sound-metadata", "--preserve-asset-compression", "--compress-assets",
		}
		conflicts = [flag for flag in flags if flag.partition("=")[0] not in allowed]
		if conflicts:
			print(Ansi.error(f"Lossless mode is incompatible with: {', '.join(conflicts)}"))
			sys.exit(1)
	opts = Options(
		lossless=lossless,
		all_lossless=all_safe,
		compact_costume_references=all_optimizations or "--compact-costume-references" in flags,
		compact_block_flags=all_flags or "--compact-block-flags" in flags,
		minimum_json=all_flags or "--minimum-json" in flags,
		json_search_rounds=values.get("--json-search-rounds", 1 if all_optimizations else 0),
		relabel_block_ids=all_flags or "--relabel-block-ids" in flags,
		auto_zopfli=all_flags,
		optimize_json=all_optimizations or "--optimize-json" in flags,
		optimize_assets=all_optimizations or "--optimize-assets" in flags,
		compact_block_defaults=all_optimizations or "--compact-block-defaults" in flags,
		zopfli="--zopfli" in flags,
		zopfli_assets="--zopfli-assets" in flags,
		zopfli_iterations=values.get("--zopfli-iterations", 5),
		comments=("--keep-comments" not in flags),
		positions=("--keep-positions" not in flags),
		covered=("--keep-covered" not in flags),
		monitors=("--keep-monitors" not in flags),
		lists=("--clear-large-lists" in flags),
		rename_block_ids=all_optimizations
		or "--rename-block-ids" in flags
		or "--frequency-block-ids" in flags
		or "--order-block-ids-by-frequency" in flags,
		rename_variable_ids=all_optimizations
		or "--rename-variable-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_list_ids=all_optimizations
		or "--rename-list-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_broadcast_ids=all_optimizations
		or "--rename-broadcast-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_identifiers="--rename-identifiers" in flags,
		rename_variable_names="--rename-variable-names" in flags,
		rename_list_names="--rename-list-names" in flags,
		rename_broadcast_names="--rename-broadcast-names" in flags,
		rename_argument_names="--rename-argument-names" in flags,
		rename_procedure_names="--rename-procedure-names" in flags,
		rename_argument_ids=all_optimizations or "--rename-argument-ids" in flags,
		remove_unused_variables=all_optimizations
		or "--remove-unused-variables" in flags,
		remove_unused_lists=all_optimizations or "--remove-unused-lists" in flags,
		remove_unused_broadcasts=all_optimizations
		or "--remove-unused-broadcasts" in flags,
		remove_unreachable=all_optimizations or "--remove-unreachable" in flags,
		remove_unused_procedures=all_optimizations
		or "--remove-unused-procedures" in flags,
		normalize_numbers=all_optimizations or "--normalize-numbers" in flags,
		remove_empty_fields=all_optimizations or "--remove-empty-fields" in flags,
		remove_empty_inputs=all_optimizations or "--remove-empty-inputs" in flags,
		remove_costume_metadata=all_optimizations
		or "--remove-costume-metadata" in flags,
		remove_default_target_properties=all_optimizations
		or "--remove-default-target-properties" in flags,
		remove_empty_target_containers=all_optimizations
		or "--remove-empty-containers" in flags
		or "--remove-empty-target-containers" in flags,
		remove_project_meta=all_optimizations or "--remove-project-meta" in flags,
		preserve_asset_compression=all_optimizations
		or "--preserve-asset-compression" in flags,
		frequency_block_ids=all_optimizations
		or "--frequency-block-ids" in flags
		or "--order-block-ids-by-frequency" in flags,
		frequency_data_ids=all_optimizations
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		compact_numeric_inputs=all_optimizations or "--compact-numeric-inputs" in flags,
		compact_field_ids=all_optimizations or "--compact-field-ids" in flags,
		compact_mutation_hasnext=all_optimizations
		or "--compact-mutation-hasnext" in flags,
		compact_mutation_metadata="--compact-mutation-metadata" in flags,
		fold_constant_variables="--fold-constant-variables" in flags,
		fold_constant_expressions=all_optimizations
		or "--fold-constant-expressions" in flags,
		simplify_boolean_control=all_optimizations or "--simplify-boolean-control" in flags,
		simplify_blocks=all_optimizations or "--simplify-blocks" in flags,
		deduplicate_assets=all_optimizations or "--deduplicate-assets" in flags,
		optimize_procedure_arguments=all_optimizations or any(
			f in flags
			for f in (
				"--optimize-procedure-arguments",
				"--optimize-custom-procedure-arguments",
				"--drop-procedure-arguments",
				"--remove-unused-procedure-arguments",
				"--remove-unused-custom-procedure-arguments",
				"--fold-constant-procedure-arguments",
				"--fold-constant-custom-procedure-arguments",
			)
		),
		merge_duplicate_procedures=all_optimizations or any(
			f in flags
			for f in (
				"--merge-duplicate-procedures",
				"--merge-duplicate-custom-procedures",
				"--merge-duplicate-custom-blocks",
				"--deduplicate-procedures",
			)
		),
		inline_single_use_procedures=all_optimizations or any(
			f in flags
			for f in (
				"--inline-single-use-procedures",
				"--inline-single-use-custom-procedures",
				"--procedure-inlining",
			)
		),
		procedure_inline_passes=values.get("--procedure-inline-passes", 8),
		branch_swapping=all_optimizations or any(f in flags for f in ("--branch-swapping", "--swap-branches", "--branch-swap")),
		trivial_loops=all_optimizations or any(f in flags for f in ("--trivial-loops", "--simplify-trivial-loops")),
		nested_conditionals=all_optimizations or any(f in flags for f in ("--nested-conditionals", "--merge-nested-conditionals", "--merge-nested-ifs")),
		associative_constant_merging=any(f in flags for f in ("--associative-constants", "--merge-associative-constants", "--reassociate-constants")),
		script_constant_propagation=all_optimizations or any(f in flags for f in ("--script-constant-propagation", "--propagate-script-constants", "--constant-propagation")),
		strip_reference_names=any(f in flags for f in ("--strip-reference-names", "--drop-reference-names", "--empty-reference-names")),
		compact_data_literals=any(f in flags for f in ("--compact-data-literals", "--numeric-data-literals", "--convert-numeric-data")),
		remove_unused_extensions=any(f in flags for f in ("--remove-unused-extensions", "--unused-extensions")),
		group_similar_sequences="--group-similar-sequences" in flags,
		sequence_threshold=values.get("--sequence-threshold", 3),
		compress_assets=all_optimizations or "--compress-assets" in flags,
		convert_wav_to_mp3=all_flags or "--convert-wav-to-mp3" in flags,
		sort_keys="--sort-keys" in flags,
		compression_level=values.get("--compression-level", 9),
		list_bytes=values.get("--list-bytes", DEFAULT_LIST_BYTES),
		list_items=values.get("--list-items", DEFAULT_LIST_ITEMS),
		normalize_epsilon=values.get("--normalize-epsilon", DEFAULT_EPSILON),
		keep_sound_metadata=("--keep-sound-metadata" in flags),
	)
	dst = args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + "_minified.sb3"
	if os.path.abspath(args[0]) == os.path.abspath(dst):
		print(Ansi.error("Error: output path must differ from input path."))
		sys.exit(1)
	sys.exit(minify_sb3(args[0], dst, opts))
