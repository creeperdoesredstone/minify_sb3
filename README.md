# minify_sb3


`minify_sb3.py` shrinks Scratch 3 project archives (`.sb3`) by removing redundant metadata and dead data while preserving project behavior, editor compatibility, and asset integrity. It targets the Scratch VM, the Scratch block editor, and TurboWarp-style editors, and it checks its own output before leaving it on disk.

- [Requirements](#requirements)
- [Quick start](#quick-start)
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
- [Example workflows](#example-workflows)

## Requirements

- **Python 3.10 or newer.** The script uses only the standard library (`collections`, `copy`, `hashlib`, `json`, `math`, `os`, `subprocess`, `sys`, `uuid`, `zipfile`, `zlib`). It relies on `X | None` annotations, which need 3.10+.
- **`ffmpeg` with `libmp3lame` (optional).** Only needed for `--convert-wav-to-mp3` (and `--all-optimizations`, which enables it).
- Enough RAM to hold the whole archive: every asset is read into memory before the output is written.

## Quick start

```bash
# Safe defaults; large lists are not scanned unless requested
python minify_sb3.py my_project.sb3

# Scan large lists and prompt before clearing any
python minify_sb3.py my_project.sb3 --clear-large-lists

# Explicit output path, enable the batch of non-interactive optimizations
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-optimizations

# Keep comments and monitors, canonicalize key order
python minify_sb3.py my_project.sb3 --keep-comments --keep-monitors --sort-keys
```

If no output path is given, the result is written next to the input as `<input name>_minified.sb3`. The output path must differ from the input path; the script refuses to overwrite its own source. Your original file is never modified.

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

A list qualifies if it is non-empty and meets **either** threshold.

### Batch flag

| Flag | Effect |
| --- | --- |
| `--all-optimizations` (alias `--all-flags`) | Enable the batch of non-interactive optional transforms. See [below](#--all-optimizations-in-detail). |

### Validation rules for valued flags

- `--list-bytes`, `--list-items`: digits only, greater than zero.
- `--compression-level`: digits only, `0` through `9`.
- `--normalize-epsilon`: any finite float greater than zero.
- A valued flag given without `=value`, or a toggle given with `=value`, is rejected as malformed.

## How the pipeline works

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

`NaN`, infinities, booleans, and values that are already integers are left alone. Sprite `x`/`y` (the sprite's position on stage) are **not** touched.

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

Clearing a list **only empties its contents** (`[]`). The list stays defined, so block references and monitors keep resolving.

### Constant variables (`--fold-constant-variables`)

Candidates are variables that satisfy **all** of these:

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

Selecting any `LOSS` candidate requires typing `yes` to confirm. `--all-optimizations` does **not** enable this transform.

## Optional transforms

### ID renaming

All renaming uses the same compact 85-character alphabet (`!@#$%^*()+_-={}|[]:;?,./~`, `A-Z` without `I`, `a-z` without `l`, and `0-9`). The first 85 IDs are one character, the next 7,225 are two characters, and so on. Every rewrite updates all references so the project stays coherent.

- **`--rename-block-ids`**: block IDs are unique per sprite, so renaming is per sprite. Rewrites the block table keys plus every `next`, `parent`, input reference (including shadow slots), and comment `blockId`. IDs that are already dangling in the source are reserved so a new ID can never collide with them.
- **`--rename-variable-ids`** / **`--rename-list-ids`**: Stage (global) data is renamed first, then each sprite's local data. All new IDs are unique across the whole project. Rewrites the `variables`/`lists` tables, `VARIABLE`/`LIST` fields, variable and list reporter primitives (including those nested in inputs and those stored in the block table), and monitor IDs, honoring local-over-global resolution.
- **`--rename-broadcast-ids`**: renames broadcast IDs project-wide while preserving each broadcast's name. Rewrites broadcast tables, `BROADCAST_OPTION`/`BROADCAST_INPUT` fields, and broadcast primitives in inputs. New IDs avoid those assigned to variables and lists.
- **`--rename-argument-ids`**: renames custom-block argument IDs inside `procedures_prototype` and `procedures_call` mutations (`argumentids`) and the matching input keys. For each procedure code within a sprite, renaming happens only if every prototype and call agrees on the same `argumentids` list; otherwise that procedure is skipped.
- **`--frequency-block-ids`**: orders block IDs by how often each block is referenced (via `next`, `parent`, inputs, and comments), so the most-referenced blocks get one-character IDs.
- **`--frequency-data-ids`**: the same idea for variables, lists, and broadcasts.

Renamed projects are harder to compare by eye. The verifier reverses every mapping before comparing against the original.

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
- **`--fold-constant-expressions`**: folds nested `+`, `-`, `*`, `/`, `%`, `=`, `<`, `>`, `and`, `or`, `not`, `length`, and `mathop` reporters for `abs`, `floor`, `ceiling`, `sqrt`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `ln`, `log`, `e ^`, and `10 ^` when their inputs are constant. Numeric strings may be decimal or prefixed with `0b`, `0o`, or `0x`; surrounding whitespace is ignored. Booleans are implicitly converted to `1` or `0` when used numerically. Expressions involving variables, unsupported math operations, division/modulo by zero, or non-finite results are left unchanged. Folded booleans are encoded according to their destination: false becomes the empty input `[2, null]`, true becomes a `not <>` reporter with an empty operand, and booleans in other sockets become text literals `"true"` or `"false"`. The transform avoids folding expressions with attached comments and verifies the exact input rewrites and any inserted reporter blocks.
- **`--remove-empty-fields`** / **`--remove-empty-inputs`**: delete empty `fields`/`inputs` objects. The default keeps these containers.
- **`--remove-costume-metadata`**: removes a costume's `md5ext` only when it equals `<assetId>.<dataFormat>` and that asset exists in the archive; removes `bitmapResolution` only from SVG costumes when it equals `1`. Bitmap costumes are never changed.
- **`--remove-default-target-properties`**: removes properties exactly equal (value and type) to Scratch's defaults.
  - Stage: `currentCostume` 0, `volume` 100, `tempo` 60, `videoTransparency` 50, `videoState` `"on"`, `textToSpeechLanguage` `null`.
  - Sprites: `currentCostume` 0, `volume` 100, `visible` true, `x` 0, `y` 0, `size` 100, `direction` 90, `draggable` false, `rotationStyle` `"all around"`.
- **`--remove-empty-containers`**: removes empty `lists`, `broadcasts`, and `comments` objects from targets.
- **`--remove-project-meta`**: removes `meta.agent` and `meta.platform`. `meta.semver` and `meta.vm` are kept.
- **`--sort-keys`**: writes every JSON object with keys in sorted order. This can help compression experiments and makes diffs stable.

## `--all-optimizations` in detail

`--all-optimizations` (alias `--all-flags`) turns on the following:

| Group | Enabled |
| --- | --- |
| ID renaming | block, variable, list, broadcast, and argument IDs, ordered by frequency (equivalent to also passing `--frequency-block-ids` and `--frequency-data-ids`) |
| Dead data | unused variables, lists, and broadcasts; unreachable blocks; unused procedures |
| Compaction | `--compact-numeric-inputs`, `--compact-field-ids`, `--compact-mutation-hasnext`, `--fold-constant-expressions`, `--normalize-numbers` |
| Trimming | `--remove-empty-fields`, `--remove-empty-inputs`, `--remove-costume-metadata`, `--remove-default-target-properties`, `--remove-empty-containers`, `--remove-project-meta` |
| Archive | `--convert-wav-to-mp3`, `--preserve-asset-compression` (and the no-op `--compress-assets`) |
| Prompts | neither interactive prompt runs; large lists are not scanned or cleared |

It does **not** enable:

- `--clear-large-lists` (not part of the batch; lists are not scanned or cleared)
- `--fold-constant-variables` (needs your selection)
- `--compact-mutation-metadata`
- `--sort-keys`
- `--keep-sound-metadata` (sound `rate`/`sampleCount` are still removed)

`--keep-*` flags, `--compression-level=N`, and `--normalize-epsilon=N` still apply on top of it. The list thresholds are used only with `--clear-large-lists`. Because `--all-optimizations` includes lossy WAV conversion and aggressive removals, review the result for projects that use unusual patterns.

## Archive and asset handling

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
| Cosmetic only (editor/layout metadata, redundant encoding) | default transforms; `--compact-field-ids`, `--compact-mutation-hasnext`, `--compact-mutation-metadata`, `--remove-empty-fields`, `--remove-empty-inputs`, `--remove-costume-metadata`, `--remove-empty-containers`, `--remove-project-meta`, `--sort-keys`, all `--rename-*` and `--frequency-*` flags |
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

- `Options` defaults differ from CLI list behavior: `comments`, `positions`, `covered`, `monitors`, and `lists` are `True` on a directly constructed `Options()` object, so programmatic use scans/prompts for large lists unless `lists=False`. The CLI enables this scan only with `--clear-large-lists`; its ordinary default and `--all-optimizations` do not scan lists. Other defaults include `compression_level=9`, `list_bytes=4096`, `list_items=1000`, and `normalize_epsilon=1e-8`.
- `fold_constant_variables=True` always prompts interactively.
- Available keyword arguments: `comments`, `positions`, `covered`, `monitors`, `lists`, `rename_block_ids`, `rename_variable_ids`, `rename_list_ids`, `rename_broadcast_ids`, `rename_argument_ids`, `remove_unused_variables`, `remove_unused_lists`, `remove_unused_broadcasts`, `remove_unreachable`, `remove_unused_procedures`, `normalize_numbers`, `remove_empty_fields`, `remove_empty_inputs`, `remove_costume_metadata`, `remove_default_target_properties`, `remove_empty_containers` (or `remove_empty_target_containers`), `remove_project_meta`, `convert_wav_to_mp3`, `compress_assets`, `sort_keys`, `compression_level`, `list_bytes`, `list_items`, `normalize_epsilon`, `keep_sound_metadata`, `preserve_asset_compression`, `frequency_block_ids`, `frequency_data_ids`, `compact_numeric_inputs`, `compact_field_ids`, `compact_mutation_hasnext`, `compact_mutation_metadata`, `fold_constant_variables`, `fold_constant_expressions`.
- Frequency ordering is selected by `frequency_block_ids`/`frequency_data_ids` **together with** the corresponding `rename_*` flag; on the CLI the `--frequency-*` flags set both for you.
- An `Options` instance accumulates run state (rename maps, cleared lists, conversion maps) that the verifier reads, so create a fresh one for each call.
- Lower-level helpers such as `find_large_lists`, `apply_transforms`, and `verify` are importable too, but they are internal and may change.

## Example workflows

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
python minify_sb3.py my_project.sb3 --list-bytes=65536 --list-items=5000

# Maximum minimization without touching IDs
python minify_sb3.py my_project.sb3 --remove-unused-variables --remove-unused-lists \
    --remove-unreachable --remove-unused-procedures --remove-empty-containers \
    --remove-default-target-properties --remove-costume-metadata
```