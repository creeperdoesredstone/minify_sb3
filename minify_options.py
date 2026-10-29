import sys

from math import isfinite


DEFAULT_LIST_BYTES = 4096
DEFAULT_LIST_ITEMS = 1000
DEFAULT_EPSILON = 1e-8


class AnsiPrint:
	RESET = "\033[0m"
	BOLD = "\033[1m"
	DIM = "\033[2m"
	RED = "\033[31m"
	GREEN = "\033[32m"
	YELLOW = "\033[33m"
	PURPLE = "\033[35m"
	CYAN = "\033[36m"

	def __init__(self, enabled=None):
		self.enabled = sys.stdout.isatty() if enabled is None else enabled

	def paint(self, text, *codes):
		if not self.enabled:
			return text
		return f"{''.join(codes)}{text}{self.RESET}"

	def heading(self, text):
		return self.paint(text, self.BOLD, self.CYAN)

	def subheading(self, text):
		return self.paint(text, self.PURPLE)

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
		rename_identifiers=False,
		rename_variable_names=False,
		rename_list_names=False,
		rename_broadcast_names=False,
		rename_argument_names=False,
		rename_procedure_names=False,
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
		factor_math_expressions=False,
		simplify_boolean_control=False,
		simplify_blocks=False,
		deduplicate_assets=False,
		compact_procedure_prototypes=False,
		clear_procedure_definition_shadows=False,
		optimize_procedure_arguments=False,
		merge_duplicate_procedures=False,
		inline_single_use_procedures=False,
		procedure_inline_passes=8,
		specialize_procedures=False,
		procedure_specialization_passes=4,
		procedure_specialization_min_calls=2,
		branch_swapping=False,
		branch_factoring=False,
		subgraph_factoring=False,
		trivial_loops=False,
		nested_conditionals=False,
		associative_constant_merging=False,
		constant_propagation=False,
		strip_reference_names=False,
		compact_data_literals=False,
		remove_unused_extensions=False,
		group_similar_sequences=False,
		sequence_threshold=3,
		lossless=False,
		all_lossless=False,
		optimize_json=False,
		optimize_assets=False,
		compact_block_defaults=False,
		zopfli=False,
		zopfli_assets=False,
		zopfli_iterations=5,
		compact_costume_references=False,
		compact_block_flags=False,
		minimum_json=False,
		json_search_rounds=0,
		relabel_block_ids=False,
		auto_zopfli=False,
		prune_orphan_arguments=False,
		compact_procedure_symbols=False,
		compact_reporter_defaults=False,
		fast_json=False,
		strip_covered_shadows=False,
		strip_editor_comments=False,
		strip_script_positions=False,
		prune_unused_data=False,
		rebuild_procedure_displays=False,
		compact_terminal_links=False,
		unbounded_json_ordering=False,
		unbounded_procedure_specialization=False,
		unbounded_sequence_length=False,
		unbounded_sequence_params=False,
		unbounded_layout=False,
	):
		self.unbounded_json_ordering = unbounded_json_ordering
		self.unbounded_procedure_specialization = unbounded_procedure_specialization
		self.unbounded_sequence_length = unbounded_sequence_length
		self.unbounded_sequence_params = unbounded_sequence_params
		self.unbounded_layout = unbounded_layout
		self.unbounded_json_search = (
			self.unbounded_json_ordering or self.unbounded_layout
		)
		self.fast_json = fast_json and not self.unbounded_json_search
		self.lossless = lossless or all_lossless

		self.rebuild_procedure_displays = rebuild_procedure_displays or all_lossless
		self.compact_terminal_links = compact_terminal_links or all_lossless
		self.strip_covered_shadows = strip_covered_shadows or all_lossless
		self.strip_editor_comments = strip_editor_comments or all_lossless
		self.prune_unused_data = prune_unused_data or all_lossless
		self.strip_script_positions = strip_script_positions or all_lossless
		self.prune_orphan_arguments = prune_orphan_arguments or all_lossless
		self.compact_procedure_symbols = compact_procedure_symbols or all_lossless
		self.compact_reporter_defaults = compact_reporter_defaults or all_lossless
		self.all_lossless = all_lossless

		self.optimize_json = (
			optimize_json
			or self.lossless
			or self.unbounded_json_search
			or zopfli
			or auto_zopfli
			or compact_block_defaults
			or compact_costume_references
			or compact_block_flags
			or minimum_json
			or bool(json_search_rounds)
			or relabel_block_ids
		)
		self.optimize_assets = (
			optimize_assets
			or compress_assets
			or all_lossless
			or zopfli_assets
			or auto_zopfli
		)
		self.compact_block_defaults = compact_block_defaults or all_lossless
		self.zopfli = zopfli or ((all_lossless or auto_zopfli) and not self.fast_json)
		self.zopfli_assets = zopfli_assets or all_lossless
		self.auto_zopfli = auto_zopfli
		self.relabel_block_ids = relabel_block_ids or all_lossless
		self.zopfli_required = zopfli or zopfli_assets
		self.zopfli_iterations = int(zopfli_iterations)
		self.compact_costume_references = compact_costume_references or all_lossless
		self.compact_block_flags = compact_block_flags or all_lossless
		self.minimum_json = minimum_json or all_lossless or self.fast_json
		self.json_search_rounds = int(json_search_rounds) or int(all_lossless)

		if self.json_search_rounds < 0:
			raise ValueError("json_search_rounds must be nonnegative")
		
		if self.zopfli_iterations < 1:
			raise ValueError("zopfli_iterations must be positive")
		
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
		self.rename_identifiers = rename_identifiers
		self.rename_variable_names = rename_variable_names
		self.rename_list_names = rename_list_names
		self.rename_broadcast_names = rename_broadcast_names
		self.rename_argument_names = rename_argument_names
		self.rename_procedure_names = rename_procedure_names

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
		self.factor_math_expressions = factor_math_expressions

		self.simplify_boolean_control = simplify_boolean_control
		self.simplify_blocks = simplify_blocks
		self.deduplicate_assets = deduplicate_assets
		self.compact_procedure_prototypes = compact_procedure_prototypes
		self.clear_procedure_definition_shadows = clear_procedure_definition_shadows
		self.optimize_procedure_arguments = optimize_procedure_arguments
		self.merge_duplicate_procedures = merge_duplicate_procedures
		self.inline_single_use_procedures = inline_single_use_procedures
		self.procedure_inline_passes = int(procedure_inline_passes)
		
		if self.procedure_inline_passes < 1:
			raise ValueError("procedure_inline_passes must be positive")
		
		self.specialize_procedures = specialize_procedures
		self.procedure_specialization_passes = int(procedure_specialization_passes)
		self.procedure_specialization_min_calls = int(
			procedure_specialization_min_calls
		)
		
		if self.procedure_specialization_passes < 1:
			raise ValueError("procedure_specialization_passes must be positive")
		
		minimum_specialization_calls = 2 - int(self.unbounded_procedure_specialization)
		if self.procedure_specialization_min_calls < minimum_specialization_calls:
			raise ValueError(
				f"procedure_specialization_min_calls must be at least {minimum_specialization_calls}"
			)
		
		self.branch_swapping = branch_swapping
		self.branch_factoring = branch_factoring
		self.subgraph_factoring = subgraph_factoring
		self.trivial_loops = trivial_loops
		self.nested_conditionals = nested_conditionals
		self.associative_constant_merging = associative_constant_merging
		self.constant_propagation = constant_propagation
		self.strip_reference_names = strip_reference_names or all_lossless
		self.compact_data_literals = compact_data_literals
		self.remove_unused_extensions = remove_unused_extensions
		self.group_similar_sequences = group_similar_sequences
		self.sequence_threshold = max(1, int(sequence_threshold))

		self.simplified_block_removed_blocks = []
		self.simplified_block_link_edits = {}
		self.simplified_block_input_edits = {}
		self.simplified_block_opcode_edits = {}
		self.renamed_block_ids = {}
		self.renamed_variable_ids = {}
		self.renamed_list_ids = {}
		self.renamed_broadcast_ids = {}
		self.renamed_argument_ids = {}
		self.renamed_identifiers = {}

		self.wav_conversions = {}
		self.asset_deduplications = {}

		self.repaired_broadcast_ids = set()
		self.broadcast_repair_conflicts = {}
		self.list_bytes, self.list_items = list_bytes, list_items
		self.cleared_lists = frozenset()

		self.normalize_epsilon = normalize_epsilon
		self.keep_sound_metadata = keep_sound_metadata

		self.folded_constant_variables = {}
		self.folded_constant_variable_literals = {}
		self.folded_constant_variable_setters = {}
		self.removed_variable_setter_blocks = []
		self.variable_setter_link_edits = {}
		self.variable_setter_input_edits = {}

		self.folded_constant_expression_link_edits = {}
		self.folded_constant_expression_inputs = {}
		self.folded_constant_expression_opcode_edits = {}
		self.folded_constant_expression_blocks = []
		self.folded_constant_expression_new_blocks = []
		self.constant_control_removed_blocks = []

		self.procedure_prototype_compaction_removed_blocks = []
		self.procedure_prototype_compaction_input_edits = {}
		self.procedure_prototype_compaction_shadow_edits = {}
		self.procedure_argument_mutation_edits = {}
		self.procedure_argument_removed_inputs = {}
		self.procedure_argument_input_edits = {}
		self.procedure_argument_removed_blocks = []
		self.merged_procedure_removed_blocks = []
		self.inlined_procedure_removed_blocks = []
		self.inlined_procedure_link_edits = {}
		self.inlined_procedure_input_edits = {}
		self.specialized_procedure_new_blocks = []
		self.specialized_procedure_call_proccodes = {}
		self.specialized_procedure_prototype_proccodes = {}
		self.specialized_procedure_prototype_code_pairs = {}
		self.specialized_procedure_prototype_blocks = {}
		self.specialized_procedure_block_origins = {}

		self.script_rewrite_removed_blocks = []
		self.script_rewrite_new_blocks = []
		self.script_rewrite_link_edits = {}
		self.script_rewrite_input_edits = {}
		self.script_rewrite_opcode_edits = {}
		self.script_rewrite_removed_inputs = {}
		self.removed_extensions = set()

		self.unreachable_removed_blocks_first = []
		self.unreachable_removed_blocks_second = []
		self.unused_procedure_removed_blocks = []

		self.grouped_sequence_removed_blocks = []
		self.grouped_sequence_new_blocks = []
		self.grouped_sequence_link_edits = {}
		self.grouped_sequence_input_edits = {}
		self.grouped_sequence_rejected_size = 0


