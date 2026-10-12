import sys
import shutil
import time


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


Ansi = AnsiPrint()


def repr_size(n: int):
	return f"{n/1048576:.4f} MiB" if n >= 1048576 else f"{n/1024:.4f} KiB"


STAT_GROUPS = [
	(
		"1.",
		"Drop topLevel:false / shadow:false; normalize mutation.warp",
		[
			("topLevel", "topLevel:false dropped"),
			("shadow", "shadow:false dropped"),
			("warp", "mutation.warp normalized to boolean"),
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


def print_transform_stats(stats, opts):
	print(Ansi.heading("Transforms applied:"))
	
	for number, title, entries in STAT_GROUPS:
		print(Ansi.subheading(f"  {number} {title}"))
		
		for key, label in entries:
			print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
			
		if number == "12.":
			if opts.simplify_blocks:
				print(Ansi.subheading("  12a. Simplify set-variable arithmetic"))
				for key, label in (
					("blocks_simplified", "set blocks simplified"),
					("set_to_change_add", "addition forms simplified"),
					("set_to_change_subtract", "subtraction forms simplified"),
					("simplify_blocks_removed", "redundant reporter blocks removed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.group_similar_sequences:
				print(Ansi.subheading("  12b. Group similar sequences (optional)"))
				for key, label in (
					("sequence_groups_created", "similar sequence groups created"),
					("sequences_grouped", "sequence instances grouped"),
					("sequence_procedures_created", "sequence procedures created"),
					("sequence_parameters", "sequence parameters created"),
					("sequence_blocks_removed", "sequence blocks removed"),
					("sequence_blocks_added", "sequence procedure blocks added"),
					("sequence_bytes_saved", "sequence grouping JSON bytes saved"),
					(
						"sequence_groups_rejected_size",
						"candidate groups rejected for size",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.simplify_boolean_control:
				print(Ansi.subheading("  12c. Simplify constant boolean control flow"))
				for key, label in (
					(
						"boolean_control_simplifications",
						"constant control blocks simplified",
					),
					("control_blocks_removed", "control blocks removed"),
					(
						"control_branch_blocks_removed",
						"branch/condition blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.branch_factoring:
				print(Ansi.subheading("  12d. Factor common branch sequences"))
				for key, label in (
					("branch_prefixes_factored", "common prefixes factored"),
					("branch_suffixes_factored", "common suffixes factored"),
					("branch_controls_factored", "identical conditionals eliminated"),
					("branch_factor_blocks_removed", "duplicate branch blocks removed"),
					("branch_factor_bytes_saved", "branch factoring JSON bytes saved"),
					("branch_factor_rejected_size", "candidates rejected for size"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.specialize_procedures:
				print(Ansi.subheading("  12d. Specialize custom procedures"))
				for key, label in (
					("procedure_specialized", "procedure variants specialized"),
					(
						"procedure_specialization_calls",
						"calls rewritten to specialized variants",
					),
					(
						"procedure_specialization_blocks_added",
						"specialized blocks added",
					),
					(
						"procedure_specialization_blocks_removed",
						"generic procedure blocks replaced",
					),
					(
						"procedure_specialization_argument_reporters_removed",
						"constant argument reporters removed",
					),
					(
						"procedure_specialization_passes",
						"specialization passes completed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.clear_procedure_definition_shadows:
				print(Ansi.subheading("  12e. Clear procedure definition shadows"))
				print(
					Ansi.muted(
						f"    {'procedure definition shadows cleared':40} {stats['procedure_definition_shadows_cleared']:>8,}"
					)
				)
				
			if opts.compact_procedure_prototypes:
				print(Ansi.subheading("  12f. Compact procedure prototypes"))
				for key, label in (
					(
						"procedure_prototypes_compacted",
						"procedure prototypes compacted",
					),
					(
						"procedure_prototype_argument_reporters_removed",
						"prototype argument reporters removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.optimize_procedure_arguments:
				print(Ansi.subheading("  12g. Optimize custom procedure arguments"))
				for key, label in (
					("procedure_arguments_removed", "procedure arguments removed"),
					(
						"procedure_constant_arguments_folded",
						"constant procedure arguments folded",
					),
					(
						"procedure_argument_reporters_removed",
						"argument reporter blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.merge_duplicate_procedures:
				print(Ansi.subheading("  12g. Merge duplicate custom procedures"))
				for key, label in (
					("duplicate_procedures_merged", "duplicate procedures merged"),
					(
						"duplicate_procedure_blocks_removed",
						"duplicate procedure blocks removed",
					),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.inline_single_use_procedures:
				print(Ansi.subheading("  12h. Inline safe single-use procedures"))
				for key, label in (
					("procedure_inlined", "single-use procedures inlined"),
					(
						"procedure_inline_blocks_removed",
						"procedure scaffolding blocks removed",
					),
					(
						"procedure_inline_argument_reporters_removed",
						"argument reporters removed during inlining",
					),
					("procedure_inline_passes", "inlining passes completed"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))
					
			if opts.constant_propagation:
				print(Ansi.subheading("  12i. Propagate script constants"))
				for key, label in (
					("script_constants_propagated", "input expressions rewritten"),
					("script_constant_cfg_changes", "CFG data-flow changes"),
				):
					print(Ansi.muted(f"    {label:40} {stats[key]:>8,}"))


def format_size_report(before_json, after_json, before_archive, after_archive):
	mib = 1024 * 1024
	saving = before_json - after_json
	percent = saving / max(1, before_json) * 100
	sign = "-" if saving >= 0 else "+"
	return (
		f"project.json : {before_json / mib:.4f} MiB -> {after_json / mib:.4f} MiB  ({sign}{abs(saving) / mib:.4f} MiB, {percent:.1f}%)",
		f"archive      : {before_archive / mib:.4f} MiB -> {after_archive / mib:.4f} MiB",
	)
