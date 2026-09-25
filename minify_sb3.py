import hashlib
import json
import os
import subprocess
import sys
import zipfile
from collections import Counter


BLOCK_ID_ALPHABET = '!@#$%^*()+_-={}|[]:;<>?,./~ABCDEFGHJKLMNOPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789'


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


def rename_block_ids(project, stats):
	total_stats = 0
	all_maps = {}
	dangling_skipped = 0

	for ti, target in enumerate(project.get("targets", [])):
		blocks = target.get("blocks", {})
		old_ids = list(blocks.keys())
		if not old_ids:
			continue

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


def _has_dangling_block_refs(target):
	return bool(_dangling_block_ids(target))


def _dangling_in_input(value, ids):
	dangling = set()
	_collect_dangling_in_input(value, ids, dangling)
	return bool(dangling)


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


def _collect_data_ids(project):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	stage_var_ids = set(targets[stage_index].get("variables", {})) if stage_index is not None else set()
	stage_list_ids = set(targets[stage_index].get("lists", {})) if stage_index is not None else set()

	used_vars = set()      # {(target_index, id)}
	used_lists = set()     # {(target_index, id)}
	used_broadcasts = set()

	def resolve_var(ti, i):
		if not isinstance(i, str):
			return
		target_ids = targets[ti].get("variables", {})
		if i in target_ids:
			used_vars.add((ti, i))
		elif stage_index is not None and i in stage_var_ids:
			used_vars.add((stage_index, i))

	def resolve_list(ti, i):
		if not isinstance(i, str):
			return
		target_ids = targets[ti].get("lists", {})
		if i in target_ids:
			used_lists.add((ti, i))
		elif stage_index is not None and i in stage_list_ids:
			used_lists.add((stage_index, i))

	for ti, target in enumerate(targets):
		for block in target.get("blocks", {}).values():
			for v in _walk_block_values(block):
				if isinstance(v, list) and v:
					tag = v[0]
					if tag == 12 and len(v) > 2 and isinstance(v[2], str):
						resolve_var(ti, v[2])
					elif tag == 13 and len(v) > 2 and isinstance(v[2], str):
						resolve_list(ti, v[2])

			if not isinstance(block, dict):
				continue
			fields = block.get("fields") or {}
			for name, f in fields.items():
				if not (isinstance(f, list) and len(f) > 1 and isinstance(f[1], str)):
					continue
				if name == "VARIABLE":
					resolve_var(ti, f[1])
				elif name == "LIST":
					resolve_list(ti, f[1])
				elif name in ("BROADCAST_OPTION", "BROADCAST_INPUT"):
					used_broadcasts.add(f[1])

	name_to_index = {t.get("name"): i for i, t in enumerate(targets) if not t.get("isStage")}
	for m in project.get("monitors", []):
		if not isinstance(m, dict):
			continue
		sprite = m.get("spriteName")
		ti = name_to_index.get(sprite, stage_index) if sprite else stage_index
		if ti is None:
			continue
		op = m.get("opcode")
		if op == "data_variable" and isinstance(m.get("id"), str):
			resolve_var(ti, m["id"])
		elif op == "data_listcontents" and isinstance(m.get("id"), str):
			resolve_list(ti, m["id"])

	return used_vars, used_lists, used_broadcasts


def _rename_id_map(ids, existing_ids=()):
	reserved = set(existing_ids)
	result = {}
	n = 0
	for old in ids:
		new = _short_id(n)
		while new in reserved or new in result.values():
			n += 1
			new = _short_id(n)
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


def rename_variable_list_ids(project, stats, rename_variables=True, rename_lists=True):
	targets = project.get("targets", [])
	stage_index = next((i for i, t in enumerate(targets) if t.get("isStage")), None)
	stage_var_ids = set(targets[stage_index].get("variables", {})) if stage_index is not None else set()
	stage_list_ids = set(targets[stage_index].get("lists", {})) if stage_index is not None else set()

	var_map = {}    # {(ti, old_id): new_id}
	list_map = {}   # {(ti, old_id): new_id}

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
			new_ids = _rename_id_map(ids, existing_ids=allocated_ids)
			allocated_ids.update(new_ids.values())
			for old, new in new_ids.items():
				var_map[(ti, old)] = new
	if rename_lists:
		stage_ids = list(targets[stage_index].get("lists", {}).keys()) if stage_index is not None else []
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


def rename_broadcast_ids(project, stats, existing_ids=()):
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
	mapping = _rename_id_map(sorted(ids), existing_ids=existing_ids)
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
	stats["broadcasts_unused"] += 0
	return used


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


