from .metadata import get_ref_scopes, get_ref_name_slots


def strip_reference_names(project, stats) -> int:
	scopes = get_ref_scopes(project)
	count = 0
	for path, container, index, ident, kind in get_ref_name_slots(project):
		name = container[index]
		if (
			type(name) is str
			and name
			and type(ident) is str
			and ident
			and ident not in ("__proto__", "constructor", "prototype")
			and scopes[path[1]].get(ident) == (kind, name)
		):
			container[index] = ""
			count += 1
	stats["reference_names_stripped"] += count
	return count
