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


def is_hat_block(block):
	if not isinstance(block, dict):
		return False

	op = block.get("opcode", "")
	return (
		op.endswith("_whenflagclicked")
		or op.endswith("_whenkeypressed")
		or (op.startswith("event_when") or op.startswith("control_start_as_clone"))
	)


class ProjectGraph:
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
					for ref in set(iter_input_block_refs(value)):
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
				for ref in set(iter_input_block_refs(value)):
					if ref in blocks:
						new_out.add(ref)

			self.edges[bid] = new_out

			for ref in new_out:
				self.parents.setdefault(ref, set()).add(bid)
				self.incoming[ref] += 1

		return self

	def refresh_after_mutation(self, changed=(), removed=()):
		removed = set(removed)
		changed = set(changed) - removed

		for bid in removed:
			changed.update(self.parents.get(bid, ()))

		changed.difference_update(removed)

		for bid in removed:
			for ref in self.edges.get(bid, ()):
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

			self.edges.pop(bid, None)
			self.parents.pop(bid, None)
			self.incoming.pop(bid, None)

		self.refresh_blocks(changed)
		return self


def build_block_graph(target, graph=None) -> dict:
	if graph is None:
		graph = ProjectGraph(target)

	return graph.edges


def repair_dangling_block_ref(value, blocks) -> tuple[list, bool]:
	if not isinstance(value, list) or not value:
		return value, False

	tag = value[0]
	if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		if len(value) <= 1:
			return value, False

		ref = value[1]

		if isinstance(ref, str):
			if ref in blocks:
				return value, False

			return None, True

		if isinstance(ref, (list, dict)):
			fixed, changed = repair_dangling_block_ref(ref, blocks)

			if fixed is None:
				return None, True

			if changed:
				value[1] = fixed

			return value, changed

		return value, False

	if tag == INPUT_DIFF_BLOCK_SHADOW:
		changed = False
		if len(value) > 1 and isinstance(value[1], str):
			if value[1] not in blocks:
				if len(value) > 2:
					shadow = value[2]

					if isinstance(shadow, str) and shadow not in blocks:
						return None, True

					# preserve shadow value
					return [INPUT_SAME_BLOCK_SHADOW, shadow], True

				return None, True

		if len(value) > 2:
			shadow = value[2]
			if isinstance(shadow, str) and shadow not in blocks:
				return [value[0], value[1]], True
			if isinstance(shadow, (list, dict)):
				fixed, shadow_changed = repair_dangling_block_ref(shadow, blocks)

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
			fixed, sub_changed = repair_dangling_block_ref(v, blocks)

			if fixed is None:
				continue

			if sub_changed:
				value[i] = fixed
				changed = True

	return value, changed


def is_orphan_arg_reporter(block, blocks) -> bool:
	if not isinstance(block, dict):
		return False

	return (
		block.get("opcode", "").startswith("argument_reporter_")
		and block.get("shadow") is True
		and isinstance(block.get("parent"), (NoneType, str))
		and (block.get("parent") is None or block.get("parent") not in blocks)
	)


def iter_input_block_refs(value):
	if not isinstance(value, list) or not value:
		return

	tag = value[0]
	if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		if len(value) > 1:
			ref = value[1]
			if isinstance(ref, str):
				yield ref
			elif isinstance(ref, (list, dict)):
				yield from iter_input_block_refs(ref)
		return
	if tag == INPUT_DIFF_BLOCK_SHADOW:
		for ref in value[1:3]:
			if isinstance(ref, str):
				yield ref
			elif isinstance(ref, (list, dict)):
				yield from iter_input_block_refs(ref)
		return
	for item in value[1:]:
		if isinstance(item, (list, dict)):
			yield from iter_input_block_refs(item)


def repair_dangling_block_refs(
	target: dict[str, Any], preserve_comments: bool = False, preserve_refs=()
):
	blocks = target.get("blocks", {})
	available = blocks.keys() | set(preserve_refs)
	fixed = 0

	# remove unresolved refs
	for block in blocks.values():
		if not isinstance(block, dict):
			continue

		for key in ("next", "parent"):
			ref = block.get(key)

			if isinstance(ref, str) and ref not in available:
				if key == "parent" and is_orphan_arg_reporter(block, blocks):
					continue

				block[key] = None
				fixed += 1

		inputs = block.get("inputs")
		if not isinstance(inputs, dict):
			continue

		for name in list(inputs):
			fixed_value, changed = repair_dangling_block_ref(inputs[name], available)

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
			for child_id in iter_input_block_refs(value):
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

	comments = {} if preserve_comments else target.get("comments") or {}
	for cid in list(comments):
		comment = comments[cid]

		if (
			isinstance(comment, dict)
			and comment.get("blockId") is not None
			and comment["blockId"] not in blocks
		):
			del comments[cid]
	return fixed


def remove_unreachable_blocks(project, stats, stat_key="unreachable_blocks_removed"):
	removed = 0

	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		roots = {
			bid
			for bid, b in blocks.items()
			if isinstance(b, list) or (isinstance(b, dict) and b.get("topLevel"))
		}
		graph = ProjectGraph(target)
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


def _new_id():
	return str(uuid.uuid4()).replace("-", "")[:20]


def procedure_key(block):
	mut = block.get("mutation") if isinstance(block, dict) else None
	if not isinstance(mut, dict):
		return None
	return mut.get("proccode")


def _sequence_reporter_block(block) -> bool:
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

	return op in {
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
	}


def _sequence_input_refs(value, blocks):
	for ref in iter_input_block_refs(value):
		if ref in blocks:
			yield ref


def _sequence_inline_parameterizable(item) -> bool:
	if item is None:
		return True

	if not isinstance(item, list):
		return False

	if len(item) == 2 and item[0] in NUMERIC_TAGS + (PRIMITIVE_TEXT,):
		return True

	if len(item) >= 3 and item[0] in (
		PRIMITIVE_BROADCAST,
		PRIMITIVE_VARIABLE,
		PRIMITIVE_LIST,
	):
		return all(isinstance(x, (str, int, float, bool, NoneType)) for x in item[1:])

	return False


def _sequence_input_parameterizable(value, blocks) -> bool:
	if not isinstance(value, list) or not value:
		return False

	refs = list(_sequence_input_refs(value, blocks))

	if refs:
		return all(_sequence_reporter_block(blocks.get(ref)) for ref in refs)

	if value[0] in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		return len(value) >= 2 and _sequence_inline_parameterizable(value[1])

	if value[0] == INPUT_DIFF_BLOCK_SHADOW:
		if len(value) < 2:
			return False
		return all(
			item is None
			or _sequence_inline_parameterizable(item)
			or (isinstance(item, str) and item in blocks)
			for item in value[1:]
		)

	return False


def _sequence_has_unsafe_nested_inputs(block, blocks) -> bool:
	for value in (block.get("inputs") or {}).values():
		for ref in _sequence_input_refs(value, blocks):
			if not _sequence_reporter_block(blocks.get(ref)):
				return True

	return False


def _sequence_block_signature(
	block: Any, blocks: Any
) -> tuple[str, str, str | None, tuple] | None:
	if not isinstance(block, dict):
		return None

	op = block.get("opcode")
	if not isinstance(op, str) or not op:
		return None

	if op.startswith(("event_", "procedures_", "argument_reporter_")):
		return None

	if op in {"control_stop", "control_delete_this_clone"}:
		return None

	if _sequence_has_unsafe_nested_inputs(block, blocks):
		return None

	fields = dumps_compact(block.get("fields") or {})
	mutation = (
		dumps_compact(block.get("mutation"))
		if block.get("mutation") is not None
		else None
	)

	inputs_sig = []

	for name in sorted((block.get("inputs") or {}).keys()):
		value = block["inputs"][name]
		if not is_boolean_slot(block, name) and _sequence_input_parameterizable(
			value, blocks
		):
			inputs_sig.append((name, "PARAM"))
		else:
			inputs_sig.append((name, "FIXED", dumps_compact(value)))

	return (op, fields, mutation, tuple(inputs_sig))


def get_sequence_runs(blocks) -> list[list[str]]:
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


def _sequence_compare(
	base, other, blocks: dict[str, Any]
) -> list[tuple[int, str]] | None:
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

		if (a.get("fields", {})) != (b.get("fields", {})):
			return None

		if a.get("mutation") != b.get("mutation"):
			return None

		a_inputs = a.get("inputs", {})
		b_inputs = b.get("inputs", {})

		if set(a_inputs) != set(b_inputs):
			return None

		for name in a_inputs:
			a_value, b_value = a_inputs[name], b_inputs[name]
			if a_value == b_value:
				continue

			if is_boolean_slot(a, name) or not (
				_sequence_input_parameterizable(a_value, blocks)
				and _sequence_input_parameterizable(b_value, blocks)
			):
				return None

			diff_positions.append((base.index(a_id), name))

	return diff_positions


def _sequence_collect_closure(sequence, blocks, graph=None) -> set:
	closure = set(sequence)
	stack = list(sequence)

	while stack:
		bid = stack.pop()
		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		for value in (block.get("inputs", {})).values():
			for ref in iter_input_block_refs(value):
				if ref in blocks and ref not in closure:
					closure.add(ref)
					stack.append(ref)

	return closure


def _sequence_has_external_owner(closure, sequence, blocks, comments, graph=None):
	if graph is None:
		graph = ProjectGraph({"blocks": blocks})

	root = sequence[0]
	root_parent = (
		blocks.get(root, {}).get("parent")
		if isinstance(blocks.get(root), dict)
		else None
	)

	for child_id in closure:
		for owner_id in graph.parents.get(child_id, ()):
			if owner_id in closure:
				continue

			if child_id == root and owner_id == root_parent:
				continue

			if child_id == sequence[-1]:
				last = blocks.get(child_id)
				if isinstance(last, dict) and last.get("next") == owner_id:
					continue

			return True

	for comment in comments.values():
		if isinstance(comment, dict) and comment.get("blockId") in closure:
			return True

	return False


