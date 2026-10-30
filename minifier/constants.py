from __future__ import annotations

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
from minify_options import Options, DEFAULT_LIST_BYTES, DEFAULT_LIST_ITEMS, Ansi, build_options
from types import SimpleNamespace, NoneType
from typing import Any, NoReturn



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
	(
		"fields",
		"inputs",
		"mutation",
		"next",
		"opcode",
		"parent",
		"shadow",
		"topLevel",
		"x",
		"y",
	),
)

_REFERENCE_FIELDS = {
	"VARIABLE": (
		"variables",
		frozenset(
			(
				"data_variable",
				"data_setvariableto",
				"data_changevariableby",
				"data_showvariable",
				"data_hidevariable",
			)
		),
	),
	"LIST": (
		"lists",
		frozenset(
			(
				"data_listcontents",
				"data_addtolist",
				"data_deleteoflist",
				"data_deletealloflist",
				"data_insertatlist",
				"data_replaceitemoflist",
				"data_itemoflist",
				"data_itemnumoflist",
				"data_lengthoflist",
				"data_listcontainsitem",
				"data_hidelist",
				"data_showlist",
			)
		),
	),
}

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

BLOCK_ID_ALPHABET = "!@#$%^*()+_-={}|[]:;?,./~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

BOOLEAN_INPUTS = {
	"operator_and": ("OPERAND1", "OPERAND2"),
	"operator_or": ("OPERAND1", "OPERAND2"),
	"operator_not": ("OPERAND",),
	"control_if": ("CONDITION",),
	"control_if_else": ("CONDITION",),
	"control_repeat_until": ("CONDITION",),
	"control_wait_until": ("CONDITION",),
	"control_while": ("CONDITION",),
}

INPUT_SAME_BLOCK_SHADOW = 1

INPUT_BLOCK_NO_SHADOW = 2

INPUT_DIFF_BLOCK_SHADOW = 3

PRIMITIVE_NUMBER = 4

PRIMITIVE_POSITIVE_NUMBER = 5

PRIMITIVE_WHOLE_NUMBER = 6

PRIMITIVE_INTEGER = 7

PRIMITIVE_ANGLE = 8

PRIMITIVE_COLOR = 9

PRIMITIVE_TEXT = 10

PRIMITIVE_BROADCAST = 11

PRIMITIVE_VARIABLE = 12

PRIMITIVE_LIST = 13

NUMERIC_TAGS = (
	PRIMITIVE_NUMBER,
	PRIMITIVE_POSITIVE_NUMBER,
	PRIMITIVE_WHOLE_NUMBER,
	PRIMITIVE_INTEGER,
	PRIMITIVE_ANGLE,
)

INPUT_TAGS = (INPUT_SAME_BLOCK_SHADOW, INPUT_BLOCK_NO_SHADOW, INPUT_DIFF_BLOCK_SHADOW)

CONTAINER_TAGS = (PRIMITIVE_VARIABLE, PRIMITIVE_LIST)

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

WAV_TO_MP3_BITRATE = "128k"

WAV_TO_MP3_SAMPLE_RATE = 44100

WAV_TO_MP3_CHANNELS = 2

SEQUENCE_MAX_PARAMETERS = 8

SEQUENCE_MAX_WINDOW = 256

_PROCEDURE_EFFECTFUL_ARGUMENT_OPCODES = {
	"operator_random",
	"sensing_askandwait",
	"procedures_call",
}

NUMERIC_REPORTER_OPCODES = {
	"operator_add",
	"operator_subtract",
	"operator_multiply",
	"operator_divide",
	"operator_mod",
	"operator_round",
	"operator_mathop",
	"operator_length",
	"motion_xposition",
	"motion_yposition",
	"motion_direction",
	"looks_size",
	"looks_costumenumbername",
	"looks_backdropnumbername",
	"sound_volume",
	"sensing_timer",
	"sensing_dayssince2000",
}