def remove_unreachable_blocks(project, stats):
	removed = 0
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		roots = {
			bid for bid, b in blocks.items()
			if isinstance(b, list) or b.get("topLevel") or b.get("parent") is None
		}
		edges = _build_block_graph(target)     # build ONCE per target, not per node
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
		comments = target.get("comments") or {}
		for cid in list(comments):
			c = comments[cid]
			if isinstance(c, dict) and c.get("blockId") is not None and c["blockId"] not in blocks:
				del comments[cid]
	stats["blocks_removed"] += removed
	return removed


def _procedure_key(block):
	mut = block.get("mutation") if isinstance(block, dict) else None
	if not isinstance(mut, dict):
		return None
	return mut.get("proccode")


def remove_unused_procedures(project, stats):
	removed = 0
	removable_total = 0
	for target in project.get("targets", []):
		blocks = target.get("blocks", {})
		calls = set()
		definitions = {}
		for bid, b in blocks.items():
			if not isinstance(b, dict):
				continue
			op = b.get("opcode")
			if op == "procedures_definition":
				proto_id = ((b.get("inputs") or {}).get("custom_block") or [None, None])
				proto_id = proto_id[1] if len(proto_id) > 1 else None
				proto = blocks.get(proto_id)
				proc = _procedure_key(proto) if isinstance(proto, dict) else None
				if proc is not None:
					definitions.setdefault(proc, []).append(bid)
			elif op == "procedures_call":
				proc = _procedure_key(b)
				if proc is not None:
					calls.add(proc)

		removable = {proc for proc in definitions if proc not in calls}
		if not removable:
			continue
		removable_total += len(removable)

		edges = _build_block_graph(target)     # build ONCE per target
		to_delete = set()
		for proc in removable:
			for bid in definitions[proc]:
				todo, seen = [bid], set()
				while todo:
					x = todo.pop()
					if x in seen or x not in blocks:
						continue
					seen.add(x)
					todo.extend(edges.get(x, ()))
				to_delete |= seen
		for x in to_delete:
			del blocks[x]
			removed += 1
	stats["procedures_removed"] = removable_total
	stats["blocks_removed"] += removed
	return removable_total


def normalize_numbers(project, stats, epsilon):
	changed = 0
	def rec(v):
		nonlocal changed
		if isinstance(v, bool):
			return v
		if isinstance(v, float) and v not in (float("inf"), float("-inf")):
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

	conversions = {}   # old_filename -> new_filename, for the caller/verifier
	for target in project.get("targets", []):
		for sound in target.get("sounds", []):
			if not isinstance(sound, dict) or sound.get("dataFormat") != "wav":
				continue
			old_ext_name = sound.get("md5ext") or f"{sound.get('assetId')}.wav"
			wav_bytes = assets.get(old_ext_name)
			if wav_bytes is None:
				continue   # asset missing from the archive; nothing to convert
			mp3_bytes = _encode_wav_to_mp3(wav_bytes)
			if mp3_bytes is None:
				stats["wav_conversion_failed"] += 1
				continue

			new_asset_id = hashlib.md5(mp3_bytes).hexdigest()
			new_ext_name = f"{new_asset_id}.mp3"

			assets[new_ext_name] = mp3_bytes
			if old_ext_name in assets and old_ext_name != new_ext_name:
				del assets[old_ext_name]

			sound["assetId"] = new_asset_id
			sound["dataFormat"] = "mp3"
			sound["md5ext"] = new_ext_name
			conversions[old_ext_name] = new_ext_name
			stats["wav_converted"] += 1
			stats["wav_bytes_saved"] += len(wav_bytes) - len(mp3_bytes)

	return conversions


def remove_sound_metadata(project):
	for target in project.get("targets", []):
		for sound in target.get("sounds", []):
			if isinstance(sound, dict):
				sound.pop("rate", None)
				sound.pop("sampleCount", None)


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


def remove_costume_metadata(project, stats):
	count = 0
	for target in project.get("targets", []):
		for costume in target.get("costumes", []):
			if isinstance(costume, dict):
				aid = costume.get("assetId")
				fmt = costume.get("dataFormat")
				if aid and fmt and costume.get("md5ext") == f"{aid}.{fmt}":
					del costume["md5ext"]
					count += 1
				if fmt == "svg" and costume.get("bitmapResolution") == 1:
					del costume["bitmapResolution"]
					count += 1
	stats["costume_metadata_removed"] += count
	return count