def _sequence_replace_direct_ref(value, old_id, new_id):
	value = copy.deepcopy(value)

	def replace(node) -> bool:
		if not isinstance(node, list) or not node:
			return False

		tag = node[0]
		if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
			if len(node) > 1 and node[1] == old_id:
				node[1] = new_id
				return True

			return False

		if tag == INPUT_DIFF_BLOCK_SHADOW:
			# [3, primary, shadow] may reference either block directly
			for i in (1, 2):
				if len(node) > i and node[i] == old_id:
					node[i] = new_id
					return True

			for i in (1, 2):
				if (
					len(node) > i
					and isinstance(node[i], (list, dict))
					and replace(node[i])
				):
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
	bid = _new_id()

	while bid in blocks or bid in generated:
		bid = _new_id()

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

		for name, child in list((new.get("inputs", {})).items()):
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

		if (
			tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
			and len(out) > 1
			and isinstance(out[1], str)
			and out[1] in blocks
		):
			new_ref = clone_block(out[1], parent)
			if new_ref is None:
				return None

			out[1] = new_ref

		elif tag == INPUT_DIFF_BLOCK_SHADOW:
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


def _sequence_default_value(value, blocks) -> str:
	if not isinstance(value, list) or not value:
		return ""

	child = value[1] if len(value) > 1 else None
	if isinstance(child, list):
		if len(child) == 2 and child[0] in NUMERIC_TAGS + (PRIMITIVE_TEXT,):
			return str(child[1]) if child[1] is not None else ""

		if len(child) >= 3 and child[0] in (
			PRIMITIVE_BROADCAST,
			PRIMITIVE_VARIABLE,
			PRIMITIVE_LIST,
		):
			return str(child[1]) if child[1] is not None else ""

	return ""


def _sequence_make_argument_reporter(
	generated, parent_id, arg_id, name, shadow=False, blocks=None
):
	bid = _new_id()

	while bid in generated or (blocks is not None and bid in blocks):
		bid = _new_id()

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


def _sequence_warp_context(blocks, block_id, cache=None):
	if cache is None:
		cache = {}

	if block_id in cache:
		return cache[block_id]

	path = []
	seen = set()
	current = block_id
	result = False

	while isinstance(current, str) and current in blocks and current not in seen:
		if current in cache:
			result = cache[current]
			break

		seen.add(current)
		path.append(current)
		block = blocks.get(current)

		if not isinstance(block, dict):
			break

		parent = block.get("parent")

		if not isinstance(parent, str) or parent not in blocks:
			break

		parent_block = blocks[parent]

		if not isinstance(parent_block, dict):
			break

		if parent_block.get("opcode") == "procedures_definition":
			custom = (parent_block.get("inputs", {})).get("custom_block")

			if isinstance(custom, list) and len(custom) > 1:
				prototype = blocks.get(custom[1])

				if isinstance(prototype, dict):
					result = _procedure_warp_enabled(
						(prototype.get("mutation", {})).get("warp")
					)
			break

		current = parent

	for item in path:
		cache[item] = result

	return result


def get_similar_sequence_candidates(runs, blocks, threshold, unbounded_sequence_length=False):
	threshold = max(1, int(threshold))
	segments = []

	for run in runs:
		if len(run) < threshold:
			continue

		ids = []
		sigs = []

		for bid in run:
			block = blocks.get(bid)

			if not isinstance(block, dict) or "comment" in block:
				if len(ids) >= threshold:
					segments.append((ids, sigs))
				ids, sigs = [], []
				continue

			sig = _sequence_block_signature(block, blocks)

			if sig is None:
				if len(ids) >= threshold:
					segments.append((ids, sigs))

				ids, sigs = [], []
				continue

			ids.append(bid)
			sigs.append(sig)

		if len(ids) >= threshold:
			segments.append((ids, sigs))

	anchors = {}

	for ids, sigs in segments:
		for pos in range(len(ids) - threshold + 1):
			key = tuple(sigs[pos : pos + threshold])
			anchors.setdefault(key, []).append((ids, sigs, pos))

	candidates = {}

	def add_candidate(group, length):
		if len(group) < 2 or length < threshold:
			return

		selected = []
		occupied = set()

		for ids, _sigs, pos in group:
			seq = tuple(ids[pos : pos + length])
			if len(seq) != length:
				continue

			seq_set = set(seq)
			if seq_set & occupied:
				continue

			selected.append(seq)
			occupied.update(seq_set)

		if len(selected) < 2:
			return

		key = tuple(sorted(selected))
		candidates[key] = [list(seq) for seq in selected]

	for occurrences in anchors.values():
		if len(occurrences) < 2:
			continue

		frontier = [(occurrences, threshold)]
		while frontier:
			group, length = frontier.pop()

			if len(group) < 2:
				continue

			add_candidate(group, length)

			if not unbounded_sequence_length and length >= SEQUENCE_MAX_WINDOW:
				continue

			next_groups = {}

			for ids, sigs, pos in group:
				next_index = pos + length
				if next_index < len(ids):
					next_groups.setdefault(sigs[next_index], []).append(
						(ids, sigs, pos)
					)

			for subgroup in next_groups.values():
				if len(subgroup) >= 2:
					frontier.append((subgroup, length + 1))

	return [
		group
		for _key, group in sorted(
			candidates.items(),
			key=lambda item: (
				-(len(item[1][0]) * len(item[1])),
				-len(item[1][0]),
				-len(item[1]),
			),
		)
	]


def _prepare_candidate(sequences, blocks, comments, graph, threshold, unbounded_sequence_params=False):
	if len(sequences) < 2 or any(len(seq) < threshold for seq in sequences):
		return None

	if any(
		not isinstance(bid, str) or bid not in blocks
		for seq in sequences
		for bid in seq
	):
		return None

	base = sequences[0]
	if any(_sequence_compare(base, seq, blocks) is None for seq in sequences[1:]):
		return None

	closures = {}

	for seq in sequences:
		closure = _sequence_collect_closure(seq, blocks, graph=graph)
		closures[tuple(seq)] = closure
		if not closure or _sequence_has_external_owner(
			closure, seq, blocks, comments, graph=graph
		):
			return None

	closure_values = list(closures.values())
	if any(
		closure_values[i] & closure_values[j]
		for i in range(len(closure_values))
		for j in range(i + 1, len(closure_values))
	):
		return None

	param_slots = []

	for index, block_id in enumerate(base):
		base_block = blocks.get(block_id)
		if not isinstance(base_block, dict):
			return None

		for input_name, base_value in (base_block.get("inputs", {})).items():
			values = [
				(blocks[seq[index]].get("inputs", {})).get(input_name)
				for seq in sequences
			]

			if len({dumps_compact(v) for v in values}) <= 1:
				continue

			if is_boolean_slot(base_block, input_name) or not all(
				_sequence_input_parameterizable(v, blocks) for v in values
			):
				return None

			param_slots.append((index, input_name, values))

	param_vectors = {}

	for _, _, values in param_slots:
		vector = tuple(dumps_compact(v) for v in values)
		param_vectors.setdefault(vector, len(param_vectors))

	parameter_count = len(param_vectors)

	if not unbounded_sequence_params and parameter_count > SEQUENCE_MAX_PARAMETERS:
		return None

	return base, closures, param_slots, parameter_count


