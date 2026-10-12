import copy
import hashlib
import os
import zlib

from .json_layout import (
	JsonNumber,
	_number_components,
	copy_json,
	dumps_exact,
	get_shortest_num_repr,
	is_record_order_irrelevant,
	loads_exact,
	quote,
	BLOCK_KEY_ORDERS
)
from .metadata import (
	_SYNC_REPORTERS,
	_rebuild_procedure_displays,
	_restore_editor_metadata,
	_terminal_link_ids,
	_covered_shadow_slots,
	_compact_procedure_symbols,
	_editor_position_keys,
	_editor_comment_ids,
	_unused_data_ids,
	_restore_unused_data,
	_restore_procedure_symbols,
	_restore_covered_shadows,
	restore_ref_names,
	get_orphan_argument_ids,
)
from .output import _render_layout
from .tags import (
	INPUT_SAME_BLOCK_SHADOW,
	INPUT_BLOCK_NO_SHADOW,
	INPUT_DIFF_BLOCK_SHADOW,
	INPUT_TAGS,
	_input_tag
)
from .transforms import strip_reference_names
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from itertools import permutations, product
from typing import Any, NoReturn


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
		original = prune_orphan_arguments(original, result)

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
		project = prune_orphan_arguments(project)

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


def _window_layout(project, orders, level, width=128):
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
		lookahead = _render_layout(parts[end : end + 16], shapes=shapes)[:4096]

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


def _screen_record_orders(project, level):
	parts = _layout_parts(_sample_blocks(project))
	shapes, scores = {}, []

	for order in permutations(("opcode", "next", "parent", "inputs", "fields")):
		scores.append(
			(len(deflate(_render_layout(parts, order, shapes), level)), order)
		)

	return [order for _, order in sorted(scores)[:4]], len(scores)


def _specialized_layout(project, orders, level, grouping):
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
		if len(members) > 96:
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
):
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
		((), 0) if fast_json else _screen_record_orders(search_project, level)
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
			search_project, orders, level, grouping
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


BLOCK_ID_ALPHABET = "!@#$%^*()+_-={}|[]:;?,./~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