BOOLEAN_REPORTER_OPCODES = {
	"operator_not",
	"operator_and",
	"operator_or",
	"operator_gt",
	"operator_lt",
	"operator_equals",
	"operator_contains",
	"sensing_touchingobject",
	"sensing_touchingcolor",
	"sensing_keypressed",
	"sensing_mousedown",
	"sensing_loud",
	"sensing_askandwait",
	"video_sensing_on",
}

FINITE_NUMERIC_REPORTER_OPCODES = {
	"operator_length",
	"motion_xposition",
	"motion_yposition",
	"motion_direction",
	"looks_size",
	"sound_volume",
	"sensing_timer",
	"sensing_dayssince2000",
}

PURE_REPORTERS = {
	"data_variable",
	"data_itemoflist",
	"data_itemnumoflist",
	"data_lengthoflist",
	"data_listcontainsitem",
	"data_listcontents",
	"looks_costumenumbername",
	"looks_backdropnumbername",
	"looks_size",
	"sound_volume",
	"motion_xposition",
	"motion_yposition",
	"motion_direction",
	"sensing_answer",
	"sensing_timer",
	"sensing_dayssince2000",
	"sensing_username",
	"sensing_current",
	"sensing_mousedown",
	"sensing_mousex",
	"sensing_mousey",
	"sensing_loudness",
}

OPERATOR_INPUTS = {
	"default": "STRING",
	"operator_add": "NUM",
	"operator_mul": "NUM",
	"operator_join": "STRING",
	"operator_and": "OPERAND",
	"operator_or": "OPERAND",
}

_CFG_SCHEDULER_YIELD_OPCODES = frozenset(
	{
		"control_wait",
	}
)

_EXTENSION_PREFIXES = {
	"pen": "pen_",
	"music": "music_",
	"videoSensing": "videoSensing_",
	"text2speech": "text2speech_",
	"translate": "translate_",
	"makeymakey": "makeymakey_",
	"microbit": "microbit_",
	"microbit_more": "microbit_more_",
	"boost": "boost_",
	"ev3": "ev3_",
	"wedo2": "wedo2_",
	"gdxfor": "gdxfor_",
}

_DEBUG_STRUCTURE_ENABLED = False

_DEBUG_STRUCTURE_BASELINE = None

_ASSET_EXTENSIONS = {
	"costume": {"png", "svg", "jpeg", "jpg", "bmp", "gif"},
	"sound": {"wav", "wave", "mp3"},
}

VERIFY_PARALLEL_MIN_BLOCKS = 3000

VERIFY_PARALLEL_MAX_WORKERS = 4

_VERIFY_WORKER_STATE = None

_VERIFY_WORKER_STATE_PATH = None

_VERIFY_BLOCK_OPT_ATTRS = (
	"comments",
	"positions",
	"strip_script_positions",
	"covered",
	"remove_unreachable",
	"remove_unused_procedures",
	"compact_terminal_links",
	"compact_field_ids",
	"compact_mutation_hasnext",
	"compact_mutation_metadata",
	"rename_argument_ids",
	"strip_reference_names",
	"normalize_epsilon",
	"normalize_numbers",
	"optimize_json",
	"minimum_json",
	"compact_numeric_inputs",
	"inline_single_use_procedures",
	"specialize_procedures",
)

_VERIFY_BLOCK_MAP_ATTRS = (
	"variable_setter_input_edits",
	"variable_setter_link_edits",
	"procedure_prototype_compaction_input_edits",
	"procedure_prototype_compaction_shadow_edits",
	"procedure_argument_input_edits",
	"procedure_argument_mutation_edits",
	"procedure_argument_removed_inputs",
	"folded_constant_expression_inputs",
	"folded_constant_expression_link_edits",
	"folded_constant_expression_opcode_edits",
	"grouped_sequence_input_edits",
	"grouped_sequence_link_edits",
	"inlined_procedure_input_edits",
	"inlined_procedure_link_edits",
	"specialized_procedure_call_proccodes",
	"specialized_procedure_prototype_proccodes",
	"specialized_procedure_prototype_blocks",
	"specialized_procedure_prototype_code_pairs",
	"specialized_procedure_block_origins",
	"script_rewrite_input_edits",
	"script_rewrite_link_edits",
	"script_rewrite_opcode_edits",
	"script_rewrite_removed_inputs",
	"simplified_block_input_edits",
	"simplified_block_link_edits",
	"simplified_block_opcode_edits",
	"folded_constant_variables",
	"folded_constant_variable_literals",
)