def _estimate_group_savings(
	sequences, closures, param_slots, parameter_count, blocks, graph
):
	if len(sequences) < 2 or not sequences:
		return None

	base = sequences[0]
	base_closure = closures.get(tuple(base))

	if not base_closure:
		return None

	fake_id = "0"
	fake_def = "1"
	fake_proto = "2"
	fake_arg_ids = [str(3 + i) for i in range(parameter_count)]
	fake_proc = "group0" + (
		" " + " ".join("%s" for _ in range(parameter_count)) if parameter_count else ""
	)

	seq_index = {bid: i for i, bid in enumerate(base)}
	body_fake_ids = {bid: short_id(20 + i) for i, bid in enumerate(base_closure)}

	for i, bid in enumerate(base):
		body_fake_ids[bid] = f"{20 + i:020d}"

	body_reporter_ids = [short_id(128 + i) for i in range(parameter_count)]

	def entry_cost(bid, block):
		return (
			len(quote(bid).encode("utf-8", "backslashreplace")) + 1 + _json_len(block)
		)

	removed_cost = 0
	removed_count = 0
	for closure in closures.values():
		for bid in closure:
			block = blocks.get(bid)

			if isinstance(block, dict):
				removed_cost += entry_cost(bid, block)
				removed_count += 1

	def normalized_input(value):
		value = copy.deepcopy(value)

		if not isinstance(value, list) or not value:
			return value

		tag = value[0]
		positions = (1, 2) if tag == INPUT_DIFF_BLOCK_SHADOW else (1,)
		if tag in INPUT_TAGS:
			for i in positions:
				if len(value) <= i:
					continue

				item = value[i]

				if isinstance(item, str) and item in body_fake_ids:
					value[i] = body_fake_ids[item]
				elif isinstance(item, list):
					value[i] = normalized_input(item)
				elif isinstance(item, dict):
					value[i] = copy.deepcopy(item)
			return value
		for i in range(1, len(value)):
			if isinstance(value[i], list):
				value[i] = normalized_input(value[i])
		return value

	generated = {}
	param_vectors = {}
	parameter_slot_map = {}

	for index, input_name, values in param_slots:
		vector = tuple(dumps_compact(v) for v in values)

		if vector not in param_vectors:
			param_vectors[vector] = len(param_vectors)
		parameter_slot_map[(index, input_name)] = param_vectors[vector]

	param_values_by_slot = {}

	for index, input_name, values in param_slots:
		slot = parameter_slot_map[(index, input_name)]
		param_values_by_slot.setdefault(slot, values)

	for old_id in base_closure:
		block = blocks.get(old_id)

		if not isinstance(block, dict):
			continue

		probe = copy.deepcopy(block)
		probe.pop("topLevel", None)
		probe.pop("x", None)
		probe.pop("y", None)

		if old_id in seq_index:
			idx = seq_index[old_id]
			probe["parent"] = fake_def if idx == 0 else body_fake_ids.get(base[idx - 1])
			original_next = block.get("next")
			probe["next"] = body_fake_ids.get(original_next)
		else:
			original_parent = block.get("parent")
			probe["parent"] = body_fake_ids.get(original_parent, fake_id)
			original_next = block.get("next")
			probe["next"] = body_fake_ids.get(original_next)

		new_inputs = {}

		for input_name, value in (block.get("inputs", {})).items():
			idx = seq_index.get(old_id)
			slot = parameter_slot_map.get((idx, input_name))

			if slot is None:
				new_inputs[input_name] = normalized_input(value)
				continue

			reporter_id = body_reporter_ids[slot]
			if (
				isinstance(value, list)
				and value
				and value[0] == INPUT_DIFF_BLOCK_SHADOW
				and len(value) > 2
			):
				shadow = copy.deepcopy(value[2])
				if isinstance(shadow, str) and shadow in body_fake_ids:
					shadow = body_fake_ids[shadow]
				elif isinstance(shadow, list):
					shadow = normalized_input(shadow)
				new_inputs[input_name] = [INPUT_DIFF_BLOCK_SHADOW, reporter_id, shadow]
			else:
				new_inputs[input_name] = [INPUT_BLOCK_NO_SHADOW, reporter_id]

		probe["inputs"] = new_inputs
		generated[old_id] = probe

	argument_names = [f"p{i}" for i in range(parameter_count)]
	argument_defaults = []

	for slot in range(parameter_count):
		values = param_values_by_slot[slot]
		argument_defaults.append(_sequence_default_value(values[0], blocks))

	prototype = {
		"opcode": "procedures_prototype",
		"next": None,
		"parent": fake_def,
		"inputs": {},
		"fields": {},
		"shadow": False,
		"mutation": {
			"tagName": "mutation",
			"children": [],
			"proccode": fake_proc,
			"argumentids": json.dumps(fake_arg_ids, separators=(",", ":")),
			"argumentnames": json.dumps(argument_names, separators=(",", ":")),
			"argumentdefaults": json.dumps(argument_defaults, separators=(",", ":")),
			"warp": False,
		},
	}

	definition = {
		"opcode": "procedures_definition",
		"next": fake_id,
		"parent": None,
		"inputs": {"custom_block": [INPUT_SAME_BLOCK_SHADOW, fake_proto]},
		"fields": {},
		"topLevel": True,
		"x": 5,
		"y": 5,
	}
	generated_cost = entry_cost(fake_proto, prototype) + entry_cost(
		fake_def, definition
	)

	for key, probe in generated.items():
		generated_cost += entry_cost(body_fake_ids[key], probe)

	fake_counter = 300

	def fresh_fake_id():
		nonlocal fake_counter
		value = short_id(fake_counter)
		fake_counter += 1
		return value

	def normalize_call_input(value, parent_id):
		memo = {}
		cloned_entries = []

		def clone_block(old_id, parent):
			if old_id in memo:
				new_id = memo[old_id]

				for index, (existing_id, block) in enumerate(cloned_entries):
					if existing_id == new_id:
						block["parent"] = parent
						cloned_entries[index] = (existing_id, block)
						break

				return new_id
			old = blocks.get(old_id)

			if not isinstance(old, dict) or old.get("next") is not None:
				raise ValueError

			new_id = fresh_fake_id()
			memo[old_id] = new_id
			new = copy.deepcopy(old)
			new["parent"] = parent
			new.pop("topLevel", None)
			new.pop("x", None)
			new.pop("y", None)
			new["next"] = None
			cloned_entries.append((new_id, new))

			for name, child in list((new.get("inputs", {})).items()):
				new["inputs"][name] = clone_value(child, new_id)

			return new_id

		def clone_value(item, parent) -> list:
			out = copy.deepcopy(item)
			if not isinstance(out, list) or not out:
				return out

			tag = out[0]

			if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW) and len(out) > 1:
				if isinstance(out[1], str) and out[1] in blocks:
					out[1] = clone_block(out[1], parent)
				elif isinstance(out[1], list):
					out[1] = clone_value(out[1], parent)
			elif tag == INPUT_DIFF_BLOCK_SHADOW:
				for index in (1, 2):
					if len(out) <= index:
						continue
					item2 = out[index]
					if isinstance(item2, str) and item2 in blocks:
						out[index] = clone_block(item2, parent)
					elif isinstance(item2, list):
						out[index] = clone_value(item2, parent)

			return out

		return clone_value(value, parent_id), cloned_entries

	root_set = {seq[0] for seq in sequences}

	for seq_index_in_group, seq in enumerate(sequences):
		root = seq[0]
		root_block = blocks.get(root)
		if not isinstance(root_block, dict):
			return None
		continuation = (
			blocks.get(seq[-1], {}).get("next")
			if isinstance(blocks.get(seq[-1]), dict)
			else None
		)
		internal_continuation = continuation if continuation in root_set else None
		call = {
			"opcode": "procedures_call",
			"next": fake_id if internal_continuation is not None else continuation,
			"parent": root_block.get("parent"),
			"inputs": {},
			"fields": {},
			"mutation": {
				"tagName": "mutation",
				"children": [],
				"proccode": fake_proc,
				"argumentids": json.dumps(fake_arg_ids, separators=(",", ":")),
				"warp": False,
			},
		}
		internal_parent = None
		for previous_seq in sequences:
			if previous_seq is not seq and previous_seq[-1] == root:
				internal_parent = fake_id
				break
		if internal_parent is not None:
			call["parent"] = internal_parent
		if root_block.get("topLevel") is True or root_block.get("parent") is None:
			call["topLevel"] = True
			call["x"] = root_block.get("x", 5)
			call["y"] = root_block.get("y", 5)
		for _index, _input_name, values in param_slots:
			slot = parameter_slot_map[(_index, _input_name)]
			value = values[seq_index_in_group]
			try:
				normalized_value, cloned_entries = normalize_call_input(value, fake_id)
			except ValueError:
				return None
			call["inputs"][fake_arg_ids[slot]] = normalized_value
			for cloned_id, cloned_block in cloned_entries:
				generated_cost += entry_cost(cloned_id, cloned_block)
		generated_cost += entry_cost(fake_id, call)

	for seq in sequences:
		root = seq[0]
		root_block = blocks.get(root)

		if not isinstance(root_block, dict):
			return None

		parent_id = root_block.get("parent")
		internal_parent = any(
			other is not seq and other[-1] == root for other in sequences
		)

		if isinstance(parent_id, str) and parent_id in blocks and not internal_parent:
			parent_block = blocks.get(parent_id)

			if isinstance(parent_block, dict):
				preview = copy.deepcopy(parent_block)

				if preview.get("next") == root:
					preview["next"] = fake_id
				else:
					changed = False
					for name, value in (preview.get("inputs", {})).items():
						new_value = _sequence_replace_direct_ref(value, root, fake_id)
						if new_value is not None:
							preview["inputs"][name] = new_value
							changed = True
							break

					if not changed:
						continue

				generated_cost += _json_len(preview) - _json_len(parent_block)

		continuation_id = (
			blocks.get(seq[-1], {}).get("next")
			if isinstance(blocks.get(seq[-1]), dict)
			else None
		)

		if (
			isinstance(continuation_id, str)
			and continuation_id in blocks
			and continuation_id not in root_set
		):
			continuation_block = blocks.get(continuation_id)

			if isinstance(continuation_block, dict):
				preview = copy.deepcopy(continuation_block)
				preview["parent"] = fake_id
				generated_cost += _json_len(preview) - _json_len(continuation_block)

	generated_cost += len(sequences) - removed_count
	return removed_cost - generated_cost


def _sequence_projected_json_size(
	target, *, rename_block_ids=False, rename_argument_ids=False
):
	projected = copy.deepcopy(target)
	dummy_stats = defaultdict(int)

	if rename_argument_ids:
		rename_argument_ids_fn = globals()["rename_argument_ids"]
		rename_argument_ids_fn(projected, dummy_stats)

	if rename_block_ids:
		rename_block_ids_fn = globals()["rename_block_ids"]
		rename_block_ids_fn(projected, dummy_stats)

	return _json_len(projected)


