from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from minify_options import Options
from typing import Any
from .constants import *

SUBGRAPH_MAX_WINDOW = 8
SUBGRAPH_MAX_WINDOWS = 250_000
SUBGRAPH_MAX_CANDIDATES = 1_500
SUBGRAPH_FORBIDDEN_OPCODES = {
	"control_stop",
	"control_delete_this_clone",
}


def get_subgraph_literal(value: Any) -> list[None]:
	if not (isinstance(value, list) and len(value) == 2):
		return None

	wrapper = scratch_numeric_tag(value[0])

	if wrapper not in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
		return None

	literal = value[1]

	if not (isinstance(literal, list) and len(literal) == 2):
		return None

	primitive_tag = scratch_numeric_tag(literal[0])
	raw = literal[1]

	if primitive_tag in NUMERIC_TAGS:
		if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
			return None

		if isinstance(raw, float) and not math.isfinite(raw):
			return None

		return copy.deepcopy(literal)

	if primitive_tag == PRIMITIVE_TEXT and isinstance(raw, str):
		return copy.deepcopy(literal)

	return None


def _subgraph_signature(block_id, blocks, memo, active, include_next=False):
	key = (block_id, bool(include_next))

	if key in memo:
		return memo[key]

	if key in active:
		return None

	block = blocks.get(block_id)
	if not isinstance(block, dict):
		return None

	opcode = block.get("opcode")

	if not isinstance(opcode, str) or not opcode:
		return None

	if opcode.startswith(("procedures_", "argument_reporter_", "event_")):
		return None

	if opcode == "control_start_as_clone" or opcode in SUBGRAPH_FORBIDDEN_OPCODES:
		return None

	active.add(key)
	child_weight = 0
	invalid = False

	def normalize_input(value):
		nonlocal child_weight, invalid

		if not isinstance(value, list) or not value:
			return copy.deepcopy(value)

		tag = scratch_numeric_tag(value[0])

		if tag not in INPUT_TAGS:
			return copy.deepcopy(value)

		normalized = copy.deepcopy(value)
		literal = get_subgraph_literal(value)
		slots = (1, 2) if tag == INPUT_DIFF_BLOCK_SHADOW else (1,)
		for index in slots:
			if len(value) <= index:
				continue

			item = value[index]

			if (
				tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW)
				and index == 1
				and literal is not None
			):
				normalized[index] = ["$literal", scratch_numeric_tag(literal[0])]
				continue

			if isinstance(item, str):
				if item not in blocks:
					invalid = True
					continue

				child = _subgraph_signature(item, blocks, memo, active, True)

				if child is None:
					invalid = True
					continue

				normalized[index] = ["$block", child[0]]
				child_weight += child[1]
			elif isinstance(item, (list, dict)):
				normalized[index] = copy.deepcopy(item)

		return normalized

	try:
		inputs = block.get("inputs") or {}

		if not isinstance(inputs, dict):
			return None

		normalized_inputs = {}

		for name in sorted(inputs):
			normalized_inputs[name] = normalize_input(inputs[name])

			if invalid:
				return None

		ignored = {"next", "parent", "topLevel", "x", "y", "inputs"}
		metadata = {
			name: copy.deepcopy(value)
			for name, value in block.items()
			if name not in ignored
		}
		payload = {"metadata": metadata, "inputs": normalized_inputs}
		weight = 1 + child_weight
		if include_next:
			nxt = block.get("next")
			if isinstance(nxt, str):
				if nxt not in blocks:
					return None

				next_signature = _subgraph_signature(nxt, blocks, memo, active, True)

				if next_signature is None:
					return None

				payload["next"] = next_signature[0]
				weight += next_signature[1]
			elif nxt is None:
				payload["next"] = None
			else:
				return None

		digest = hashlib.sha256(
			json.dumps(
				payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
			).encode("utf-8")
		).hexdigest()
		result = (digest, weight)
		memo[key] = result
		return result
	finally:
		active.discard(key)


def _subgraph_input_refs(value, blocks):
	if not isinstance(value, list) or not value:
		return

	tag = scratch_numeric_tag(value[0])

	if tag in INPUT_TAGS:
		indices = (1, 2) if tag == INPUT_DIFF_BLOCK_SHADOW else (1,)

		for index in indices:
			if len(value) <= index:
				continue

			item = value[index]

			if isinstance(item, str) and item in blocks:
				yield item
			elif isinstance(item, (list, dict)):
				yield from _subgraph_input_refs(item, blocks)
		return

	for item in value[1:]:
		if isinstance(item, (list, dict)):
			yield from _subgraph_input_refs(item, blocks)


