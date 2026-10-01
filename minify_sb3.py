import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import zipfile
import zlib
from collections import Counter


BLOCK_ID_ALPHABET = '!@#$%^*()+_-={}|[]:;?,./~ABCDEFGHJKLMNOPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789'


class Ansi:
	RESET = "\033[0m"
	BOLD = "\033[1m"
	DIM = "\033[2m"
	RED = "\033[31m"
	GREEN = "\033[32m"
	YELLOW = "\033[33m"
	CYAN = "\033[36m"

	def __init__(self, enabled=None):
		self.enabled = sys.stdout.isatty() if enabled is None else enabled

	def paint(self, text, *codes):
		if not self.enabled:
			return text
		return f"{''.join(codes)}{text}{self.RESET}"

	def heading(self, text):
		return self.paint(text, self.BOLD, self.CYAN)

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


Ansi = Ansi()


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
	if not isinstance(value, list) or not value:
		return
	if value[0] in (1, 2) and len(value) > 1:
		value[1] = _mapped_block_id(value[1], block_ids)
	elif value[0] == 3:
		if len(value) > 1:
			value[1] = _mapped_block_id(value[1], block_ids)
		if len(value) > 2:
			if isinstance(value[2], str):
				value[2] = _mapped_block_id(value[2], block_ids)
			else:
				_replace_input_block_ids(value[2], block_ids)


def _count_input_block_refs(value, blocks, refs):
	if not isinstance(value, list) or not value:
		return
	if value[0] in (1, 2) and len(value) > 1:
		if isinstance(value[1], str) and value[1] in blocks:
			refs[value[1]] += 1
	elif value[0] == 3:
		if len(value) > 1 and isinstance(value[1], str) and value[1] in blocks:
			refs[value[1]] += 1
		if len(value) > 2:
			if isinstance(value[2], str) and value[2] in blocks:
				refs[value[2]] += 1
			else:
				_count_input_block_refs(value[2], blocks, refs)
	else:
		for v in value:
			_count_input_block_refs(v, blocks, refs)


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
			refs = _count_block_references(target)
			old_ids = sorted(old_ids, key=lambda x: (-refs[x], x))

		dangling = _dangling_block_ids(target)
		block_ids = _rename_id_map(old_ids, existing_ids=dangling)
		all_maps[ti] = block_ids

		new_blocks = {block_ids.get(old_id, old_id): block for old_id, block in blocks.items()}
		for block in new_blocks.values():
			if isinstance(block, dict):
				for key in ("next", "parent"):
					if block.get(key) in block_ids:
						block[key] = block_ids[block[key]]
				for value in (block.get("inputs") or {}).values():
					_replace_input_block_ids(value, block_ids)
		for comment in (target.get("comments") or {}).values():
			if isinstance(comment, dict) and "blockId" in comment and comment["blockId"] is not None:
				comment["blockId"] = block_ids.get(comment["blockId"], comment["blockId"])
		target["blocks"] = new_blocks
		total_stats += len(block_ids)

	stats["block_ids"] += total_stats
	stats["dangling_refs_skipped"] += dangling_skipped
	return all_maps


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
		for value in (block.get("inputs") or {}).values():
			_collect_dangling_in_input(value, ids, dangling)
	for comment in (target.get("comments") or {}).values():
		if isinstance(comment, dict):
			bid = comment.get("blockId")
			if isinstance(bid, str) and bid not in ids:
				dangling.add(bid)
	return dangling


def _collect_dangling_in_input(value, ids, dangling):
	if not isinstance(value, list) or not value:
		return
	if value[0] in (1, 2) and len(value) > 1:
		if isinstance(value[1], str) and value[1] not in ids:
			dangling.add(value[1])
	elif value[0] == 3:
		if len(value) > 1 and isinstance(value[1], str) and value[1] not in ids:
			dangling.add(value[1])
		if len(value) > 2:
			if isinstance(value[2], str) and value[2] not in ids:
				dangling.add(value[2])
			else:
				_collect_dangling_in_input(value[2], ids, dangling)
	else:
		for v in value:
			_collect_dangling_in_input(v, ids, dangling)


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


# input type tags whose obscured shadow holds a plain number / text literal
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
	return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=sort_keys)


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


def prompt_for_lists(
	project, candidates, project_bytes
) -> set | None:
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
				input(Ansi.prompt("Clear which lists? [Enter = keep all] > ")).strip().lower()
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
				Ansi.warning(f"  WARNING: {len(risky)} of them are used by the project:")
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