def group_similar_sequences(project, stats, threshold=3, opts=None):
	threshold = max(1, int(threshold))
	unbounded_sequence_length = getattr(opts, "unbounded_sequence_length", False)
	unbounded_sequence_params = getattr(opts, "unbounded_sequence_params", False)

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks") or {}
		comments = target.get("comments") or {}
		graph = ProjectGraph(target)
		runs = get_sequence_runs(blocks)
		candidate_groups = get_similar_sequence_candidates(runs, blocks, threshold, unbounded_sequence_length)

		if not candidate_groups:
			continue

		scored_candidates = []

		for sequences in candidate_groups:
			prepared = _prepare_candidate(
				sequences, blocks, comments, graph, threshold, unbounded_sequence_params
			)

			if prepared is None:
				continue

			base, closures, param_slots, parameter_count = prepared
			estimated_savings = _estimate_group_savings(
				sequences,
				closures,
				param_slots,
				parameter_count,
				blocks,
				graph,
			)

			if estimated_savings is None or estimated_savings <= 0:
				if opts is not None:
					opts.grouped_sequence_rejected_size += 1
				stats["sequence_groups_rejected_size"] += 1
				continue

			scored_candidates.append((estimated_savings, sequences))

		scored_candidates.sort(key=lambda item: -item[0])

		proc_counter = 0
		existing_procs = {
			procedure_key(block)
			for block in blocks.values()
			if isinstance(block, dict)
			and block.get("opcode") in ("procedures_prototype", "procedures_call")
			and isinstance(procedure_key(block), str)
		}

		for _, candidate_sequences in scored_candidates:
			sequences = [
				seq
				for seq in candidate_sequences
				if all(bid in blocks for bid in seq) and len(seq) >= threshold
			]

			if len(sequences) < 2:
				continue

			prepared = _prepare_candidate(
				sequences, blocks, comments, graph, threshold, unbounded_sequence_params
			)

			if prepared is None:
				continue

			base, closures, param_slots, parameter_count = prepared

			param_vectors = {}
			param_for_slot = {}

			for index, input_name, values in param_slots:
				vector = tuple(dumps_compact(v) for v in values)
				if vector not in param_vectors:
					param_vectors[vector] = len(param_vectors)
				param_for_slot[(index, input_name)] = param_vectors[vector]

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
				arg_id = _new_id()

				while arg_id in generated or arg_id in blocks or arg_id in argument_ids:
					arg_id = _new_id()

				argument_ids.append(arg_id)
				arg_name = f"p{pidx}"
				argument_names.append(arg_name)
				first_values = next(
					values
					for index, input_name, values in param_slots
					if param_for_slot[(index, input_name)] == pidx
				)
				argument_defaults.append(
					_sequence_default_value(first_values[0], blocks)
				)

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
					"argumentdefaults": json.dumps(
						argument_defaults, separators=(",", ":")
					),
					"warp": False,
				},
			}

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

				for input_name, original_value in (
					old_original.get("inputs") or {}
				).items():
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
							and original_value[0] == INPUT_DIFF_BLOCK_SHADOW
							and len(original_value) > 2
						):
							shadow = copy.deepcopy(original_value[2])

							if isinstance(shadow, str) and shadow in blocks:
								shadow_value = _sequence_clone_input(
									[INPUT_SAME_BLOCK_SHADOW, shadow],
									blocks,
									generated,
									new_id,
								)

								if (
									isinstance(shadow_value, list)
									and len(shadow_value) == 2
								):
									shadow = shadow_value[1]
								else:
									continue

							new_inputs[input_name] = [
								INPUT_DIFF_BLOCK_SHADOW,
								arg_reporter_id,
								shadow,
							]
						else:
							new_inputs[input_name] = [
								INPUT_BLOCK_NO_SHADOW,
								arg_reporter_id,
							]
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
				"inputs": {"custom_block": [INPUT_SAME_BLOCK_SHADOW, proto_id]},
				"fields": {},
				"topLevel": True,
				"x": 5,
				"y": 5,
			}

			construction_ok = True
			call_ids = {
				seq[0]: _sequence_new_id(blocks, generated) for seq in sequences
			}

			for seq in sequences:
				root = seq[0]
				root_block = blocks[root]
				call_id = call_ids[root]
				call_inputs = {}

				for idx, old_id in enumerate(seq):
					for input_name, value in (
						blocks[old_id].get("inputs") or {}
					).items():
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

				continuation_id = blocks[seq[-1]].get("next")
				internal_continuation = (
					call_ids.get(continuation_id)
					if isinstance(continuation_id, str)
					else None
				)
				call_next = (
					internal_continuation
					if internal_continuation is not None
					else continuation_id if isinstance(continuation_id, str) else None
				)
				internal_parent = None
				for previous_seq in sequences:
					if previous_seq[-1] == root and previous_seq is not seq:
						internal_parent = call_ids[previous_seq[0]]
						break
				call_parent = (
					internal_parent
					if internal_parent is not None
					else root_block.get("parent")
				)
				call = {
					"opcode": "procedures_call",
					"next": call_next,
					"parent": call_parent,
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

				if (
					root_block.get("topLevel") is True
					or root_block.get("parent") is None
				):
					call["topLevel"] = True
					call["x"] = root_block.get("x", 5)
					call["y"] = root_block.get("y", 5)
				generated[call_id] = call

				if (
					isinstance(continuation_id, str)
					and continuation_id in blocks
					and internal_continuation is None
				):
					continuation = blocks.get(continuation_id)
					if (
						not isinstance(continuation, dict)
						or continuation.get("parent") != seq[-1]
						or graph.parents.get(continuation_id, set()) - {seq[-1]}
					):
						construction_ok = False
						break

					preview = copy.deepcopy(continuation)
					preview["parent"] = call_id
					owner_previews[continuation_id] = preview

				parent_id = (
					None if internal_parent is not None else root_block.get("parent")
				)
				if isinstance(parent_id, str) and parent_id in blocks:
					parent_block = owner_previews.get(parent_id, blocks[parent_id])
					if not isinstance(parent_block, dict):
						construction_ok = False
						break

					if parent_block.get("next") == root:
						preview = copy.deepcopy(parent_block)
						preview["next"] = call_id
						owner_previews[parent_id] = preview
						link_changes.append((parent_id, preview, root, call_id))
					else:
						found = False
						for input_name, input_value in (
							parent_block.get("inputs") or {}
						).items():
							replaced = _sequence_replace_direct_ref(
								input_value, root, call_id
							)

							if replaced is None:
								continue

							preview = copy.deepcopy(parent_block)
							preview["inputs"][input_name] = replaced
							owner_previews[parent_id] = preview
							input_changes.append(
								(
									parent_id,
									input_name,
									preview,
									replaced,
									root,
									call_id,
								)
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

			candidate_blocks = dict(blocks)
			for owner_id, preview in owner_previews.items():
				candidate_blocks[owner_id] = preview

			for new_id, block in generated.items():
				candidate_blocks[new_id] = block

			for old_id in removed:
				candidate_blocks.pop(old_id, None)

			candidate_target = copy.copy(target)
			candidate_target["blocks"] = candidate_blocks

			if opts is not None and (
				getattr(opts, "rename_block_ids", False)
				or getattr(opts, "rename_argument_ids", False)
			):
				old_target_bytes = _sequence_projected_json_size(
					target,
					rename_block_ids=getattr(opts, "rename_block_ids", False),
					rename_argument_ids=getattr(opts, "rename_argument_ids", False),
				)
				new_target_bytes = _sequence_projected_json_size(
					candidate_target,
					rename_block_ids=getattr(opts, "rename_block_ids", False),
					rename_argument_ids=getattr(opts, "rename_argument_ids", False),
				)
			else:
				old_target_bytes = _json_len(target)
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

				for (
					owner_id,
					input_name,
					_,
					new_value,
					_,
					_,
				) in input_changes:
					opts.grouped_sequence_input_edits[(ti, owner_id, input_name)] = (
						copy.deepcopy(new_value)
					)

			stats["sequence_groups_created"] += 1
			stats["sequences_grouped"] += len(sequences)
			stats["sequence_procedures_created"] += 1
			stats["sequence_parameters"] += parameter_count
			stats["sequence_blocks_removed"] += len(removed)
			stats["sequence_blocks_added"] += len(generated)
			stats["sequence_bytes_saved"] += byte_delta
			graph.refresh_after_mutation(
				changed=set(owner_previews) | set(generated),
				removed=removed,
			)
			_debug_structure_checkpoint(
				project,
				f"sequence group commit in target {ti}, generated {len(generated)} blocks",
			)


def get_argument_kinds(proccode, count):
	if not isinstance(proccode, str):
		return None

	kinds = [token for token in proccode.split(" ") if token in ("%s", "%b")]
	return kinds if len(kinds) == count else None


def remove_procedure_placeholder(proccode, argument_index):
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
		graph = ProjectGraph(target)

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

	# definition closure must be owned by its definition
	for bid in closure:
		for parent in graph.parents.get(bid, set()):
			if bid == definition_id:
				if parent not in closure:
					continue
			elif parent not in closure:
				return None

	# definition must be a top-level root or parentless
	def_block = blocks[definition_id]
	if def_block.get("parent") is not None:
		return None

	return closure


def _procedure_infos(target):
	blocks = target.get("blocks") or {}
	graph = ProjectGraph(target)
	by_proc = {}

	for definition_id, definition in blocks.items():
		if (
			not isinstance(definition, dict)
			or definition.get("opcode") != "procedures_definition"
		):
			continue

		custom = (definition.get("inputs") or {}).get("custom_block")
		proto_id = (
			custom[1]
			if isinstance(custom, list)
			and len(custom) > 1
			and isinstance(custom[1], str)
			else None
		)
		proto = blocks.get(proto_id)

		if not isinstance(proto, dict) or proto.get("opcode") != "procedures_prototype":
			continue

		mut = proto.get("mutation")
		proc = procedure_key(proto)
		argids = parse_argument_ids(mut)

		if not isinstance(mut, dict) or proc is None or argids is None:
			continue

		closure = _procedure_definition_closure(target, definition_id, graph)

		if closure is None:
			continue

		by_proc.setdefault(proc, []).append(
			{
				"definition_id": definition_id,
				"prototype_id": proto_id,
				"prototype": proto,
				"closure": closure,
				"argument_ids": list(argids),
				"mutation": mut,
			}
		)

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
		if bid == info["prototype_id"]:
			continue

		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		op = block.get("opcode", "")

		if not op.startswith("argument_reporter_"):
			continue

		field = (block.get("fields") or {}).get("VALUE")
		name = (
			field[0]
			if isinstance(field, list) and field and isinstance(field[0], str)
			else None
		)

		if name not in name_to_index:
			continue

		idx = name_to_index[name]
		used.add(idx)
		reporter_ids.setdefault(idx, set()).add(bid)

	return used, reporter_ids, arg_names


def _procedure_input_is_discardable(
	value, blocks: dict[str, Any], graph: ProjectGraph
) -> bool:
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


def _replace_argument_reporter_refs(
	value, reporter_ids, replacement
) -> tuple[Any | list, bool]:
	if not isinstance(value, list) or not value:
		return value, False

	tag = value[0]
	if tag in INPUT_TAGS:
		primary = value[1] if len(value) > 1 else None
		if isinstance(primary, str) and primary in reporter_ids:
			return copy.deepcopy(replacement), True

		if isinstance(primary, list):
			value[1], changed = _replace_argument_reporter_refs(
				primary, reporter_ids, replacement
			)

			if changed:
				return value, True

		if tag == INPUT_DIFF_BLOCK_SHADOW and len(value) > 2:
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

		if isinstance(defaults, list) and len(defaults) >= len(arg_ids) + len(
			removed_indices
		):
			kept_defaults = [
				v for i, v in enumerate(defaults) if i not in removed_indices
			]
			out["argumentdefaults"] = json.dumps(
				kept_defaults, separators=(",", ":"), ensure_ascii=False
			)

	out["proccode"] = new_proccode
	return out


def _procedure_warp_enabled(value):
	return value is True or (isinstance(value, str) and value.lower() == "true")


def _procedure_recursive_set(by_proc):
	call_graph = {proc: set() for proc in by_proc}
	for proc, infos in by_proc.items():
		for info in infos:
			blocks = info.get("blocks", {})
			for bid in info["closure"]:
				block = blocks.get(bid)
				if (
					not isinstance(block, dict)
					or block.get("opcode") != "procedures_call"
				):
					continue
				called = procedure_key(block)
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


def _inline_argument_reporter_refs(
	value, reporter_ids, replacement
) -> tuple[Any, bool]:
	if not isinstance(value, list) or not value:
		return value, False

	tag = value[0]

	if tag in INPUT_TAGS:
		positions = (1, 2) if tag == INPUT_DIFF_BLOCK_SHADOW else (1,)

		for index in positions:
			if index >= len(value):
				continue

			item = value[index]

			if isinstance(item, str) and item in reporter_ids:
				return copy.deepcopy(replacement), True

			if isinstance(item, (list, dict)):
				new_item, changed = _inline_argument_reporter_refs(
					item, reporter_ids, replacement
				)

				if changed:
					value[index] = new_item

					if index == 1 and is_literal_input(new_item):
						return new_item, True

					return value, True

		return value, False

	for index in range(1, len(value)):
		item = value[index]

		if isinstance(item, (list, dict)):
			new_item, changed = _inline_argument_reporter_refs(
				item, reporter_ids, replacement
			)

			if changed:
				value[index] = new_item
				return value, True

	return value, False


def _specialized_proccode(proccode, serial):
	if not isinstance(proccode, str):
		return None

	percent_index = proccode.find("%")

	if percent_index >= 0:
		prefix = proccode[:percent_index]
		suffix_start = percent_index
		while suffix_start > 0 and proccode[suffix_start - 1].isspace():
			suffix_start -= 1
		return f"{prefix} !s{serial}{proccode[suffix_start:]}"

	return f"{proccode} !s{serial}"


def _clone_procedure_closure(
	target: dict[str, Any],
	info,
	specialized_proccode,
	constant_replacements,
	stats,
	opts,
):
	blocks = target.get("blocks") or {}
	closure = set(info["closure"])

	if not closure:
		return None

	graph = ProjectGraph(target)
	comments = target.get("comments") or {}

	if any(
		isinstance(comment, dict) and comment.get("blockId") in closure
		for comment in comments.values()
	):
		return None

	for old_id in closure:
		block = blocks.get(old_id)

		if not isinstance(block, dict):
			return None

		nxt = block.get("next")
		parent = block.get("parent")

		if isinstance(nxt, str) and nxt not in closure:
			return None

		if isinstance(parent, str) and parent not in closure:
			return None

		for raw in (block.get("inputs") or {}).values():
			if any(ref not in closure for ref in iter_input_block_refs(raw)):
				return None

	old_ids = list(closure)
	new_ids = {}

	for old_id in old_ids:
		new_bid = _new_id()

		while new_bid in blocks or new_bid in new_ids.values():
			new_bid = _new_id()

		new_ids[old_id] = new_bid

	clones = {}

	for old_id in old_ids:
		old = blocks.get(old_id)
		new = copy.deepcopy(old)

		if isinstance(new.get("next"), str):
			new["next"] = new_ids.get(new["next"], new["next"])

		if isinstance(new.get("parent"), str):
			new["parent"] = new_ids.get(new["parent"], new["parent"])

		if isinstance(new.get("inputs"), dict):
			for raw in new["inputs"].values():
				replace_input_block_ids(raw, new_ids)

		if old_id == info["definition_id"]:
			new["parent"] = None

		clones[new_ids[old_id]] = new

	new_prototype_id = new_ids[info["prototype_id"]]
	new_definition_id = new_ids[info["definition_id"]]
	new_root = clones[new_definition_id].get("next")

	if not isinstance(new_root, str) or new_root not in clones:
		return None

	prototype = clones[new_prototype_id]
	prototype_mutation = prototype.get("mutation")

	if not isinstance(prototype_mutation, dict):
		return None

	prototype_mutation["proccode"] = specialized_proccode

	body_info = dict(info)
	prototype_owned = {info["prototype_id"]}
	stack = [info["prototype_id"]]

	while stack:
		current = stack.pop()

		for ref in graph.edges.get(current, ()):
			if ref in closure and ref not in prototype_owned:
				prototype_owned.add(ref)
				stack.append(ref)

	body_info["closure"] = closure - prototype_owned - {info["definition_id"]}
	used, reporter_map, _ = _procedure_body_argument_uses(body_info, blocks)

	if used is None:
		return None

	removed_reporters = set()
	for arg_index, replacement in constant_replacements.items():
		reporter_ids = set(reporter_map.get(arg_index, ()))

		if not reporter_ids:
			continue

		cloned_reporters = {new_ids[rid] for rid in reporter_ids if rid in new_ids}

		if not cloned_reporters:
			continue

		for block_id in list(clones):
			if block_id in cloned_reporters:
				continue

			block = clones.get(block_id)

			if not isinstance(block, dict):
				continue

			for name, raw in list((block.get("inputs") or {}).items()):
				new_raw = copy.deepcopy(raw)
				new_raw, changed = _inline_argument_reporter_refs(
					new_raw, cloned_reporters, replacement
				)

				if changed:
					block.setdefault("inputs", {})[name] = new_raw

		still_referenced = False
		for block_id, block in clones.items():
			if not isinstance(block, dict):
				continue

			if any(
				rid in cloned_reporters
				for raw in (block.get("inputs") or {}).values()
				for rid in iter_input_block_refs(raw)
			):
				still_referenced = True
				break

		if still_referenced:
			return None

		for reporter_id in cloned_reporters:
			clones.pop(reporter_id, None)

		removed_reporters.update(cloned_reporters)

	for block in clones.values():
		if not isinstance(block, dict):
			continue

		for key in ("next", "parent"):
			ref = block.get(key)
			if isinstance(ref, str) and ref not in clones and ref not in (None,):
				return None

		for raw in (block.get("inputs") or {}).values():
			refs = set(iter_input_block_refs(raw))
			if any(ref not in clones for ref in refs):
				return None

	return clones, new_ids, new_definition_id, new_prototype_id, len(removed_reporters)


def specialized_proc_trial_size(target, variants, opts):
	probe = copy.deepcopy(target)
	blocks = probe.get("blocks") or {}
	probe["blocks"] = blocks

	for variant in variants:
		blocks.update(copy.deepcopy(variant["clones"]))
		for call_id, _call, _replacement_constants in variant["members"]:
			call = blocks.get(call_id)
			mutation = call.get("mutation") if isinstance(call, dict) else None
			if isinstance(mutation, dict):
				mutation["proccode"] = variant["specialized_proc"]

	trial_project = {"targets": [probe]}
	if getattr(opts, "remove_unused_procedures", False):
		remove_unused_procedures(trial_project, defaultdict(int))

	if getattr(opts, "rename_procedure_names", False):
		rename_identifiers(
			trial_project,
			defaultdict(int),
			rename_variable_names=False,
			rename_list_names=False,
			rename_broadcast_names=False,
			rename_argument_names=False,
			rename_procedure_names=True,
		)

	serialized = dumps_compact(probe, getattr(opts, "sort_keys", False))
	return len(serialized.encode("utf-8", "backslashreplace"))


def try_specialize_procedure_variants(
	target, ti, proc, info, by_proc, qualifying, stats, opts, serial,
	unbounded_procedure_specialization,
):
	variant_plans = []
	planning_target = copy.deepcopy(target)
	planning_blocks = planning_target.get("blocks") or {}
	planning_target["blocks"] = planning_blocks

	for signature, members in (
		qualifying if unbounded_procedure_specialization else qualifying[:8]
	):
		if len(variant_plans) >= 8:
			break

		representative_constants = members[0][2]
		if not representative_constants:
			continue

		serial += 1
		specialized_proc = _specialized_proccode(proc, serial)
		if not specialized_proc or specialized_proc in by_proc:
			continue

		cloned = _clone_procedure_closure(
			planning_target,
			info,
			specialized_proc,
			representative_constants,
			stats,
			opts,
		)
		if cloned is None:
			continue

		(
			clones,
			id_map,
			new_definition_id,
			new_prototype_id,
			removed_reporter_count,
		) = cloned
		variant_plans.append(
			{
				"signature": signature,
				"members": members,
				"clones": clones,
				"id_map": id_map,
				"new_definition_id": new_definition_id,
				"new_prototype_id": new_prototype_id,
				"removed_reporter_count": removed_reporter_count,
				"specialized_proc": specialized_proc,
			}
		)
		planning_blocks.update(copy.deepcopy(clones))

	if not variant_plans:
		return None

	baseline_size = specialized_proc_trial_size(target, (), opts)
	selected_indices = []
	selected_size = baseline_size
	all_indices = list(range(len(variant_plans)))
	all_size = specialized_proc_trial_size(target, variant_plans, opts)

	if all_size < selected_size:
		selected_size = all_size
		selected_indices = all_indices

	for idx, variant in enumerate(variant_plans):
		size = specialized_proc_trial_size(target, [variant], opts)
		if size < selected_size:
			selected_size = size
			selected_indices = [idx]

	for first in range(len(variant_plans)):
		for second in range(first + 1, len(variant_plans)):
			size = specialized_proc_trial_size(
				target, [variant_plans[first], variant_plans[second]], opts
			)

			if size < selected_size:
				selected_size = size
				selected_indices = [first, second]

	if selected_indices and len(selected_indices) < len(variant_plans):
		while True:
			best_addition = None

			for idx in range(len(variant_plans)):
				if idx in selected_indices:
					continue

				trial_indices = selected_indices + [idx]
				trial = [variant_plans[i] for i in trial_indices]
				size = specialized_proc_trial_size(target, trial, opts)

				if size < selected_size and (
					best_addition is None or size < best_addition[0]
				):
					best_addition = (size, idx)

			if best_addition is None:
				break

			selected_size, idx = best_addition
			selected_indices.append(idx)

	if not selected_indices:
		return serial, 0, 0

	blocks = target.get("blocks") or {}
	original_prototype = blocks.get(info["prototype_id"])
	original_mutation = (
		(original_prototype or {}).get("mutation")
		if isinstance(original_prototype, dict)
		else None
	)
	original_proccode = (
		original_mutation.get("proccode")
		if isinstance(original_mutation, dict)
		else None
	)
	total_calls = 0

	for idx in selected_indices:
		variant = variant_plans[idx]
		clones = variant["clones"]
		id_map = variant["id_map"]
		new_prototype_id = variant["new_prototype_id"]
		specialized_proc = variant["specialized_proc"]
		members = variant["members"]
		blocks.update(clones)
		stats["procedure_specialization_blocks_added"] += len(clones)
		stats["procedure_specialization_argument_reporters_removed"] += (
			variant["removed_reporter_count"]
		)

		for old_id, new_id in id_map.items():
			opts.specialized_procedure_block_origins[(ti, new_id)] = old_id

		opts.specialized_procedure_prototype_blocks[
			(ti, info["prototype_id"])
		] = True
		opts.specialized_procedure_prototype_blocks[(ti, new_prototype_id)] = True
		opts.specialized_procedure_prototype_proccodes[
			(ti, info["prototype_id"])
		] = specialized_proc
		opts.specialized_procedure_prototype_proccodes[
			(ti, new_prototype_id)
		] = specialized_proc

		if isinstance(original_proccode, str):
			pairs = opts.specialized_procedure_prototype_code_pairs.setdefault(
				(ti, original_proccode), set()
			)
			pairs.add(specialized_proc)

		opts.specialized_procedure_new_blocks[ti].update(clones)

		for call_id, _call, _replacement_constants in members:
			call = blocks.get(call_id)
			mutation = call.get("mutation") if isinstance(call, dict) else None

			if not isinstance(mutation, dict):
				continue

			mutation["proccode"] = specialized_proc
			opts.specialized_procedure_call_proccodes[(ti, call_id)] = specialized_proc

		total_calls += len(members)

	return serial, len(selected_indices), total_calls


def specialize_procedures(project, stats, opts, progress):
	targets = project.get("targets", [])
	opts.specialized_procedure_new_blocks = [set() for _ in targets]
	opts.specialized_procedure_removed_blocks = [set() for _ in targets]
	opts.specialized_procedure_call_proccodes = {}
	opts.specialized_procedure_prototype_proccodes = {}
	opts.specialized_procedure_prototype_code_pairs = {}
	opts.specialized_procedure_prototype_blocks = {}
	opts.specialized_procedure_block_origins = {}
	unbounded_procedure_specialization = getattr(opts, "unbounded_procedure_specialization", False)

	max_passes = max(1, int(getattr(opts, "procedure_specialization_passes", 4)))
	min_calls = (
		1
		if unbounded_procedure_specialization
		else max(2, int(getattr(opts, "procedure_specialization_min_calls", 2)))
	)
	total_variants = 0
	total_calls = 0
	completed_passes = 0
	serial = 0
	pass_idx = 0
	projected_sizes = [None for _ in targets]

	while unbounded_procedure_specialization or pass_idx < max_passes:
		progress.detail(f"pass {pass_idx + 1}")
		changed_this_pass = False

		for ti, target in enumerate(targets):
			blocks = target.get("blocks") or {}

			if not blocks:
				continue

			progress.detail(f"pass {pass_idx + 1}, {target['name']}, {len(blocks)}")
			projected_size = projected_sizes[ti]
			by_proc, graph = _procedure_infos(target)

			if not by_proc:
				continue

			for infos in by_proc.values():
				for info in infos:
					info["blocks"] = blocks

			recursive = _procedure_recursive_set(by_proc)

			calls_by_proc = {}

			for call_id, block in blocks.items():
				if (
					not isinstance(block, dict)
					or block.get("opcode") != "procedures_call"
				):
					continue

				proc = procedure_key(block)

				if proc is not None:
					calls_by_proc.setdefault(proc, []).append((call_id, block))

			for proc, infos in list(by_proc.items()):
				if proc in recursive or len(infos) != 1:
					continue

				calls = calls_by_proc.get(proc, ())

				if len(calls) < min_calls:
					continue

				info = infos[0]

				prototype_owned = {info["prototype_id"]}
				stack = [info["prototype_id"]]

				while stack:
					current = stack.pop()

					for ref in graph.edges.get(current, ()):
						if ref in info["closure"] and ref not in prototype_owned:
							prototype_owned.add(ref)
							stack.append(ref)

				body_info = dict(info)
				body_info["closure"] = (
					set(info["closure"]) - prototype_owned - {info["definition_id"]}
				)
				used, _, _ = _procedure_body_argument_uses(body_info, blocks)

				if used is None or not used:
					continue

				groups = {}

				for call_id, call in calls:
					call_ids = parse_argument_ids(call.get("mutation"))

					if call_ids is None or len(call_ids) != len(info["argument_ids"]):
						continue

					signature_items = []
					replacement_constants = {}

					for idx in sorted(used):
						if idx >= len(call_ids):
							signature_items = []
							break

						raw = (call.get("inputs") or {}).get(call_ids[idx])
						literal = _folded_literal_input(raw)

						if literal is None:
							continue

						signature_items.append((idx, literal[0], literal[1]))
						replacement, _ = constant_to_input(literal, blocks, None)

						if replacement is not None:
							replacement_constants[idx] = replacement

					if not signature_items:
						continue

					signature = tuple(signature_items)
					groups.setdefault(signature, []).append(
						(call_id, call, replacement_constants)
					)

				qualifying = [
					(signature, members)
					for signature, members in groups.items()
					if len(members) >= min_calls and signature
				]

				if not qualifying:
					continue

				qualifying.sort(
					key=lambda item: (
						-(len(item[1]) * max(1, len(item[0]))),
						-len(item[1]),
						-len(item[0]),
					)
				)

				body_size = len(body_info["closure"])

				if not unbounded_procedure_specialization and body_size > 192:
					continue

				existing_variant_count = 0
				for signature, members in (qualifying if unbounded_procedure_specialization else qualifying[:8]):
					if existing_variant_count >= 8:
						break

					representative_constants = members[0][2]

					if not representative_constants:
						continue

					serial += 1
					specialized_proc = _specialized_proccode(proc, serial)

					if not specialized_proc or specialized_proc in by_proc:
						continue

					cloned = _clone_procedure_closure(
						target,
						info,
						specialized_proc,
						representative_constants,
						stats,
						opts,
					)

					if cloned is None:
						continue

					(
						clones,
						id_map,
						new_definition_id,
						new_prototype_id,
						removed_reporter_count,
					) = cloned

					def estimated_variant_block(block_id, block):
						if not isinstance(block, dict):
							return block

						if not getattr(opts, "rename_procedure_names", False):
							return block

						if block_id != new_prototype_id:
							return block

						probe = copy.deepcopy(block)
						mutation = probe.get("mutation")

						if (
							isinstance(mutation, dict)
							and mutation.get("proccode") == specialized_proc
						):
							mutation["proccode"] = "a"

						return probe

					call_delta = 0

					for call_id, call, _ in members:
						before_call = len(
							dumps_compact(call).encode("utf-8", "backslashreplace")
						)
						probe_call = copy.deepcopy(call)
						probe_mutation = probe_call.get("mutation")

						if isinstance(probe_mutation, dict):
							probe_mutation["proccode"] = (
								"a"
								if getattr(opts, "rename_procedure_names", False)
								else specialized_proc
							)

						after_call = len(
							dumps_compact(probe_call).encode(
								"utf-8", "backslashreplace"
							)
						)
						call_delta += after_call - before_call

					variant_probe = {
						"clones": clones,
						"members": members,
						"specialized_proc": specialized_proc,
					}
					if projected_size is None:
						projected_size = specialized_proc_trial_size(target, (), opts)
						projected_sizes[ti] = projected_size
					variant_size = specialized_proc_trial_size(
						target, [variant_probe], opts
					)
					if variant_size >= projected_size:
						continue

					original_prototype = blocks.get(info["prototype_id"])
					projected_size = variant_size
					projected_sizes[ti] = projected_size
					original_mutation = (
						(original_prototype or {}).get("mutation")
						if isinstance(original_prototype, dict)
						else None
					)
					original_proccode = (
						original_mutation.get("proccode")
						if isinstance(original_mutation, dict)
						else None
					)

					blocks.update(clones)
					stats["procedure_specialization_blocks_added"] += len(clones)
					stats[
						"procedure_specialization_argument_reporters_removed"
					] += removed_reporter_count

					for old_id, new_id in id_map.items():
						opts.specialized_procedure_block_origins[(ti, new_id)] = old_id

					opts.specialized_procedure_prototype_blocks[
						(ti, info["prototype_id"])
					] = True
					opts.specialized_procedure_prototype_blocks[
						(ti, new_prototype_id)
					] = True

					opts.specialized_procedure_prototype_proccodes[
						(ti, info["prototype_id"])
					] = specialized_proc
					opts.specialized_procedure_prototype_proccodes[
						(ti, new_prototype_id)
					] = specialized_proc

					if isinstance(original_proccode, str):
						pairs = (
							opts.specialized_procedure_prototype_code_pairs.setdefault(
								(ti, original_proccode), set()
							)
						)
						pairs.add(specialized_proc)

					opts.specialized_procedure_new_blocks[ti].update(clones)

					for call_id, call, _ in members:
						mutation = call.get("mutation")

						if not isinstance(mutation, dict):
							continue

						mutation["proccode"] = specialized_proc
						opts.specialized_procedure_call_proccodes[(ti, call_id)] = (
							specialized_proc
						)

					total_variants += 1
					total_calls += len(members)
					existing_variant_count += 1
					changed_this_pass = True

		if not changed_this_pass:
			break

		completed_passes += 1
		pass_idx += 1

	stats["procedure_specialized"] += total_variants
	stats["procedure_specialization_variants"] += total_variants
	stats["procedure_specialization_calls"] += total_calls
	stats["procedure_specialization_passes"] += completed_passes
	return total_variants


def inline_single_use_procedures(project, stats, opts):
	targets = project.get("targets", [])
	opts.inlined_procedure_removed_blocks = [set() for _ in targets]
	opts.inlined_procedure_link_edits = {}
	opts.inlined_procedure_input_edits = {}

	max_passes = max(1, int(getattr(opts, "procedure_inline_passes", 8)))
	total = 0
	total_removed = 0
	total_reporters = 0
	completed_passes = 0

	for _ in range(max_passes):
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
				if (
					not isinstance(block, dict)
					or block.get("opcode") != "procedures_call"
				):
					continue

				proc = procedure_key(block)

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

				if any(
					isinstance(blocks.get(body_id), dict)
					and blocks[body_id].get("opcode") == "control_stop"
					for body_id in info["closure"]
				):
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
				if (
					not isinstance(body_root, str)
					or body_root not in blocks
					or body_root not in closure
				):
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

				if call_next is not None and (
					not isinstance(call_next, str) or call_next not in blocks
				):
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
				used, reporter_map, _arg_names = _procedure_body_argument_uses(
					body_info, blocks
				)

				if used is None:
					continue

				formal_ids = list(info["argument_ids"])
				call_ids = parse_argument_ids(call.get("mutation"))

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
						literal = _folded_literal_input(raw)
						if literal is None:
							can_inline = False
							break

						replacement, _ = constant_to_input(literal, blocks, None)

						if replacement is None:
							can_inline = False
							break

						replacements[formal_id] = (
							set(reporter_map.get(idx, ())),
							replacement,
						)
					elif not _procedure_input_is_discardable(raw, blocks, graph):
						can_inline = False
						break

				if not can_inline:
					continue

				all_reporters = (
					set().union(*(set(v) for v in reporter_map.values()))
					if reporter_map
					else set()
				)
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

				for survivor_id in closure - removed:
					block = blocks.get(survivor_id)

					if not isinstance(block, dict):
						continue

					for key in ("next", "parent"):
						ref = block.get(key)
						if ref in removed:
							if (
								survivor_id == body_root
								and key == "parent"
								and ref == definition_id
							):
								continue
							can_inline = False
							break

					if not can_inline:
						break

					for input_name, raw in (block.get("inputs") or {}).items():
						check_value = planned_inputs.get((survivor_id, input_name), raw)

						if any(
							ref in removed for ref in iter_input_block_refs(check_value)
						):
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
					opts.inlined_procedure_input_edits[
						(ti, survivor_id, input_name)
					] = copy.deepcopy(new_raw)

				root_block["parent"] = parent_id
				opts.inlined_procedure_link_edits[(ti, body_root, "parent")] = parent_id
				tail_block["next"] = call_next
				opts.inlined_procedure_link_edits[(ti, tail_id, "next")] = call_next

				if call_next is not None:
					next_block["parent"] = tail_id
					opts.inlined_procedure_link_edits[(ti, call_next, "parent")] = (
						tail_id
					)

				if parent_mode == "next":
					parent_block["next"] = body_root
					opts.inlined_procedure_link_edits[(ti, parent_id, "next")] = (
						body_root
					)
				else:
					parent_block["inputs"][parent_input_name] = parent_input_replacement
					opts.inlined_procedure_input_edits[
						(ti, parent_id, parent_input_name)
					] = copy.deepcopy(parent_input_replacement)

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


def _clear_procedure_definition_shadow(target, proto_id):
	blocks = target.get("blocks") or {}
	proto = blocks.get(proto_id)

	if not isinstance(proto, dict):
		return False

	parent = blocks.get(proto.get("parent"))

	if not isinstance(parent, dict):
		return False

	inputs = parent.get("inputs") or {}
	custom = inputs.get("custom_block")

	if (
		not isinstance(custom, list)
		or not custom
		or scratch_numeric_tag(custom[0]) != INPUT_DIFF_BLOCK_SHADOW
	):
		return False

	if len(custom) < 2 or custom[1] != proto_id:
		return False

	custom[:] = [INPUT_BLOCK_NO_SHADOW, proto_id]
	return True


def clear_procedure_definition_shadows(project, stats, opts):
	total = 0

	for target in project.get("targets", []):
		blocks = target.get("blocks") or {}

		for proto_id, proto in list(blocks.items()):
			if (
				not isinstance(proto, dict)
				or proto.get("opcode") != "procedures_prototype"
			):
				continue

			if _clear_procedure_definition_shadow(target, proto_id):
				total += 1

	stats["procedure_definition_shadows_cleared"] += total
	return total


def compact_procedure_prototypes(project, stats, opts):
	targets = project.get("targets", [])
	opts.procedure_prototype_compaction_removed_blocks = [set() for _ in targets]
	opts.procedure_prototype_compaction_input_edits = {}
	opts.procedure_prototype_compaction_shadow_edits = {}
	total = 0
	total_reporters = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		for proto_id, proto in list(blocks.items()):
			if (
				not isinstance(proto, dict)
				or proto.get("opcode") != "procedures_prototype"
			):
				continue

			parent_id = proto.get("parent")
			parent = blocks.get(parent_id) if isinstance(parent_id, str) else None

			if proto.get("shadow") is not False:
				proto["shadow"] = False
				opts.procedure_prototype_compaction_shadow_edits[(ti, proto_id)] = False

			if isinstance(parent, dict):
				inputs = parent.get("inputs") or {}
				custom = inputs.get("custom_block")

				if (
					isinstance(custom, list)
					and len(custom) >= 2
					and custom[0] == INPUT_SAME_BLOCK_SHADOW
					and custom[1] == proto_id
				):
					canonical = custom[:2]

					if custom != canonical:
						inputs["custom_block"] = canonical
						parent["inputs"] = inputs
						opts.procedure_prototype_compaction_input_edits[
							(ti, parent_id)
						] = {"custom_block": copy.deepcopy(canonical)}

			removed = set()

			for child_id, child in list(blocks.items()):
				if child_id == proto_id or not isinstance(child, dict):
					continue

				if child.get("parent") != proto_id:
					continue

				if not str(child.get("opcode", "")).startswith("argument_reporter_"):
					continue

				removed.add(child_id)

			for child_id in removed:
				del blocks[child_id]

			opts.procedure_prototype_compaction_removed_blocks[ti].update(removed)
			total_reporters += len(removed)

			old_inputs = proto.get("inputs") or {}

			if old_inputs:
				proto["inputs"] = {}
				opts.procedure_prototype_compaction_input_edits[(ti, proto_id)] = {}
			elif "inputs" not in proto:
				...
			else:
				proto["inputs"] = {}

			total += 1

	stats["procedure_prototypes_compacted"] += total
	stats["procedure_prototype_argument_reporters_removed"] += total_reporters
	return total


def optimize_procedure_arguments(project, stats, opts):
	opts.procedure_argument_mutation_edits = {}
	opts.procedure_argument_removed_inputs = {}
	opts.procedure_argument_input_edits = {}
	opts.procedure_argument_removed_blocks = [set() for _ in project.get("targets", [])]
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
				proc = procedure_key(block)

				if proc is not None:
					calls_by_proc.setdefault(proc, []).append((bid, block))

		for proc, infos in by_proc.items():
			if not infos:
				continue

			base_ids = infos[0]["argument_ids"]
			base_mut = infos[0]["mutation"]
			kinds = get_argument_kinds(proc, len(base_ids))

			if kinds is None:
				continue

			if any(info["argument_ids"] != base_ids for info in infos):
				# different argument IDs are OK, but the positions/count must agree
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
					safe = True

					for call_id, call in calls:
						call_ids = parse_argument_ids(call.get("mutation"))

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

				if used and kind == "%b":
					continue

				if not calls:
					continue

				observed = None
				ok = True

				for call_id, call in calls:
					call_ids = parse_argument_ids(call.get("mutation"))

					if call_ids is None or idx >= len(call_ids):
						ok = False
						break

					call_value = (call.get("inputs") or {}).get(call_ids[idx])
					literal = _folded_literal_input(call_value)

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

			# build per-definition reporter replacement plans for constant args
			constant_reporters = {}
			can_constant_fold = True
			for info, usage in body_uses:
				_, reporter_map, arg_names = usage
				for idx, literal in constants.items():
					reporters = set(reporter_map.get(idx, ()))

					if not reporters:
						continue

					if any(
						bid
						in {
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
				only_unused = [idx for idx in removable if idx not in constants]
				removed_set = set(only_unused)
				constants = {}

				if not removed_set:
					continue

			commented_ids = get_comment_ids(target)
			safe_removed = set(removed_set)
			for call_id, call in calls:
				call_mut = call.get("mutation")
				call_ids = parse_argument_ids(call_mut)
				if (
					not isinstance(call_mut, dict)
					or call_ids is None
					or len(call_ids) != len(base_ids)
				):
					safe_removed.clear()
					break

				call_inputs = call.get("inputs") or {}
				for idx in tuple(safe_removed):
					subtree = _exclusive_input_block_subtree(
						call_inputs.get(call_ids[idx]), blocks, call_id
					)
					if subtree is None or any(
						bid in commented_ids
						or (
							isinstance(blocks.get(bid), dict)
							and "comment" in blocks[bid]
						)
						for bid in (subtree or ())
					):
						safe_removed.discard(idx)

			removed_set = safe_removed
			constants = {
				idx: literal for idx, literal in constants.items() if idx in removed_set
			}
			if not removed_set:
				continue
			remaining_indices = [
				i for i in range(len(base_ids)) if i not in removed_set
			]

			call_removed_subtrees = {}
			for call_id, call in calls:
				call_ids = parse_argument_ids(call.get("mutation"))
				if call_ids is None or len(call_ids) != len(base_ids):
					call_removed_subtrees[call_id] = set()
					continue
				call_inputs = call.get("inputs") or {}
				removed_blocks = set()
				for idx in removed_set:
					subtree = _exclusive_input_block_subtree(
						call_inputs.get(call_ids[idx]), blocks, call_id
					)
					if subtree:
						removed_blocks.update(subtree)
				call_removed_subtrees[call_id] = removed_blocks

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
					new_proc = remove_procedure_placeholder(new_proc, idx)

					if new_proc is None:
						break

				if new_proc is None:
					continue

				new_ids = [old_ids[i] for i in remaining_indices]
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

				proto["inputs"] = old_inputs if old_inputs else {}

				for idx, literal in constants.items():
					reporters = constant_reporters.get(
						(info["definition_id"], idx), set()
					)

					if not reporters:
						continue

					replacement, _ = constant_to_input(literal, blocks, None)

					if replacement is None:
						continue

					for parent_id in info["closure"]:
						parent = blocks.get(parent_id)

						if not isinstance(parent, dict):
							continue

						for input_name, raw in list(
							(parent.get("inputs") or {}).items()
						):
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

			# rewrite every call with the same signature
			for call_id, call in calls:
				call_mut = call.get("mutation")
				call_ids = parse_argument_ids(call_mut)
				if (
					not isinstance(call_mut, dict)
					or call_ids is None
					or len(call_ids) != len(base_ids)
				):
					continue

				old_ids = list(call_ids)
				new_ids = [old_ids[i] for i in remaining_indices]
				new_inputs = dict(call.get("inputs") or {})

				for idx in removed_set:
					new_inputs.pop(old_ids[idx], None)

				new_proc = proc

				for idx in sorted(removed_set, reverse=True):
					new_proc = remove_procedure_placeholder(new_proc, idx)

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

				removed_value_blocks = call_removed_subtrees.get(call_id, set())
				for value_block_id in removed_value_blocks:
					blocks.pop(value_block_id, None)
				opts.procedure_argument_removed_blocks[ti].update(removed_value_blocks)

			reference_counts = count_block_refs(target)
			old_names = json.loads(base_mut.get("argumentnames") or "[]")
			removed_names = {old_names[i] for i in removed_set if i < len(old_names)}

			for reporter_id, block in list(blocks.items()):
				if not isinstance(block, dict) or block.get("opcode", "") not in (
					"argument_reporter_string_number",
					"argument_reporter_boolean",
				):
					continue

				field = (block.get("fields") or {}).get("VALUE")
				name = (
					field[0]
					if isinstance(field, list) and field and isinstance(field[0], str)
					else None
				)

				if (
					name not in removed_names
					or reference_counts.get(reporter_id, 1) != 1
				):
					continue

				if not any(
					reporter_id in info["closure"]
					or block.get("parent") == info["prototype_id"]
					for info, _usage in body_uses
				):
					continue

				blocks.pop(reporter_id, None)
				opts.procedure_argument_removed_blocks[ti].add(reporter_id)
				total_reporters_removed += 1

			total_removed += len(removed_set)
			total_constant += len(constants)
			graph.rebuild()

	stats["procedure_arguments_removed"] += total_removed
	stats["procedure_constant_arguments_folded"] += total_constant
	stats["procedure_argument_reporters_removed"] += total_reporters_removed
	return total_removed


def _canonicalize_procedure(target, info, graph=None, procedure_aliases=None):
	blocks = target.get("blocks") or {}
	closure = info["closure"]
	if graph is None:
		graph = ProjectGraph(target)
	procedure_aliases = procedure_aliases or {}
	self_proc = info["mutation"].get("proccode")
	procedure_arg_ids = list(info.get("argument_ids", []))
	procedure_arg_id_map = {old: f"@arg{i}" for i, old in enumerate(procedure_arg_ids)}
	procedure_arg_names = {}
	raw_argument_names = info["mutation"].get("argumentnames")

	try:
		decoded_argument_names = (
			json.loads(raw_argument_names)
			if isinstance(raw_argument_names, str)
			else None
		)
	except (TypeError, ValueError):
		decoded_argument_names = None

	if isinstance(decoded_argument_names, list) and len(decoded_argument_names) == len(
		procedure_arg_ids
	):
		procedure_arg_names = {
			name: f"@arg{i}"
			for i, name in enumerate(decoded_argument_names)
			if isinstance(name, str)
		}

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
			for ref in reversed(list(iter_input_block_refs(inputs[name]))):
				if ref in closure and ref not in seen:
					stack.append(ref)

		nxt = block.get("next")

		if isinstance(nxt, str) and nxt in closure and nxt not in seen:
			stack.append(nxt)

	labels = {bid: f"$B{index}" for index, bid in enumerate(order)}

	def canonical_proc_reference(proccode):
		if not isinstance(proccode, str):
			return proccode

		alias = procedure_aliases.get(proccode, proccode)
		return "@SELF" if alias == self_proc else f"@PROC:{alias}"

	def canonical_proccode_signature(proccode, argument_count):
		if not isinstance(proccode, str):
			return None

		kinds = tuple(re.findall(r"(?<!\S)(%s|%b)(?!\S)", proccode))
		return kinds if len(kinds) == argument_count else None

	def canonical_input(value, arg_id_map=None):
		if not isinstance(value, list) or not value:
			return value

		tag = value[0]
		out = copy.deepcopy(value)

		if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
			if len(out) > 1:
				item = out[1]

				if isinstance(item, str) and item in labels:
					out[1] = labels[item]
				elif isinstance(item, list):
					out[1] = canonical_input(item, arg_id_map)
		elif tag == INPUT_DIFF_BLOCK_SHADOW:
			for i in (1, 2):
				if len(out) <= i:
					continue

				item = out[i]

				if isinstance(item, str) and item in labels:
					out[i] = labels[item]
				elif isinstance(item, list):
					out[i] = canonical_input(item, arg_id_map)

		return out

	def canonical_mutation(block, is_prototype=False):
		mut = block.get("mutation") if isinstance(block, dict) else None

		if not isinstance(mut, dict):
			return None

		out = {}
		argids = parse_argument_ids(mut)

		for key in sorted(mut):
			value = mut[key]

			if key == "proccode":
				if is_prototype:
					out[key] = canonical_proccode_signature(value, len(argids or []))
				else:
					out[key] = canonical_proc_reference(value)
				continue

			if key == "argumentids":
				if argids is not None:
					out[key] = [f"@arg{i}" for i in range(len(argids))]
					continue

			if key == "argumentnames":
				try:
					names = json.loads(value) if isinstance(value, str) else None
				except (TypeError, ValueError):
					names = None

				if isinstance(names, list) and len(names) == len(argids or []):
					out[key] = [f"@arg{i}" for i in range(len(names))]
					continue

			out[key] = copy.deepcopy(value)

		return out

	result = {
		"prototype_mutation": canonical_mutation(info["prototype"], True),
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
				ids = parse_argument_ids(block.get("mutation"))
				arg_map = {old: f"@arg{i}" for i, old in enumerate(ids or [])}

				for name in sorted(block["inputs"]):
					cname = arg_map.get(name, name)
					inputs[cname] = canonical_input(block["inputs"][name], arg_map)

				item[key] = inputs
			elif key == "mutation":
				item[key] = canonical_mutation(
					block, block.get("opcode") == "procedures_prototype"
				)
			elif key == "fields":
				fields = {}

				for name, value in block.get("fields", {}).items():
					field = copy.deepcopy(value)

					if (
						block.get("opcode", "").startswith("argument_reporter_")
						and name == "VALUE"
						and isinstance(field, list)
					):
						if field and isinstance(field[0], str):
							field[0] = procedure_arg_names.get(field[0], field[0])

						if len(field) > 1 and isinstance(field[1], str):
							field[1] = procedure_arg_id_map.get(field[1], field[1])

					fields[name] = field

				item[key] = fields
			else:
				item[key] = copy.deepcopy(block[key])

		item["next"] = labels.get(block.get("next"))
		item["parent"] = labels.get(block.get("parent"))
		result["blocks"].append(item)
	return json.dumps(result, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _procedure_merge_safe(target, info, graph, commented):
	closure = set(info["closure"])

	if closure & commented:
		return False

	return not any(
		any(parent not in closure for parent in graph.parents.get(bid, set()))
		for bid in closure
	)


def _rewrite_procedure_call_to_canonical(call, canonical_proc, canonical_ids):
	mut = call.get("mutation")
	old_ids = parse_argument_ids(mut)

	if (
		not isinstance(mut, dict)
		or old_ids is None
		or len(old_ids) != len(canonical_ids)
	):
		return False

	inputs = call.get("inputs") or {}
	new_inputs = {}

	for index, old_id in enumerate(old_ids):
		if old_id in inputs:
			new_inputs[canonical_ids[index]] = inputs[old_id]

	for key, value in inputs.items():
		if key not in old_ids:
			new_inputs[key] = value

	new_mut = copy.deepcopy(mut)
	new_mut["proccode"] = canonical_proc
	new_mut["argumentids"] = json.dumps(
		canonical_ids, separators=(",", ":"), ensure_ascii=False
	)
	call["mutation"] = new_mut
	call["inputs"] = new_inputs
	return True


def merge_duplicate_procedures(project, stats, opts):
	opts.merged_procedure_removed_blocks = [set() for _ in project.get("targets", [])]
	stats["duplicate_procedures_merged"] += 0
	stats["duplicate_procedure_blocks_removed"] += 0
	total_procedures = 0
	total_blocks = 0

	for ti, target in enumerate(project.get("targets", [])):
		max_passes = max(1, len(target.get("blocks") or {}) // 2 + 1)
		procedure_aliases = {}

		for _pass in range(max_passes):
			by_proc, graph = _procedure_infos(target)

			if not by_proc:
				break

			procedure_aliases.update(
				{proc: procedure_aliases.get(proc, proc) for proc in by_proc}
			)
			comments = target.get("comments") or {}
			commented = {
				c.get("blockId")
				for c in comments.values()
				if isinstance(c, dict) and c.get("blockId") is not None
			}
			buckets = {}

			for proc, infos in by_proc.items():
				for info in infos:
					if not _procedure_merge_safe(target, info, graph, commented):
						continue

					signature = _canonicalize_procedure(
						target, info, graph, procedure_aliases
					)
					buckets.setdefault(signature, []).append((proc, info))

			merged_this_pass = 0

			for entries in buckets.values():
				if len(entries) < 2:
					continue

				group_by_proc = {}

				for proc, info in entries:
					group_by_proc.setdefault(proc, []).append(info)

				if any(
					len(group_by_proc[proc]) != len(by_proc.get(proc, ()))
					for proc in group_by_proc
				):
					continue

				if len(group_by_proc) == 1 and len(entries) <= 1:
					continue

				call_counts = Counter()

				for block in (target.get("blocks") or {}).values():
					if (
						isinstance(block, dict)
						and block.get("opcode") == "procedures_call"
					):
						call_counts[procedure_key(block)] += 1

				def key(item):
					proc, info = item
					return (
						len(proc or "") * call_counts.get(proc, 0) + len(proc or ""),
						len(proc or ""),
						proc or "",
						info["definition_id"],
					)

				canonical_proc, canonical = min(entries, key=key)
				canonical_ids = list(canonical["argument_ids"])

				if any(
					len(info["argument_ids"]) != len(canonical_ids)
					for _, info in entries
				):
					continue

				valid = True

				for source_proc, info in entries:
					if source_proc == canonical_proc:
						continue

					for call in (target.get("blocks") or {}).values():
						if (
							isinstance(call, dict)
							and call.get("opcode") == "procedures_call"
							and procedure_key(call) == source_proc
						):
							old_ids = parse_argument_ids(call.get("mutation"))
							if old_ids is None or len(old_ids) != len(canonical_ids):
								valid = False
								break

					if not valid:
						break

				if not valid:
					continue

				for source_proc, info in entries:
					if info is canonical:
						continue

					if source_proc != canonical_proc:
						for call in list((target.get("blocks") or {}).values()):
							if (
								isinstance(call, dict)
								and call.get("opcode") == "procedures_call"
								and procedure_key(call) == source_proc
							):
								_rewrite_procedure_call_to_canonical(
									call, canonical_proc, canonical_ids
								)

						procedure_aliases[source_proc] = canonical_proc

					closure = set(info["closure"])
					blocks = target.get("blocks") or {}

					for bid in closure:
						blocks.pop(bid, None)

					opts.merged_procedure_removed_blocks[ti].update(closure)
					total_blocks += len(closure)
					total_procedures += 1
					merged_this_pass += 1

			if not merged_this_pass:
				break

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

		graph = ProjectGraph(target)
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

			custom = b.get("inputs", {}).get("custom_block", [None, None])
			proto_id = (
				custom[1] if len(custom) > 1 and isinstance(custom[1], str) else None
			)
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
					proc = procedure_key(b)

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

		stats["procedure_dangling_block_refs_fixed"] += repair_dangling_block_refs(
			target
		)

	stats["procedures_removed"] += removed_procedures
	stats["procedure_blocks_removed"] += removed_blocks
	return removed_procedures
