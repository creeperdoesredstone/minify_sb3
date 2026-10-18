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
		print(line[: width - 1].ljust(width - 1), end="", flush=True)

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


def progress_stage_count(opts: Options) -> int:
	count = 1  # normalize block defaults
	count += opts.convert_wav_to_mp3
	count += opts.deduplicate_assets
	count += opts.comments
	count += opts.positions
	count += opts.covered
	count += opts.monitors
	count += opts.remove_unreachable
	count += opts.remove_unused_procedures
	count += opts.remove_unreachable or opts.remove_unused_procedures
	count += (
		opts.constant_propagation
		or opts.fold_constant_expressions
		or opts.associative_constant_merging
		or opts.factor_math_expressions
	)
	count += bool(opts.cleared_lists)
	count += bool(opts.folded_constant_variables)
	count += (
		opts.branch_swapping
		or opts.branch_factoring
		or opts.trivial_loops
		or opts.nested_conditionals
	)
	count += opts.simplify_boolean_control or opts.trivial_loops
	count += opts.simplify_blocks
	count += opts.group_similar_sequences
	count += opts.compact_procedure_prototypes
	count += opts.clear_procedure_definition_shadows
	count += opts.optimize_procedure_arguments
	count += opts.merge_duplicate_procedures
	count += opts.specialize_procedures
	count += opts.inline_single_use_procedures
	count += opts.remove_unused_procedures and (
		opts.optimize_procedure_arguments or opts.merge_duplicate_procedures
	)
	count += opts.remove_unused_variables or opts.remove_unused_lists
	count += True
	count += opts.remove_unused_broadcasts
	count += (
		opts.rename_identifiers
		or opts.rename_variable_names
		or opts.rename_list_names
		or opts.rename_broadcast_names
		or opts.rename_argument_names
		or opts.rename_procedure_names
	)
	count += opts.rename_variable_ids or opts.rename_list_ids
	count += opts.rename_broadcast_ids
	count += opts.rename_argument_ids
	count += opts.rename_block_ids
	count += opts.compact_numeric_inputs
	count += opts.compact_field_ids
	count += opts.compact_mutation_hasnext
	count += opts.compact_mutation_metadata
	count += opts.normalize_numbers
	count += not opts.keep_sound_metadata
	count += opts.remove_empty_fields
	count += opts.remove_empty_inputs
	count += opts.remove_costume_metadata
	count += opts.remove_default_target_properties
	count += opts.remove_empty_target_containers
	count += opts.remove_project_meta
	count += opts.compact_data_literals
	count += opts.strip_reference_names
	count += opts.remove_unused_extensions
	count += True
	count += opts.relabel_block_ids
	count += opts.optimize_json
	count += True
	count += True
	return count


def short_id(index):
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


def mapped_block_id(value, block_ids):
	return block_ids.get(value, value) if isinstance(value, str) else value


def replace_input_block_ids(value, block_ids):
	for node in iterate_input_nodes(value):
		tag = node[0]
		if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW) and len(node) > 1:
			node[1] = mapped_block_id(node[1], block_ids)
		elif tag == INPUT_DIFF_BLOCK_SHADOW:
			if len(node) > 1:
				node[1] = mapped_block_id(node[1], block_ids)
			if len(node) > 2:
				node[2] = mapped_block_id(node[2], block_ids)


def count_input_block_refs(value, blocks, refs):
	for ref in iter_input_block_refs(value):
		if ref in blocks:
			refs[ref] += 1


def count_block_refs(target):
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
			for value in block.get("inputs", {}).values():
				count_input_block_refs(value, blocks, refs)
	for comment in target.get("comments", {}).values():
		if isinstance(comment, dict):
			cbid = comment.get("blockId")
			if isinstance(cbid, str) and cbid in blocks:
				refs[cbid] += 1
	return refs


def analyze_block_ids(target):
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

		for value in block.get("inputs", {}).values():
			for ref in iter_input_block_refs(value):
				if ref in ids:
					refs[ref] += 1
				else:
					dangling.add(ref)

	for comment in target.get("comments", {}).values():
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
			refs, dangling = analyze_block_ids(target)
			old_ids = sorted(old_ids, key=lambda x: (-refs[x], x))
		else:
			_, dangling = analyze_block_ids(target)
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

				for value in block.get("inputs", {}).values():
					replace_input_block_ids(value, block_ids)

		for comment in target.get("comments", {}).values():
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


