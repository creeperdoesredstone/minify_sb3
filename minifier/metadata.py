import copy
import json
import re

from .json_layout import JsonNumber, _number_components, _REFERENCE_FIELDS
from .json_optimizer import (
	javascript_keys,
	_block_slots,
	_identifier_names
)
from .options import Options
from .tags import (
	INPUT_SAME_BLOCK_SHADOW,
	INPUT_BLOCK_NO_SHADOW,
	INPUT_DIFF_BLOCK_SHADOW,
	PRIMITIVE_NUMBER,
	PRIMITIVE_POSITIVE_NUMBER,
	PRIMITIVE_WHOLE_NUMBER,
	PRIMITIVE_INTEGER,
	PRIMITIVE_ANGLE,
	PRIMITIVE_COLOR,
	PRIMITIVE_TEXT,
	PRIMITIVE_BROADCAST,
	PRIMITIVE_VARIABLE,
	PRIMITIVE_LIST,
	INPUT_TAGS,
	NUMERIC_TAGS,
	CONTAINER_TAGS,
	_input_tag
)
from collections import Counter
from collections.abc import Generator
from typing import Any


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


def prune_orphan_arguments(project, result=None):
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


_SYNC_REPORTERS = frozenset(
	(
		"operator_add",
		"operator_subtract",
		"operator_multiply",
		"operator_divide",
		"operator_random",
		"operator_gt",
		"operator_lt",
		"operator_equals",
		"operator_and",
		"operator_or",
		"operator_not",
		"operator_join",
		"operator_letter_of",
		"operator_length",
		"operator_contains",
		"operator_mod",
		"operator_round",
		"operator_mathop",
		"argument_reporter_string_number",
		"argument_reporter_boolean",
		"data_variable",
		"data_itemoflist",
		"data_itemnumoflist",
		"data_lengthoflist",
		"data_listcontainsitem",
		"motion_xposition",
		"motion_yposition",
		"motion_direction",
		"looks_size",
		"looks_costumenumbername",
		"looks_backdropnumbername",
		"sound_volume",
		"sensing_timer",
		"sensing_mousex",
		"sensing_mousey",
		"sensing_mousedown",
		"sensing_keypressed",
		"sensing_current",
		"sensing_dayssince2000",
		"sensing_username",
		"sensing_of",
		"sensing_answer",
		"sensing_loudness",
		"sensing_touchingobject",
		"sensing_touchingcolor",
		"sensing_coloristouchingcolor",
		"sensing_distanceto",
	)
)

_SPECIAL_ARGUMENT_NAMES = frozenset(
	("last key pressed", "is compiled?", "is turbowarp?")
)
_RESERVED_PROCEDURE_SYMBOLS = frozenset(
	(
		"__proto__",
		"constructor",
		"prototype",
		"toString",
		"toLocaleString",
		"valueOf",
		"hasOwnProperty",
		"isPrototypeOf",
		"propertyIsEnumerable",
		"__defineGetter__",
		"__defineSetter__",
		"__lookupGetter__",
		"__lookupSetter__",
		"mutation",
	)
)


_SYNC_TERMINAL_COMMANDS = frozenset(
	(
		"data_setvariableto",
		"data_changevariableby",
		"data_addtolist",
		"data_deleteoflist",
		"data_deletealloflist",
		"data_insertatlist",
		"data_replaceitemoflist",
	)
)
_SYNC_TERMINAL_MENUS = frozenset(
	(
		"sensing_of_object_menu",
		"looks_costume",
		"sensing_keyoptions",
		"sound_sounds_menu",
		"control_create_clone_of_menu",
		"looks_backdrops",
		"motion_goto_menu",
		"motion_pointtowards_menu",
	)
)


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