def _walk_block_values(value):
	yield value
	if isinstance(value, dict):
		for v in value.values():
			yield from _walk_block_values(v)
	elif isinstance(value, list):
		for v in value:
			yield from _walk_block_values(v)


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
	stage_var_ids = set(targets[stage_index].get("variables", {})) if stage_index is not None else set()
	stage_list_ids = set(targets[stage_index].get("lists", {})) if stage_index is not None else set()

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
			for v in _walk_block_values(block):
				if isinstance(v, list) and v:
					tag = v[0]
					if tag == 12 and len(v) > 2 and isinstance(v[2], str):
						add_var_ref(ti, v[2], desc)
					elif tag == 13 and len(v) > 2 and isinstance(v[2], str):
						add_list_ref(ti, v[2], desc)
					elif tag == 11 and len(v) > 2 and isinstance(v[2], str):
						broadcast_refs.setdefault(v[2], []).append(desc)

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
				if isinstance(prop_field, list) and len(prop_field) > 0 and isinstance(prop_field[0], str):
					prop_name = prop_field[0]
					for target_idx, t in enumerate(targets):
						for vid, vdata in (t.get("variables") or {}).items():
							if isinstance(vdata, list) and len(vdata) > 0 and vdata[0] == prop_name:
								add_var_ref(target_idx, vid, f"sensing_of property {prop_name!r} in {desc}")

	name_to_index = {t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")}
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
	reserved = set(existing_ids)
	result = {}
	n = 0
	for old in ids:
		new = f"{prefix}{_short_id(n)}"
		while new in reserved or new in result.values():
			n += 1
			new = f"{prefix}{_short_id(n)}"
		result[old] = new
		n += 1
	return result


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


def rename_variable_list_ids(project, stats, rename_variables=True, rename_lists=True, frequency_order=False):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	stage_var_ids = set(targets[stage_index].get("variables", {})) if stage_index is not None else set()
	stage_list_ids = set(targets[stage_index].get("lists", {})) if stage_index is not None else set()

	var_refs, list_refs, _ = _collect_data_references(project) if frequency_order else ({}, {}, {})

	var_map = {}
	list_map = {}

	def sprites_referencing_stage_ids(stage_ids, tag):
		hits = set()
		if stage_index is None or not stage_ids:
			return hits
		for ti, target in enumerate(targets):
			if ti == stage_index:
				continue
			local_ids = set((target.get("variables") if tag == 12 else target.get("lists")) or {})

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
				if isinstance(f, list) and len(f) > 1 and f[1] in stage_ids and f[1] not in local_ids:
					hits.add(ti)
				for value in (block.get("inputs") or {}).values():
					scan(value)
			if ti in hits:
				continue
			for m in project.get("monitors", []):
				if not isinstance(m, dict) or m.get("spriteName") != target.get("name"):
					continue
				op = "data_variable" if tag == 12 else "data_listcontents"
				if m.get("opcode") == op and m.get("id") in stage_ids and m.get("id") not in local_ids:
					hits.add(ti)
		return hits

	allocated_ids = set()
	if rename_variables:
		stage_ids = list(targets[stage_index].get("variables", {}).keys()) if stage_index is not None else []
		if frequency_order and stage_index is not None:
			stage_ids = sorted(stage_ids, key=lambda vid: (-len(var_refs.get((stage_index, vid), [])), vid))
		stage_new_ids = _rename_id_map(stage_ids, existing_ids=allocated_ids) if stage_ids else {}
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
				ids = sorted(ids, key=lambda vid: (-len(var_refs.get((ti, vid), [])), vid))
			new_ids = _rename_id_map(ids, existing_ids=allocated_ids)
			allocated_ids.update(new_ids.values())
			for old, new in new_ids.items():
				var_map[(ti, old)] = new
	if rename_lists:
		stage_ids = list(targets[stage_index].get("lists", {}).keys()) if stage_index is not None else []
		if frequency_order and stage_index is not None:
			stage_ids = sorted(stage_ids, key=lambda lid: (-len(list_refs.get((stage_index, lid), [])), lid))
		stage_new_ids = _rename_id_map(stage_ids, existing_ids=allocated_ids) if stage_ids else {}
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
				ids = sorted(ids, key=lambda lid: (-len(list_refs.get((ti, lid), [])), lid))
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
			if isinstance(block, list):        # [12/13, name, id, x, y]
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

	name_to_index = {t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")}
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
	if isinstance(value, list):
		if len(value) > 2 and value[0] == 12 and value[2] in var_map:
			value[2] = var_map[value[2]]
		elif len(value) > 2 and value[0] == 13 and value[2] in list_map:
			value[2] = list_map[value[2]]
		for v in value:
			_replace_nested_primitive_ids(v, var_map, list_map)
	elif isinstance(value, dict):
		for v in value.values():
			_replace_nested_primitive_ids(v, var_map, list_map)


def _replace_broadcast_ids_in_value(value, mapping):
	if not isinstance(value, list) or not value:
		return
	if len(value) > 2 and value[0] == 11 and isinstance(value[2], str) and value[2] in mapping:
		value[2] = mapping[value[2]]
	else:
		for v in value:
			_replace_broadcast_ids_in_value(v, mapping)


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
					add(f[1], f[0] if isinstance(f[0], str) else None,
						f"block {bid!r} ({block.get('opcode')}) in {tname!r}")
			for value in (block.get("inputs") or {}).values():
				_collect_broadcast_refs_in_value(value, add,
						f"block {bid!r} ({block.get('opcode')}) in {tname!r}")
	return refs


def _collect_broadcast_refs_in_value(value, add, desc=None):
	if not isinstance(value, list) or not value:
		return
	if len(value) > 2 and value[0] == 11 and isinstance(value[2], str):
		name = value[1] if len(value) > 1 and isinstance(value[1], str) else None
		add(value[2], name, desc)
		return
	for v in value:
		_collect_broadcast_refs_in_value(v, add, desc)


def _all_broadcast_ids(project):
	ids = set()
	for target in project.get("targets", []):
		ids.update((target.get("broadcasts") or {}).keys())
	return ids


def _repair_dangling_broadcast_refs(project, stats):
	"""Restore missing broadcast definitions only when their name is unambiguous."""
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
		for old_id in (target.get("broadcasts") or {}):
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
				mapping.get(old, old): name for old, name in target["broadcasts"].items()
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
	mapping = {}   # {(target_index, proccode, old_id): new_id} -- for the verifier
	total = 0
	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		by_proccode = {}
		for bid, b in blocks.items():
			if not isinstance(b, dict) or b.get("opcode") not in ("procedures_prototype", "procedures_call"):
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
					[local_map.get(x, x) for x in vals], separators=(",", ":"), ensure_ascii=False
				)
				inputs = b.get("inputs") or {}
				b["inputs"] = {local_map.get(k, k): v for k, v in inputs.items()}
			for old, new in local_map.items():
				mapping[(ti, proc, old)] = new
			total += len(local_map)

	stats["argument_ids"] += total
	return mapping


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
				is_cloud = isinstance(entry, list) and len(entry) > 2 and entry[2] is True
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
	return op.endswith("_whenflagclicked") or op.endswith("_whenkeypressed") or (
		op.startswith("event_when") or op.startswith("control_start_as_clone")
	) or op in {
		"event_whenthisspriteclicked",
		"event_whenbackdropswitchesto",
		"event_whenbroadcastreceived",
		"event_whengreaterthan",
	}


def _build_block_graph(target):
	blocks = target.get("blocks", {})
	edges = {}
	for bid, b in blocks.items():
		if not isinstance(b, dict):
			edges[bid] = set()
			continue
		out = set()
		nxt = b.get("next")
		if isinstance(nxt, str) and nxt in blocks:
			out.add(nxt)
		for v in (b.get("inputs") or {}).values():
			_collect_block_refs(v, blocks, out)
		edges[bid] = out
	return edges


def _collect_block_refs(value, blocks, out):
	if isinstance(value, list):
		if value and value[0] in (1, 2, 3):
			if len(value) > 1 and isinstance(value[1], str) and value[1] in blocks:
				out.add(value[1])
			if value[0] == 3 and len(value) > 2:
				if isinstance(value[2], str) and value[2] in blocks:
					out.add(value[2])
				else:
					_collect_block_refs(value[2], blocks, out)
		else:
			for v in value:
				_collect_block_refs(v, blocks, out)
	elif isinstance(value, dict):
		for v in value.values():
			_collect_block_refs(v, blocks, out)


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
					# Preserve the shadow value when the covering block vanished.
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
	"""Tolerate detached shadow argument reporters left in some Scratch projects."""
	if not isinstance(block, dict):
		return False
	return (
		block.get("opcode", "").startswith("argument_reporter_")
		and block.get("shadow") is True
		and isinstance(block.get("parent"), (type(None), str))
		and (block.get("parent") is None or block.get("parent") not in blocks)
	)


def _iter_input_block_refs(value):
	"""Yield block IDs referenced by a serialized Scratch input tuple."""
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
	"""Repair block links in both directions without inventing ambiguous edges."""
	blocks = target.get("blocks", {})
	fixed = 0

	# Remove references that cannot resolve.
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

	# When an existing next/input edge unambiguously identifies the owner,
	# repair the child's reverse parent link too. Never overwrite another valid parent.
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
		if isinstance(c, dict) and c.get("blockId") is not None and c["blockId"] not in blocks:
			del comments[cid]
	return fixed

def remove_unreachable_blocks(project, stats):
	removed = 0
	dangling_fixed = 0
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		roots = {
			bid
			for bid, b in blocks.items()
			if isinstance(b, list) or (isinstance(b, dict) and b.get("topLevel"))
		}
		edges = _build_block_graph(target)
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


	stats["blocks_removed"] += removed
	return removed


def _procedure_key(block):
	mut = block.get("mutation") if isinstance(block, dict) else None
	if not isinstance(mut, dict):
		return None
	return mut.get("proccode")


def remove_unused_procedures(project, stats):
	removed_blocks = 0
	removed_procedures = 0

	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		if not blocks:
			continue

		children = {}
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			parent = block.get("parent")
			if isinstance(parent, str) and parent in blocks:
				children.setdefault(parent, set()).add(bid)

		definition_blocks = {}  # (proccode, definition_id) -> complete definition closure
		for bid, b in blocks.items():
			if not isinstance(b, dict) or b.get("opcode") != "procedures_definition":
				continue
			custom = (b.get("inputs") or {}).get("custom_block") or [None, None]
			proto_id = custom[1] if len(custom) > 1 and isinstance(custom[1], str) else None
			proto = blocks.get(proto_id)
			proc = _procedure_key(proto) if isinstance(proto, dict) else None
			if proc is None:
				continue

			# In Scratch serialization, the procedure body is connected through
			# parent links; definition.next is the next top-level stack, not the body.
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

		# Calls outside procedure closures are the program's procedure entry points.
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

		stats["dangling_block_refs_fixed"] += _repair_dangling_block_refs(target)

	stats["procedures_removed"] += removed_procedures
	stats["blocks_removed"] += removed_blocks
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
			["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
		)
		return r.returncode == 0
	except (OSError, subprocess.TimeoutExpired):
		return False


def _encode_wav_to_mp3(wav_bytes):
	try:
		proc = subprocess.run(
			[
				"ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
				"-i", "pipe:0",
				"-c:a", "libmp3lame", "-b:a", WAV_TO_MP3_BITRATE,
				"-ar", str(WAV_TO_MP3_SAMPLE_RATE), "-ac", str(WAV_TO_MP3_CHANNELS),
				"-f", "mp3", "pipe:1",
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

	# The source may be shared by several sounds, so remove it only after the
	# conversion mapping has been established for every reference.
	for old_name in conversions:
		assets.pop(old_name, None)

	return conversions


def remove_sound_metadata(project):
	for target in project.get("targets", []):
		for sound in target.get("sounds", []):
			if isinstance(sound, dict):
				sound.pop("rate", None)
				sound.pop("sampleCount", None)


def compact_redundant_field_ids(project, stats):
	"""Remove the optional null field-ID slot from serialized Scratch fields.

	Scratch field tuples are commonly serialized as [value, null] when a field
	has no associated variable/list/broadcast ID. The ID slot is optional, so
	[value] is an equivalent and smaller representation. Only an explicit null
	slot is removed; empty-string and non-null IDs are preserved.
	"""
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
	"""Remove mutation.hasnext only when it is explicitly false."""
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
	"""Resolve a variable reference using Scratch's local-then-global scope rules."""
	targets = project.get("targets", [])
	if not isinstance(variable_id, str):
		return None
	if 0 <= ti < len(targets) and variable_id in (targets[ti].get("variables") or {}):
		return (ti, variable_id)
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	if stage_index is not None and variable_id in (targets[stage_index].get("variables") or {}):
		return (stage_index, variable_id)
	return None


def _variable_literal(value):
	"""Return the shortest Scratch primitive representation for a scalar variable value."""
	if isinstance(value, bool):
		# Scratch scalar variables normally deserialize as strings/numbers. Treat a
		# boolean as unsupported rather than silently changing its serialized type.
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
			and isinstance(raw, (bool, str, int, float))
			and (not isinstance(raw, float) or math.isfinite(raw))
		)
	return tag == _TEXT_TAG and isinstance(raw, str)


def _primitive_json_len(value):
	return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _find_constant_variables(project):
	"""Find variables with exactly one set-variable-to and at least one reporter use."""
	targets = project.get("targets", [])
	set_blocks = {}
	reporter_counts = Counter()
	change_counts = Counter()
	setter_ids = {}
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
						if _is_literal_value((block.get("inputs") or {}).get("VALUE")):
							literal_setters.add(owner)
					elif block.get("opcode") == "data_changevariableby":
						change_counts[owner] = change_counts.get(owner, 0) + 1
			for value in (block.get("inputs") or {}).values():
				scan_input(value, ti)

	candidates = []
	for owner, count in set_blocks.items():
		# variable modified by `change variable by` is runtime-mutable -> not foldable.
		if (
			count != 1
			or owner not in literal_setters
			or reporter_counts.get(owner, 0) <= 0
			or change_counts.get(owner, 0) > 0
		):
			continue
		ti, vid = owner
		entry = (targets[ti].get("variables") or {}).get(vid)
		if not isinstance(entry, list) or len(entry) < 2:
			continue

		# Cloud variables are externally mutable -> not foldable.
		if len(entry) >= 3 and entry[2] is True:
			continue
		literal = _variable_literal(entry[1])
		if literal is None:
			continue
		
		old = [12, entry[0], vid]
		per_use = _primitive_json_len(old) - _primitive_json_len(literal)
		total = per_use * reporter_counts[owner]
		scope = "GLOBAL" if targets[ti].get("isStage") else f"local:{targets[ti].get('name')}"
		candidates.append({
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
			"literal": literal,
		})
	candidates.sort(key=lambda c: (-c["bytes"], c["scope"], c["name"], c["id"]))
	return candidates


def _replace_constant_variable_reporters(value, ti, selected, project, stats):
	"""Replace serialized variable-reporter primitives with selected literals."""
	if not isinstance(value, list) or not value:
		if isinstance(value, dict):
			changed = 0
			for child in value.values():
				changed += _replace_constant_variable_reporters(child, ti, selected, project, stats)
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
			changed += _replace_constant_variable_reporters(child, ti, selected, project, stats)
	return changed


def fold_constant_variables(project, selected, stats):
	if not selected:
		return 0
	selected_map = { (ti, vid): literal for (ti, vid), literal in selected.items() }
	changed = 0
	for ti, target in enumerate(project.get("targets", [])):
		for block in (target.get("blocks") or {}).values():
			if not isinstance(block, dict):
				continue
			for value in (block.get("inputs") or {}).values():
				changed += _replace_constant_variable_reporters(value, ti, selected_map, project, stats)
	return changed


def _numeric_primitive_value(value):
	if not (isinstance(value, list) and len(value) == 2 and value[0] in _NUMERIC_TAGS):
		return None
	raw = value[1]
	if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
		return None
	try:
		if isinstance(raw, str):
			text = raw.strip()
			prefixes = (("0b", 2, "01"), ("0o", 8, "01234567"), ("0x", 16, "0123456789abcdef"))
			prefix = next((item for item in prefixes if text.lower().startswith(item[0])), None)
			if prefix is not None:
				digits = text[2:].lower()
				if not digits or any(digit not in prefix[2] for digit in digits):
					return None
				number = float(int(digits, prefix[1]))
			else:
				number = float(raw)
		else:
			number = float(raw)
	except (TypeError, ValueError, OverflowError):
		return None
	return number if math.isfinite(number) else None


def _constant_expression_from_input(value, blocks, visiting=frozenset()):
	if not (isinstance(value, list) and len(value) > 1):
		return None
	if value[0] in (1, 2):
		child = value[1]
	elif value[0] == 3:
		child = value[1]
	else:
		return None

	number = _numeric_primitive_value(child)
	if number is not None:
		return [4, number], set()
	if isinstance(child, str):
		return _constant_expression_block(child, blocks, visiting)
	return None


def _constant_expression_block(block_id, blocks, visiting):
	if block_id in visiting:
		return None
	block = blocks.get(block_id)
	if not isinstance(block, dict):
		return None

	operations = {
		"operator_add": "add",
		"operator_subtract": "subtract",
		"operator_multiply": "multiply",
		"operator_divide": "divide",
		"operator_mod": "modulo",
	}
	operation = operations.get(block.get("opcode"))
	if operation is None:
		return None

	inputs = block.get("inputs") or {}
	left = _constant_expression_from_input(inputs.get("NUM1"), blocks, visiting | {block_id})
	right = _constant_expression_from_input(inputs.get("NUM2"), blocks, visiting | {block_id})
	if left is None or right is None:
		return None

	left_value = float(left[0][1])
	right_value = float(right[0][1])
	try:
		if operation == "add":
			result = left_value + right_value
		elif operation == "subtract":
			result = left_value - right_value
		elif operation == "multiply":
			result = left_value * right_value
		elif operation == "divide":
			if right_value == 0:
				return None
			result = left_value / right_value
		else:
			if right_value == 0:
				return None
			result = left_value % right_value
	except (OverflowError, ValueError, ZeroDivisionError):
		return None
	if not math.isfinite(result):
		return None
	value = int(result) if result.is_integer() else result
	return [4, value], left[1] | right[1] | {block_id}


def fold_constant_expressions(project, stats, opts):
	"""Fold finite numeric arithmetic reporters whose operands are all literals."""
	targets = project.get("targets", [])
	opts.folded_constant_expression_inputs = {}
	opts.folded_constant_expression_blocks = [set() for _ in targets]
	folded = 0

	for ti, target in enumerate(targets):
		blocks = target.get("blocks") or {}
		candidates = []
		for parent_id, parent in blocks.items():
			if not isinstance(parent, dict):
				continue
			for input_name, value in (parent.get("inputs") or {}).items():
				result = _constant_expression_from_input(value, blocks)
				if result is not None and result[1]:
					candidates.append((parent_id, input_name, result[0], result[1]))

		consumed_by_candidates = set().union(*(entry[3] for entry in candidates)) if candidates else set()
		removed = set()
		for parent_id, input_name, literal, expression_blocks in candidates:
			if parent_id in consumed_by_candidates:
				continue
			parent = blocks.get(parent_id)
			if not isinstance(parent, dict):
				continue
			inputs = parent.get("inputs") or {}
			if input_name not in inputs:
				continue
			replacement = [1, literal]
			inputs[input_name] = replacement
			opts.folded_constant_expression_inputs[(ti, parent_id, input_name)] = replacement
			removed.update(expression_blocks)
			folded += 1

		for block_id in removed:
			blocks.pop(block_id, None)
		opts.folded_constant_expression_blocks[ti] = removed

	stats["constant_expressions_folded"] += folded
	return folded


def prompt_for_constant_variables(project, candidates) -> dict:
	"""Interactively choose constant-variable candidates whose reporter reads may be folded."""
	if not candidates:
		print(Ansi.muted("\nNo constant-variable candidates found."))
		return {}

	positive = [c for c in candidates if c["bytes"] > 0]
	negative = [c for c in candidates if c["bytes"] < 0]
	total_positive = sum(c["bytes"] for c in positive)
	w_scope = max(len(c["scope"]) for c in candidates)
	w_name = min(34, max(len(c["name"]) for c in candidates))

	print("")
	print(Ansi.heading(
		f"Constant variables found: {len(candidates)}  "
		f"({sum(c['reporters'] for c in candidates):,} reporter uses)"
	))
	print(Ansi.muted(
		f"  Positive-size candidates: {len(positive)} (about {total_positive:,} bytes saved)"
	))
	print(Ansi.muted(
		f"  Negative-size candidates: {len(negative)} (folding would increase project.json)"
	))
	print("")
	print(f"  {'#':>4}  {'scope':<{w_scope}}  {'variable':<{w_name}}  {'uses':>5}  {'bytes':>8}  notes")
	for i, c in enumerate(candidates, 1):
		nm = c["name"] if len(c["name"]) <= w_name else c["name"][:w_name - 1] + "…"
		notes = []
		if c["bytes"] < 0:
			notes.append("LOSS")
		if c["changes"]:
			notes.append(f"+{c['changes']} change")
		print(f" {i:>4}  {c['scope']:<{w_scope}}  {nm:<{w_name}}  {c['reporters']:>5,}  {c['bytes']:>+8,}  {'; '.join(notes)}")
	print("")
	print(Ansi.warning(
		"  This replaces every serialized variable reporter with the variable's "
		"initial value from project.json. Variables with change-by blocks are marked."
	))
	print(Ansi.muted("  Negative byte estimates are marked LOSS and are not selected by 'p'."))
	print("")
	print(Ansi.muted("  Enter / n       keep every variable"))
	print(Ansi.muted("  p               fold only candidates that reduce project.json"))
	print(Ansi.muted("  a               fold all candidates"))
	print(Ansi.muted("  u               fold only candidates without change-variable-by"))
	print(Ansi.muted("  1,3,5-7         fold those numbers"))
	print("")

	while True:
		try:
			answer = input(Ansi.prompt("Fold which variables? [Enter = keep all] > ")).strip().lower()
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
			picked = {i for i, c in enumerate(candidates, 1) if c["changes"] == 0 and c["bytes"] > 0}
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
		print(Ansi.success(
			f"  Selected {len(chosen)} variable(s), estimated project.json change: {estimate:+,} bytes."
		))
		if risky:
			print(Ansi.warning(f"  WARNING: {len(risky)} selected variable(s) need extra care:"))
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
				confirm = input("  Type 'yes' to fold them anyway, anything else to go back > ").strip().lower()
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
	"""Return only target properties with fixed Scratch defaults."""
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
	"""Remove target properties only when their value is exactly Scratch's default."""
	count = 0
	for target in project.get("targets", []):
		for key, default in _target_default_properties_for_removal(target).items():
			if key in target and target[key] == default and type(target[key]) is type(default):
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
			canonical = f"{aid}.{fmt}" if isinstance(aid, str) and isinstance(fmt, str) else None

			# Do not remove md5ext unless we can prove it is derivable and the
			# referenced asset is actually present in the archive.
			if (
				canonical
				and costume.get("md5ext") == canonical
				and (assets is None or canonical in assets)
			):
				del costume["md5ext"]
				count += 1

			# SVGs use vector geometry directly; bitmapResolution=1 is the
			# default and is safe to omit. Never do this for bitmap costumes.
			if fmt == "svg" and costume.get("bitmapResolution") == 1:
				del costume["bitmapResolution"]
				count += 1

	stats["costume_metadata_removed"] += count
	return count


def remove_empty_target_containers(project, stats):
	"""Remove schema-optional target containers when they are empty."""
	count = 0
	for target in project.get("targets", []):
		for key in ("lists", "broadcasts", "comments"):
			if target.get(key) == {}:
				del target[key]
				count += 1
	stats["empty_containers_removed"] += count
	return count


# Backwards-compatible internal alias for older callers.
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
					if isinstance(item, list) and len(item) > 1 and item[0] in _NUMERIC_TAGS:
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
								if "." in v and "e" not in v.lower() and str(fv) == v and not (fv != fv or fv in (float("inf"), float("-inf"))):
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
	):
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
		self.renamed_block_ids = {}
		self.renamed_variable_ids = {}
		self.renamed_list_ids = {}
		self.renamed_broadcast_ids = {}
		self.renamed_argument_ids = {}
		self.wav_conversions = {}
		self.repaired_broadcast_ids = set()
		self.broadcast_repair_conflicts = {}
		self.list_bytes, self.list_items = list_bytes, list_items
		self.cleared_lists = frozenset()
		self.normalize_epsilon = normalize_epsilon
		self.keep_sound_metadata = keep_sound_metadata
		self.folded_constant_variables = {}
		self.folded_constant_variable_literals = {}
		self.folded_constant_expression_inputs = {}
		self.folded_constant_expression_blocks = []


def apply_transforms(project, opts: Options, assets=None):
	stats = minify_blocks(project)
	if opts.convert_wav_to_mp3 and assets is not None:
		opts.wav_conversions = convert_wav_sounds_to_mp3(project, assets, stats)
	if opts.comments:
		strip_sprite_comments(project, stats)
	if opts.positions:
		round_positions(project, stats)
	if opts.covered:
		clear_covered_values(project, stats)
	if opts.monitors:
		clean_monitors(project, stats)
	if opts.cleared_lists:
		clear_large_lists(project, opts.cleared_lists, stats)
	if opts.remove_unreachable:
		remove_unreachable_blocks(project, stats)
	if opts.remove_unused_procedures:
		remove_unused_procedures(project, stats)
	if opts.remove_unreachable:
		remove_unreachable_blocks(project, stats)
	if opts.folded_constant_variables:
		fold_constant_variables(project, opts.folded_constant_variables, stats)
	if opts.fold_constant_expressions:
		fold_constant_expressions(project, stats, opts)
	if opts.remove_unused_variables or opts.remove_unused_lists:
		remove_unused_data(
			project, stats, opts.remove_unused_variables, opts.remove_unused_lists
		)
	opts.repaired_broadcast_ids, opts.broadcast_repair_conflicts = _repair_dangling_broadcast_refs(project, stats)
	if opts.remove_unused_broadcasts:
		remove_unused_broadcasts(project, stats)
	used_data_ids = set()
	if opts.rename_variable_ids or opts.rename_list_ids:
		opts.renamed_variable_ids, opts.renamed_list_ids = rename_variable_list_ids(
			project, stats, opts.rename_variable_ids, opts.rename_list_ids,
			frequency_order=opts.frequency_data_ids,
		)
		used_data_ids.update(opts.renamed_variable_ids.values())
		used_data_ids.update(opts.renamed_list_ids.values())
	if opts.rename_broadcast_ids:
		opts.renamed_broadcast_ids = rename_broadcast_ids(
			project, stats, existing_ids=used_data_ids,
			frequency_order=opts.frequency_data_ids,
		)
	if opts.rename_argument_ids:
		opts.renamed_argument_ids = rename_argument_ids(project, stats)
	if opts.rename_block_ids:
		opts.renamed_block_ids = rename_block_ids(
			project, stats, frequency_order=opts.frequency_block_ids
		)
	if opts.compact_numeric_inputs:
		compact_numeric_inputs(project, stats)
	if opts.compact_field_ids:
		compact_redundant_field_ids(project, stats)
	if opts.compact_mutation_hasnext:
		compact_mutation_hasnext(project, stats)
	if opts.compact_mutation_metadata:
		compact_mutation_metadata(project, stats)
	if opts.normalize_numbers:
		normalize_numbers(project, stats, opts.normalize_epsilon)
	if not opts.keep_sound_metadata:
		remove_sound_metadata(project)
	if opts.remove_empty_fields:
		remove_empty_fields(project, stats)
	if opts.remove_empty_inputs:
		remove_empty_inputs(project, stats)
	if opts.remove_costume_metadata:
		remove_costume_metadata(project, stats, assets)
	if opts.remove_default_target_properties:
		remove_default_target_properties(project, stats)
	if opts.remove_empty_target_containers:
		remove_empty_target_containers(project, stats)
	if opts.remove_project_meta:
		remove_project_meta(project, stats)
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
			block_ids.get(block_id, block_id): block for block_id, block in blocks.items()
		}
		for block in target["blocks"].values():
			if isinstance(block, dict):
				for key in ("next", "parent"):
					if key in block:
						block[key] = block_ids.get(block[key], block[key])
				for value in (block.get("inputs") or {}).values():
					_replace_input_block_ids(value, block_ids)
		for comment in (target.get("comments") or {}).values():
			if isinstance(comment, dict) and "blockId" in comment and comment["blockId"] is not None:
				comment["blockId"] = block_ids.get(comment["blockId"], comment["blockId"])


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
			var_rev.get(ti, {}).get(k, k): v for k, v in (target.get("variables") or {}).items()
		}
		target["lists"] = {
			list_rev.get(ti, {}).get(k, k): v for k, v in (target.get("lists") or {}).items()
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

	name_to_index = {t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")}
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
	rev = {}   # {(ti, proccode): {new_id: old_id}}
	for (ti, proc, old), new in (mapping or {}).items():
		rev.setdefault((ti, proc), {})[new] = old

	for ti, target in enumerate(project.get("targets", [])):
		for block in target.get("blocks", {}).values():
			if not isinstance(block, dict) or block.get("opcode") not in ("procedures_prototype", "procedures_call"):
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
				[local_rev.get(x, x) for x in vals], separators=(",", ":"), ensure_ascii=False
			)
			inputs = block.get("inputs") or {}
			block["inputs"] = {local_rev.get(k, k): v for k, v in inputs.items()}


def _num_eq(a, b):
	return (
		isinstance(a, (int, float))
		and isinstance(b, (int, float))
		and not isinstance(a, bool)
		and not isinstance(b, bool)
		and int(round(a)) == b
	)



def _check_broadcast_consistency(project):
	"""Require every broadcast reference to resolve to one consistent message name."""
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
				if isinstance(f, list) and len(f) > 1 and isinstance(f[1], str) and f[1] not in valid:
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
	"""Require all serialized input/next references to resolve."""
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		for bid, block in blocks.items():
			if not isinstance(block, dict):
				continue
			parent = block.get("parent")
			if isinstance(parent, str) and parent not in blocks and not _is_known_orphan_argument_reporter(block, blocks):
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
		if not isinstance(b, dict) or b.get("opcode") not in ("procedures_prototype", "procedures_call"):
			continue
		vals = _parse_argumentids(b.get("mutation"))
		if vals is None:
			continue
		if set(vals) != set((b.get("inputs") or {}).keys()):
			return (f"{where}/{bid!r}: procedures argumentids {vals} does not match "
					f"inputs keys {sorted((b.get('inputs') or {}).keys())}")
	return None


def _input_val_eq(vo, vm, opts):
	if vo == vm:
		return True
	if opts.normalize_numbers and (_num_eq(vo, vm) or vo == vm):
		return True
	if opts.compact_numeric_inputs and isinstance(vo, str) and isinstance(vm, (int, float)):
		try:
			if isinstance(vm, int) and str(int(vo)) == vo and int(vo) == vm:
				return True
			if isinstance(vm, float) and str(float(vo)) == vo and float(vo) == vm:
				return True
		except ValueError:
			pass
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
	"""Canonicalize selected variable reporter primitives in an input value."""
	if not isinstance(value, list):
		if isinstance(value, dict):
			return {k: _folded_input_equivalent(v, literals, project, target_index) for k, v in value.items()}
		return value
	if value and value[0] == 12 and len(value) > 2 and isinstance(value[2], str):
		owner = _resolve_variable_owner(project, target_index, value[2])
		if owner in literals:
			return copy.deepcopy(literals[owner])
	return [_folded_input_equivalent(v, literals, project, target_index) if isinstance(v, (list, dict)) else v for v in value]

def _check_inputs_match(oi, mi, opts, original_blocks=None, remaining_blocks=None, target_index=None, _allow_folded=True):
	if oi == mi:
		return True
	if _allow_folded:
		folded_literals = (
			getattr(opts, "folded_constant_variable_literals", None)
			or getattr(opts, "folded_constant_variables", None)
		)
		verify_project = getattr(opts, "_verify_project", None)
		if folded_literals and target_index is not None and verify_project is not None:
			folded = _folded_input_equivalent(oi, folded_literals, verify_project, target_index)
			if folded != oi:
				return _check_inputs_match(
					folded, mi, opts, original_blocks, remaining_blocks,
					target_index, _allow_folded=False,
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
				expected, mi, opts, None, None, target_index,
				_allow_folded=_allow_folded,
			)
	if not (isinstance(oi, list) and isinstance(mi, list) and len(oi) == len(mi)):
		return False
	if not oi or not mi or oi[0] != mi[0]:
		return False
	if oi[0] in (1, 2):
		if len(oi) > 1 and len(mi) > 1:
			if isinstance(oi[1], list) and isinstance(mi[1], list) and len(oi[1]) == len(mi[1]):
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
			if isinstance(oi[2], list) and isinstance(mi[2], list) and len(oi[2]) == len(mi[2]):
				if oi[2][0] == mi[2][0] and oi[2][0] in _NUMERIC_TAGS:
					return _input_val_eq(oi[2][1], mi[2][1], opts)
				return oi[2] == mi[2]
	return False


def _check_fields_match(original_fields, minified_fields, opts):
	if original_fields == minified_fields:
		return True
	if not (opts.compact_field_ids and isinstance(original_fields, dict) and isinstance(minified_fields, dict)):
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


def _check_mutation_match(original_mutation, minified_mutation, opts):
	if original_mutation == minified_mutation:
		return True
	if not (isinstance(original_mutation, dict) and isinstance(minified_mutation, dict)):
		return False
	missing_keys = set(original_mutation) - set(minified_mutation)
	extra_keys = set(minified_mutation) - set(original_mutation)
	allowed_missing = set()
	if opts.compact_mutation_hasnext and missing_keys == {"hasnext"} and original_mutation.get("hasnext") in (False, "false"):
		allowed_missing.add("hasnext")
	if opts.compact_mutation_metadata:
		# canonicalization of these three JSON-string arrays.
		if missing_keys or extra_keys:
			if missing_keys - allowed_missing or extra_keys:
				return False
	else:
		if set(original_mutation) != set(minified_mutation) and (missing_keys - allowed_missing or extra_keys):
			return False
	for key, ov in original_mutation.items():
		if key not in minified_mutation:
			if key in allowed_missing:
				continue
			return False
		mv = minified_mutation[key]
		if ov == mv:
			continue
		if opts.compact_mutation_metadata and key in ("argumentids", "argumentnames", "argumentdefaults") and isinstance(ov, str) and isinstance(mv, str):
			try:
				if json.loads(ov) == json.loads(mv):
					continue
			except (TypeError, ValueError):
				pass
		return False
	return True


def _check_blocks(o, m, opts, where, original_blocks=None, remaining_blocks=None, target_index=None, block_id=None):
	remaining_blocks = remaining_blocks if remaining_blocks is not None else set(original_blocks or ())
	if set(o) != set(m):
		gone = set(o) - set(m)
		extra = set(m) - set(o)
		if extra or not (opts.comments and gone <= {"comment"}):
			return f"{where} (opcode: {o.get('opcode')}): block keys changed. Missing keys: {sorted(gone)}, unexpected extra keys: {sorted(extra)}"
	allow_repairs = opts.remove_unreachable or opts.remove_unused_procedures
	for k in o:
		if k not in m:
			continue
		if k == "fields":
			if not _check_fields_match(o[k], m[k], opts):
				return f"{where} (opcode: {o.get('opcode')}): fields changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "mutation":
			if not _check_mutation_match(o[k], m[k], opts):
				return f"{where} (opcode: {o.get('opcode')}): mutation changed from original {o[k]!r} to minified {m[k]!r}"
		elif k in ("x", "y") and opts.positions:
			if not _num_eq(o[k], m[k]) and o[k] != m[k]:
				return f"{where} (opcode: {o.get('opcode')}): coordinate {k!r} changed from original {o[k]!r} to minified {m[k]!r}"
		elif k == "inputs":
			gone_inputs = set(o["inputs"]) - set(m["inputs"])
			extra_inputs = set(m["inputs"]) - set(o["inputs"])
			allowed_gone = set()
			if allow_repairs and remaining_blocks is not None:
				for name in gone_inputs:
					fixed, changed = _repair_dangling_block_ref(copy.deepcopy(o["inputs"][name]), remaining_blocks)
					if changed and fixed is None:
						allowed_gone.add(name)
			if extra_inputs or gone_inputs - allowed_gone:
				return f"{where} (opcode: {o.get('opcode')}): input names changed. Original inputs: {sorted(o['inputs'])}, minified inputs: {sorted(m['inputs'])}"
			for name in set(o["inputs"]) & set(m["inputs"]):
				oi, mi = o["inputs"][name], m["inputs"][name]
				folded_inputs = getattr(opts, "folded_constant_expression_inputs", {})
				folded_input = folded_inputs.get((target_index, block_id, name))
				if folded_input is not None and (
					folded_input == mi
					or _check_inputs_match(
						folded_input, mi, opts, original_blocks, remaining_blocks, target_index
					)
				):
					continue
				if not _check_inputs_match(oi, mi, opts, original_blocks, remaining_blocks, target_index):
					return f"{where} (opcode: {o.get('opcode')}): input {name!r} changed from original {oi!r} to minified {mi!r}"
		else:
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
	"""Return archive entry names, rejecting duplicate/unsafe entries."""
	infos = zf.infolist()
	names = [info.filename for info in infos]
	if len(names) != len(set(names)):
		dupes = sorted(name for name, n in Counter(names).items() if n > 1)
		return None, f"archive contains duplicate entry names: {dupes[:10]}"
	for name in names:
		if not isinstance(name, str) or not name or name.startswith("/"):
			return None, f"archive contains an invalid absolute/empty entry name: {name!r}"
		parts = name.replace("\\", "/").split("/")
		if any(part in ("", ".", "..") for part in parts if name != "project.json"):
			return None, f"archive contains an unsafe entry path: {name!r}"
	return set(names), None


def _valid_asset_id(value):
	return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


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


def _validate_asset_entries(project, zf, label):
	"""Validate every referenced costume/sound and its backing archive asset."""
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
						return f"{where}: md5ext extension {ext!r} is invalid for {kind}"
					filename = md5ext
				else:
					filename = _asset_filename(entry, kind)
					if filename is None:
						return f"{where}: cannot derive an asset filename"

				if filename not in names:
					return f"{where}: referenced asset {filename!r} is missing from archive"
				payload = zf.read(filename)
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
						if axis in entry and (isinstance(entry[axis], bool) or not isinstance(entry[axis], (int, float))):
							return f"{where}: invalid {axis} {entry[axis]!r}"
				else:
					for field in ("rate", "sampleCount"):
						if field in entry and (isinstance(entry[field], bool) or not isinstance(entry[field], (int, float))):
							return f"{where}: invalid {field} {entry[field]!r}"
	return None


def _serialized_input_refs(value, out):
	"""Collect only positions in Scratch input tuples that are block IDs."""
	if not isinstance(value, list) or not value:
		return
	tag = value[0]
	if tag in (1, 2):
		if len(value) > 1 and isinstance(value[1], str):
			out.add(value[1])
		elif len(value) > 1 and isinstance(value[1], (list, dict)):
			_serialized_input_refs(value[1], out)
		return
	if tag == 3:
		for item in value[1:3]:
			if isinstance(item, str):
				out.add(item)
			elif isinstance(item, (list, dict)):
				_serialized_input_refs(item, out)
		return
	for item in value[1:]:
		if isinstance(item, (list, dict)):
			_serialized_input_refs(item, out)


def _validate_block_structure(project, label):
	"""Strictly validate the block graph of the produced project."""
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
					if key == "parent" and _is_known_orphan_argument_reporter(block, blocks):
						_orphan_parent_quirk = True
						continue
					return f"{where}: {key} points to missing block {ref!r}"
			if not isinstance(block.get("inputs"), dict) or not isinstance(block.get("fields"), dict):
				return f"{where}: inputs/fields must be objects"
			if isinstance(block.get("topLevel"), bool) and block.get("topLevel") and block.get("parent") is not None:
				return f"{where}: topLevel block has non-null parent {block['parent']!r}"
			if isinstance(block.get("shadow"), bool) and block.get("shadow") and block.get("topLevel"):
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

		# next must be reciprocated by parent.
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
				if pb.get("next") != bid and parent not in input_parents.get(bid, set()):
					# The input-parent map is keyed by child; require this parent to
					# actually be one of the serialized owners of the child.
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: parent {parent!r} does not reference this block"
			for child in input_parents.get(bid, set()):
				cb = blocks.get(bid)
				if isinstance(cb, dict) and cb.get("parent") != child:
					return f"{label}, target {ti} ({target.get('name')!r}), block {bid!r}: input owner {child!r} disagrees with parent {cb.get('parent')!r}"
	return None


def _check_costumes(original_target, minified_target, opts, zf):
	original = original_target.get("costumes", [])
	minified = minified_target.get("costumes", [])
	if len(original) != len(minified):
		return f"Target {original_target.get('name')!r}: costume count changed from {len(original)} to {len(minified)}"

	allowed_keys = {"assetId", "name", "md5ext", "dataFormat", "bitmapResolution", "rotationCenterX", "rotationCenterY"}
	for index, (co, cm) in enumerate(zip(original, minified)):
		where = f"Target {original_target.get('name')!r}, costume {index}"
		if not isinstance(co, dict) or not isinstance(cm, dict):
			if co != cm:
				return f"{where}: costume entry changed"
			continue

		extra = set(cm) - set(co)
		if extra:
			return f"{where}: unexpected attribute(s) appeared: {sorted(extra)}"

		allowed_removed = set()
		if (
			opts.remove_costume_metadata
			and co.get("md5ext") == f"{co.get('assetId')}.{co.get('dataFormat')}"
		):
			allowed_removed.add("md5ext")
		if (
			opts.remove_costume_metadata
			and co.get("dataFormat") == "svg"
			and co.get("bitmapResolution") == 1
		):
			allowed_removed.add("bitmapResolution")
		missing = (set(co) - set(cm)) - allowed_removed
		if missing:
			return f"{where}: attribute(s) were removed unexpectedly: {sorted(missing)}"

		for key in set(co) & set(cm):
			ov, mv = co[key], cm[key]
			if key in ("rotationCenterX", "rotationCenterY") and opts.positions:
				if ov != mv and not _num_eq(ov, mv):
					return f"{where}: {key!r} changed from {ov!r} to {mv!r}"
			elif ov != mv:
				return f"{where}: attribute {key!r} changed from {ov!r} to {mv!r}"

	return None

def _reachable_block_ids(target):
	blocks = target.get("blocks", {})
	edges = _build_block_graph(target)
	roots = {
		bid for bid, block in blocks.items()
		if isinstance(block, list) or (isinstance(block, dict) and block.get("topLevel"))
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
		if not isinstance(block, dict) or block.get("opcode") != "procedures_definition":
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
		if opts.remove_unreachable:
			ids.update(set(target.get("blocks", {})) - _reachable_block_ids(target))
		if opts.remove_unused_procedures:
			ids.update(_dead_procedure_block_ids(target))
		folded_blocks = getattr(opts, "folded_constant_expression_blocks", ())
		if ti < len(folded_blocks):
			ids.update(folded_blocks[ti])
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


def verify(original_path, minified_path, opts):
	with zipfile.ZipFile(original_path) as a, zipfile.ZipFile(minified_path) as b:
		if a.testzip() is not None:
			return False, f"input zip '{original_path}' failed CRC test"
		if b.testzip() is not None:
			return False, f"output zip '{minified_path}' failed CRC test"
		for zf, zpath in ((a, original_path), (b, minified_path)):
			names, err = _zip_entry_names(zf)
			if err:
				return False, f"archive '{zpath}': {err}"
			if "project.json" not in names:
				return False, f"archive '{zpath}' has no project.json"
		orig_assets = {name for name in a.namelist() if name != "project.json"}
		mini_assets = {name for name in b.namelist() if name != "project.json"}
		conversions = getattr(opts, "wav_conversions", {}) or {}
		expected_assets = (orig_assets - set(conversions)) | set(conversions.values())
		if mini_assets != expected_assets:
			diff_missing = sorted(expected_assets - mini_assets)
			diff_extra = sorted(mini_assets - expected_assets)
			return False, f"zip archive entries differ. Missing: {diff_missing[:10]}, unexpected extra: {diff_extra[:10]}"
		for name in orig_assets:
			if name in conversions:
				new_name = conversions[name]
				new_bytes = b.read(new_name)
				if hashlib.md5(new_bytes).hexdigest() != new_name.rsplit(".", 1)[0]:
					return False, f"converted asset {new_name!r} does not match its MD5 asset ID"
			else:
				if a.read(name) != b.read(name):
					return False, f"asset byte-for-byte mismatch: {name!r} ({len(a.read(name))} bytes vs {len(b.read(name))} bytes)"

		try:
			orig_raw = json.loads(a.read("project.json"))
			mini_raw_project = json.loads(b.read("project.json"))
		except (UnicodeDecodeError, json.JSONDecodeError) as exc:
			return False, f"project.json is not valid UTF-8 JSON: {exc}"
		if not isinstance(orig_raw, dict) or not isinstance(mini_raw_project, dict):
			return False, "project.json root must be an object"
		if not isinstance(orig_raw.get("targets"), list) or not isinstance(mini_raw_project.get("targets"), list):
			return False, "project.json targets must be arrays"
		orig = _reinflate(orig_raw)
		mini = _reinflate(mini_raw_project)
		opts._verify_project = orig

		err = _validate_asset_entries(mini, b, "minified project")
		if err:
			return False, err
		# The original is checked too, but referenced assets may legitimately be
		# renamed by an explicitly requested WAV->MP3 conversion. Its asset bytes
		# still have to be internally consistent.
		err = _validate_asset_entries(orig, a, "original project")
		if err:
			return False, err
		err = _validate_block_structure(mini, "minified project")
		if err:
			return False, err

		conflicts = getattr(opts, "broadcast_repair_conflicts", {}) or {}
		if conflicts:
			first_id = sorted(conflicts)[0]
			return False, f"broadcast {first_id!r} has conflicting reference names {conflicts[first_id]!r}; refusing to guess a definition"
		err = _check_broadcast_ids_resolve(mini_raw_project)
		if err:
			return False, err
		err = _check_block_references_resolve(mini_raw_project)
		if err:
			return False, err

		if opts.rename_block_ids:
			_restore_block_ids(mini, opts.renamed_block_ids)
		if opts.rename_variable_ids or opts.rename_list_ids:
			_restore_data_ids(mini, opts.renamed_variable_ids, opts.renamed_list_ids)
		if opts.rename_broadcast_ids:
			_restore_broadcast_ids(mini, opts.renamed_broadcast_ids)
		if opts.rename_argument_ids:
			_restore_argument_ids(mini, opts.renamed_argument_ids)

		for key in set(orig) | set(mini):
			if key not in ("targets", "monitors") and orig.get(key) != mini.get(key):
				if key == "meta" and opts.remove_project_meta:
					mo = orig.get("meta") or {}
					mm = mini.get("meta") or {}
					if mo.get("semver") == mm.get("semver") and mo.get("vm") == mm.get("vm"):
						continue
				return False, f"Top-level project key {key!r} changed: original has {orig.get(key)!r}, minified has {mini.get(key)!r}"
		if len(orig["targets"]) != len(mini["targets"]):
			return False, f"Target count changed: original has {len(orig['targets'])}, minified has {len(mini['targets'])}"

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
				k for k in missing_target_keys
					if k in ("lists", "broadcasts", "comments") and to.get(k) == {}
				)
			if opts.remove_unused_lists and "lists" in missing_target_keys:
				allowed_container_keys.add("lists")
			if opts.remove_unused_broadcasts and "broadcasts" in missing_target_keys:
				allowed_container_keys.add("broadcasts")
			if opts.comments and not to.get("isStage") and "comments" in missing_target_keys:
				allowed_container_keys.add("comments")
			allowed_default_keys = set()
			if opts.remove_default_target_properties:
				defaults = _target_default_properties(to)
				allowed_default_keys = {
					k for k in missing_target_keys
					if k in defaults and to.get(k) == defaults[k]
				}
			if missing_target_keys - allowed_default_keys - allowed_container_keys or extra_target_keys:
				return False, f"Target {ti} ({name!r}): target keys changed. Missing: {sorted(missing_target_keys - allowed_default_keys - allowed_container_keys)}, unexpected extra: {sorted(extra_target_keys)}"
			for k in to:
				if k in ("blocks", "comments", "lists", "variables", "sounds", "costumes", "broadcasts"):
					continue
				if k not in tm and k in allowed_default_keys:
					continue
				if to[k] != tm[k]:
					if opts.normalize_numbers and _num_eq(to[k], tm[k]):
						continue
					return False, f"Target {ti} ({name!r}): property {k!r} changed from {to[k]!r} to {tm[k]!r}"

			to_bcasts = to.get("broadcasts", {})
			tm_bcasts = tm.get("broadcasts", {})
			unexpected_bcasts = set(tm_bcasts) - set(to_bcasts)
			allowed_repaired = set(getattr(opts, "repaired_broadcast_ids", ()) or ())
			if unexpected_bcasts - allowed_repaired:
				return False, f"Target {ti} ({name!r}): unexpected new broadcast ID(s) appeared in minified project: {sorted(unexpected_bcasts - allowed_repaired)}"
			missing_bcasts = set(to_bcasts) - set(tm_bcasts)
			if missing_bcasts and not (opts.remove_unused_broadcasts or (opts.remove_empty_containers and not to.get("isStage") and not to_bcasts)):
				return False, f"Target {ti} ({name!r}): {len(missing_bcasts)} broadcast(s) removed without --remove-unused-broadcasts: {sorted(missing_bcasts)}"
			if missing_bcasts and opts.remove_unused_broadcasts:
				still_used_bcasts = {bid: mini_bcast_refs.get(bid, []) for bid in missing_bcasts if bid in mini_bcast_refs}
				if still_used_bcasts:
					first_bid = next(iter(still_used_bcasts))
					return False, (
						f"Target {ti} ({name!r}): broadcast {to_bcasts.get(first_bid)!r} (id: {first_bid!r}) was removed, "
						f"but is still referenced in the minified project by: {', '.join(still_used_bcasts[first_bid])}"
					)
			for bid, bo in to_bcasts.items():
				if bid in tm_bcasts and bo != tm_bcasts[bid]:
					return False, f"Target {ti} ({name!r}): broadcast name for id {bid!r} changed from {bo!r} to {tm_bcasts[bid]!r}"

			to_vars = to.get("variables", {})
			tm_vars = tm.get("variables", {})
			if set(tm_vars) - set(to_vars):
				return False, f"Target {ti} ({name!r}): unexpected new variable ID(s) appeared in minified project: {sorted(set(tm_vars) - set(to_vars))}"
			missing_vars = set(to_vars) - set(tm_vars)
			if missing_vars and not opts.remove_unused_variables:
				return False, f"Target {ti} ({name!r}): {len(missing_vars)} variable(s) removed without --remove-unused-variables: {sorted(missing_vars)}"
			if missing_vars:
				still_used = {vid: mini_var_refs.get((ti, vid), []) for vid in missing_vars if (ti, vid) in mini_var_refs}
				if still_used:
					first_vid = next(iter(still_used))
					first_name = to_vars[first_vid][0] if isinstance(to_vars[first_vid], list) and to_vars[first_vid] else first_vid
					return False, (
						f"Target {ti} ({name!r}): variable {first_name!r} (id: {first_vid!r}) was removed, "
						f"but is still referenced in the minified project by: {', '.join(still_used[first_vid])}"
					)
			for vid, vo in to_vars.items():
				if vid in tm_vars:
					vm = tm_vars[vid]
					if vo == vm:
						continue
					if (
						opts.normalize_numbers
						and isinstance(vo, list)
						and isinstance(vm, list)
						and len(vo) == len(vm)
						and vo[0] == vm[0]
						and (_num_eq(vo[1], vm[1]) or vo[1] == vm[1])
						and (len(vo) < 3 or vo[2:] == vm[2:])
					):
						continue
					return False, f"Target {ti} ({name!r}): variable {vo[0] if isinstance(vo, list) and vo else vid!r} (id: {vid!r}) value changed from {vo!r} to {vm!r}"

			to_lists = to.get("lists", {})
			tm_lists = tm.get("lists", {})
			if set(tm_lists) - set(to_lists):
				return False, f"Target {ti} ({name!r}): unexpected new list ID(s) appeared in minified project: {sorted(set(tm_lists) - set(to_lists))}"
			missing_lists = set(to_lists) - set(tm_lists)
			if missing_lists and not opts.remove_unused_lists:
				return False, f"Target {ti} ({name!r}): {len(missing_lists)} list(s) removed without --remove-unused-lists: {sorted(missing_lists)}"
			if missing_lists:
				still_used = {lid: mini_list_refs.get((ti, lid), []) for lid in missing_lists if (ti, lid) in mini_list_refs}
				if still_used:
					first_lid = next(iter(still_used))
					first_name = to_lists[first_lid][0] if isinstance(to_lists[first_lid], list) and to_lists[first_lid] else first_lid
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
				if (
					opts.normalize_numbers
					and isinstance(lo, list)
					and isinstance(lm, list)
					and len(lo) == len(lm)
					and lo[0] == lm[0]
					and isinstance(lo[1], list)
					and isinstance(lm[1], list)
					and len(lo[1]) == len(lm[1])
					and all(
						x == y or _num_eq(x, y)
						for x, y in zip(lo[1], lm[1])
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

			if set(tm["blocks"]) - set(to["blocks"]):
				extra_blocks = sorted(set(tm["blocks"]) - set(to["blocks"]))
				return False, f"Target {ti} ({name!r}): {len(extra_blocks)} unexpected new block ID(s) appeared: {extra_blocks[:10]}"
			missing_blocks = set(to["blocks"]) - set(tm["blocks"])
			illegal_missing = missing_blocks - allowed_removed_by_target[ti]
			if illegal_missing:
				return False, (
					f"Target {ti} ({name!r}): block(s) were removed even though the original "
					f"project proves they were reachable/live: {sorted(illegal_missing)[:10]}"
				)
			for bid, bo in to["blocks"].items():
				if bid not in tm["blocks"]:
					continue
				bm = tm["blocks"][bid]
				if isinstance(bo, list) or isinstance(bm, list):
					ok = (
						isinstance(bo, list)
						and isinstance(bm, list)
						and len(bo) == len(bm)
						and bo[:3] == bm[:3]
						and all(_num_eq(x, y) or x == y for x, y in zip(bo[3:], bm[3:]))
					)
					if not (ok and (opts.positions or bo == bm)):
						return False, f"Target {ti} ({name!r}), primitive block {bid!r} changed from {bo!r} to {bm!r}"
					continue
				err = _check_blocks(bo, bm, opts, f"Target {ti} ({name!r}), block {bid!r}", to["blocks"], set(tm["blocks"]), ti, bid)
				if err:
					return False, err
				if not isinstance(bm.get("inputs"), dict) or not isinstance(
					bm.get("fields"), dict
				):
					return (
						False,
						f"Target {ti} ({name!r}), block {bid!r} ({bm.get('opcode')}): inputs or fields is not a dict (violates Scratch VM format)",
					)
				if "next" not in bm or "parent" not in bm:
					return False, f"Target {ti} ({name!r}), block {bid!r} ({bm.get('opcode')}): missing required 'next' or 'parent' attribute"
				mu = bm.get("mutation")
				if mu is not None:
					if not isinstance(mu, dict):
						return False, f"Target {ti} ({name!r}), block {bid!r} ({bm.get('opcode')}): mutation is not an object"
					if not opts.compact_mutation_metadata and ("tagName" not in mu or "children" not in mu):
						return False, f"Target {ti} ({name!r}), block {bid!r} ({bm.get('opcode')}): lost required mutation tagName or children"

			co, cm = to.get("comments", {}), tm.get("comments", {})
			if not isinstance(cm, dict):
				return False, f"Target {ti} ({name!r}): comments is not a dict (violates Scratch VM format)"
			if opts.comments and not to.get("isStage"):
				if cm:
					return False, f"Target {ti} ({name!r}): comments remain ({len(cm)} comments) despite comment stripping enabled"
			else:
				if set(co) != set(cm):
					return False, f"Target {ti} ({name!r}): comment ID set changed. Missing: {sorted(set(co) - set(cm))}, extra: {sorted(set(cm) - set(co))}"
				for cid in co:
					for f in set(co[cid]) | set(cm[cid]):
						x, y = co[cid].get(f), cm[cid].get(f)
						if f in ("x", "y", "width", "height") and opts.positions:
							if not (x == y or _num_eq(x, y)):
								return False, f"Target {ti} ({name!r}): comment {cid!r} attribute {f!r} changed from {x!r} to {y!r}"
						elif x != y:
							return False, f"Target {ti} ({name!r}): comment {cid!r} attribute {f!r} changed from {x!r} to {y!r}"
			if opts.comments and not to.get("isStage"):
				dangling_comments = [
					bid for bid, x in tm["blocks"].items()
					if isinstance(x, dict) and "comment" in x
				]
				if dangling_comments:
					return False, f"Target {ti} ({name!r}): dangling block.comment link in block(s): {dangling_comments[:10]}"

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
		err = _check_broadcast_ids_resolve(mini)
		if err:
			return False, err
		err = _check_block_references_resolve(mini)
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

		expected = dict(so)
		old_name = so.get("md5ext") or (f"{so.get('assetId')}.wav" if so.get("dataFormat") == "wav" else None)
		if so.get("dataFormat") == "wav" and old_name in conversions:
			new_name = conversions[old_name]
			expected["assetId"] = new_name.rsplit(".", 1)[0]
			expected["dataFormat"] = "mp3"
			expected["md5ext"] = new_name

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
		return None if mo == mm else "monitors changed without monitor optimization (use --keep-monitors to preserve)"

	def key(m):
		return (m.get("id"), m.get("spriteName"), m.get("opcode"))

	by_key = {key(m): m for m in mo}
	order = [key(m) for m in mo]
	prev = -1
	for m in mm:
		k = key(m)
		if k not in by_key:
			return f"Monitor {k} appeared in minified project but was not present in original"
		if order.index(k) < prev:
			return f"Monitor {k} order changed in project monitors list"
		prev = order.index(k)
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


def human(n):
	return f"{n/1048576:.2f} MiB" if n >= 1048576 else f"{n/1024:.1f} KiB"


STAT_ORDER = [
	("topLevel", "topLevel:false dropped"),
	("shadow", "shadow:false dropped"),
	("warp", "warp string -> boolean"),
	("comments", "sprite comments removed"),
	("comment_links", "block comment links removed"),
	("rounded", "position values rounded"),
	("covered", "covered values reset"),
	("monitors_orphan", "orphaned monitors removed"),
	("monitors_unused", "unused monitors removed"),
	("monitor_params", "list-monitor params cleared"),
	("monitor_value", "monitor values normalized"),
	("lists_cleared", "large lists cleared"),
	("list_items_cleared", "list items removed"),
	("block_ids", "block IDs renamed"),
	("dangling_refs_skipped", "sprites skipped (already-dangling refs)"),
	("dangling_block_refs_fixed", "dangling block references repaired"),
	("broadcast_refs_repaired", "missing broadcast definitions restored"),
	("broadcast_ref_conflicts", "conflicting broadcast references"),
	("variable_ids", "variable IDs renamed"),
	("list_ids", "list IDs renamed"),
	("broadcast_ids", "broadcast IDs renamed"),
	("argument_ids", "argument IDs renamed"),
	("variables_removed", "unused variables removed"),
	("lists_removed", "unused lists removed"),
	("broadcasts_removed", "unused broadcasts removed"),
	("blocks_removed", "unreachable/procedure blocks removed"),
	("procedures_removed", "unused procedures removed"),
	("numbers_normalized", "integral numbers normalized"),
	("empty_fields_removed", "empty block fields removed"),
	("empty_inputs_removed", "empty block inputs removed"),
	("costume_metadata_removed", "redundant costume metadata removed"),
	("default_target_properties_removed", "default target properties removed"),
	("empty_containers_removed", "empty target containers removed"),
	("project_meta_cleaned", "project meta fields cleaned"),
	("numeric_inputs_compacted", "numeric string inputs compacted"),
	("field_ids_compacted", "redundant null field IDs removed"),
	("mutation_hasnext_compacted", "mutation hasnext=false removed"),
	("mutation_metadata_compacted", "mutation JSON compacted"),
	("constant_variable_reporters", "constant variable reporters folded"),
	("constant_variable_bytes_saved", "constant-variable JSON bytes saved"),
	("constant_expressions_folded", "constant numeric expressions folded"),
]


def minify_sb3(src, dst, opts=None):
	opts = opts or Options()
	if not os.path.isfile(src):
		print(Ansi.error(f'Error: file not found: "{src}"'))
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

		asset_infos = {item.filename: item for item in zin.infolist() if item.filename != "project.json"}
		assets = {
			item.filename: zin.read(item.filename)
			for item in zin.infolist()
			if item.filename != "project.json"
		}

		stats = apply_transforms(project, opts, assets)
		out_json = dumps_compact(project, opts.sort_keys).encode("utf-8")
		with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=opts.compression_level) as zout:
			zout.writestr("project.json", out_json)
			for name, data in assets.items():
				written = False
				if opts.preserve_asset_compression and name in asset_infos and zin.read(name) == data:
					orig_info = asset_infos[name]
					if orig_info.compress_type == zipfile.ZIP_DEFLATED:
						py_comp_size = len(zlib.compress(data, opts.compression_level)) - 6
						if orig_info.compress_size <= py_comp_size:
							zin.fp.seek(orig_info.header_offset)
							header = zin.fp.read(30)
							fn_len = int.from_bytes(header[26:28], "little")
							extra_len = int.from_bytes(header[28:30], "little")
							zin.fp.seek(orig_info.header_offset + 30 + fn_len + extra_len)
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
					zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=opts.compression_level)

	print(
		f'Input : "{src}"\nOutput: "{dst}"\n\n'
		+ Ansi.heading("Transforms applied:")
	)
	for k, label in STAT_ORDER:
		print(Ansi.muted(f"  {label:40} {stats[k]:>8,}"))
	b, a = len(raw), len(out_json)
	print(
		f"\n{Ansi.heading('project.json')} : {human(b)} -> {human(a)}  (-{human(b-a)}, {(b-a)/b*100:.1f}%)"
	)
	print(
		f"archive      : {human(os.path.getsize(src))} -> {human(os.path.getsize(dst))}"
	)

	print(Ansi.heading("\nVerifying (independent original-vs-result check)..."))
	ok, msg = verify(src, dst, opts)
	if not ok:
		print(Ansi.error(f"Verification failed: {msg}"))
		os.remove(dst)
		print(Ansi.muted("Output deleted."))
		return 2
	print(Ansi.success("Verified successfully."))
	return 0


if __name__ == "__main__":
	flags = [a for a in sys.argv[1:] if a.startswith("--")]
	args = [a for a in sys.argv[1:] if not a.startswith("--")]
	toggles = {
		"--all-optimizations",
		"--all-flags",
		"--keep-comments",
		"--keep-positions",
		"--keep-covered",
		"--keep-monitors",
		"--keep-lists",
		"--rename-block-ids",
		"--rename-variable-ids",
		"--rename-list-ids",
		"--rename-broadcast-ids",
		"--rename-argument-ids",
		"--remove-unused-variables",
		"--remove-unused-lists",
		"--remove-unused-broadcasts",
		"--remove-unreachable",
		"--remove-unused-procedures",
		"--normalize-numbers",
		"--remove-empty-fields",
		"--remove-empty-inputs",
		"--remove-costume-metadata",
		"--remove-default-target-properties",
		"--remove-empty-containers",
		"--remove-project-meta",
		"--sort-keys",
		"--keep-sound-metadata",
		"--preserve-asset-compression",
		"--compress-assets",
		"--convert-wav-to-mp3",
		"--frequency-block-ids",
		"--order-block-ids-by-frequency",
		"--frequency-data-ids",
		"--order-data-ids-by-frequency",
		"--compact-numeric-inputs",
		"--compact-field-ids",
		"--compact-mutation-hasnext",
		"--compact-mutation-metadata",
		"--fold-constant-variables",
		"--fold-constant-expressions",
		"--remove-empty-target-containers",
	}
	valued = {"--list-bytes", "--list-items", "--compression-level", "--normalize-epsilon"}
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
				f"Valid: {sorted(toggles)} and --list-bytes=N --list-items=N (positive), --compression-level=N (0-9), --normalize-epsilon=N (positive)"
			)
		)
		sys.exit(1)
	if not args:
		print(__doc__)
		sys.exit(1)
	all_optimizations = "--all-optimizations" in flags or "--all-flags" in flags
	opts = Options(
		comments=("--keep-comments" not in flags),
		positions=("--keep-positions" not in flags),
		covered=("--keep-covered" not in flags),
		monitors=("--keep-monitors" not in flags),
		lists=False if ("--keep-lists" in flags or all_optimizations) else True,
		rename_block_ids=all_optimizations or "--rename-block-ids" in flags or "--frequency-block-ids" in flags or "--order-block-ids-by-frequency" in flags,
		rename_variable_ids=all_optimizations or "--rename-variable-ids" in flags or "--frequency-data-ids" in flags or "--order-data-ids-by-frequency" in flags,
		rename_list_ids=all_optimizations or "--rename-list-ids" in flags or "--frequency-data-ids" in flags or "--order-data-ids-by-frequency" in flags,
		rename_broadcast_ids=all_optimizations or "--rename-broadcast-ids" in flags or "--frequency-data-ids" in flags or "--order-data-ids-by-frequency" in flags,
		rename_argument_ids=all_optimizations or "--rename-argument-ids" in flags,
		remove_unused_variables=all_optimizations or "--remove-unused-variables" in flags,
		remove_unused_lists=all_optimizations or "--remove-unused-lists" in flags,
		remove_unused_broadcasts=all_optimizations or "--remove-unused-broadcasts" in flags,
		remove_unreachable=all_optimizations or "--remove-unreachable" in flags,
		remove_unused_procedures=all_optimizations or "--remove-unused-procedures" in flags,
		normalize_numbers=all_optimizations or "--normalize-numbers" in flags,
		remove_empty_fields=all_optimizations or "--remove-empty-fields" in flags,
		remove_empty_inputs=all_optimizations or "--remove-empty-inputs" in flags,
		remove_costume_metadata=all_optimizations or "--remove-costume-metadata" in flags,
		remove_default_target_properties=all_optimizations or "--remove-default-target-properties" in flags,
		remove_empty_target_containers=all_optimizations or "--remove-empty-containers" in flags or "--remove-empty-target-containers" in flags,
		remove_project_meta=all_optimizations or "--remove-project-meta" in flags,
		preserve_asset_compression=all_optimizations or "--preserve-asset-compression" in flags,
		frequency_block_ids=all_optimizations or "--frequency-block-ids" in flags or "--order-block-ids-by-frequency" in flags,
		frequency_data_ids=all_optimizations or "--frequency-data-ids" in flags or "--order-data-ids-by-frequency" in flags,
		compact_numeric_inputs=all_optimizations or "--compact-numeric-inputs" in flags,
		compact_field_ids=all_optimizations or "--compact-field-ids" in flags,
		compact_mutation_hasnext=all_optimizations or "--compact-mutation-hasnext" in flags,
		compact_mutation_metadata="--compact-mutation-metadata" in flags,
		fold_constant_variables="--fold-constant-variables" in flags,
		fold_constant_expressions=all_optimizations or "--fold-constant-expressions" in flags,
		compress_assets=all_optimizations or "--compress-assets" in flags,
		convert_wav_to_mp3=all_optimizations or "--convert-wav-to-mp3" in flags,
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