def collect_dangling_in_input(value, ids, dangling):
	for ref in iter_input_block_refs(value):
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


def round_num(v):
	if not isinstance(v, (int, float)):
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
						n = round_num(block[k])
						if n != block[k] or type(n) is not type(block[k]):
							block[k] = n
							stats["rounded"] += 1
			elif isinstance(block, list) and len(block) >= 5:  # [12, name, id, x, y]
				for i in (3, 4):
					n = round_num(block[i])
					if n != block[i] or type(n) is not type(block[i]):
						block[i] = n
						stats["rounded"] += 1

		for comment in target.get("comments", {}).values():
			if not isinstance(comment, dict):
				continue

			for k in ("x", "y", "width", "height"):
				if k in comment:
					n = round_num(comment[k])
					if n != comment[k] or type(n) is not type(comment[k]):
						comment[k] = n
						stats["rounded"] += 1

		for costume in target.get("costumes", []):
			if not isinstance(costume, dict):
				continue

			for k in ("rotationCenterX", "rotationCenterY"):
				if k in costume:
					n = round_num(costume[k])

					if n != costume[k] or type(n) is not type(costume[k]):
						costume[k] = n
						stats["rounded"] += 1


def get_bool_input_names(block) -> frozenset:
	if not isinstance(block, dict):
		return frozenset()

	op = block.get("opcode")
	names = set(BOOLEAN_INPUTS.get(op, ()))

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
						i
						for i, k in zip(ids, kinds)
						if k == "%b" and isinstance(i, str)
					)

	return frozenset(names)


def is_boolean_slot(block, input_name) -> bool:
	return input_name in get_bool_input_names(block)


def scratch_numeric_tag(value) -> int | None:
	if isinstance(value, JsonNumber):
		try:
			return int(value)
		except (TypeError, ValueError, OverflowError):
			return None

	if type(value) is int:
		return value
	return None


def is_literal_input(value) -> bool:
	if not (isinstance(value, list) and len(value) > 1):
		return False

	tag = scratch_numeric_tag(value[0])
	if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_DIFF_BLOCK_SHADOW):
		return isinstance(value[1], list)

	return False


def clear_covered_values(project, stats):
	for target in project.get("targets", []):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict):
				continue

			for iv in block.get("inputs", {}).values():
				if not (
					isinstance(iv, list)
					and len(iv) == 3
					and iv[0] == INPUT_DIFF_BLOCK_SHADOW
				):
					continue

				shadow = iv[2]
				if not (isinstance(shadow, list) and len(shadow) == 2):
					continue  # block-id reference or 3-long broadcast: leave alone

				tag = shadow[0]
				if tag in NUMERIC_TAGS:
					empty = 0
				elif tag == PRIMITIVE_TEXT:
					empty = ""
				else:
					continue  # color (9) etc.: validated by sb3fix, keep

				if shadow[1] != empty or type(shadow[1]) is not type(empty):
					shadow[1] = empty
					stats["covered"] += 1


def _variable_ids_used_by_blocks(project):
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
					f = block.get("fields", {}).get(fk)

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


def _json_len(obj: object) -> int:
	return len(
		json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
	)


def get_list_usage(project, ti, list_id) -> Counter:
	targets = project.get("targets", [])
	owner = targets[ti]
	if owner.get("isStage"):
		scan = [
			i
			for i, t in enumerate(targets)
			if i == ti or list_id not in t.get("lists", {})
		]
	else:
		scan = [ti]

	use = Counter()

	def scan_prim(e):
		if isinstance(e, list):
			if len(e) >= 3 and e[0] == PRIMITIVE_LIST and e[2] == list_id:
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

			f = block.get("fields", {}).get("LIST")

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

			for iv in block.get("inputs", {}).values():
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
) -> list[dict[str, Any]]:
	found: list[dict[str, Any]] = []

	for ti, target in enumerate(project.get("targets", [])):
		for lid, entry in target.get("lists", {}).items():
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

			label, risky = describe_list_usage(get_list_usage(project, ti, lid))
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