def remove_empty_containers(project, stats):
	count = 0
	for target in project.get("targets", []):
		if not target.get("isStage"):
			if target.get("broadcasts") == {}:
				del target["broadcasts"]
				count += 1
			if target.get("comments") == {}:
				del target["comments"]
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
		remove_unreachable=False,
		remove_unused_procedures=False,
		normalize_numbers=False,
		remove_empty_fields=False,
		remove_empty_inputs=False,
		remove_costume_metadata=False,
		remove_empty_containers=False,
		remove_project_meta=False,
		convert_wav_to_mp3=False,
		sort_keys=False,
		compression_level=9,
		list_bytes=DEFAULT_LIST_BYTES,
		list_items=DEFAULT_LIST_ITEMS,
		normalize_epsilon=DEFAULT_EPSILON,
		keep_sound_metadata=False
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
		self.remove_unreachable = remove_unreachable
		self.remove_unused_procedures = remove_unused_procedures
		self.normalize_numbers = normalize_numbers
		self.remove_empty_fields = remove_empty_fields
		self.remove_empty_inputs = remove_empty_inputs
		self.remove_costume_metadata = remove_costume_metadata
		self.remove_empty_containers = remove_empty_containers
		self.remove_project_meta = remove_project_meta
		self.convert_wav_to_mp3 = convert_wav_to_mp3
		self.sort_keys = sort_keys
		self.compression_level = compression_level
		self.renamed_block_ids = {}
		self.renamed_variable_ids = {}
		self.renamed_list_ids = {}
		self.renamed_broadcast_ids = {}
		self.renamed_argument_ids = {}
		self.wav_conversions = {}
		self.list_bytes, self.list_items = list_bytes, list_items
		self.cleared_lists = frozenset()
		self.normalize_epsilon = normalize_epsilon
		self.keep_sound_metadata = keep_sound_metadata


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
	if opts.remove_unused_variables or opts.remove_unused_lists:
		remove_unused_data(
			project, stats, opts.remove_unused_variables, opts.remove_unused_lists
		)
	if opts.remove_unused_procedures:
		remove_unused_procedures(project, stats)
	if opts.remove_unreachable:
		remove_unreachable_blocks(project, stats)
	used_data_ids = set()
	if opts.rename_variable_ids or opts.rename_list_ids:
		opts.renamed_variable_ids, opts.renamed_list_ids = rename_variable_list_ids(
			project, stats, opts.rename_variable_ids, opts.rename_list_ids
		)
		used_data_ids.update(opts.renamed_variable_ids.values())
		used_data_ids.update(opts.renamed_list_ids.values())
	if opts.rename_broadcast_ids:
		opts.renamed_broadcast_ids = rename_broadcast_ids(
			project, stats, existing_ids=used_data_ids
		)
	if opts.rename_argument_ids:
		opts.renamed_argument_ids = rename_argument_ids(project, stats)
	if opts.rename_block_ids:
		opts.renamed_block_ids = rename_block_ids(project, stats)
	if opts.normalize_numbers:
		normalize_numbers(project, stats, opts.normalize_epsilon)
	if not opts.keep_sound_metadata:
		remove_sound_metadata(project)
	if opts.remove_empty_fields:
		remove_empty_fields(project, stats)
	if opts.remove_empty_inputs:
		remove_empty_inputs(project, stats)
	if opts.remove_costume_metadata:
		remove_costume_metadata(project, stats)
	if opts.remove_empty_containers:
		remove_empty_containers(project, stats)
	if opts.remove_project_meta:
		remove_project_meta(project, stats)
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


def _check_broadcast_ids_resolve(project):
	stage = next((t for t in project.get("targets", []) if t.get("isStage")), None)
	valid = set((stage or {}).get("broadcasts") or {})
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
	return None


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