def _subgraph_segment_closure(blocks, sequence, graph):
	closure = set(sequence)
	stack = []

	for bid in sequence:
		block = blocks.get(bid)
		if isinstance(block, dict):
			for value in (block.get("inputs") or {}).values():
				stack.extend(_subgraph_input_refs(value, blocks))

	while stack:
		bid = stack.pop()

		if bid in closure or bid not in blocks:
			continue

		block = blocks.get(bid)

		if not isinstance(block, dict):
			continue

		closure.add(bid)

		for child in graph.edges.get(bid, ()):
			if child not in closure:
				stack.append(child)

		for value in (block.get("inputs") or {}).values():
			stack.extend(_subgraph_input_refs(value, blocks))

	return closure


def get_subgraph_sequence_runs(target):
	blocks = target.get("blocks") or {}
	incoming_next = {
		block.get("next")
		for block in blocks.values()
		if isinstance(block, dict) and isinstance(block.get("next"), str)
	}
	runs = []

	for bid, block in blocks.items():
		if not isinstance(block, dict) or bid in incoming_next:
			continue

		opcode = block.get("opcode", "")

		if (
			opcode.startswith(("event_", "procedures_definition"))
			or opcode == "control_start_as_clone"
		):
			start = block.get("next")
		elif block.get("topLevel") is True:
			start = bid
		elif isinstance(block.get("parent"), str):
			owner_edge = get_control_owner_edge(blocks, bid)
			if (
				owner_edge is None
				or owner_edge[1] != "input"
				or not isinstance(owner_edge[2], str)
				or not owner_edge[2].startswith("SUBSTACK")
			):
				continue
			start = bid
		else:
			continue

		if not isinstance(start, str) or start not in blocks:
			continue

		sequence = []
		seen = set()
		current = start

		while isinstance(current, str) and current in blocks and current not in seen:
			current_block = blocks.get(current)

			if not isinstance(current_block, dict):
				break

			current_opcode = current_block.get("opcode", "")

			if (
				current_opcode.startswith(
					("event_", "procedures_", "argument_reporter_")
				)
				or current_opcode == "control_start_as_clone"
			):
				break

			seen.add(current)
			sequence.append(current)
			current = current_block.get("next")

		if sequence:
			runs.append(sequence)

	return runs


def collect_subgraph_literals(sequence, blocks):
	slots = []
	seen = set()

	def visit(block_id, follow_next):
		if block_id in seen:
			return

		block = blocks.get(block_id)

		if not isinstance(block, dict):
			return

		seen.add(block_id)
		inputs = block.get("inputs") or {}

		for name in sorted(inputs):
			value = inputs[name]
			literal = get_subgraph_literal(value)
			if literal is not None:
				slots.append(
					{
						"block_id": block_id,
						"input_name": name,
						"value": copy.deepcopy(value),
						"literal": literal,
					}
				)
			for child in _subgraph_input_refs(value, blocks):
				visit(child, True)

		if follow_next:
			nxt = block.get("next")
			if isinstance(nxt, str) and nxt in blocks:
				visit(nxt, True)

	for bid in sequence:
		visit(bid, False)

	return slots


def _subgraph_new_id(existing):
	bid = _new_id()

	while bid in existing:
		bid = _new_id()

	existing.add(bid)
	return bid


def analyze_occurence(target, sequence, graph, commented):
	blocks = target.get("blocks") or {}
	if not sequence or any(bid not in blocks for bid in sequence):
		return None
	closure = _subgraph_segment_closure(blocks, sequence, graph)
	if not closure or closure & commented:
		return None
	for bid in closure:
		block = blocks.get(bid)
		if not isinstance(block, dict) or "comment" in block:
			return None
		opcode = block.get("opcode", "")
		if (
			not isinstance(opcode, str)
			or opcode.startswith(("procedures_", "argument_reporter_", "event_"))
			or opcode == "control_start_as_clone"
			or opcode in SUBGRAPH_FORBIDDEN_OPCODES
		):
			return None
		nxt = block.get("next")
		if isinstance(nxt, str) and nxt not in closure and bid != sequence[-1]:
			return None
		if isinstance(nxt, str) and nxt not in blocks:
			return None
		for value in (block.get("inputs") or {}).values():
			if any(ref not in closure for ref in _subgraph_input_refs(value, blocks)):
				return None

	root_id = sequence[0]
	root = blocks[root_id]
	root_parent = root.get("parent")
	owner = None
	if isinstance(root_parent, str):
		owner_edge = get_control_owner_edge(blocks, root_id)
		if owner_edge is None:
			return None
		owner_id, edge_kind, edge_name = owner_edge
		if owner_id in closure:
			return None
		owner = {"owner_id": owner_id, "edge_kind": edge_kind, "edge_name": edge_name}
	else:
		if root.get("topLevel") is not True:
			return None
		owner = {"owner_id": None, "edge_kind": None, "edge_name": None}

	for bid in closure:
		external_parents = set(graph.parents.get(bid, ())) - closure
		if bid == root_id and owner["owner_id"] is not None:
			if external_parents != {owner["owner_id"]}:
				return None
		elif external_parents:
			return None

	tail_id = sequence[-1]
	continuation_id = blocks[tail_id].get("next")
	if isinstance(continuation_id, str):
		if continuation_id in closure:
			return None
		continuation = blocks.get(continuation_id)
		if (
			not isinstance(continuation, dict)
			or continuation.get("parent") != tail_id
			or set(graph.parents.get(continuation_id, ())) != {tail_id}
		):
			return None
	elif continuation_id is not None:
		return None

	warp = _sequence_warp_context(blocks, root_id)
	return {
		"sequence": tuple(sequence),
		"closure": closure,
		"owner": owner,
		"continuation_id": continuation_id,
		"warp": warp,
		"slots": collect_subgraph_literals(sequence, blocks),
	}