def parse_selection(text: str, n: int) -> set[str]:
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

	invalid_list_nums = sorted(i for i in chosen if not 1 <= i <= n)

	if invalid_list_nums:
		raise ValueError(f"no such list number(s): {invalid_list_nums} (valid: 1-{n})")

	return chosen


def prompt_for_lists(project, candidates, project_bytes) -> set | None:
	if not candidates:
		return set()
	total = sum(c["bytes"] for c in candidates)
	w_scope = max(len(c["scope"]) for c in candidates)
	w_name = min(34, max(len(c["name"]) for c in candidates))

	print()
	print(
		Ansi.heading(
			f"Large lists found: {len(candidates)}  "
			f"({total:,} bytes = {total / max(1, project_bytes) * 100:.1f}% of project.json)"
		)
	)
	print()
	print(
		f"  {'#':>3}  {'scope':<{w_scope}}  {'list':<{w_name}}  {'items':>6}  {'bytes':>8}  usage"
	)

	for i, c in enumerate(candidates, 1):
		nm = (
			c["name"]
			if len(c["name"]) <= w_name
			else c["name"][: w_name - 1] + "Ã¢â‚¬Â¦"
		)
		mark = Ansi.warning("!") if c["risky"] else " "
		print(
			f" {mark}{i:>3}  {c['scope']:<{w_scope}}  {nm:<{w_name}}  {c['items']:>6,}  {c['bytes']:>8,}  {c['label']}"
		)

	print()
	print(
		Ansi.warning(
			"  '!' = clearing this list can change how the project behaves. Blocks that read a"
		)
	)
	print(
		"  cleared list get empty values, and 'replace item N' on an emptied list does nothing."
	)
	print("  Cleared lists stay defined, only their contents are removed.")
	print()

	print(Ansi.muted("  Enter / n   keep every list"))
	print(Ansi.muted("  a           clear all large lists"))
	print(Ansi.muted("  u           clear only lists nothing references"))
	print(Ansi.muted("  1,3,5-7     clear those numbers"))
	print(Ansi.muted("  s N         show a sample of list N"))
	print()

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
				picked = parse_selection(answer, len(candidates))
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


def iterate_input_nodes(value):
	stack = [value]

	while stack:
		node = stack.pop()
		if not isinstance(node, list) or not node:
			continue

		yield node
		tag = node[0]

		if tag in (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW):
			if len(node) > 1 and isinstance(node[1], list):
				stack.append(node[1])
		elif tag == INPUT_DIFF_BLOCK_SHADOW:
			if len(node) > 2 and isinstance(node[2], list):
				stack.append(node[2])
			if len(node) > 1 and isinstance(node[1], list):
				stack.append(node[1])
		else:
			for child in node[1:]:
				if isinstance(child, list):
					stack.append(child)