def _check_blocks(o, m, opts, where):
	if set(o) != set(m):
		# comment link is the only key allowed to disappear, and only when stripping comments
		gone = set(o) - set(m)
		extra = set(m) - set(o)
		if extra or not (opts.comments and gone <= {"comment"}):
			return f"{where}: block keys changed {sorted(gone)} / {sorted(extra)}"
	for k in o:
		if k not in m:
			continue  # 'comment', handled above
		if k in ("x", "y") and opts.positions:
			if not _num_eq(o[k], m[k]) and o[k] != m[k]:
				return f"{where}: {k} changed {o[k]!r} -> {m[k]!r}"
		elif k == "inputs":
			if set(o["inputs"]) != set(m["inputs"]):
				return f"{where}: input names changed"
			for name in o["inputs"]:
				oi, mi = o["inputs"][name], m["inputs"][name]
				if oi == mi:
					continue
				# only obscured shadow literal of a covered input is allowed
				ok = (
					opts.covered
					and isinstance(oi, list)
					and isinstance(mi, list)
					and len(oi) == 3
					and len(mi) == 3
					and oi[0] == 3
					and mi[0] == 3
					and oi[1] == mi[1]  # covering block untouched
					and isinstance(oi[2], list)
					and isinstance(mi[2], list)
					and len(oi[2]) == 2
					and len(mi[2]) == 2
					and oi[2][0] == mi[2][0]  # type tag untouched
					and oi[2][0] in (4, 5, 6, 7, 8, 10)
				)  # never color/broadcast
				if not ok:
					return f"{where}: input {name!r} changed {oi!r} -> {mi!r}"
		elif o[k] != m[k]:
			return f"{where}: {k} changed"
	return None


def verify(original_path, minified_path, opts):
	with zipfile.ZipFile(original_path) as a, zipfile.ZipFile(minified_path) as b:
		if b.testzip() is not None:
			return False, "output zip failed CRC test"
		if set(a.namelist()) != set(b.namelist()):
			return False, "zip entry lists differ"
		for name in a.namelist():
			if name != "project.json" and a.read(name) != b.read(name):
				return False, f"asset changed: {name}"

		orig = _reinflate(json.loads(a.read("project.json")))
		mini = _reinflate(json.loads(b.read("project.json")))

		if opts.rename_broadcast_ids:
			err = _check_broadcast_ids_resolve(json.loads(b.read("project.json")))
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
				return False, f"top-level key {key!r} changed"
		if len(orig["targets"]) != len(mini["targets"]):
			return False, "target count changed"

		approved = set(getattr(opts, "cleared_lists", ()) or ())
		for ti, (to, tm) in enumerate(zip(orig["targets"], mini["targets"])):
			name = to.get("name")
			if set(to) != set(tm):
				return False, f"{name!r}: target keys changed"
			for k in to:
				if k in ("blocks", "comments", "lists", "variables", "sounds", "costumes"):
					continue
				if to[k] != tm[k]:
					if opts.normalize_numbers and _num_eq(to[k], tm[k]):
						continue
					return False, f"{name!r}: {k} changed"

			to_vars = to.get("variables", {})
			tm_vars = tm.get("variables", {})
			if set(tm_vars) - set(to_vars):
				return False, f"{name!r}: unexpected variable ids appeared"
			missing_vars = set(to_vars) - set(tm_vars)
			if missing_vars and not opts.remove_unused_variables:
				return False, f"{name!r}: variable id set changed"
			if missing_vars:
				used_vars = _collect_data_ids(orig)[0]
				still_used = {vid for vid in missing_vars if (ti, vid) in used_vars}
				if still_used:
					return False, f"{name!r}: referenced variable was removed"
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
					return False, f"{name!r}: variable {vo[0] if isinstance(vo, list) and vo else vid!r} changed"

			to_lists = to.get("lists", {})
			tm_lists = tm.get("lists", {})
			if set(tm_lists) - set(to_lists):
				return False, f"{name!r}: unexpected list ids appeared"
			missing_lists = set(to_lists) - set(tm_lists)
			if missing_lists and not opts.remove_unused_lists:
				return False, f"{name!r}: list id set changed"
			if missing_lists:
				used_lists = _collect_data_ids(orig)[1]
				still_used = {lid for lid in missing_lists if (ti, lid) in used_lists}
				if still_used:
					return False, f"{name!r}: referenced list was removed"
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
						f"{name!r}: list {lo[0]!r} changed but was not approved for clearing",
					)

			# blocks
			if set(tm["blocks"]) - set(to["blocks"]):
				return False, f"{name!r}: new block ids appeared"
			missing_blocks = set(to["blocks"]) - set(tm["blocks"])
			if missing_blocks and not (opts.remove_unreachable or opts.remove_unused_procedures):
				return False, f"{name!r}: block id set changed"
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
						return False, f"{name!r}/{bid!r}: primitive changed"
					continue
				err = _check_blocks(bo, bm, opts, f"{name!r}/{bid!r}")
				if err:
					return False, err
				# structural rules the VM / sb3fix rely on
				if not isinstance(bm.get("inputs"), dict) or not isinstance(
					bm.get("fields"), dict
				):
					return (
						False,
						f"{name!r}/{bid!r}: inputs/fields not objects (sb3fix rejects)",
					)
				if "next" not in bm or "parent" not in bm:
					return False, f"{name!r}/{bid!r}: lost next/parent"
				mu = bm.get("mutation")
				if mu is not None and ("tagName" not in mu or "children" not in mu):
					return False, f"{name!r}/{bid!r}: lost mutation tagName/children"

			# comments
			co, cm = to["comments"], tm["comments"]
			if not isinstance(cm, dict):
				return False, f"{name!r}: comments not an object (sb3fix rejects)"
			if opts.comments and not to.get("isStage"):
				if cm:
					return False, f"{name!r}: comments remain"
			else:
				if set(co) != set(cm):
					return False, f"{name!r}: comment id set changed"
				for cid in co:
					for f in set(co[cid]) | set(cm[cid]):
						x, y = co[cid].get(f), cm[cid].get(f)
						if f in ("x", "y", "width", "height") and opts.positions:
							if not (x == y or _num_eq(x, y)):
								return False, f"{name!r}: comment {cid!r} {f} changed"
						elif x != y:
							return False, f"{name!r}: comment {cid!r} {f} changed"
			if opts.comments and not to.get("isStage"):
				if any(
					isinstance(x, dict) and "comment" in x
					for x in tm["blocks"].values()
				):
					return False, f"{name!r}: dangling block.comment link"

			for snd in tm.get("sounds", []):
				if "md5ext" not in snd:
					return False, f"sound {snd.get('name')!r} lost md5ext"

			to_costumes = to.get("costumes", [])
			tm_costumes = tm.get("costumes", [])
			if len(to_costumes) != len(tm_costumes):
				return False, f"{name!r}: costume count changed"
			for co, cm in zip(to_costumes, tm_costumes):
				for key, value in cm.items():
					if key in ("rotationCenterX", "rotationCenterY"):
						continue
					if value != co.get(key):
						return False, f"costume data doesn't match at key '{key}'"
				if opts.remove_costume_metadata:
					diff = set(co) - set(cm)
					for d in diff:
						if d == "md5ext" and co.get("md5ext") == f"{co.get('assetId')}.{co.get('dataFormat')}":
							continue
						if d == "bitmapResolution" and co.get("dataFormat") == "svg" and co.get("bitmapResolution") == 1:
							continue
						return False, f"unexpected costume key {d!r} removed"
				elif set(co) != set(cm):
					return False, f"costume keys changed without --remove-costume-metadata"

			if _check_argument_id_consistency(to, name) is None:
				err = _check_argument_id_consistency(tm, name)
				if err:
					return False, err

		# monitors
		err = _check_monitors(orig, mini, opts)
		if err:
			return False, err
	return True, "ok"


