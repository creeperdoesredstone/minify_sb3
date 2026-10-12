from .json_layout import JsonNumber, _number_components


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