def are_compatible_subgraphs(info, selected):
	for other in selected:
		if info["closure"] & other["closure"]:
			return False
		info_owner = info["owner"]["owner_id"]
		other_owner = other["owner"]["owner_id"]
		if info_owner in other["closure"] or other_owner in info["closure"]:
			return False
		if (
			info["continuation_id"] in other["closure"]
			or other["continuation_id"] in info["closure"]
		):
			return False
		if info_owner is not None and info_owner == other["continuation_id"]:
			return False
		if other_owner is not None and other_owner == info["continuation_id"]:
			return False
	return True


def make_subgraph_plan(target, occurrences):
	if len(occurrences) < 2 or len({item["warp"] for item in occurrences}) != 1:
		return None
	blocks = target.get("blocks") or {}
	slot_lists = [item["slots"] for item in occurrences]
	slot_count = len(slot_lists[0])
	if any(len(items) != slot_count for items in slot_lists):
		return None
	for index in range(slot_count):
		first = slot_lists[0][index]
		if any(
			item[index]["input_name"] != first["input_name"]
			or scratch_numeric_tag(item[index]["literal"][0])
			!= scratch_numeric_tag(first["literal"][0])
			or item[index]["value"][0] != first["value"][0]
			for item in slot_lists[1:]
		):
			return None

	param_vectors = {}
	parameter_slot_indices = []
	parameter_defaults = []
	for slot_index in range(slot_count):
		vector = tuple(
			dumps_compact(items[slot_index]["literal"]) for items in slot_lists
		)
		if len(set(vector)) <= 1:
			continue
		parameter_index = param_vectors.get(vector)
		if parameter_index is None:
			parameter_index = len(parameter_slot_indices)
			param_vectors[vector] = parameter_index
			parameter_slot_indices.append([])
			raw = slot_lists[0][slot_index]["literal"][1]
			parameter_defaults.append(str(raw))
		parameter_slot_indices[parameter_index].append(slot_index)

	existing_ids = set(blocks)
	def_id = _subgraph_new_id(existing_ids)
	proto_id = _subgraph_new_id(existing_ids)
	argument_ids = [_subgraph_new_id(existing_ids) for _ in parameter_slot_indices]
	argument_names = [f"p{i}" for i in range(len(argument_ids))]
	proc_codes = {
		procedure_key(block)
		for block in blocks.values()
		if isinstance(block, dict)
		and block.get("opcode") in ("procedures_prototype", "procedures_call")
		and isinstance(procedure_key(block), str)
	}
	proc_counter = 0
	while True:
		proc_name = f"sg{proc_counter}" + (
			" " + " ".join("%s" for _ in argument_ids) if argument_ids else ""
		)
		if proc_name not in proc_codes:
			break
		proc_counter += 1

	representative = occurrences[0]
	body_map = {
		bid: _subgraph_new_id(existing_ids) for bid in representative["closure"]
	}
	generated = {}
	for old_id in representative["closure"]:
		old = blocks[old_id]
		new = copy.deepcopy(old)
		new.pop("topLevel", None)
		new.pop("x", None)
		new.pop("y", None)
		parent_id = old.get("parent")
		if old_id == representative["sequence"][0]:
			new["parent"] = def_id
		elif parent_id in body_map:
			new["parent"] = body_map[parent_id]
		else:
			return None
		next_id = old.get("next")
		new["next"] = body_map.get(next_id)
		for input_value in (new.get("inputs") or {}).values():
			replace_input_block_ids(input_value, body_map)
		generated[body_map[old_id]] = new

	for parameter_index, slot_indices in enumerate(parameter_slot_indices):
		arg_id = argument_ids[parameter_index]
		for slot_index in slot_indices:
			slot = slot_lists[0][slot_index]
			clone_id = body_map[slot["block_id"]]
			generated[clone_id].setdefault("inputs", {})[slot["input_name"]] = [
				INPUT_BLOCK_NO_SHADOW,
				_subgraph_new_id(existing_ids),
			]
			reporter_id = generated[clone_id]["inputs"][slot["input_name"]][1]
			generated[reporter_id] = {
				"opcode": "argument_reporter_string_number",
				"next": None,
				"parent": clone_id,
				"inputs": {},
				"fields": {"VALUE": [argument_names[parameter_index], None]},
			}

	warp = representative["warp"]
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
			"argumentdefaults": json.dumps(parameter_defaults, separators=(",", ":")),
			"warp": warp,
		},
	}
	generated[def_id] = {
		"opcode": "procedures_definition",
		"next": body_map[representative["sequence"][0]],
		"parent": None,
		"inputs": {"custom_block": [INPUT_SAME_BLOCK_SHADOW, proto_id]},
		"fields": {},
		"topLevel": True,
		"x": 5,
		"y": 5,
	}

	call_blocks = []
	for occurrence_index, occurrence in enumerate(occurrences):
		call_id = _subgraph_new_id(existing_ids)
		call_inputs = {}
		for parameter_index, slot_indices in enumerate(parameter_slot_indices):
			slot = slot_lists[occurrence_index][slot_indices[0]]
			call_inputs[argument_ids[parameter_index]] = copy.deepcopy(slot["value"])
		root_id = occurrence["sequence"][0]
		root = blocks[root_id]
		call = {
			"opcode": "procedures_call",
			"next": occurrence["continuation_id"],
			"parent": occurrence["owner"]["owner_id"],
			"inputs": call_inputs,
			"fields": {},
			"mutation": {
				"tagName": "mutation",
				"children": [],
				"proccode": proc_name,
				"argumentids": json.dumps(argument_ids, separators=(",", ":")),
				"warp": warp,
			},
		}
		if occurrence["owner"]["owner_id"] is None:
			call["topLevel"] = True
			call["x"] = root.get("x", 5)
			call["y"] = root.get("y", 5)
		call_blocks.append({"id": call_id, "block": call, "occurrence": occurrence})

	return {
		"new_blocks": generated,
		"call_blocks": call_blocks,
		"removed": set().union(*(item["closure"] for item in occurrences)),
		"proc_name": proc_name,
	}