STAT_GROUPS = [
	(
		"1.",
		"Optimize blocks",
		[
			("topLevel", "topLevel:false dropped"),
			("shadow", "shadow:false dropped"),
			("warp", "mutation.warp normalized to boolean"),
            ("orphaned_reporters_removed", "orphaned reporters removed"),
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
			("boolean_complement", "complement law rewrites"),
			("boolean_idempotent", "idempotent law rewrites"),
			("boolean_absorption", "absorption law rewrites"),
			("boolean_factoring", "boolean factorizations"),
			("math_factoring", "math expression factorizations"),
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

__all__ = ['FAST_JSON_RAW_THRESHOLD', 'FAST_JSON_BLOCK_THRESHOLD', 'FAST_JSON_ORDER_COUNT', 'FAST_JSON_SCREEN_LEVEL', 'FAST_JSON_SEARCH_LEVEL', 'BLOCK_KEY_ORDERS', '_REFERENCE_FIELDS', '_SYNC_REPORTERS', '_SPECIAL_ARGUMENT_NAMES', '_RESERVED_PROCEDURE_SYMBOLS', '_SYNC_TERMINAL_COMMANDS', '_SYNC_TERMINAL_MENUS', '_COVERED_INPUT_OPCODES', '_DATA_PRUNING_OPCODES', 'BLOCK_ID_ALPHABET', 'BOOLEAN_INPUTS', 'INPUT_SAME_BLOCK_SHADOW', 'INPUT_BLOCK_NO_SHADOW', 'INPUT_DIFF_BLOCK_SHADOW', 'PRIMITIVE_NUMBER', 'PRIMITIVE_POSITIVE_NUMBER', 'PRIMITIVE_WHOLE_NUMBER', 'PRIMITIVE_INTEGER', 'PRIMITIVE_ANGLE', 'PRIMITIVE_COLOR', 'PRIMITIVE_TEXT', 'PRIMITIVE_BROADCAST', 'PRIMITIVE_VARIABLE', 'PRIMITIVE_LIST', 'NUMERIC_TAGS', 'INPUT_TAGS', 'CONTAINER_TAGS', 'LIST_READ_OPS', 'LIST_ADD_OPS', 'LIST_REPLACE_OPS', 'LIST_DELETE_OPS', 'LIST_SHOWHIDE_OPS', 'WAV_TO_MP3_BITRATE', 'WAV_TO_MP3_SAMPLE_RATE', 'WAV_TO_MP3_CHANNELS', 'SEQUENCE_MAX_PARAMETERS', 'SEQUENCE_MAX_WINDOW', '_PROCEDURE_EFFECTFUL_ARGUMENT_OPCODES', 'NUMERIC_REPORTER_OPCODES', 'BOOLEAN_REPORTER_OPCODES', 'FINITE_NUMERIC_REPORTER_OPCODES', 'PURE_REPORTERS', 'OPERATOR_INPUTS', '_CFG_SCHEDULER_YIELD_OPCODES', '_EXTENSION_PREFIXES', '_DEBUG_STRUCTURE_ENABLED', '_DEBUG_STRUCTURE_BASELINE', '_ASSET_EXTENSIONS', 'VERIFY_PARALLEL_MIN_BLOCKS', 'VERIFY_PARALLEL_MAX_WORKERS', '_VERIFY_WORKER_STATE', '_VERIFY_WORKER_STATE_PATH', '_VERIFY_BLOCK_OPT_ATTRS', '_VERIFY_BLOCK_MAP_ATTRS', 'STAT_GROUPS']