def _check_monitors(orig, mini, opts):
	mo, mm = orig.get("monitors", []), mini.get("monitors", [])
	if not opts.monitors:
		return None if mo == mm else "monitors changed with --keep-monitors"

	def key(m):
		return (m.get("id"), m.get("spriteName"), m.get("opcode"))

	by_key = {key(m): m for m in mo}
	order = [key(m) for m in mo]
	prev = -1
	for m in mm:
		k = key(m)
		if k not in by_key:
			return f"monitor {k} appeared from nowhere"
		if order.index(k) < prev:
			return "monitor order changed"
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
			return f"monitor {k}: field {f!r} changed {o.get(f)!r} -> {m.get(f)!r}"

	# every removed monitor must be one we are allowed to remove
	survivors = {key(m) for m in mm}
	targets = orig["targets"]
	stage = next((t for t in targets if t.get("isStage")), None)
	by_name = {t.get("name"): t for t in targets if not t.get("isStage")}
	shown = _variable_ids_used_by_blocks(orig)

	for m in mo:
		if key(m) in survivors:
			continue
		if m.get("opcode") not in ("data_variable", "data_listcontents"):
			return f"removed a non-variable/list monitor {key(m)}"
		owner = by_name.get(m.get("spriteName")) if m.get("spriteName") else stage
		store = "lists" if m["opcode"] == "data_listcontents" else "variables"
		orphan = owner is None or m.get("id") not in owner.get(store, {})
		unused = (
			m.get("visible") is False
			and m.get("id") not in shown
			and _is_default_layout(m)
		)
		if not (orphan or unused):
			return f"monitor {key(m)} removed but is neither orphaned nor unused"
		if m.get("visible") is True:
			return f"a visible monitor {key(m)} was removed"
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
	("variable_ids", "variable IDs renamed"),
	("list_ids", "list IDs renamed"),
	("broadcast_ids", "broadcast IDs renamed"),
	("argument_ids", "argument IDs renamed"),
	("variables_removed", "unused variables removed"),
	("lists_removed", "unused lists removed"),
	("blocks_removed", "unreachable/procedure blocks removed"),
	("procedures_removed", "unused procedures removed"),
	("numbers_normalized", "integral numbers normalized"),
	("empty_fields_removed", "empty block fields removed"),
	("empty_inputs_removed", "empty block inputs removed"),
	("costume_metadata_removed", "redundant costume metadata removed"),
	("empty_containers_removed", "empty target containers removed"),
	("project_meta_cleaned", "project meta fields cleaned"),
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

		assets = {
			item.filename: zin.read(item.filename)
			for item in zin.infolist()
			if item.filename != "project.json"
		}
		# original_asset_names = set(assets)

		stats = apply_transforms(project, opts, assets)
		out_json = dumps_compact(project, opts.sort_keys).encode("utf-8")
		with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=opts.compression_level) as zout:
			zout.writestr("project.json", out_json)
			for name, data in assets.items():
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
		print(Ansi.muted("Output deleted. Original untouched."))
		return 2
	print(Ansi.success("Verified: only the intended fields differ, all assets bfb ident."))
	return 0


