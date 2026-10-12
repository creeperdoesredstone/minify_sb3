import copy
import json

from functools import lru_cache
from typing import Any, NoReturn


class JsonNumber(str):
	__slots__ = ()

	def __deepcopy__(self, memo):
		return self


def get_unique_object(pairs) -> dict:
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError(f"duplicate JSON object key: {key!r}")
		result[key] = value
	return result


def invalid_constant(value) -> NoReturn:
	raise ValueError(f"non-finite JSON number: {value}")


def loads_exact(data) -> dict:
	if isinstance(data, (bytes, bytearray)):
		data = data.decode("utf-8-sig")
	return json.loads(
		data,
		parse_int=JsonNumber,
		parse_float=JsonNumber,
		parse_constant=invalid_constant,
		object_pairs_hook=get_unique_object,
	)


def copy_json(value, memo=None):
	kind = type(value)

	if value is None or kind in (str, bool, int, float, JsonNumber):
		return value

	if memo is None:
		memo = {}

	if kind not in (dict, list):
		return copy.deepcopy(value, memo)

	identity = id(value)
	if identity in memo:
		return memo[identity]

	result = {} if kind is dict else []
	memo[identity] = result

	if kind is dict:
		for key, child in value.items():
			result[key if type(key) in (str, JsonNumber) else copy_json(key, memo)] = (
				copy_json(child, memo)
			)
	else:
		result.extend(copy_json(child, memo) for child in value)
	return result


@lru_cache(maxsize=8192)
def _number_components(token: str) -> tuple[bool, str, int]:
	sign = token.startswith("-")
	mantissa, _, power = (token[1:] if sign else token).lower().partition("e")
	whole, _, fraction = mantissa.partition(".")
	digits = (whole + fraction).lstrip("0")

	if not digits:
		return sign, "0", 0

	coefficient = digits.rstrip("0")

	return (
		sign,
		coefficient,
		int(power or "0") - len(fraction) + len(digits) - len(coefficient),
	)


@lru_cache(maxsize=8192)
def get_shortest_num_repr(token) -> str:
	sign, coefficient, exponent = _number_components(token)
	prefix = "-" if sign else ""
	if coefficient == "0":
		return prefix + "0"

	n = len(coefficient)
	best_length, best_point = len(token), None
	lower = min(n, n + 2 + len(prefix) - best_length)
	upper = max(n, best_length - len(prefix))

	for point_position in range(lower, upper + 1):
		mantissa_length = (
			n
			if point_position == n
			else (
				n + 1
				if 0 < point_position < n
				else point_position if point_position > n else 2 - point_position + n
			)
		)
		power = exponent + n - point_position
		length = len(prefix) + mantissa_length + (1 + len(str(power)) if power else 0)
		if length < best_length:
			best_length, best_point = length, point_position

	if best_point is None:
		return token

	point_position = best_point
	if point_position <= 0:
		mantissa = "0." + "0" * -point_position + coefficient
	elif point_position < n:
		mantissa = coefficient[:point_position] + "." + coefficient[point_position:]
	else:
		mantissa = coefficient + "0" * (point_position - n)

	power = exponent + n - point_position
	return prefix + mantissa + (f"e{power}" if power else "")


@lru_cache(maxsize=65536)
def quote(value) -> str:
	return json.dumps(value, ensure_ascii=False)


def encode_parts(value: Any, short_numbers: bool) -> list[str] | NoReturn:
	parts = []

	def emit(item: Any):
		match item:
			case JsonNumber():
				parts.append(get_shortest_num_repr(item) if short_numbers else item)
			case str():
				parts.append(quote(item))
			case None:
				parts.append("null")
			case bool():
				parts.append(str(item).lower())
			case dict():
				parts.append("{")
				for index, (key, child) in enumerate(item.items()):
					if index:
						parts.append(",")
					parts.append(quote(key))
					parts.append(":")
					emit(child)
				parts.append("}")
			case list():
				parts.append("[")
				for index, child in enumerate(item):
					if index:
						parts.append(",")
					emit(child)
				parts.append("]")
			case _:
				raise TypeError(f"unsupported exact JSON value: {type(item).__name__}")

	emit(value)
	return parts


def dumps_exact(project, short_numbers: bool = False) -> str:
	return "".join(encode_parts(project, short_numbers)).encode(
		"utf-8", "backslashreplace"
	)


def dumps_exact_layout(project, order, short_numbers: bool = False) -> bytes:
	parts = []
	order_tuple = tuple(order or ())

	def emit(value: Any, is_block: bool = False):
		match value:
			case JsonNumber():
				parts.append(get_shortest_num_repr(value) if short_numbers else value)
			case str():
				parts.append(quote(value))
			case None:
				parts.append("null")
			case bool():
				parts.append(str(item).lower())
			case dict():
				parts.append("{")
				if is_block and order_tuple:
					keys = dict.fromkeys((*order_tuple, *value))
					items = ((key, value[key]) for key in keys if key in value)
				else:
					items = value.items()
				for index, (key, item) in enumerate(items):
					if index:
						parts.append(",")
					parts.append(quote(key))
					parts.append(":")
					emit(item, False)
				parts.append("}")
			case list():
				parts.append("[")
				for index, item in enumerate(value):
					if index:
						parts.append(",")
					emit(item, False)
				parts.append("]")
			case _:
				raise TypeError(f"unsupported exact JSON value: {type(value).__name__}")

	emit(
		{
			key: (
				[
					(
						{**target, "blocks": target["blocks"]}
						if isinstance(target, dict) and "blocks" in target
						else target
					)
					for target in value
				]
				if key == "targets" and isinstance(value, list)
				else value
			)
			for key, value in project.items()
		}
	)
	return "".join(parts).encode("utf-8", "backslashreplace")


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


def is_record_order_irrelevant(path: list) -> bool:
	return len(path) == 4 and path[0] == "targets" and path[2] == "blocks"


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