def get_data_refs(project: dict[str, Any], var_owners=None, list_owners=None):
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
	named_ids = {}

	for ti, target in enumerate(targets):
		for kind in ("variables", "lists"):
			for ident, entry in (target.get(kind) or {}).items():
				if isinstance(entry, list) and entry and isinstance(entry[0], str):
					named_ids.setdefault((ti, kind, entry[0]), []).append(ident)

	def add_named_refs(ti, kind, name, refs, desc):
		if not isinstance(name, str):
			return

		for owner in {ti, stage_index}:
			for ident in named_ids.get((owner, kind, name), ()):
				refs.setdefault((owner, ident), []).append(desc)

	def add_var_ref(ti, i, desc, name=None):
		add_named_refs(ti, "variables", name, var_refs, desc)

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

	def add_list_ref(ti, i, desc, name=None):
		add_named_refs(ti, "lists", name, list_refs, desc)

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
				if block[0] == PRIMITIVE_VARIABLE:
					add_var_ref(ti, block[2], desc, block[1])
				elif block[0] == PRIMITIVE_LIST:
					add_list_ref(ti, block[2], desc, block[1])
				elif block[0] == PRIMITIVE_BROADCAST:
					broadcast_refs.setdefault(block[2], []).append(desc)

			for v in (
				block.get("inputs", {}).values() if isinstance(block, dict) else ()
			):
				for node in iterate_input_nodes(v):
					if len(node) <= 2 or not isinstance(node[2], str):
						continue

					tag = node[0]

					if tag == PRIMITIVE_VARIABLE:
						add_var_ref(ti, node[2], desc, node[1])
					elif tag == PRIMITIVE_LIST:
						add_list_ref(ti, node[2], desc, node[1])
					elif tag == PRIMITIVE_BROADCAST:
						broadcast_refs.setdefault(node[2], []).append(desc)

			if not isinstance(block, dict):
				continue

			fields = block.get("fields", {})
			for name, f in fields.items():
				if isinstance(f, list) and f and name in ("VARIABLE", "LIST"):
					add_named_refs(
						ti,
						"variables" if name == "VARIABLE" else "lists",
						f[0],
						var_refs if name == "VARIABLE" else list_refs,
						desc,
					)

				if not (isinstance(f, list) and len(f) > 1 and isinstance(f[1], str)):
					continue

				if name == "VARIABLE":
					add_var_ref(ti, f[1], desc)

				elif name == "LIST":
					add_list_ref(ti, f[1], desc)

				elif name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
					broadcast_refs.setdefault(f[1], []).append(desc)

			if block.get("opcode") in ("sensing_of", "sensing_of_property_menu"):
				prop_field = fields.get("PROPERTY")
				if (
					isinstance(prop_field, list)
					and len(prop_field) > 0
					and isinstance(prop_field[0], str)
				):
					prop_name = prop_field[0]
					for target_idx, t in enumerate(targets):
						for vid, vdata in t.get("variables", {}).items():
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
			add_var_ref(ti, mid, desc, m.get("params", {}).get("VARIABLE"))
		elif op == "data_listcontents" and isinstance(mid, str):
			add_list_ref(ti, mid, desc, m.get("params", {}).get("LIST"))

	return var_refs, list_refs, broadcast_refs


def _collect_data_ids(project):
	var_refs, list_refs, broadcast_refs = get_data_refs(project)
	return set(var_refs.keys()), set(list_refs.keys()), set(broadcast_refs.keys())


def _rename_id_map(ids, existing_ids=(), prefix=""):
	used = set(existing_ids)
	result = {}
	n = 0

	for old in ids:
		new = f"{prefix}{short_id(n)}"
		while new in used:
			n += 1
			new = f"{prefix}{short_id(n)}"
		result[old] = new
		used.add(new)
		n += 1

	return result


def _frequency_optimal_id_map(ids, frequencies=None, existing_ids=(), prefix=""):
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
	fields = block.get("fields", {})
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

	var_refs, list_refs, _ = get_data_refs(project) if frequency_order else ({}, {}, {})

	var_map = {}
	list_map = {}
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
			if len(value) > 2 and value[0] == PRIMITIVE_VARIABLE:
				value[2] = rewrite_var(ti, value[2])
			elif len(value) > 2 and value[0] == PRIMITIVE_LIST:
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
				if len(block) > 2 and block[0] == PRIMITIVE_VARIABLE:
					block[2] = rewrite_var(ti, block[2])
				elif len(block) > 2 and block[0] == PRIMITIVE_LIST:
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


def replace_broadcast_ids_in_value(value, mapping):
	for node in iterate_input_nodes(value):
		if (
			len(node) > 2
			and node[0] == PRIMITIVE_BROADCAST
			and isinstance(node[2], str)
			and node[2] in mapping
		):
			node[2] = mapping[node[2]]


def get_broadcast_refs(project: dict[str, Any]) -> dict[str, Any]:
	refs = {}

	def add(bid: str, name: str | None = None, desc: str | None = None):
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
				get_broadcast_refs_in_value(
					value, add, f"block {bid!r} ({block.get('opcode')}) in {tname!r}"
				)

	return refs


