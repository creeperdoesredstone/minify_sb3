**Usage:**
```
	python minify_sb3.py input.sb3 [output.sb3]
		[--keep-comments] [--keep-positions] [--keep-covered] [--keep-monitors]
		[--keep-lists] [--rename-block-ids] [--rename-variable-ids] [--rename-list-ids]
		[--rename-broadcast-ids] [--rename-argument-ids]
		[--remove-unused-variables] [--remove-unused-lists]
		[--remove-unreachable] [--remove-unused-procedures]
		[--normalize-numbers] [--sort-keys] [--compression-level=N]
		[--list-bytes=N] [--list-items=N]
```

**Transforms:**
* `topLevel`: drop "topLevel": false as VM only ever tests `if (block.topLevel)`
* `shadow`: drop "shadow": false as VM only ever tests `block.shadow` truthiness
* `warp`: "true"/"false" -> true/false sequencer.js and irgen.js accept both forms (only applied to mutation.warp)
* `compact`: no whitespace and escape characters
* `ids`: Enable --rename-block-ids to give blocks sequential shortest IDs from the Scratch VM character pool, updating every block reference. may increase file size.
* `comments`: remove every comment from every non-Stage target (sprites)
			The Stage is left completely untouched as it has TurboWarp's
			"_twconfig_" settings comment, which is read when the project is loaded.
			Disable with --keep-comments.
* `rounding`: round block / primitive / comment x,y,(width,height) to whole numbers.
			These are editor layout only. Applies to the Stage's comments too.
			Disable with --keep-positions.
* `covered`: input slots hidden under a reporter, e.g. [3, "blockId", [4, "10"]],
			keep their obscured shadow (the editor needs a well-formed one to
			rebuild the workspace) but reset its value: numeric slots to 0, text
			slots to "". Only fires when a reporter covers the slot, so the value
			is never read at runtime (compiler follows input.block only). It is
			revealed only if someone drags the covering reporter out.
			Color ([9,..]) and broadcast ([11,..]) shadows are never touched.
			Disable with --keep-covered.
* `monitors`
  * drop the redundant "params" of list monitors (the loader
	  re-derives params.LIST from the monitor id) and normalize "value" to
	  its placeholder
  * delete monitors that are hidden and have no block that could show them
	  and were never moved/resized, or pointed at a variable/list that no longer exists.
	  Hidden monitors are re-created on demand by the VM.
	Disable with --keep-monitors.
* `lists`: detect large lists (>= 4096 bytes of JSON / >= 1000 items by default)
			and prompt which ones to clear.
			Pressing Enter, EOF, or a closed stdin keeps everything.
			For each list, it shows how the project uses it (read by blocks,
			modified at runtime, shown on stage, or unreferenced).
			Clearing only empties the list's contents.
			Skip the whole step with --keep-lists.
			Change detection thresholds for large lists with --list-bytes=N and --list-items=N.

**Additional transforms (optional):**
* variable/list IDs: shorten data IDs and update fields, reporters, monitors, and list primitives.
* broadcast IDs: shorten broadcast IDs while preserving broadcast names.
* argument IDs: shorten custom-block argument IDs, including embedded mutation JSON.
* unused data: remove variables/lists with no block or monitor references.
* unreachable blocks: remove blocks that are not reachable from a top-level/root script.
* unused procedures: remove custom procedure definitions that have no calls.
* number normalization: rewrite integral JSON floats such as 1.0 as 1.
* sorted keys: optionally canonicalize JSON object key order for compression experiments.
* compression level: choose ZIP DEFLATE level 0-9 for the generated archive.

**Removing these would change project behavior or crash the editor:**
* `fields: {} / inputs: {}`: the VM loader tolerates their absence, but sb3fix (and other strict validators) throw, which is not worth it.
* `next / parent : null`: VM uses strict `!== null` (getTopLevelScript, deleteBlock)
* `mutation.tagName/children`: Blocks.mutationToXML() dereferences both when the project is opened in the block editor
* `costume/sound md5ext`: the sound loader has no fallback (md5: soundSource.md5ext), so dropping it from sounds silently breaks audio
* every block, variable, list, comment, costume, sound, monitor
* `numeric strings ("5")`: string vs number semantics differ in comparisons

After writing, the tool reloads both archives, re-inflates the omitted defaults, and checks whether the two projects are identical, and that every other zip entry is byte-for-byte unchanged.

In the case of a mismatch, the output sb3 is deleted.
