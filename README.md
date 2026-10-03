# minify_sb3


`minify_sb3.py` minimizes `project.json` and compresses Scratch 3 project archives (`.sb3`). Preservation modes retain exact saved values and asset bytes; separate flags enable internal representation changes and lossy audio conversion. The legacy cleanup pipeline remains available. Every pipeline verifies its output.

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Lossless compression](#lossless-compression)
- [Minimum JSON and opt-in representation changes](#minimum-json-and-opt-in-representation-changes)
- [JSON minimum proofs](#json-minimum-proofs)
- [CLI reference](#command-line-reference)
- [How the pipeline works](#how-the-pipeline-works)
- [Always-on behavior](#always-on-behavior)
- [Default transforms](#default-transforms)
- [Interactive prompts](#interactive-prompts)
- [Optional transforms](#optional-transforms)
- [`--all-optimizations` in detail](#--all-optimizations-in-detail)
- [Archive and asset handling](#archive-and-asset-handling)
- [Invariants the script protects](#invariants-the-script-protects)
- [Verification](#verification)
- [Output report](#output-report)
- [Exit codes](#exit-codes)
- [Which flags can change behavior?](#which-flags-can-change-behavior)
- [Known caveats and limitations](#known-caveats-and-limitations)
- [Programmatic use](#programmatic-use)
- [Example usage](#example-usage)

## Requirements

- **Python 3.10 or newer.** Built-in methods use only the standard library. It relies on `X | None` annotations, which need 3.10+.
- **`zopfli` (optional).** Stronger, slower lossless DEFLATE encoding: `python -m pip install zopfli`. `--all-lossless` uses it when installed and reports when it is unavailable. Explicit `--zopfli` / `--zopfli-assets` flags require it.
- **`ffmpeg` with `libmp3lame` (optional).** Needed for lossy `--convert-wav-to-mp3`, also included in `--all-flags`.
- Enough RAM to hold the whole archive: every asset is read into memory before the output is written.

## Quick start

```bash
# All safe methods, prioritizing raw project.json size
python minify_sb3.py my_project.sb3 --all-safe-flags

# Legacy defaults; large lists are not scanned unless requested
python minify_sb3.py my_project.sb3

# Scan large lists and prompt before clearing any
python minify_sb3.py my_project.sb3 --clear-large-lists

# Explicit output path, enable the batch of non-interactive optimizations
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-optimizations

# Keep comments and monitors, canonicalize key order
python minify_sb3.py my_project.sb3 --keep-comments --keep-monitors --sort-keys
```

If no output path is given, the result is written next to the input as `<input name>_minified.sb3`. The output path must differ from the input path; the script refuses to overwrite its own source. Your original file is never modified.

## Lossless compression

Use `--all-safe-flags` (aliases `--all-safe`, `--all-lossless`) to retain saved values, original block IDs, explicit false block flags, editor data and every uncompressed asset byte. It enables shortest exact JSON tokens, omission of empty block fields/inputs that the loader reconstructs, omission of canonical costume filename references that the loader derives, measured block-record layouts, ZIP asset compression, reuse of smaller original streams, and optional Zopfli on JSON and assets. It prioritizes raw `project.json` bytes, then compressed JSON bytes.

This preset preserves variable/list values and names, broadcasts, procedures, all blocks and IDs, comments, monitors, coordinates, costume centers, sound metadata, project metadata, and collection order. It does not remove dead code, round numbers, reset covered shadows, clear lists, rename identifiers, repair existing graph quirks, or transcode assets. Asset names and their uncompressed bytes are identical to the source. All existing script, block, input, variable, list, sprite, costume, and monitor order is retained.

`--lossless` is the narrower preset: exact JSON encoding and original asset streams, without empty-container omission, asset recompression, or automatic Zopfli. Add individual lossless flags as desired.

| Flag | Effect |
| --- | --- |
| `--all-safe-flags`, `--all-safe`, `--all-lossless` | Enable every safe method; minimize raw JSON first and use optional Zopfli when available. |
| `--lossless` | Bypass every legacy transform and preserve the exact saved data. |
| `--optimize-json` | Measure several block-record property layouts and DEFLATE settings; keep the smallest actual compressed result. Collection order is preserved. |
| `--compact-block-defaults` | Omit only empty block `fields`/`inputs`. Implies JSON optimization. `next: null` and parent links are retained. |
| `--compact-costume-references` | Omit costume `md5ext` only when exactly equal to the loader-derived `assetId.dataFormat`. The explicit `dataFormat` remains. |
| `--minimum-json` | Prioritize raw JSON size; use compressed JSON size to choose between equally short representations. Enabled by the safe preset. |
| `--json-search-rounds=N` | Additional contiguous-window layout passes, alternating 128 and 64 blocks. Positive integer; default `1` in presets, otherwise `0`. |
| `--optimize-assets` | Try stored and several DEFLATE encodings of identical asset bytes, retaining smaller original streams. |
| `--zopfli` | Apply stronger DEFLATE encoding to `project.json`. Requires the optional package. |
| `--zopfli-assets` | Apply stronger DEFLATE encoding to assets. Requires the optional package; can take several minutes. |
| `--zopfli-iterations=N` | Set the positive Zopfli search iteration count. Default `5`; higher values cost more time. |

JSON encoding tries exact decimal/scientific number spellings, consistent block-record property layouts, and layouts chosen separately for each target, record shape, opcode, or target/opcode combination. A bounded sample screens all 120 permutations of the five core block properties; the strongest candidates are then measured on the complete project. Additional window passes score layouts against the preceding 32 KiB of DEFLATE history and a following context sample. Several complete layouts are screened with Zopfli before the strongest receives the requested iteration budget. Every candidate is measured after compression. The original layout and compressed entry remain fallbacks under the selected objective. In `--minimum-json` mode, a shorter raw representation takes priority even if its compressed size increases; otherwise compressed size takes priority. Property order inside a block record can change; block table order and input evaluation order cannot.

Numbers are parsed as decimal tokens rather than binary floats. This protects large integers, significant decimal digits, and negative zero. Strings that look like numbers remain strings. Duplicate JSON keys and non-finite constants are rejected rather than silently rewritten.

The lossless verifier independently reloads both archives and checks every JSON value, collection order, ZIP metadata, archive entry order, and asset byte. It permits only the explicitly enabled loader defaults and canonical costume reference to be reconstructed. Opt-in representation flags authorize only their named block changes. Existing parent-link quirks are preserved. Output is written to a temporary archive and replaces the destination only after verification succeeds; a failed run preserves an existing destination.

Scratch's [SB3 deserializer](https://github.com/scratchfoundation/scratch-vm/blob/develop/src/serialization/sb3.js) reconstructs empty fields and inputs. Its serializer also documents why `next: null` must remain explicit.

Preservation modes reject legacy cleanup, renaming, graph optimization, sorting and audio conversion. The separately named representation flags below can be combined explicitly with `--lossless` or the safe preset. Audio conversion is never enabled by the safe preset or `--all-optimizations`; it is enabled by `--all-flags`.

```bash
# Standard-library lossless compression, preserving all original containers
python minify_sb3.py my_project.sb3 --lossless --optimize-assets

# Every lossless method; more CPU time for the optional compressor
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-lossless --zopfli-iterations=15

# Explicitly accept lossy WAV-to-MP3 conversion
python minify_sb3.py my_project.sb3 --convert-wav-to-mp3
```

## Minimum JSON and opt-in representation changes

`--minimum-json` reaches the certified byte floor for the selected fixed-tree model. Exact numeric minimization enumerates all potentially shortest decimal-point placements. The certificate counts required punctuation, keys, scalar strings, number tokens and literals independently. [JSON minimum proofs](#json-minimum-proofs) gives the formal proofs and precise model restrictions. A zero certificate gap establishes that scoped raw-JSON minimum; it does not establish a minimum over all equivalent Scratch programs or DEFLATE encodings.

| Flag | Effect |
| --- | --- |
| `--compact-block-flags` | Omit only explicit false `topLevel` / `shadow`. Loaded property presence changes, so this is excluded from the safe preset. |
| `--relabel-block-ids` | Assign the shortest admissible JSON names to the most frequently referenced blocks. Use names that survive unescaped editor XML attributes; preserve JavaScript enumeration order and unresolved references; do not rename literal strings, data IDs or procedure arguments. Excluded from the safe preset. |
| `--all-flags` | Enable the non-interactive legacy batch, the representation methods above, raw JSON minimization, optional Zopfli and lossy WAV conversion. This preset may change editor data and audio quality. |

For stronger JSON compression while retaining all asset bytes and all other saved data, opt in to only the two representation methods:

```bash
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-safe-flags --compact-block-flags --relabel-block-ids --zopfli-iterations=15
```

The ID assignment has a rearrangement proof for its supported name domain. All names costing one or two JSON payload bytes are enumerated; remaining names cost three bytes. Unsupported larger domains fail instead of receiving a false optimality certificate. These passes combine established techniques; they are not claimed as new mathematical laws.

Validation on `XenonOS Round 3 Submission.sb3` preserved all 1,100 asset files byte for byte. The official Scratch VM load/save and graph comparisons checked 14 targets, 68,814 hydrated blocks and 734 ordered scripts. All 28,046 serialized block IDs passed the actual VM XML serializer and strict XML parsing. The local regression suite passed 44 tests. These were headless checks, without exhaustive interactive execution or guarantees for tools that store old IDs.

| Policy | Raw project.json | JSON inside ZIP | Archive |
| --- | ---: | ---: | ---: |
| Original source | 5,019,694 bytes | 631,748 bytes | 43,652,150 bytes |
| Previous reliable implementation | 4,756,938 bytes | 534,398 bytes | 42,707,165 bytes |
| All safe methods | 4,700,154 bytes | 529,906 bytes | 42,702,673 bytes |
| Explicit block flag and ID changes | 4,214,547 bytes | 519,295 bytes | 42,692,062 bytes |

The original upstream minifier ran first but failed its verifier on an inherited parent-link inconsistency and was disqualified. Every later JSON trial reused the previous reliable baseline's exact asset compression streams. The optimized result saves 542,391 raw JSON bytes (11.40%) against that baseline. A larger layout/Zopfli search tied the winner. Both safe and opt-in results reach the scoped raw byte minima proved below.

## CLI reference

```
python minify_sb3.py input.sb3 [output.sb3] [flags]
```

Flags starting with `--` may appear anywhere. Anything not starting with `--` is a positional argument (the first is the input, the second is the output). Flags that take a value must be written as `--name=value` (no space). Any unrecognized or malformed flag prints the list of valid flags and exits with status 1 before touching any file.

### Opt-out flags (disable a default transform)

| Flag | Effect |
| --- | --- |
| `--keep-comments` | Do not strip sprite comments or block comment links. |
| `--keep-positions` | Do not round block, comment, or costume-rotation-center coordinates. |
| `--keep-covered` | Do not reset covered shadow values. |
| `--keep-monitors` | Do not clean monitors. |
| `--keep-sound-metadata` | Keep `rate` and `sampleCount` on sounds. |

### Interactive list prompt

| Flag | Effect |
| --- | --- |
| `--clear-large-lists` | Scan for large lists and prompt for optional clearing. Off by default and skipped by `--all-optimizations`. |

### Name shortening

| Flag | Effect |
| --- | --- |
| `--rename-identifiers` | Shorten variable, list, broadcast, and custom-block argument names; equivalent to enabling all four category flags below. IDs are unchanged. |
| `--rename-variable-names` | Shorten variable names only. |
| `--rename-list-names` | Shorten list names only. |
| `--rename-broadcast-names` | Shorten broadcast message names only. |
| `--rename-argument-names` | Shorten custom-block argument names only. |

### ID renaming

| Flag | Effect |
| --- | --- |
| `--rename-block-ids` | Shorten block IDs (per sprite). |
| `--rename-variable-ids` | Shorten variable IDs. |
| `--rename-list-ids` | Shorten list IDs. |
| `--rename-broadcast-ids` | Shorten broadcast IDs (names are preserved). |
| `--rename-argument-ids` | Shorten custom-block argument IDs. |
| `--frequency-block-ids` (alias `--order-block-ids-by-frequency`) | Implies `--rename-block-ids`; most-referenced blocks get the shortest IDs. |
| `--frequency-data-ids` (alias `--order-data-ids-by-frequency`) | Implies `--rename-variable-ids`, `--rename-list-ids`, and `--rename-broadcast-ids`; most-referenced data gets the shortest IDs. |

### Dead-data removal

| Flag | Effect |
| --- | --- |
| `--remove-unused-variables` | Remove variables nothing references (cloud variables are always kept). |
| `--remove-unused-lists` | Remove lists nothing references. |
| `--remove-unused-broadcasts` | Remove broadcast definitions nothing references. |
| `--remove-unreachable` | Remove blocks not reachable from a top-level script. |
| `--remove-unused-procedures` | Remove custom-block definitions that are never called. |

### Normalization and compaction

| Flag | Effect |
| --- | --- |
| `--normalize-numbers` | Rewrite integral floats (`1.0`) as integers and snap near-integers within a tolerance. |
| `--normalize-epsilon=N` | Tolerance for `--normalize-numbers`. Positive float, default `1e-8`. |
| `--compact-numeric-inputs` | Convert canonical numeric input strings to JSON numbers. |
| `--compact-field-ids` | Remove explicit `null` ID slots from block fields. |
| `--compact-mutation-hasnext` | Remove `mutation.hasnext` when it is explicitly false. |
| `--compact-mutation-metadata` | Canonicalize the JSON strings in custom-block mutations. Not included in `--all-optimizations`. |
| `--fold-constant-expressions` | Fold supported arithmetic, comparison, boolean, text-length, and math-function reporters whose inputs are constant. Included in `--all-optimizations`. |
| `--fold-constant-variables` | Interactively replace reporters of eligible write-once variables with their initial value. May also remove a matching setter block. Not included in `--all-optimizations`. |

### Structural trimming

| Flag | Effect |
| --- | --- |
| `--remove-empty-fields` | Drop empty `fields` objects from blocks. |
| `--remove-empty-inputs` | Drop empty `inputs` objects from blocks. |
| `--remove-costume-metadata` | Drop provably redundant costume `md5ext` and SVG `bitmapResolution`. |
| `--remove-default-target-properties` | Drop target properties that equal Scratch's defaults. |
| `--remove-empty-containers` (alias `--remove-empty-target-containers`) | Drop empty `lists`, `broadcasts`, and `comments` containers. |
| `--remove-project-meta` | Drop `meta.agent` and `meta.platform`. |
| `--sort-keys` | Write JSON with object keys sorted. Not included in `--all-optimizations`. |

### Archive and asset flags

| Flag | Effect |
| --- | --- |
| `--convert-wav-to-mp3` | Convert WAV sounds to MP3 with `ffmpeg` (lossy). |
| `--preserve-asset-compression` | Reuse an unchanged asset's original DEFLATE bytes when they are no larger than recompressing. |
| `--compression-level=N` | ZIP DEFLATE level, integer `0` to `9`. Default `9`. |
| `--compress-assets` | Accepted for compatibility; currently stored but does not run any transform. |

### Large-list thresholds

| Flag | Effect |
| --- | --- |
| `--list-bytes=N` | A list is "large" if its JSON size is at least `N` bytes. Positive integer, default `4096`. |
| `--list-items=N` | A list is "large" if it has at least `N` items. Positive integer, default `1000`. |

A list qualifies if it is non-empty and meets either threshold.

### Batch flag

| Flag | Effect |
| --- | --- |
| `--all-optimizations` | Enable the legacy batch of non-interactive optional transforms. See [below](#--all-optimizations-in-detail). |
| `--all-flags` | Include that batch, the new representation methods, optional Zopfli, minimum raw JSON and lossy WAV conversion. |
| `--all-safe-flags` | Every safe method; preserve original block IDs, explicit false flags, saved state and asset bytes. |

### Validation rules for valued flags

- `--list-bytes`, `--list-items`, `--zopfli-iterations`, `--json-search-rounds`: digits only, greater than zero.
- `--compression-level`: digits only, `0` through `9`.
- `--normalize-epsilon`: any finite float greater than zero.
- A valued flag given without `=value`, or a toggle given with `=value`, is rejected as malformed.

## How the pipeline works

The steps below describe the legacy pipeline. Preservation modes use the independent exact-JSON pipeline described above.

1. `project.json` is parsed (UTF-8 JSON) and every other archive entry is read into memory as an asset.
2. If `--clear-large-lists` is given, the large-list prompt runs. If `--fold-constant-variables` is given, the constant-variable prompt runs. Both happen before anything is written. Aborting with Ctrl-C at either prompt exits with status 130 and writes nothing. Neither prompt runs by default or with `--all-optimizations` alone.
3. Transforms are applied in this fixed order:
   1. Drop `topLevel: false` and `shadow: false`; normalize `mutation.warp` to booleans.
   2. WAV to MP3 conversion (if enabled).
   3. Strip sprite comments (default).
   4. Round positions (default).
   5. Reset covered shadow values (default).
   6. Clean monitors (default).
   7. Clear any lists chosen at the prompt.
   8. Remove unreachable blocks.
   9. Remove unused procedures.
   10. Remove unreachable blocks again (to sweep anything the procedure pass orphaned).
   11. Fold selected constant variable reporters, then remove safe matching setter blocks.
   12. Fold constant expressions.
   13. Remove unused variables and lists.
   14. Repair dangling broadcast references.
   15. Remove unused broadcasts.
   16. Rename variable and list IDs, then broadcast IDs, then argument IDs, then block IDs.
   17. Compact numeric inputs, field IDs, and mutation `hasnext`/metadata.
   18. Normalize numbers.
   19. Remove sound `rate` and `sampleCount` (unless `--keep-sound-metadata`).
   20. Remove empty fields, empty inputs, costume metadata, default target properties, empty containers, and project meta.
   21. Repair any dangling block links ([always-on](#always-on-behavior)).
4. `project.json` is serialized compactly (`separators=(",", ":")`, `ensure_ascii=False`), then written first into a new ZIP, followed by the assets.
5. Per-transform counters and size totals are printed.
6. The original and output archives are independently reloaded and compared. On any mismatch, the output file is deleted and the script exits with status 2.

The order matters: unreachable-block and procedure removal run before unused-data removal, so variables referenced only by dead code are correctly detected as unused; ID renaming runs after removal so it never renames data that is about to be deleted.

## Always-on behavior

A few things happen regardless of flags (except where noted):

- `topLevel: false`, `shadow: false`, and `mutation.warp` always run. Scratch only tests whether `topLevel` and `shadow` are truthy, so the `false` values carry no information. `"true"`/`"false"` strings in `mutation.warp` become real booleans.
- `rate` and `sampleCount` are removed from every sound unless `--keep-sound-metadata` is given.
- After all other transforms, every sprite is scanned and repaired:
  - `next` or `parent` pointing at a block that does not exist becomes `null`.
  - Inputs that point at a missing block are removed. If a covering block vanished but a shadow value remains, the input falls back to the shadow value alone.
  - A child whose `parent` is `null` (and which is not marked `topLevel`) gets its `parent` restored when exactly one owner (via `next` or exactly one input) unambiguously claims it. Existing valid parents are never overwritten.
  - Comments that point at a missing block are deleted.
- A broadcast ID used by a block but defined nowhere is added to the Stage's `broadcasts` table, named after the reference. If one ID is referenced under several different names, the script refuses to guess; verification then fails and nothing is written.
- Verification always runs.

## Default transforms

### Metadata cleanup

Described under [Always-on behavior](#always-on-behavior). WAV sounds are left unchanged unless you ask for conversion.

### Sprite comments and block comment links

- Every comment on every non-Stage target is removed, and the target's `comments` is set to `{}` (the loader expects an object).
- Each block's `comment` link is deleted so nothing dangles.
- The Stage is deliberately untouched, because TurboWarp stores its `_twconfig_` metadata there and some projects depend on it.

Disable with `--keep-comments`.

### Position rounding

Floats become integers (`int(round(v))`, which uses Python's round-half-to-even) for:

- block `x` and `y`;
- variable/list primitive blocks stored in the block table (indices 3 and 4 of `[12|13, name, id, x, y]`);
- comment `x`, `y`, `width`, and `height`;
- costume `rotationCenterX` and `rotationCenterY`.

`NaN`, infinities, booleans, and values that are already integers are left alone. Sprite `x`/`y` (the sprite's position on stage) are untouched.

Disable with `--keep-positions`.

### Covered shadow values

When a reporter sits in an input, the shadow underneath is still stored so the editor can rebuild the workspace, but its value is never read at runtime. For inputs of the form `[3, <covering block>, [tag, value]]`:

- numeric shadows (tags 4 to 8: number, positive number, whole number, integer, angle) are reset to `0`;
- text shadows (tag 10) are reset to `""`;
- everything else is left alone (colors, tag 9, and shadows that are block references or three-element broadcast/variable primitives).

Disable with `--keep-covered`.

### Monitor cleanup

Only variable monitors (`data_variable`) and list monitors (`data_listcontents`) are touched; all other monitors (sensing, motion, and so on) are always preserved exactly.

- Orphaned hidden monitors are dropped: hidden monitors whose variable or list is not defined on the owning sprite/Stage. An orphaned visible monitor is kept.
- Unused hidden monitors are dropped when all of the following hold: it is hidden, no show/hide block (`data_showvariable`, `data_hidevariable`, `data_showlist`, `data_hidelist`) targets its ID, and it has the default layout (`x`/`y` equal to 5 and `width`/`height` equal to 0, using those defaults when the keys are missing).
- List-monitor `params` are cleared to `{}`; the loader re-derives them from the monitor ID.
- Monitor `value` is normalized to `[]` for lists and `0` for variables where a `value` key exists. This applies to surviving visible monitors too; the displayed value is recomputed at runtime.
- Relative order of surviving monitors is preserved.

Disable with `--keep-monitors`.

### Large-list prompt

See [Interactive prompts](#interactive-prompts). Enable it with `--clear-large-lists`; it is off by default and skipped by `--all-optimizations`.

## Interactive prompts

The large-list prompt runs only with `--clear-large-lists`; the constant-variable prompt runs only with `--fold-constant-variables`. Both read from standard input. If stdin is closed or hits EOF, the safe answer ("keep everything") is assumed. Ctrl-C aborts the entire run with exit status 130 and nothing is written. Pressing Enter keeps everything. Output is colored only when stdout is a terminal.

### Large lists (`--clear-large-lists`)

The scan finds non-empty lists meeting either threshold (`--list-bytes`, `--list-items`) and prints a table sorted largest first:

```
Large lists found: 3  (48,213 bytes = 31.4% of project.json)

   #  scope         list        items   bytes  usage
 !  1  GLOBAL        Highscores  1,204  19,880  modified at runtime (2 add, 1 delete); read by 3 blocks
    2  local:Player  Palette       412   9,102  not referenced by any block
```

- **scope** is `GLOBAL` for Stage lists or `local:<sprite name>`.
- **usage** is derived from the project's blocks and monitors, classified as read (`itemoflist`, `lengthoflist`, `listcontainsitem`, `itemnumoflist`, the list reporter), add (`addtolist`, `insertatlist`), replace, delete (`deleteoflist`, `deletealloflist`), show/hide, or other. Variable-style list reporters in inputs count as reads. A visible monitor counts as "shown on stage". For Stage lists, sprites that define a local list with the same ID are excluded from the scan.
- A `!` marks a list whose clearing could change behavior (anything referenced). A list that only uses `replace item` is annotated as "looks like a fixed-size array", since replacing items in an emptied list does nothing.

Responses:

| Input | Action |
| --- | --- |
| Enter, `n`, `no`, `none`, `keep` | Keep every list. |
| `a`, `all` | Select all listed lists. |
| `u`, `unreferenced`, `safe` | Select only lists nothing references (no `!`). |
| `1,3,5-7` | Select those numbers; commas or spaces separate, `a-b` is an inclusive range. |
| `s N` | Print the first 8 items of list N (each truncated to 70 characters), then ask again. |

After a selection the script reports how many bytes it expects to save. If any selected list is marked `!`, it lists them and requires you to type `yes` to proceed; anything else returns you to the prompt.

Clearing a list only empties its contents (`[]`). The list stays defined, so block references and monitors keep resolving.

### Constant variables (`--fold-constant-variables`)

Candidates are variables that satisfy all of these:

- exactly one `set variable to` block in the whole project, and its value input is a literal (numeric tags 4 to 8 or text tag 10);
- at least one reporter use in an input;
- no `change variable by` block;
- not a cloud variable;
- an initial value that is a finite number or a string (booleans are skipped).

Variable names are resolved with Scratch's local-then-Stage scope rule. Folding replaces each reporter use (`[12, name, id]`) with `[4, value]` for numbers or `[10, value]` for strings, using the variable's **initial value stored in `project.json`**, which can differ from the value the setter assigns at runtime. After folding, the setter block is removed only if it assigns the same value as the initial value and has no attached comment; otherwise it remains. The variable definition remains.

The table shows scope, name, reporter-use count, and an estimated byte change per candidate. Candidates where folding would grow the file are marked `LOSS`; variables with `change variable by` blocks are excluded by the finder.

| Input | Action |
| --- | --- |
| Enter, `n`, `no`, `none`, `keep` | Keep every variable. |
| `p`, `positive`, `safe`, `s` | Fold only candidates that reduce the file. |
| `u`, `unchanged` | Fold only candidates that reduce the file and have no `change variable by`. |
| `a`, `all` | Fold every candidate. |
| `1,3,5-7` | Fold those numbers. |

Selecting any `LOSS` candidate requires typing `yes` to confirm. `--all-optimizations` does not enable this transform.

## Optional transforms

### ID renaming

All renaming uses the same 87-character alphabet (`!@#$%^*()+_-={}|[]:;?,./~`, `A-Z`, `a-z`, and `0-9`). The first 87 IDs are one character, the next 7569 are two characters, and so on. Every rewrite updates all references so the project stays functional.

- **`--rename-block-ids`**: block IDs are unique per sprite, so renaming is per sprite. Rewrites the block table keys plus every `next`, `parent`, input reference (including shadow slots), and comment `blockId`. IDs that are already dangling in the source are reserved so a new ID can never collide with them.
- **`--rename-variable-ids`** / **`--rename-list-ids`**: Stage (global) data is renamed first, then each sprite's local data. All new IDs are unique across the whole project. Rewrites the `variables`/`lists` tables, `VARIABLE`/`LIST` fields, variable and list reporter primitives (including those nested in inputs and those stored in the block table), and monitor IDs, honoring local-over-global resolution.
- **`--rename-broadcast-ids`**: renames broadcast IDs project-wide while preserving each broadcast's name. Rewrites broadcast tables, `BROADCAST_OPTION`/`BROADCAST_INPUT` fields, and broadcast primitives in inputs. New IDs avoid those assigned to variables and lists.
- **`--rename-argument-ids`**: renames custom-block argument IDs inside `procedures_prototype` and `procedures_call` mutations (`argumentids`) and the matching input keys. For each procedure code within a sprite, renaming happens only if every prototype and call agrees on the same `argumentids` list; otherwise that procedure is skipped.
- **`--frequency-block-ids`**: orders block IDs by how often each block is referenced (via `next`, `parent`, inputs, and comments), so the most-referenced blocks get one-character IDs.
- **`--frequency-data-ids`**: the same idea for variables, lists, and broadcasts.

Renamed projects are harder to compare by eye. The verifier reverses every mapping before comparing against the original.

### Identifier name shortening

`--rename-identifiers` enables `--rename-variable-names`, `--rename-list-names`, `--rename-broadcast-names`, and `--rename-argument-names`. Each category flag can also be used independently. Renaming updates variable/list reporter labels, broadcast definitions and references, procedure mutation `argumentnames`, and `argument_reporter` labels together. These flags do not change variable, list, broadcast, block, or argument IDs; use the `--rename-*-ids` flags for IDs.

### Removing dead data

- **`--remove-unused-variables`** / **`--remove-unused-lists`**: remove entries that no block or monitor references. A reference is a `VARIABLE`/`LIST` field, a reporter primitive anywhere in a block, or a monitor entry. A variable is also treated as referenced if a `sensing_of` block selects a property with that variable's name (for reading another sprite's variable). **Cloud variables are never removed.**
- **`--remove-unused-broadcasts`**: removes broadcast definitions that no block references.
- **`--remove-unreachable`**: removes any block that cannot be reached from a root. Roots are blocks marked `topLevel` and variable/list primitives in the block table. Reachability follows `next` and every block or shadow reference in inputs; it does not follow `parent` links. Loose, detached fragments are deleted.
- **`--remove-unused-procedures`**: within each sprite, finds `procedures_definition` blocks and gathers each body by following `parent` links. Calls made from outside any definition are entry points; calls inside definitions keep their callees alive transitively. Definitions (and their bodies) of procedures never reached are deleted. Matching is by `proccode`. A procedure that is only called by itself (or only by other dead procedures) is considered dead.

These can change behavior if a project relies on data or scripts that are only reachable through means the script does not model. Review the result before shipping.

### Normalization and compaction

- **`--normalize-numbers`** walks every target and monitor and rewrites floats:
  - integral floats (`1.0`, `-3.0`) become integers (negative zero `-0.0` is preserved);
  - floats within `--normalize-epsilon` (default `1e-8`) of a non-zero integer snap to that integer (`2.000000001` becomes `2`);
  - values near zero are not snapped to zero; `NaN`/infinity and strings are untouched.

  This applies everywhere numbers appear, including variable values, list items, and sprite properties, so the near-integer snapping is lossy by design.
- **`--compact-numeric-inputs`**: for numeric-tag shadow values stored as strings (tags 4 to 8), converts the string to a JSON number only when the text is the canonical form of that number: `"15"` becomes `15`, `"1.5"` becomes `1.5`. Text such as `"007"`, `"+5"`, `" 5"`, `"1.50"`, `"-0"`, or anything with an exponent is left as is. Plain text values (tag 10) are never converted.
- **`--compact-field-ids`**: field tuples serialized as `[value, null]` become `[value]`. Only an explicit `null` slot is removed; empty-string and non-null IDs are preserved.
- **`--compact-mutation-hasnext`**: removes `mutation.hasnext` when it is `false` or `"false"`.
- **`--compact-mutation-metadata`**: re-serializes the JSON-encoded `argumentids`, `argumentnames`, and `argumentdefaults` strings in mutations without whitespace, with decoded values unchanged. `tagName` and `children` are never removed.
- **`--fold-constant-expressions`**:
  - Folds nested `+`, `-`, `*`, `/`, `%`, `=`, `<`, `>`, `and`, `or`, `not`, `length`, and `mathop` reporters for `abs`, `floor`, `ceiling`, `sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `ln`, `log`, `e ^`, and `10 ^` when their inputs are constant.
  - Numeric strings may be decimal or prefixed with `0b`, `0o`, or `0x`; surrounding whitespace is ignored.
  - Booleans are implicitly converted to `1` or `0` when used numerically.
  - Expressions involving variables, unsupported math operations, division/modulo by zero, or non-finite results are left unchanged.
  - Folded booleans are encoded according to their destination: false becomes the empty input `[2, null]`, true becomes a `not <>` reporter with an empty operand, and booleans in other sockets become text literals `"true"` or `"false"`.
  - The transform avoids folding expressions with attached comments and verifies the exact input rewrites and any inserted reporter blocks.
- **`--remove-empty-fields`** / **`--remove-empty-inputs`**: delete empty `fields`/`inputs` objects. The default keeps these containers.
- **`--remove-costume-metadata`**: removes a costume's `md5ext` only when it equals `<assetId>.<dataFormat>` and that asset exists in the archive; removes `bitmapResolution` only from SVG costumes when it equals `1`. Bitmap costumes are never changed.
- **`--remove-default-target-properties`**: removes properties exactly equal (value and type) to Scratch's defaults.
  - Stage:
    - `currentCostume`: 0
    - `volume`: 100
    - `tempo`: 60
    - `videoTransparency`: 50
    - `videoState`: `"on"`
    - `textToSpeechLanguage`: `null`.
  - Sprites:
    - `currentCostume`: 0
    - `volume`: 100
    - `visible`: true
    - `x`: 0
    - `y`: 0
    - `size`: 100
    - `direction`: 90
    - `draggable`: false
    - `rotationStyle`: `"all around"`.
- **`--remove-empty-containers`**: removes empty `lists`, `broadcasts`, and `comments` objects from targets.
- **`--remove-project-meta`**: removes `meta.agent` and `meta.platform`. `meta.semver` and `meta.vm` are kept.
- **`--sort-keys`**: writes every JSON object with keys in sorted order. This can help compression experiments and makes diffs stable.

## `--all-optimizations` in detail

`--all-optimizations` turns on the following legacy batch:

| Group | Enabled |
| --- | --- |
| Identifier renaming | Shortened variable, list, broadcast, and argument names, plus block, variable, list, broadcast, and argument IDs ordered by frequency |
| Dead data | unused variables, lists, and broadcasts; unreachable blocks; unused procedures |
| Compaction | `--compact-numeric-inputs`, `--compact-field-ids`, `--compact-mutation-hasnext`, `--fold-constant-expressions`, `--normalize-numbers` |
| Trimming | `--remove-empty-fields`, `--remove-empty-inputs`, `--remove-costume-metadata`, `--remove-default-target-properties`, `--remove-empty-containers`, `--remove-project-meta` |
| Archive | `--optimize-json`, `--compact-block-defaults`, `--compact-costume-references`, `--optimize-assets`, `--preserve-asset-compression` (and the no-op `--compress-assets`) |
| Prompts | neither interactive prompt runs; large lists are not scanned or cleared |

It does not enable:

- `--clear-large-lists` (not part of the batch; lists are not scanned or cleared)
- `--fold-constant-variables` (needs your selection)
- `--compact-mutation-metadata`
- `--sort-keys`
- `--keep-sound-metadata` (sound `rate`/`sampleCount` are still removed)

`--keep-*` flags, `--compression-level=N`, and `--normalize-epsilon=N` still apply on top of it. The list thresholds are used only with `--clear-large-lists`. This legacy preset includes aggressive removals and does not preserve all editor data. Use `--all-lossless` for preservation. `--all-flags` additionally enables `--compact-block-flags`, `--relabel-block-ids`, `--minimum-json`, optional Zopfli and `--convert-wav-to-mp3`. It does not include interactive prompts or every mutually incompatible CLI option.

## Archive and asset handling

The details below describe the legacy pipeline. Preservation modes retain entry order and metadata, measure stored/DEFLATE alternatives, and publish atomically after verification.

### Output archive

- Entries are written as `project.json` first, then assets in the order they appeared in the input. Converted MP3s are appended after the original assets.
- All entries use DEFLATE at `--compression-level` (default 9).
- Non-`project.json` assets are not modified, except for the explicit WAV conversion.

### `--preserve-asset-compression`

For each unchanged asset that the original archive stored with DEFLATE, the script compares the original compressed size with what Python's zlib would produce at the chosen level. If the original is no larger, its raw compressed bytes are copied straight into the new archive (with the original CRC, size, and timestamp). Otherwise the asset is recompressed.

### `--convert-wav-to-mp3`

- Requires `ffmpeg`. Each WAV sound is piped through `ffmpeg` with `libmp3lame` at 128 kbit/s, 44.1 kHz, stereo, with a 120-second timeout per file.
- The sound's `assetId`, `dataFormat`, and `md5ext` are updated to the MP3's MD5, so asset IDs always match the data.
- A WAV shared by several sounds is converted once and every sound is repointed; the original WAV is dropped only after all references are updated.
- If `ffmpeg` is missing or an individual conversion fails, the WAV is kept. The report table does not print a message for this, so check the archive size if you expected savings.
- Conversion is lossy and re-encodes to a fixed sample rate and channel layout. Because `rate` and `sampleCount` are removed (unless `--keep-sound-metadata`), no stale values are left behind.

## Invariants the script protects

Some fields cannot be removed without breaking the editor, loader, or VM. The script keeps them, and the verifier enforces them:

- `next` and `parent` stay present on every block (as `null` or a valid block ID).
- `mutation.tagName` and `mutation.children` are retained so the block-to-XML conversion works when the project is opened in the editor.
- Sound and costume `md5ext`/`assetId` values stay consistent with the archive's asset names and actual MD5 hashes.
- Variable, list, comment, costume, sound, monitor, and target entries are not removed wholesale unless a transform explicitly says so.
- Numeric strings are preserved unless you request `--compact-numeric-inputs` or `--fold-constant-expressions`, which operate only on the specific shapes described above.
- Variable definitions and values, broadcast names, and list contents are unchanged unless a transform explicitly changes them. Constant-variable folding can replace reporters and may remove a setter that exactly matches the initial value; constant-expression folding rewrites only the selected constant input trees.
- Stage comments and `_twconfig_` metadata are untouched.

## Verification

The checks below describe the legacy verifier. Preservation modes use the stricter exact-value and byte-identity audit described above, retaining inherited graph quirks.

After the output is written, the script reloads both archives and checks the following. Any failure deletes the output file, prints the reason, and exits with status 2. The original archive is never modified.

**Archive level**
- Both ZIPs pass their CRC test, contain `project.json`, and have no duplicate, absolute, or path-traversing entry names.
- The output's asset set equals the input's, minus converted WAVs, plus their MP3 replacements.
- Unchanged assets are byte-for-byte identical; converted assets' MD5s match their new names.
- Every costume and sound has a valid 32-character hex `assetId`, supported `dataFormat`, a consistent `md5ext`, an existing archive entry whose MD5 matches, and valid numeric `bitmapResolution`/rotation-center/`rate`/`sampleCount` values where present.

**Block graph**
- Every block has an opcode, valid `next`/`parent` types, `inputs` and `fields` objects (after normalizing omitted empties), and well-formed mutations.
- `next` links are reciprocated by the child's `parent`; parents reference their child through `next` or an input; input owners agree with the child's `parent`.
- Top-level blocks have no parent and shadow blocks are not top-level.
- No block, broadcast, or comment reference dangles.

**Original-vs-minified comparison** (after reversing any ID renames)
- Top-level project keys are unchanged (except `meta.agent`/`meta.platform` when `--remove-project-meta` is on).
- Target count and target properties match, apart from removals explicitly enabled by flags.
- Variables, lists, and broadcasts match the original except for approved removals or clears; no unexpected new IDs appear; list contents change only for the lists you selected; variable values are unchanged.
- Blocks match the original except for blocks that the enabled flags say should be gone, with each allowed difference verified (shadow resets, rounding, folded values, compacted fields and mutations, and so on).
- Comments match, or are fully stripped when `--keep-comments` is off.
- Sounds match except for removed `rate`/`sampleCount` and approved WAV to MP3 changes; costumes match except for removed metadata.
- Monitors: nothing new appears, order is preserved, non-variable/list monitors are intact, and every removed monitor was either an orphan or an unused default-layout hidden monitor. No visible monitor is removed.

## Output report

The script prints the input and output paths, then one line per transform with its count, for example:
```
Input : "my_project.sb3"
Output: "my_project_minified.sb3"
Transforms applied:
  topLevel:false dropped                       12,340
  shadow:false dropped                          9,121
  sprite comments removed                          14
  position values rounded                       4,882
  ...
project.json : 812.3000 KiB -> 301.7000 KiB  (-510.6000 KiB, 62.9%)
archive      : 1.8400 MiB -> 1.2700 MiB
```

Counters appear in this order:
- `topLevel`,
- `shadow`,
- `warp`
- comments
- comment links
- rounded positions
- covered values
- monitor changes,
- large lists cleared and items removed
- block IDs
- dangling-reference repairs
- broadcast repairs and conflicts
- variable/list/broadcast/argument ID renames
- removed variables/lists/broadcasts
- removed blocks and procedures
- normalized numbers
- empty fields/inputs
- costume metadata
- default target properties
- empty containers
- project meta
- numeric inputs
- field IDs
- `hasnext`
- mutation JSON
- constant variable reporters and bytes saved
- constant-variable setters removed/kept
- shortened identifier names
- constant expressions folded

Each prints even when it is zero. The sizes shown are for `project.json` and for the whole archive.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success; output written and verified. |
| `1` | Bad usage: malformed or unknown flag, no input given, output path equal to input path, input file not found, or no `project.json` in the archive. |
| `2` | Verification failed; the output file was deleted. |
| `130` | Aborted (Ctrl-C) at an interactive prompt; nothing was written. |

## Which flags can change behavior?

| Risk | Flags |
| --- | --- |
| Cosmetic or label changes | default transforms; `--compact-field-ids`, `--compact-mutation-hasnext`, `--compact-mutation-metadata`, `--remove-empty-fields`, `--remove-empty-inputs`, `--remove-costume-metadata`, `--remove-empty-containers`, `--remove-project-meta`, `--sort-keys`, all ID-renaming flags, `--rename-identifiers` |
| Low risk, depends on project contents | `--compact-numeric-inputs`, `--remove-default-target-properties`, `--fold-constant-expressions`, `--remove-unused-variables`, `--remove-unused-lists`, `--remove-unused-broadcasts` |
| Can change behavior | clearing lists with `--clear-large-lists`, `--remove-unreachable`, `--remove-unused-procedures`, `--normalize-numbers` (near-integer snapping), `--fold-constant-variables`, `--fold-constant-expressions` (constant-evaluation semantics), `--convert-wav-to-mp3` (lossy audio) |

## Known caveats and limitations

These come from reading the code; test the minified project when any of them might apply to you.

- `--compact-numeric-inputs` is textual and does not bound magnitude, so a very long integer string could become a JSON number that loses precision when parsed by a JS-based tool.
- `--remove-unreachable` and `--remove-unused-procedures` only model `topLevel`, `next`, input references, and `parent` links; scripts that exist but never run (for example, hats nothing triggers) are kept, and unusual hand-edited projects may be pruned more than expected.
- `--normalize-numbers` rounds floats in any location, including variable values and list items.
- `--fold-constant-variables` uses the stored initial value, not necessarily the value the setter assigns. A setter is removed only when the script can prove it matches that initial value and has no comment.
- `--fold-constant-expressions` implements only the listed Scratch operators and a conservative subset of their coercion behavior; review projects using unusual literal types or editor extensions.
- Missing `ffmpeg` is silent in the transform table; the WAVs are simply retained.
- `--compress-assets` is parsed but performs no transform.
- Running with no arguments prints the script's module docstring, which is empty, so see this README for usage.
- The full archive is held in memory during processing.
- The Stage's comments and metadata are intentionally skipped by comment stripping.

## Programmatic use

The module exposes an `Options` class and `minify_sb3(src, dst, opts=None)`. The function prints progress to stdout, returns the same status codes as the CLI (`0`, `1`, `2`, or `130`), and writes the output archive on success.

```python
from minify_sb3 import Options, minify_sb3

opts = Options(
    lists=False,  # do not scan/prompt for large lists
    rename_block_ids=True,
    remove_unreachable=True,
    normalize_numbers=True,
    compression_level=9,
)
status = minify_sb3("in.sb3", "out.sb3", opts)
```

Notes:

- `Options` defaults differ from CLI list behavior: `comments`, `positions`, `covered`, `monitors`, and `lists` are `True` on a directly constructed `Options()` object, so programmatic use scans/prompts for large lists unless `lists=False`. The CLI enables this scan only with `--clear-large-lists`; its ordinary default and `--all-optimizations` do not scan lists.
- Other defaults include:
  - `compression_level=9`
  - `list_bytes=4096`
  - `list_items=1000`
  - `normalize_epsilon=1e-8`
- `fold_constant_variables=True` always prompts interactively.
- Available keyword arguments:
  - `comments`
  - `positions`
  - `covered`
  - `monitors`
  - `lists`
  - `rename_identifiers`
  - `rename_variable_names`
  - `rename_list_names`
  - `rename_broadcast_names`
  - `rename_argument_names`
  - `rename_block_ids`
  - `rename_variable_ids`
  - `rename_list_ids`
  - `rename_broadcast_ids`
  - `rename_argument_ids`
  - `remove_unused_variables`
  - `remove_unused_lists`
  - `remove_unused_broadcasts`
  - `remove_unreachable`
  - `remove_unused_procedures`
  - `normalize_numbers`
  - `remove_empty_fields`
  - `remove_empty_inputs`
  - `remove_costume_metadata`
  - `remove_default_target_properties`
  - `remove_empty_containers` / `remove_empty_target_containers`
  - `remove_project_meta`
  - `convert_wav_to_mp3`
  - `compress_assets`
  - `sort_keys`
  - `compression_level`
  - `list_bytes`
  - `list_items`
  - `normalize_epsilon`
  - `keep_sound_metadata`
  - `preserve_asset_compression`
  - `frequency_block_ids`
  - `frequency_data_ids`
  - `compact_numeric_inputs`
  - `compact_field_ids`
  - `compact_mutation_hasnext`
  - `compact_mutation_metadata`
  - `fold_constant_variables`
  - `fold_constant_expressions`
  - `group_similar_sequences`
- Frequency ordering is selected by `frequency_block_ids`/`frequency_data_ids` together with the corresponding `rename_*` flag; on the CLI the `--frequency-*` flags set both for you.
- An `Options` instance accumulates run state (rename maps, cleared lists, conversion maps) that the verifier reads, so create a fresh one for each call.
- Lower-level helpers such as `find_large_lists`, `apply_transforms`, and `verify` are importable too, but they are internal and may change.

## Example usage

```bash
# Defaults; large-list scan is off
python minify_sb3.py my_project.sb3

# Opt in to scanning and prompting about large lists
python minify_sb3.py my_project.sb3 --clear-large-lists

# Every non-interactive optimization
python minify_sb3.py my_project.sb3 my_project_minified.sb3 --all-optimizations

# Keep editor data, canonical key order
python minify_sb3.py my_project.sb3 --keep-comments --keep-monitors --sort-keys

# Constant folding only
python minify_sb3.py my_project.sb3 --fold-constant-expressions

# Shrink audio only
python minify_sb3.py my_project.sb3 --convert-wav-to-mp3

# Aggressive, then fold write-once variables interactively
python minify_sb3.py my_project.sb3 --all-optimizations --fold-constant-variables

# Treat only large lists as candidates for clearing.
python minify_sb3.py my_project.sb3 --clear-large-lists --list-bytes=65536 --list-items=5000

# Maximum minimization without touching IDs
python minify_sb3.py my_project.sb3 --remove-unused-variables --remove-unused-lists \
    --remove-unreachable --remove-unused-procedures --remove-empty-containers \
    --remove-default-target-properties --remove-costume-metadata

# Shrink all identifiers
python minify_sb3.py my_project.sb3 --rename-identifiers

# Shrink all variable & list names only
python minify_sb3.py my_project.sb3 --rename-variable-names --rename-list-names
```

## JSON minimum proofs

This project does **not** claim an absolute minimum for all equivalent Scratch programs or an optimal DEFLATE stream. Its certificates prove minima in explicitly bounded representation models. The search methods combine established ideas; no claim is made that they are new fundamental laws or novel research results.

### What a certificate means

Let `P` be an accepted SB3 project parsed into exact JSON values. Decimal numbers retain their exact values and the sign of zero. Strings retain their characters and types. A model fixes the target sequence, block graph, variable/list contents, names, procedure definitions, costume/sound metadata, comments, monitors, unknown properties and every ordered collection.

`fixed-tree-v2` allows only:

1. JSON whitespace removal and alternative spellings of the same exact numbers and strings.
2. Property permutations within block records. Block-table and input-map order remain fixed.
3. If enabled, omission of empty block `inputs` and `fields` objects, which the loader recreates.
4. If enabled, omission of a costume `md5ext` exactly equal to its loader-derived `assetId.dataFormat`. The explicit `dataFormat` remains mandatory.
5. Only with `--compact-block-flags`, omission of `shadow: false` and `topLevel: false`. This changes property presence in the hydrated records and therefore belongs to the opt-in tier. True flags, links and coordinates remain unchanged.

`fixed-graph-block-ids-v3` additionally allows bijective renaming of each target's block IDs and their schema-defined references. Output names are nonempty, compatible with XML attribute round trips, exclude ampersand, less-than, quotation mark and normalized attribute whitespace, cannot be JavaScript array-index property names or prototype-sensitive names, and cannot capture unresolved references. Every other identifier namespace remains fixed. The certificate is supported when all required names fit in at most three JSON payload bytes; unsupported cases fail rather than receive a false certificate.

The model deliberately excludes changes to procedure names, variable/list/broadcast IDs, assets, program structure, execution schedules and opaque extension metadata. It also excludes external tools that observe or store the old block IDs. Thus a zero gap does **not** establish a global Scratch minimum.

The fixed-tree version 2 uses strict UTF-8 decoding. Graph version 3 adds the verified reference-type contract and the stricter alphabet required by the editor's unescaped XML attributes. Historical graph versions 1/2 permitted XML-unsafe names; those ID candidates were disqualified and their certificates are superseded. Compare a certificate with its stated policy, input and model version, not just its byte count. A proposed PNG format-default omission was rejected by actual SB3 validation and is excluded from both supported models.

### 1. Exact shortest number tokens

For a nonzero decimal value, strip leading and trailing coefficient zeros to obtain

`v = (-1)^s C × 10^e`,

where `C` has `n` significant digits, begins and ends with a nonzero digit, and `s` is the sign. No binary floating-point conversion is used.

After removing redundant exponent signs/zeros and fractional trailing zeros, every JSON number representing this value has a mantissa obtained by putting the decimal point at an integer position `p` relative to `C`. Its required exponent is `q = e + n - p`. Its mantissa length is

```text
M(p) = n              if p = n
       n + 1          if 0 < p < n
       p              if p > n
       n + 2 - p      if p <= 0
```

These cases respectively encode `C`, an internal decimal point, appended integer zeros, or `0.` followed by leading fractional zeros. The total length is

`L(p) = s + M(p) + E(q)`,

where `E(0)=0`, and otherwise `E(q)=1+len(str(q))`: one exponent marker followed by the minimally spelled signed integer exponent. Uppercase `E` has the same cost as lowercase `e`; an explicit plus sign or exponent leading zeros cannot improve it.

**Theorem.** Minimizing `L(p)` over the bounded positions searched by `_short_number` gives a shortest JSON number token for the exact value.

**Proof.** The representation above covers every irredundant mantissa. Let `U` be the length of the original valid token, an available upper bound. Any improvement must satisfy `s+M(p)<=U`. For `p<=0`, this implies `p>=n+2+s-U`; for `p>n`, it implies `p<=U-s`. All internal positions are finite. The implementation enumerates an interval containing those positions, evaluates their exact lengths, and constructs a best token. Any position outside the interval already exceeds the upper bound before its exponent is included. No omitted representation can be shorter. Zero separately has minimum token `0`; negative zero has minimum token `-0` under the required sign-preservation rule. QED.

The permitted token syntax follows the [JSON number grammar](https://www.rfc-editor.org/rfc/rfc8259#section-6). The regression suite also checks decimal equivalence, large exponents, signed zero and an independent enumeration of small valid tokens.

### 2. Minimum strings and structure

Let `S(x)` be the minimum byte length of a quoted JSON string. Its two quotation marks are mandatory. Each quotation mark, backslash, or character with a standard two-byte short escape requires two bytes. Other C0 controls and isolated surrogate code units require a six-byte escape. Remaining characters use their literal UTF-8 encoding, which is shorter than their Unicode escape. Summing these costs gives `S(x)`. This uses the [JSON string and UTF-8 rules](https://www.rfc-editor.org/rfc/rfc8259#section-7).

For an object with `m` properties, the mandatory structural cost is

`2 + m + max(m-1,0)`:

two braces, `m` colons, and its commas. Add `S(key)` and the minimum value length for every property. For an array with `m` elements the structural cost is `2+max(m-1,0)`, plus its element lengths. The literal minima are four bytes for `null` and `true`, and five for `false`.

**Theorem.** The recursive sum produced by `minimum_json_size` is the exact minimum in `fixed-tree-v2` under the selected omission policy.

**Proof.** Every retained key, scalar and punctuation token is required by the fixed JSON tree and its grammar. Each scalar lower bound is attainable independently. Property permutations cannot change their byte counts. Every permitted omission decreases the recursive length, so a minimum omits every eligible property. There is no remaining sharing or abbreviation operation in this model. Encoding the normalized tree without whitespace and with the shortest tokens attains the sum. Hence the sum is both a lower bound and an achieved upper bound. QED.

The certificate reports independent categories for structure, keys, string values, number tokens and literals. `--minimum-json` prioritizes this raw byte objective; compression-aware mode can deliberately choose a longer numeric spelling if it compresses better and reports a nonzero gap.

### 3. Optimal block ID byte assignment

Block references in this model are only block-table keys, `next`, `parent`, the string reference positions of input tags 1/2/3, and comment `blockId`. Literal strings, variable/list IDs in primitive descriptors, argument IDs and broadcasts are never renamed.

Let `f_i>=1` be the occurrence count of block ID `i`, including its declaration. Let `c_j` be the number of JSON payload bytes needed for available name `j`, excluding the two quotation marks. With structure and all other values fixed, the variable part of the raw length is

`Σ_i f_i c_assignment(i)`.

Unresolved reference strings are reserved, preventing an invalid old edge from becoming a valid new edge. JavaScript's actual property enumeration order is retained; generated names are never array-index keys.

The generator covers **every** admissible name of cost one or two:

- One byte: 96 characters from U+0020 through U+007F, minus quotation mark, backslash, ampersand and less-than, minus ten array-index digits: **82** names before reservations.
- Two raw ASCII characters: `92² - 90 = 8,374` names after excluding canonical two-digit array indices.
- Single backslash: **1** name, requiring a two-byte JSON escape. Quotation mark is excluded because the editor interpolates IDs into double-quoted XML attributes without escaping.
- Single U+0080 through U+07FF character: **1,920** UTF-8 names of two bytes.

This gives **10,295** two-byte names before reservations. The character domain follows the [XML character rules](https://www.w3.org/TR/xml/#charsets) and [attribute-value grammar](https://www.w3.org/TR/xml/#NT-AttValue); TAB/LF/CR are excluded because attribute normalization would change them. Ampersand, less-than and double quote are excluded because Scratch VM's `blockToXML` interpolates block IDs without escaping. For the supported domain, additional ASCII names of cost three supply enough remaining IDs. Other admissible three-byte names can only tie that cost; they cannot improve the lower bound.

**Theorem.** Assigning available names in ascending byte cost to IDs in descending `f_i` order minimizes the raw JSON length in `fixed-graph-block-ids-v3`.

**Proof.** A minimum uses the shortest available names: replacing a used longer name with an unused shorter one strictly reduces cost because every `f_i` is positive. For two frequencies `f_i>=f_j` and name costs `c_a<=c_b`, compare the ordered assignment with its swap. The difference is

`(f_i c_a + f_j c_b) - (f_i c_b + f_j c_a) = (f_i-f_j)(c_a-c_b) <= 0`.

Thus exchanging inversions never increases cost. Repeated exchanges produce the assignment used by the implementation. Independent target namespaces contribute additively. Combining this achieved ID-cost bound with the fixed-tree bound proves the reported graph-model minimum. QED.

The regression suite verifies the complete short-name buckets, weighted reference costs, preservation of literal/data IDs, dangling-reference reservations and numeric-key enumeration order and strict XML attribute round trips. An independent verifier infers the ID correspondence from semantic record order; it does not trust optimizer-generated maps.

### 4. What the behavior checks establish

Safe flags retain exact hydrated block graphs, saved data and asset bytes. Derived costume fields produce identical loaded filenames, formats and metadata in the official Scratch VM. For opt-in block changes, verification restores the inferred ID correspondence and normalizes only the explicitly permitted false flags.

The validation runs also load both complete archives in Scratch VM 5.0.300, compares actual load/save state, ordered scripts and loaded costume/sound metadata, checks actual editor XML ID round trips, and compares every hydrated block after the declared normalization. It independently checks every uncompressed asset byte and requires later JSON experiments to retain the previous winner's exact compressed asset streams.

These checks are evidence for the standard loader/graph model. They do not prove every possible interaction, third-party extension, editor add-on or externally stored ID reference remains identical. Representation-changing flags therefore remain outside `--all-safe-flags`.

### 5. Why no absolute compressed minimum is claimed

Raw JSON length and the size of JSON inside the ZIP are different objectives. The compressor searches actual candidate streams, tries zlib settings and optional Zopfli, and retains smaller existing streams. Window scores are heuristics; a complete output measurement decides whether a candidate is accepted. A search plateau is evidence about the explored candidates, not a proof of an optimum.

[DEFLATE](https://www.rfc-editor.org/rfc/rfc1951) permits different LZ77 parses, Huffman tables and block partitions. We have not exhaustively minimized all such streams, much less every loader-equivalent JSON representation. For a fixed JSON byte sequence and a known valid stream of `b` bits, enumerating all bit strings of length at most `b` and validating their decompression would establish a minimum in principle. That finite search is far beyond the performed benchmark.

Byte-frequency entropy alone is not a universal lower bound for this individual structured document. Also, with an unrestricted bespoke decoder whose size is not counted, define `D_P(empty)=P`; a particular project then needs zero payload bytes. Thus a useful mathematical limit must specify the decoder, transformation class, byte accounting and observable state.

The reproducible result is an achieved scoped raw-JSON minimum plus measured compressed improvements. **No global Scratch or DEFLATE minimum has been proved.**