def get_broadcast_refs_in_value(value, add, desc=None):
	for node in iterate_input_nodes(value):
		if (
			len(node) > 2
			and node[0] == PRIMITIVE_BROADCAST
			and isinstance(node[2], str)
		):
			name = node[1] if len(node) > 1 and isinstance(node[1], str) else None
			add(node[2], name, desc)


def get_all_broadcast_ids(project):
	ids = set()

	for target in project.get("targets", []):
		ids.update((target.get("broadcasts") or {}).keys())

	return ids


def repair_dangling_broadcast_refs(project, stats):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)

	if stage_index is None:
		return set(), {}

	stage = targets[stage_index]
	broadcasts = stage.setdefault("broadcasts", {})
	defined = get_all_broadcast_ids(project)
	repaired = set()
	conflicts = {}

	for bid, info in get_broadcast_refs(project).items():
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
					get_broadcast_ids_in_value(value, ids)

	if frequency_order:
		_, _, bcast_refs = get_data_refs(project)
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
				replace_broadcast_ids_in_value(value, mapping)

	stats["broadcast_ids"] += len(mapping)
	return mapping


def get_broadcast_ids_in_value(value, ids):
	if not isinstance(value, list) or not value:
		return

	if len(value) > 2 and value[0] == PRIMITIVE_BROADCAST and isinstance(value[2], str):
		ids.add(value[2])
	else:
		for v in value:
			get_broadcast_ids_in_value(v, ids)