def build_options(raw_flags: list[str], args: list[str]) -> Options:
	from minify_flags import groups, toggles, valued
	
	# expand named groups recursively while preserving first-seen order
	values, bad = {}, []
	expanded_flags = []
	seen_flags = set()
	
	def expand_group(flag, stack=()):
		if flag in stack:
			raise ValueError(f"cyclic flag group: {' -> '.join((*stack, flag))}")
		for member in groups.get(flag, (flag,)):
			if member in groups:
				expand_group(member, (*stack, flag))
			elif member not in seen_flags:
				seen_flags.add(member)
				expanded_flags.append(member)
	
	try:
		for f in raw_flags:
			(
				expand_group(f)
				if f.split("=", 1)[0] in groups and "=" not in f
				else expanded_flags.append(f)
			)
	except ValueError as error:
		print(Ansi.error(f"Invalid flag group: {error}"))
		sys.exit(1)
	
	flags = expanded_flags
	
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
				if isfinite(n) and n > 0:
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
				f"Valid: {sorted(toggles)} and --list-bytes=N --list-items=N --sequence-threshold=N --zopfli-iterations=N --json-search-rounds=N (positive), --compression-level=N (0-9), --normalize-epsilon=N (positive)"
			)
		)
		sys.exit(1)
	
	if not args:
		print(Ansi.error("Path and flags are not specified."))
		print(
			Ansi.error("Usage: ")
			+ Ansi.warning("python minify_sb3.py path/to/project.sb3 --flags")
		)
		sys.exit(1)
	
	if "--strip-covered-shadows" in flags and "--keep-covered" in flags:
		print(Ansi.error("--strip-covered-shadows is incompatible with --keep-covered"))
		sys.exit(1)
	
	for strip, keep in (
		("--strip-editor-comments", "--keep-comments"),
		("--strip-script-positions", "--keep-positions"),
		("--prune-unused-data", "--keep-unused-data"),
		("--rebuild-procedure-displays", "--keep-procedure-displays"),
		("--compact-terminal-links", "--keep-terminal-links"),
	):
		if strip in flags and keep in flags:
			print(Ansi.error(f"{strip} is incompatible with {keep}"))
			sys.exit(1)
	
	if "--fast-json" in flags and "--thorough-json" in flags:
		print(Ansi.error("--fast-json is incompatible with --thorough-json"))
		sys.exit(1)

	if "--unbounded-json-ordering" in flags or "--unbounded-layout" in flags:
		print(Ansi.warning("These flag(s) only improve archive compression and can heavily increase runtime:"))

		for flg in ("--unbounded-json-ordering", "--unbounded-layout"):
			if flg in flags: print(Ansi.muted(f"  - {flg}"))

		confirmation: str = input(Ansi.prompt("Are you sure you want to proceed with these flags? [y/n] > ")).lower().strip()
		
		if confirmation != "y":
			for flg in ("--unbounded-json-ordering", "--unbounded-layout"):
				if flg in flags: flags.remove(flg)
	
	all_flags = "--all-flags" in flags
	all_lossless = any(
		flag in flags for flag in ("--all-lossless", "--all-safe", "--all-safe-flags")
	)
	lossless = "--lossless" in flags or all_lossless
	opts = Options(
		lossless=lossless,
		rebuild_procedure_displays=(
			(all_flags or all_lossless) and "--keep-procedure-displays" not in flags
		)
		or "--rebuild-procedure-displays" in flags,
		compact_terminal_links=(
			(all_flags or all_lossless) and "--keep-terminal-links" not in flags
		)
		or "--compact-terminal-links" in flags,
		fast_json="--fast-json" in flags
		or (all_flags and "--thorough-json" not in flags),
		prune_orphan_arguments=all_flags
		or all_lossless
		or "--prune-orphan-arguments" in flags,
		compact_procedure_symbols=all_flags
		or all_lossless
		or "--compact-procedure-symbols" in flags,
		compact_reporter_defaults=all_flags
		or all_lossless
		or "--compact-reporter-defaults" in flags,
		strip_covered_shadows=(
			(all_flags or all_lossless) and "--keep-covered" not in flags
		)
		or "--strip-covered-shadows" in flags,
		strip_editor_comments=(
			(all_flags or all_lossless) and "--keep-comments" not in flags
		)
		or "--strip-editor-comments" in flags,
		prune_unused_data=(
			(all_flags or all_lossless) and "--keep-unused-data" not in flags
		)
		or "--prune-unused-data" in flags,
		strip_script_positions=(
			(all_flags or all_lossless) and "--keep-positions" not in flags
		)
		or "--strip-script-positions" in flags,
		all_lossless=all_lossless,
		compact_costume_references=all_lossless
		or "--compact-costume-references" in flags,
		compact_block_flags=all_flags
		or all_lossless
		or "--compact-block-flags" in flags,
		minimum_json=all_flags
		or all_lossless
		or "--minimum-json" in flags
		or "--thorough-json" in flags,
		json_search_rounds=values.get("--json-search-rounds", 0),
		relabel_block_ids=all_flags or all_lossless or "--relabel-block-ids" in flags,
		auto_zopfli=all_flags,
		optimize_json="--optimize-json" in flags,
		optimize_assets="--optimize-assets" in flags or all_lossless,
		compact_block_defaults="--compact-block-defaults" in flags,
		zopfli="--zopfli" in flags or (all_lossless and "--fast-json" not in flags),
		zopfli_assets="--zopfli-assets" in flags or all_lossless,
		zopfli_iterations=values.get("--zopfli-iterations", 5),
		comments=not lossless and "--keep-comments" not in flags,
		positions=not lossless and "--keep-positions" not in flags,
		covered=not lossless and "--keep-covered" not in flags,
		monitors=not lossless and "--keep-monitors" not in flags,
		lists=not lossless and "--clear-large-lists" in flags,
		rename_block_ids="--rename-block-ids" in flags
		or "--frequency-block-ids" in flags
		or "--order-block-ids-by-frequency" in flags,
		rename_variable_ids="--rename-variable-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_list_ids="--rename-list-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_broadcast_ids="--rename-broadcast-ids" in flags
		or "--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		rename_identifiers="--rename-identifiers" in flags,
		rename_variable_names="--rename-variable-names" in flags,
		rename_list_names="--rename-list-names" in flags,
		rename_broadcast_names="--rename-broadcast-names" in flags,
		rename_argument_names="--rename-argument-names" in flags,
		rename_procedure_names="--rename-procedure-names" in flags,
		rename_argument_ids="--rename-argument-ids" in flags,
		remove_unused_variables="--remove-unused-variables" in flags,
		remove_unused_lists="--remove-unused-lists" in flags,
		remove_unused_broadcasts="--remove-unused-broadcasts" in flags,
		remove_unreachable="--remove-unreachable" in flags,
		remove_unused_procedures="--remove-unused-procedures" in flags,
		normalize_numbers="--normalize-numbers" in flags,
		remove_empty_fields="--remove-empty-fields" in flags,
		remove_empty_inputs="--remove-empty-inputs" in flags,
		remove_costume_metadata="--remove-costume-metadata" in flags,
		remove_default_target_properties="--remove-default-target-properties" in flags,
		remove_empty_target_containers="--remove-empty-containers" in flags
		or "--remove-empty-target-containers" in flags,
		remove_project_meta="--remove-project-meta" in flags,
		preserve_asset_compression=lossless or "--preserve-asset-compression" in flags,
		frequency_block_ids="--frequency-block-ids" in flags
		or "--order-block-ids-by-frequency" in flags,
		frequency_data_ids="--frequency-data-ids" in flags
		or "--order-data-ids-by-frequency" in flags,
		compact_numeric_inputs="--compact-numeric-inputs" in flags,
		compact_field_ids="--compact-field-ids" in flags,
		compact_mutation_hasnext="--compact-mutation-hasnext" in flags,
		compact_mutation_metadata="--compact-mutation-metadata" in flags,
		fold_constant_variables="--fold-constant-variables" in flags,
		fold_constant_expressions="--fold-constant-expressions" in flags,
		factor_math_expressions="--factor-math-expressions" in flags
		or "--fms" in flags,
		simplify_boolean_control="--simplify-boolean-control" in flags,
		simplify_blocks="--simplify-blocks" in flags,
		deduplicate_assets="--deduplicate-assets" in flags,
		optimize_procedure_arguments=any(
			f in flags
			for f in (
				"--optimize-procedure-arguments",
				"--opa",
			)
		),
		merge_duplicate_procedures=any(
			f in flags
			for f in (
				"--merge-duplicate-procedures",
				"--mdp",
			)
		),
		branch_swapping=any(f in flags for f in ("--branch-swapping", "--bs")),
		subgraph_factoring="--subgraph-factoring" in flags,
		trivial_loops=any(f in flags for f in ("--trivial-loops", "--tl")),
		nested_conditionals=any(f in flags for f in ("--nested-conditionals", "--nc")),
		associative_constant_merging=any(
			f in flags for f in ("--associative-constants", "--ac")
		),
		constant_propagation=any(
			f in flags for f in ("--constant-propagation", "--cp")
		),
		strip_reference_names=all_flags
		or all_lossless
		or any(f in flags for f in ("--strip-reference-names", "--srn")),
		compact_data_literals=any(
			f in flags for f in ("--compact-data-literals", "--cdl")
		),
		remove_unused_extensions=any(
			f in flags for f in ("--remove-unused-extensions", "--rue")
		),
		group_similar_sequences="--group-similar-sequences" in flags,
		sequence_threshold=values.get("--sequence-threshold", 3),
		compress_assets="--compress-assets" in flags,
		convert_wav_to_mp3="--convert-wav-to-mp3" in flags,
		sort_keys="--sort-keys" in flags,
		compression_level=values.get("--compression-level", 9),
		list_bytes=values.get("--list-bytes", DEFAULT_LIST_BYTES),
		list_items=values.get("--list-items", DEFAULT_LIST_ITEMS),
		normalize_epsilon=values.get("--normalize-epsilon", DEFAULT_EPSILON),
		keep_sound_metadata=lossless or "--keep-sound-metadata" in flags,
		compact_procedure_prototypes=any(
			f in flags
			for f in (
				"--procedure-prototype-compaction",
				"--ppc",
			)
		),
		clear_procedure_definition_shadows=any(
			f in flags
			for f in (
				"--clear-procedure-definition-shadows",
				"--cpds",
			)
		),
		inline_single_use_procedures=any(
			f in flags
			for f in (
				"--inline-single-use-procedures",
				"--isup",
			)
		),
		procedure_inline_passes=values.get("--procedure-inline-passes", 8),
		specialize_procedures=any(
			f in flags for f in ("--specialize-procedures", "--sp")
		),
		procedure_specialization_passes=values.get(
			"--procedure-specialization-passes", 4
		),
		procedure_specialization_min_calls=values.get(
			"--procedure-specialization-min-calls", 2
		),
		branch_factoring="--branch-factoring" in flags,
		unbounded_json_ordering="--unbounded-json-ordering" in flags,
		unbounded_procedure_specialization="--unbounded-procedure-specialization" in flags,
		unbounded_sequence_length="--unbounded-sequence-length" in flags,
		unbounded_sequence_params="--unbounded-sequence-params" in flags,
		unbounded_layout="--unbounded-layout" in flags,
	)

	return opts


Ansi = AnsiPrint()