def apply_subgraph_plan(target, plan, opts, stats, ti):
	blocks = target.get("blocks") or {}
	removed = plan["removed"]
	for call_entry in plan["call_blocks"]:
		occurrence = call_entry["occurrence"]
		root_id = occurrence["sequence"][0]
		tail_id = occurrence["sequence"][-1]
		if root_id not in blocks or any(
			bid not in blocks for bid in occurrence["closure"]
		):
			return False
		owner = occurrence["owner"]
		owner_id = owner["owner_id"]
		if owner_id is not None:
			if owner_id in removed or owner_id not in blocks:
				return False
			owner_block = blocks[owner_id]
			if owner["edge_kind"] == "next":
				if owner_block.get("next") != root_id:
					return False
			else:
				value = (owner_block.get("inputs") or {}).get(owner["edge_name"])
				if not (
					isinstance(value, list) and len(value) > 1 and value[1] == root_id
				):
					return False
		continuation_id = occurrence["continuation_id"]
		if isinstance(continuation_id, str):
			if continuation_id in removed or continuation_id not in blocks:
				return False
			if blocks[continuation_id].get("parent") != tail_id:
				return False

	for call_entry in plan["call_blocks"]:
		occurrence = call_entry["occurrence"]
		root_id = occurrence["sequence"][0]
		tail_id = occurrence["sequence"][-1]
		owner = occurrence["owner"]
		owner_id = owner["owner_id"]
		call_id = call_entry["id"]
		if owner_id is not None:
			owner_block = blocks[owner_id]
			if owner["edge_kind"] == "next":
				owner_block["next"] = call_id
				opts.script_rewrite_link_edits[(ti, owner_id, "next")] = call_id
			else:
				replacement = get_input_replacement(
					owner_block, "input", owner["edge_name"], root_id, call_id
				)
				if replacement is None:
					return False
				opts.script_rewrite_input_edits[(ti, owner_id, owner["edge_name"])] = (
					copy.deepcopy(owner_block.get("inputs", {}).get(owner["edge_name"]))
				)
		blocks[call_id] = call_entry["block"]
		opts.script_rewrite_link_edits[(ti, call_id, "parent")] = owner_id
		opts.script_rewrite_link_edits[(ti, call_id, "next")] = occurrence[
			"continuation_id"
		]
		if isinstance(occurrence["continuation_id"], str):
			blocks[occurrence["continuation_id"]]["parent"] = call_id
			opts.script_rewrite_link_edits[
				(ti, occurrence["continuation_id"], "parent")
			] = call_id

	blocks.update(plan["new_blocks"])
	record_new_blocks(
		opts,
		ti,
		set(plan["new_blocks"]) | {entry["id"] for entry in plan["call_blocks"]},
	)
	record_removed_blocks(opts, ti, removed)
	for bid in removed:
		blocks.pop(bid, None)
	stats["subgraph_factor_groups"] += 1
	stats["subgraph_factor_occurrences"] += len(plan["call_blocks"])
	stats["subgraph_factor_blocks_removed"] += len(removed)
	stats["subgraph_factor_blocks_added"] += len(plan["new_blocks"]) + len(
		plan["call_blocks"]
	)
	return True