_COVERED_INPUT_OPCODES = _SYNC_REPORTERS | frozenset(
	(
		"procedures_call",
		"control_if",
		"control_if_else",
		"control_repeat",
		"control_repeat_until",
		"control_wait",
		"control_wait_until",
		"data_setvariableto",
		"data_changevariableby",
		"data_addtolist",
		"data_deleteoflist",
		"data_insertatlist",
		"data_replaceitemoflist",
		"motion_movesteps",
		"motion_turnright",
		"motion_turnleft",
		"motion_gotoxy",
		"motion_glidesecstoxy",
		"motion_pointindirection",
		"motion_changexby",
		"motion_setx",
		"motion_changeyby",
		"motion_sety",
		"looks_sayforsecs",
		"looks_say",
		"looks_thinkforsecs",
		"looks_think",
		"looks_changesizeby",
		"looks_setsizeto",
		"looks_changeeffectby",
		"looks_seteffectto",
		"looks_goforwardbackwardlayers",
		"sound_changeeffectby",
		"sound_seteffectto",
		"sound_changevolumeby",
		"sound_setvolumeto",
		"sensing_askandwait",
		"pen_setPenColorToColor",
		"pen_changePenColorParamBy",
		"pen_setPenColorParamTo",
		"pen_changePenSizeBy",
		"pen_setPenSizeTo",
	)
)


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


_DATA_PRUNING_OPCODES = _COVERED_INPUT_OPCODES | frozenset(
	(
		"control_create_clone_of",
		"control_create_clone_of_menu",
		"control_delete_this_clone",
		"control_for_each",
		"control_forever",
		"control_start_as_clone",
		"control_stop",
		"control_while",
		"control_all_at_once",
		"data_deletealloflist",
		"data_listcontents",
		"data_showvariable",
		"data_hidevariable",
		"data_showlist",
		"data_hidelist",
		"event_broadcast",
		"event_broadcastandwait",
		"event_broadcast_menu",
		"event_whenbroadcastreceived",
		"event_whenflagclicked",
		"event_whenkeypressed",
		"event_whenthisspriteclicked",
		"event_whenstageclicked",
		"event_whenbackdropswitchesto",
		"event_whengreaterthan",
		"looks_backdrops",
		"looks_cleargraphiceffects",
		"looks_costume",
		"looks_gotofrontback",
		"looks_hide",
		"looks_show",
		"looks_switchbackdropto",
		"looks_switchbackdroptoandwait",
		"looks_nextbackdrop",
		"looks_switchcostumeto",
		"looks_nextcostume",
		"motion_goto",
		"motion_goto_menu",
		"motion_glideto",
		"motion_glideto_menu",
		"motion_ifonedgebounce",
		"motion_pointtowards",
		"motion_pointtowards_menu",
		"motion_setrotationstyle",
		"music_midiSetInstrument",
		"music_playNoteForBeats",
		"music_setTempo",
		"music_getTempo",
		"music_changeTempo",
		"music_playDrumForBeats",
		"music_midiPlayDrumForBeats",
		"music_restForBeats",
		"music_setInstrument",
		"music_menu_DRUM",
		"music_menu_INSTRUMENT",
		"pen_clear",
		"pen_stamp",
		"pen_penDown",
		"pen_penUp",
		"pen_menu_colorParam",
		"pen_changePenHueBy",
		"pen_setPenHueToNumber",
		"pen_changePenShadeBy",
		"pen_setPenShadeToNumber",
		"procedures_definition",
		"procedures_prototype",
		"sensing_keyoptions",
		"sensing_of_object_menu",
		"sensing_of_property_menu",
		"sensing_touchingobjectmenu",
		"sensing_distancetomenu",
		"sensing_resettimer",
		"sensing_setdragmode",
		"sound_play",
		"sound_playuntildone",
		"sound_sounds_menu",
		"sound_stopallsounds",
		"sound_cleareffects",
		"math_number",
		"math_positive_number",
		"math_whole_number",
		"math_integer",
		"math_angle",
		"colour_picker",
		"text",
	)
)


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