if __name__ == "__main__":
	flags = [a for a in sys.argv[1:] if a.startswith("--")]
	args = [a for a in sys.argv[1:] if not a.startswith("--")]
	toggles = {
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
		"--remove-unreachable",
		"--remove-unused-procedures",
		"--normalize-numbers",
		"--remove-empty-fields",
		"--remove-empty-inputs",
		"--remove-costume-metadata",
		"--remove-empty-containers",
		"--remove-project-meta",
		"--sort-keys",
		"--keep-sound-metadata",
	}
	valued = {"--list-bytes", "--list-items", "--compression-level"}
	values, bad = {}, []
	for f in flags:
		key, eq, val = f.partition("=")
		if key in toggles and not eq:
			continue
		if key in valued and eq and val.isdigit():
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
				f"Valid: {sorted(toggles)} and --list-bytes=N --list-items=N (positive), --compression-level=N (0-9)"
			)
		)
		sys.exit(1)
	if not args:
		print(__doc__)
		sys.exit(1)
	all_flags = "--all-flags" in flags
	opts = Options(
		comments="--keep-comments" not in flags,
		positions="--keep-positions" not in flags,
		covered="--keep-covered" not in flags,
		monitors="--keep-monitors" not in flags,
		lists="--keep-lists" not in flags,
		rename_block_ids=all_flags or "--rename-block-ids" in flags,
		rename_variable_ids=all_flags or "--rename-variable-ids" in flags,
		rename_list_ids=all_flags or "--rename-list-ids" in flags,
		rename_broadcast_ids=all_flags or "--rename-broadcast-ids" in flags,
		rename_argument_ids=all_flags or "--rename-argument-ids" in flags,
		remove_unused_variables=all_flags or "--remove-unused-variables" in flags,
		remove_unused_lists=all_flags or "--remove-unused-lists" in flags,
		remove_unreachable=all_flags or "--remove-unreachable" in flags,
		remove_unused_procedures=all_flags or "--remove-unused-procedures" in flags,
		normalize_numbers=all_flags or "--normalize-numbers" in flags,
		remove_empty_fields=all_flags or "--remove-empty-fields" in flags,
		remove_empty_inputs=all_flags or "--remove-empty-inputs" in flags,
		remove_costume_metadata=all_flags or "--remove-costume-metadata" in flags,
		remove_empty_containers=all_flags or "--remove-empty-containers" in flags,
		remove_project_meta=all_flags or "--remove-project-meta" in flags,
		sort_keys="--sort-keys" in flags,
		compression_level=values.get("--compression-level", 9),
		list_bytes=values.get("--list-bytes", DEFAULT_LIST_BYTES),
		list_items=values.get("--list-items", DEFAULT_LIST_ITEMS),
		normalize_epsilon=values.get("--normalize_epsilon", DEFAULT_EPSILON),
		keep_sound_metadata="--keep-sound-metadata" in flags,
	)
	dst = args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + "_minified.sb3"
	if os.path.abspath(args[0]) == os.path.abspath(dst):
		print(Ansi.error("Error: output path must differ from input path."))
		sys.exit(1)
	sys.exit(minify_sb3(args[0], dst, opts))