def parse_argument_ids(mut):
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

			vals = parse_argument_ids(b.get("mutation"))
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

	var_refs, list_refs, broadcast_refs = get_data_refs(project)
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
			if (vid not in valid_variables or not rename_variable_names)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		}
		reserved_lists = {
			entry[0]
			for lid, entry in lists.items()
			if (lid not in valid_lists or not rename_list_names)
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
			_frequency_optimal_id_map(valid_lists, list_frequencies, reserved_lists)
			if rename_list_names
			else {}
		)

		if rename_variable_names:
			variable_new_names.update(
				{(stage_index, vid): stage_var_map[vid] for vid in valid_variables}
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
				{(stage_index, lid): stage_list_map[lid] for lid in valid_lists}
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
			if (vid not in valid_variables or not rename_variable_names)
			and isinstance(entry, list)
			and entry
			and isinstance(entry[0], str)
		)
		reserved_lists = set(stage_list_names)
		reserved_lists.update(
			entry[0]
			for lid, entry in lists.items()
			if (lid not in valid_lists or not rename_list_names)
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
			_frequency_optimal_id_map(valid_lists, list_frequencies, reserved_lists)
			if rename_list_names
			else {}
		)

		if rename_variable_names:
			variable_new_names.update(
				{(ti, vid): var_map[vid] for vid in valid_variables}
			)

		for vid, entry in valid_variables.items():
			new_name = variable_new_names.get((ti, vid), entry[0])
			if rename_variable_names and entry[0] != new_name:
				variable_names[(ti, vid)] = entry[0]
				entry[0] = new_name
				changed += 1

		if rename_list_names:
			list_new_names.update({(ti, lid): list_map[lid] for lid in valid_lists})

		for lid, entry in valid_lists.items():
			new_name = list_new_names.get((ti, lid), entry[0])
			if rename_list_names and entry[0] != new_name:
				list_names[(ti, lid)] = entry[0]
				entry[0] = new_name
				changed += 1

		if rename_variable_names:
			variable_new_names.update(
				{(ti, vid): var_map[vid] for vid in valid_variables}
			)

		for vid, entry in valid_variables.items():
			if rename_variable_names and entry[0] != variable_new_names[(ti, vid)]:
				variable_names[(ti, vid)] = entry[0]
				entry[0] = variable_new_names[(ti, vid)]
				changed += 1

		if rename_list_names:
			list_new_names.update({(ti, lid): list_map[lid] for lid in valid_lists})

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
				proccode = (
					mutation.get("proccode") if isinstance(mutation, dict) else None
				)

				if isinstance(proccode, str) and proccode:
					procedure_frequencies[proccode] += 1

			old_proccodes = list(procedure_frequencies)
			procedure_alphabet = (
				"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
			)

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
		new_names = _frequency_optimal_id_map(old_argument_names, argument_frequencies)
		argument_new_names[ti] = new_names
		argument_names.update(
			{(ti, new): old for old, new in new_names.items() if old != new}
		)

	def get_owner(ti, id, value_type: str) -> tuple | None:
		if id in targets[ti].get(value_type, {}):
			return (ti, id)

		if stage_index is not None and id in targets[stage_index].get(value_type, {}):
			return (stage_index, id)

		return None

	def variable_owner(ti, variable_id) -> tuple | None:
		return get_owner(ti, variable_id, "variables")

	def list_owner(ti, list_id) -> tuple | None:
		return get_owner(ti, list_id, "lists")

	def rename_nested_names(ti, value):
		nonlocal changed
		if isinstance(value, list):
			if (
				len(value) > 2
				and value[0] == PRIMITIVE_VARIABLE
				and isinstance(value[2], str)
			):
				owner = variable_owner(ti, value[2])

				if (
					owner in variable_new_names
					and value[1] != variable_new_names[owner]
				):
					value[1] = variable_new_names[owner]
					changed += 1
				return

			if (
				len(value) > 2
				and value[0] == PRIMITIVE_LIST
				and isinstance(value[2], str)
			):
				owner = list_owner(ti, value[2])
				if owner in list_new_names and value[1] != list_new_names[owner]:
					value[1] = list_new_names[owner]
					changed += 1
				return

			if (
				len(value) > 2
				and value[0] == PRIMITIVE_BROADCAST
				and value[2] in broadcast_new_names
			):
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

				if isinstance(field, list) and field and isinstance(field[0], str):
					property_ti = resolve_sensing_of_target(
						project, ti, block, stage_index
					)
					new_prop = resolve_renamed_property_name(
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


def get_target_idx_from_obj(targets, object_name, stage_index):
	if not isinstance(object_name, str):
		return None

	if object_name == "_stage_":
		return stage_index

	for target_index, target in enumerate(targets):
		if target.get("name") == object_name:
			return target_index

	return None


def resolve_sensing_of_target(project, ti, block, stage_index):
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
								index = get_target_idx_from_obj(
									targets, field[0], stage_index
								)
								if index is not None:
									return index
						index = get_target_idx_from_obj(targets, ref, stage_index)
						if index is not None:
							return index
				else:
					index = resolve(ref)
					if index is not None:
						return index

			if value[0] == PRIMITIVE_TEXT and isinstance(value[1], str):
				index = get_target_idx_from_obj(targets, value[1], stage_index)
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
				index = get_target_idx_from_obj(targets, field[0], stage_index)
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


def resolve_renamed_property_name(
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


def restore_identifier_names(project, renamed_names):
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
			if (
				len(value) > 2
				and value[0] == PRIMITIVE_VARIABLE
				and isinstance(value[2], str)
			):
				owner = restore_variable_owner(ti, value[2])
				if owner in variable_names:
					value[1] = variable_names[owner]
				return

			if (
				len(value) > 2
				and value[0] == PRIMITIVE_LIST
				and isinstance(value[2], str)
			):
				owner = restore_list_owner(ti, value[2])
				if owner in list_names:
					value[1] = list_names[owner]
				return

			if (
				len(value) > 2
				and value[0] == PRIMITIVE_BROADCAST
				and value[2] in broadcast_names
			):
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

				if isinstance(field, list) and field and isinstance(field[0], str):
					property_ti = resolve_sensing_of_target(
						project, ti, block, stage_index
					)
					restored = resolve_original_property_name(
						ti,
						field[0],
						variable_name_rev,
						list_name_rev,
						stage_index,
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


def resolve_original_property_name(
	ti,
	current_name,
	variable_name_rev,
	list_name_rev,
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
		key = (owner_ti, current_name)

		if key in variable_name_rev:
			return variable_name_rev[key]

		if key in list_name_rev:
			return list_name_rev[key]

	return None


def all_referenced_ids(project):
	vars_, lists_, broadcasts = _collect_data_ids(project)
	return vars_, lists_, broadcasts


def remove_unused_data(project, stats, remove_variables=True, remove_lists=True):
	used_vars, used_lists, _ = all_referenced_ids(project)
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