def factor_repeated_subgraphs(project, stats, opts, progress) -> int:
	targets = project.get("targets", [])
	script_prepare_maps(opts, targets)
	total = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}

		if len(blocks) < 6:
			continue

		progress.detail(f"target {target['name']}")

		signature_memo = {}
		groups = defaultdict(list)
		windows_seen = 0

		for run in get_subgraph_sequence_runs(target):
			for start in range(len(run)):
				signatures = []
				max_length = (
					(len(run) - start)
					if opts.unbounded_subgraph_window
					else min(SUBGRAPH_MAX_WINDOW, len(run) - start)
				)

				for length in range(1, max_length + 1):
					bid = run[start + length - 1]
					signature = _subgraph_signature(
						bid, blocks, signature_memo, set(), False
					)

					if signature is None:
						break

					signatures.append(signature)
					sequence = tuple(run[start : start + length])
					groups[tuple(item[0] for item in signatures)].append(sequence)
					windows_seen += 1

					if windows_seen >= SUBGRAPH_MAX_WINDOWS:
						break

				if windows_seen >= SUBGRAPH_MAX_WINDOWS:
					break

			if windows_seen >= SUBGRAPH_MAX_WINDOWS:
				break

		if windows_seen >= SUBGRAPH_MAX_WINDOWS:
			stats["subgraph_factor_search_capped"] += 1

		candidates = []
		for key, sequences in groups.items():
			if len(sequences) < 2:
				continue

			unique = list(dict.fromkeys(sequences))

			if len(unique) < 2:
				continue

			weight = sum(
				(
					_subgraph_signature(bid, blocks, signature_memo, set(), False)
					or ("", 1)
				)[1]
				for bid in unique[0]
			)
			candidates.append((weight * (len(unique) - 1), tuple(unique)))

		candidates.sort(key=lambda item: item[0], reverse=True)

		candidates = candidates[:SUBGRAPH_MAX_CANDIDATES]

		stats["subgraph_factor_candidates"] += len(candidates)

		for _score, sequences in candidates:
			if (
				sum(
					1
					for seq in sequences
					if all(bid in (target.get("blocks") or {}) for bid in seq)
				)
				< 2
			):
				continue

			current_graph = ProjectGraph(target)
			current_commented = get_comment_ids(target)
			selected = []

			for sequence in sequences:
				info = analyze_occurence(
					target, sequence, current_graph, current_commented
				)

				if info is None or any(
					info["warp"] != item["warp"] for item in selected
				):
					continue

				if not are_compatible_subgraphs(info, selected):
					continue
				selected.append(info)

			if len(selected) < 2:
				continue

			plan = make_subgraph_plan(target, selected)

			if plan is None:
				continue

			before = _json_len(target)
			trial_target = copy.deepcopy(target)
			trial_opts = Options()
			script_prepare_maps(trial_opts, [trial_target])
			trial_stats = Counter()

			if not apply_subgraph_plan(
				trial_target, copy.deepcopy(plan), trial_opts, trial_stats, ti
			):
				continue

			after = _json_len(trial_target)

			if after >= before:
				stats["subgraph_factor_rejected_size"] += 1
				continue

			local_stats = Counter()

			if not apply_subgraph_plan(target, plan, opts, local_stats, ti):
				continue

			stats["subgraph_factor_bytes_saved"] += before - after

			for key, value in local_stats.items():
				stats[key] += value

			total += 1

	return total
