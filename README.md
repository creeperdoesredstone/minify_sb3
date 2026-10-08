# minify_sb3

`minify_sb3.py` shrinks Scratch 3 project archives (`.sb3`) by removing redundant metadata and dead data, rewriting project structures, shortening identifiers, optimizing custom procedures, and optimizing ZIP/`project.json` encoding. It verifies the generated archive before reporting success.

The script is designed around Scratch 3's serialized project format and preserves editor/runtime compatibility for transformations that it explicitly models. Some optional transforms are intentionally aggressive or lossy; review the resulting project when using them.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [CLI syntax](#cli-syntax)
- [Flag groups and modes](#flag-groups-and-modes)
- [Default behavior](#default-behavior)
- [CLI reference](#cli-reference)
- [Interactive prompts](#interactive-prompts)
- [Transformation pipeline](#transformation-pipeline)
- [JSON and archive optimization](#json-and-archive-optimization)
- [Lossless mode](#lossless-mode)
- [Verification](#verification)
- [Output and progress reporting](#output-and-progress-reporting)
- [Exit codes](#exit-codes)
- [Behavior-changing and potentially lossy transforms](#behavior-changing-and-potentially-lossy-transforms)
- [Known limitations](#known-limitations)
- [Programmatic use](#programmatic-use)
- [Examples](#examples)

## Requirements

- **Python 3.10 or newer.**
- **`minify_flags.py`** must be importable by the script. The CLI uses it for flag groups, toggles, and valued options.
- **`ffmpeg` with `libmp3lame`** is needed for `--convert-wav-to-mp3` and for `--all-flags`, which enables that conversion.
You can download FFMPEG [here](https://ffmpeg.org/).
- **The Python `zopfli` package** is needed when Zopfli is explicitly requested (`--zopfli`, `--zopfli-assets`) or when using the all-lossless/safe group. Install it with:
  ```bash
  python -m pip install zopfli
  ```
- Enough RAM to hold the archive's assets in memory. The normal pipeline reads all non-`project.json` archive entries before writing the output.

## Quick start

```bash
# Default minification
python minify_sb3.py my_project.sb3

# Explicit output path
python minify_sb3.py my_project.sb3 my_project_small.sb3

# All non-interactive aggressive optimizations
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-optimizations

# The stronger all-flags bundle, including block-ID relabeling,
# minimum-JSON search, automatic Zopfli, and WAV -> MP3 conversion
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-flags

# Scan large lists and interactively choose which to clear
python minify_sb3.py my_project.sb3 --clear-large-lists

# Lossless compression/representation optimization
python minify_sb3.py my_project.sb3 my_project_small.sb3 --lossless

# Full lossless bundle
python minify_sb3.py my_project.sb3 my_project_small.sb3 --all-lossless
```

If no output path is supplied, the output is written beside the input as:

```text
<input name>_minified.sb3
```

The script refuses to overwrite the input file.

## CLI syntax

```text
python minify_sb3.py input.sb3 [output.sb3] [flags]
```

Flags may appear anywhere. Non-flag arguments are positional: the first is the input archive and the second is the output archive.

Valued flags use `--name=value`; a space-separated form such as `--compression-level 9` is rejected.

Unknown or malformed options are rejected before the input archive is modified.

## Flag groups and modes

`minify_flags.py` defines the CLI's flag vocabulary. Named groups are expanded
**recursively**, in first-seen order, before the individual options are parsed.
This means a group can contain another group, and duplicate flags are applied
only once.

### Optimization groups

| Group | Expands to |
| --- | --- |
| `--recommended` | `--all-optimizations`, `--fold-constant-variables`, `--compact-mutation-metadata`, `--clear-large-lists`, all name-shortening flags, `--all-flags`, procedure-argument optimization, duplicate-procedure merging, associative constant merging, constant propagation, reference-name stripping, compact data literals, unused-extension removal, WAV→MP3 conversion, nested-conditionals |
| `--maximum-size` | `--recommended`, `--inline-single-use-procedures`, `--specialize-procedures` |
| `--procedures` | Procedure-argument optimization, duplicate-procedure merging, single-use procedure inlining, procedure specialization |
| `--cleanup` | Unused variable/list/broadcast/procedure removal, unreachable-block removal, empty-field/input/container removal, costume-metadata removal, default-target-property removal, project-metadata removal, unused-extension removal, reference-name stripping |
| `--names` | Variable/list/broadcast/argument/procedure name shortening, variable/list/broadcast/argument ID shortening, frequency-based data-ID ordering |
| `--control-flow` | Branch swapping, trivial-loop simplification, nested-conditionals, associative constant merging, constant propagation, constant-expression folding, boolean-control simplification, block simplification |
| `--data-optimization` | Constant-variable folding, procedure-argument optimization, compact data literals, compact numeric inputs, compact field IDs, compact `mutation.hasnext`, compact mutation metadata, number normalization |
| `--compression` | `--optimize-json`, `--optimize-assets`, compact block defaults, compact costume references, frequency-based block/data IDs, asset deduplication, WAV→MP3 conversion |
| `--all-groups` | `--procedures`, `--cleanup`, `--names`, `--control-flow`, `--data-optimization`, `--compression`, `--all-optimizations`, and `--all-flags` |

Because `--all-flags` implies `--all-optimizations`, `--all-groups` is effectively
the broadest predefined optimization group. Some groups, especially
`--recommended`, also contain interactive operations such as
`--clear-large-lists` and `--fold-constant-variables`.

### `--all-optimizations`

Enables the main aggressive optimization bundle directly in `minify_sb3.py`.
It enables the transforms represented by the `all_optimizations` option set,
including:

- compact costume references and block defaults;
- optimized `project.json` and asset compression;
- frequency-based block and data ID assignment;
- variable/list/broadcast/argument/block ID shortening;
- unused variable/list/broadcast/procedure removal;
- unreachable-block removal;
- numeric/input/field/mutation compaction;
- number normalization;
- constant-expression folding;
- boolean/control-flow and block simplification;
- custom-procedure argument optimization;
- duplicate-procedure merging;
- single-use procedure inlining;
- procedure specialization;
- branch swapping;
- trivial-loop simplification;
- nested-conditional simplification;
- associative constant merging;
- script constant propagation;
- asset deduplication;
- removal of empty/default structural data where modeled.

It does **not** by itself enable the separate interactive or extra-compression flags
such as `--clear-large-lists`, `--fold-constant-variables`,
`--compact-mutation-metadata`, `--sort-keys`, WAV→MP3 conversion,
`--minimum-json`, block-ID relabeling, or automatic Zopfli.

### `--all-flags`

`--all-flags` implies `--all-optimizations` and additionally enables:

- `--compact-block-flags`;
- `--minimum-json`;
- `--relabel-block-ids`;
- automatic Zopfli compression;
- WAV→MP3 conversion.

WAV→MP3 is lossy. The minimum-JSON and block-ID relabeling features are
representation/compression optimizations accounted for by the verifier.

### `--lossless`

Enters lossless mode. The CLI rejects semantic or lossy transforms such as
dead-data removal, renaming, number normalization, control-flow rewrites,
procedure transformations, and WAV→MP3 conversion.

Lossless mode permits only the explicitly allowed representation/compression
options, such as JSON/asset optimization, compact block defaults/flags,
compact costume references, Zopfli, minimum-JSON search, and block-ID relabeling.
The verifier checks the resulting archive against the lossless invariants.

### `--all-lossless`, `--all-safe`, `--all-safe-flags`

These three flags activate the same `all_safe` path in `minify_sb3.py`:
lossless mode plus the complete lossless/safe option set.

They are **not** optimization groups in `minify_flags.py`; they are special
mode toggles recognized directly by `minify_sb3.py`.

The lossless/safe path enables the lossless JSON and asset compression features,
including Zopfli. It therefore requires the Python `zopfli` package when those
features are actually used.

### Group aliases

Many individual transforms have multiple accepted spellings. They are all
listed in the CLI reference below; the aliases are equivalent when passed
individually.

## Default behavior

Without optional flags, the CLI performs these operations:

1. Drops `topLevel: false` and `shadow: false`.
2. Normalizes `mutation.warp` from `"true"`/`"false"` strings to JSON booleans.
3. Strips sprite comments from non-Stage targets.
4. Rounds supported coordinate values.
5. Resets covered numeric/text shadow values.
6. Cleans variable/list monitors.
7. Removes sound `rate` and `sampleCount`.
8. Repairs dangling broadcast references.
9. Repairs dangling block links.
10. Verifies the complete output archive.

The Stage is deliberately excluded from sprite-comment stripping because Stage metadata can contain TurboWarp configuration.

Large-list scanning is **off by default**.

`--all-optimizations` does not scan or prompt about large lists.

## CLI reference

### Default-transform opt-outs

| Flag | Effect |
| --- | --- |
| `--keep-comments` | Keep sprite comments and block comment links. |
| `--keep-positions` | Do not round supported block/comment/costume coordinates. |
| `--keep-covered` | Do not reset covered shadow values. |
| `--keep-monitors` | Do not clean variable/list monitors. |
| `--keep-sound-metadata` | Keep sound `rate` and `sampleCount`. |

### Large-list handling

| Flag | Effect |
| --- | --- |
| `--clear-large-lists` | Find large lists and prompt before clearing selected lists. |
| `--list-bytes=N` | Minimum serialized JSON size for a list to be considered large. Default: `4096`. |
| `--list-items=N` | Minimum item count for a list to be considered large. Default: `1000`. |

A list qualifies when it is non-empty and meets either threshold.

### Valued options

All valued options use the `--name=value` form. A space-separated form such as
`--compression-level 9` is rejected.

| Option | Accepted value | Default |
| --- | --- | --- |
| `--zopfli-iterations=N` | Positive integer | `5` |
| `--json-search-rounds=N` | Positive integer | `0` normally; `1` with `--all-optimizations` |
| `--list-bytes=N` | Positive integer | `4096` |
| `--list-items=N` | Positive integer | `1000` |
| `--compression-level=N` | Integer `0`–`9` | `9` |
| `--normalize-epsilon=N` | Positive finite number | `1e-8` |
| `--sequence-threshold=N` | Positive integer | `3` |
| `--procedure-inline-passes=N` | Positive integer | `8` |
| `--procedure-specialization-passes=N` | Positive integer | `4` |
| `--procedure-specialization-min-calls=N` | Positive integer | `2` |

The parser accepts only positive integers for the integer-valued options above,
except `--compression-level`, which accepts `0` through `9`. `--normalize-epsilon`
must be a finite positive number.

### Identifier names

| Flag | Effect |
| --- | --- |
| `--rename-identifiers` | Shorten variable, list, broadcast, custom-block argument, and procedure names. |
| `--rename-variable-names` | Shorten variable names. |
| `--rename-list-names` | Shorten list names. |
| `--rename-broadcast-names` | Shorten broadcast names. |
| `--rename-argument-names` | Shorten custom-block argument names. |
| `--rename-procedure-names` | Shorten procedure names. |

These flags change names, not IDs.

### ID shortening

| Flag | Effect |
| --- | --- |
| `--rename-block-ids` | Shorten block IDs. |
| `--rename-variable-ids` | Shorten variable IDs. |
| `--rename-list-ids` | Shorten list IDs. |
| `--rename-broadcast-ids` | Shorten broadcast IDs. |
| `--rename-argument-ids` | Shorten custom-block argument IDs. |
| `--frequency-block-ids` | Shorten block IDs and order new IDs by reference frequency. |
| `--order-block-ids-by-frequency` | Alias for `--frequency-block-ids`. |
| `--frequency-data-ids` | Shorten variable/list/broadcast IDs and order them by reference frequency. |
| `--order-data-ids-by-frequency` | Alias for `--frequency-data-ids`. |
| `--relabel-block-ids` | Use the minimum-cost block-ID assignment during JSON optimization. |

`--relabel-block-ids` is a representation optimization used by the lossless/all-flags compression path; the verifier accounts for the approved ID relabeling.

### Dead-data and reachability removal

| Flag | Effect |
| --- | --- |
| `--remove-unused-variables` | Remove variables with no modeled references. Cloud variables are retained. |
| `--remove-unused-lists` | Remove lists with no modeled references. |
| `--remove-unused-broadcasts` | Remove unused broadcast definitions. |
| `--remove-unreachable` | Remove blocks unreachable from modeled roots. |
| `--remove-unused-procedures` | Remove custom procedures that are never reached through modeled procedure calls. |

### Numeric and structural compaction

| Flag | Effect |
| --- | --- |
| `--normalize-numbers` | Normalize integral/near-integral numeric values. |
| `--normalize-epsilon=N` | Positive tolerance for near-integer snapping. Default: `1e-8`. |
| `--compact-numeric-inputs` | Convert canonical numeric shadow strings to JSON numbers. |
| `--compact-field-ids` | Remove explicit `null` field-ID slots. |
| `--compact-mutation-hasnext` | Remove `mutation.hasnext` when false. |
| `--compact-mutation-metadata` | Compact JSON encoded in custom-block mutation metadata. |
| `--remove-empty-fields` | Remove empty `fields` objects. |
| `--remove-empty-inputs` | Remove empty `inputs` objects. |
| `--remove-costume-metadata` | Remove provably redundant costume metadata. |
| `--remove-default-target-properties` | Remove properties equal to Scratch defaults. |
| `--remove-empty-containers` | Remove empty `lists`, `broadcasts`, and `comments` containers. |
| `--remove-empty-target-containers` | Alias for `--remove-empty-containers`. |
| `--remove-project-meta` | Remove `meta.agent` and `meta.platform`. |
| `--sort-keys` | Sort JSON object keys during final serialization. |

### Constant folding and script rewrites

| Flag | Effect |
| --- | --- |
| `--fold-constant-variables` | Interactively replace eligible write-once variable reporters with their initial literals. |
| `--fold-constant-expressions` | Fold supported constant Scratch reporter expressions. |
| `--constant-propagation` | Propagate constants. |
| `--cp` | Alias for constant propagation. |
| `--branch-swapping` | Rewrite eligible conditional branches to reduce serialized size. |
| `--bs` | Alias for branch swapping. |
| `--trivial-loops` | Simplify supported trivial boolean loops. |
| `--tl` | Alias for trivial-loop simplification. |
| `--nested-conditionals` | Simplify/merge supported nested conditionals. |
| `--nc` | Alias for nested-conditional simplification. |
| `--associative-constants` | Merge/reassociate supported constant arithmetic expressions. |
| `--ac` | Alias for reassociating constant arithmetic expressions. |
| `--simplify-boolean-control` | Simplify constant boolean control flow. |
| `--simplify-blocks` | Simplify supported setter RHS block patterns. |

### Custom-procedure optimization

| Flag | Effect |
| --- | --- |
| `--optimize-procedure-arguments` | Remove/fold unnecessary custom-procedure arguments. |
| `--opa` | Alias. |
| `--merge-duplicate-procedures` | Merge equivalent custom procedures. |
| `--mdp` | Alias. |
| `--inline-single-use-procedures` | Inline safe single-use procedures. |
| `--isup` | Alias. |
| `--procedure-inline-passes=N` | Maximum inlining passes. Default: `8`. |
| `--specialize-procedures` | Specialize eligible custom procedures. |
| `--sp` | Alias. |
| `--procedure-specialization-passes=N` | Specialization passes. Default: `4`. |
| `--procedure-specialization-min-calls=N` | Minimum call count for specialization. Default: `2`. |

After procedure transformations, the script can perform another unused-procedure sweep when applicable.

### Sequence grouping

| Flag | Effect |
| --- | --- |
| `--group-similar-sequences` | Replace sufficiently similar repeated block sequences with custom procedures. |
| `--sequence-threshold=N` | Minimum sequence length. Default: `3`. |

Sequence grouping is not automatically enabled by `--all-optimizations`.

### Data/metadata cleanup

| Flag | Effect |
| --- | --- |
| `--strip-reference-names` | Strip supported redundant reference names. |
| `--srn` | Alias. |
| `--compact-data-literals` | Compact numeric data literals. |
| `--cdl` | Alias. |
| `--remove-unused-extensions` | Remove extensions that are no longer referenced. |
| `--rue` | Alias. |

### Asset handling

| Flag | Effect |
| --- | --- |
| `--convert-wav-to-mp3` | Convert WAV sounds to MP3 using `ffmpeg`; lossy. |
| `--deduplicate-assets` | Deduplicate identical assets and update references. |
| `--compress-assets` | Enable asset-compression handling; accepted by the CLI and retained for compatibility. |
| `--optimize-assets` | Optimize asset ZIP compression. |
| `--preserve-asset-compression` | Reuse an unchanged asset's existing DEFLATE stream when it is no larger than recompression. |
| `--zopfli-assets` | Use Zopfli for eligible asset compression. |
| `--zopfli-iterations=N` | Zopfli effort. Default: `5`. |
| `--compression-level=N` | Python zlib DEFLATE level, `0` through `9`. Default: `9`. |

Already internally compressed formats such as PNG/JPEG/GIF/WebP/MP3/OGG/M4A/AAC are not needlessly recompressed by the optimized-asset path.

### `project.json` optimization

| Flag | Effect |
| --- | --- |
| `--optimize-json` | Search multiple valid JSON encodings/layouts and retain the best compressed result. |
| `--compact-block-defaults` | Omit redundant block defaults during JSON optimization. |
| `--compact-costume-references` | Omit provably redundant costume `md5ext` references. |
| `--compact-block-flags` | Omit false `topLevel`/`shadow` block flags during the representation search. |
| `--minimum-json` | Optimize against the shortest supported raw JSON representation rather than only compressed size. |
| `--json-search-rounds=N` | Additional bounded layout-search rounds. Default: `0`, or `1` under `--all-optimizations`. |
| `--zopfli` | Use Zopfli for `project.json`. |
| `--zopfli-iterations=N` | Zopfli iterations; default `5`. |
| `--relabel-block-ids` | Apply minimum-cost block-ID relabeling as part of JSON optimization. |

For very large projects, the JSON search deliberately uses cheaper/bounded search settings. The semantic transforms are unchanged.

## Interactive prompts

### Large lists

`--clear-large-lists` scans non-empty lists meeting either configured threshold and presents them largest-first.

The prompt supports:

| Input | Action |
| --- | --- |
| Enter / `n` / `no` / `none` / `keep` | Keep all lists. |
| `a` / `all` | Select all candidates. |
| `u` / `unreferenced` / `safe` | Select only lists with no modeled references. |
| `1,3,5-7` | Select list numbers/ranges. |
| `s N` | Show the first eight items of list `N`, then prompt again. |

Clearing a list empties its contents but leaves the list definition in place.

Lists marked as behavior-sensitive require an explicit `yes` confirmation before they are cleared.

### Constant variables

`--fold-constant-variables` prompts for eligible variables.

A candidate must have a modeled write-once pattern, a literal setter input, a reporter use, no `change variable by`, no cloud status, and a finite numeric/string initial value.

The transform substitutes the **initial value stored in `project.json`**, not necessarily the value later assigned by the setter.

Pressing Enter keeps everything. The prompt also supports selecting positive-saving candidates or individual candidates. Loss-making candidates require explicit confirmation.

EOF or Ctrl-C is handled safely; Ctrl-C exits with status `130` and writes nothing.

## Transformation pipeline

For the normal non-lossless pipeline, transforms run in this order, subject to their options:

1. Normalize block defaults:
   - drop `topLevel: false`;
   - drop `shadow: false`;
   - normalize `mutation.warp` to a boolean.
2. Convert WAV sounds to MP3, if enabled.
3. Deduplicate assets, if enabled.
4. Strip sprite comments, unless `--keep-comments`.
5. Round positions, unless `--keep-positions`.
6. Reset covered shadow values, unless `--keep-covered`.
7. Clean monitors, unless `--keep-monitors`.
8. Clear lists selected by the interactive prompt.
9. Remove unreachable blocks.
10. Remove unused procedures.
11. Re-scan and remove newly unreachable blocks.
12. Fold selected constant variables.
13. Propagate script constants.
14. Rewrite script control structures.
15. Merge associative constants.
16. Specialize custom procedures.
17. Fold constant expressions.
18. Simplify boolean control flow.
19. Simplify setter RHS blocks.
20. Group similar sequences.
21. Optimize custom-procedure arguments.
22. Merge duplicate procedures.
23. Inline safe single-use procedures.
24. Remove procedures made unused by the preceding procedure passes.
25. Remove unused variables/lists.
26. Repair dangling broadcast references.
27. Remove unused broadcasts.
28. Rename names/identifiers.
29. Rename variable/list IDs.
30. Rename broadcast IDs.
31. Rename argument IDs.
32. Rename block IDs.
33. Compact numeric inputs.
34. Compact redundant field IDs.
35. Compact `mutation.hasnext`.
36. Compact mutation metadata.
37. Normalize numbers.
38. Remove sound metadata unless `--keep-sound-metadata`.
39. Remove empty fields.
40. Remove empty inputs.
41. Remove redundant costume metadata.
42. Remove default target properties.
43. Remove empty target containers.
44. Remove project metadata.
45. Compact data literals.
46. Strip reference names.
47. Remove unused extensions.
48. Repair dangling block links.

Then:

49. Optionally relabel block IDs for the JSON minimum model.
50. Serialize `project.json`.
51. Optionally optimize its layout/compression.
52. Write `project.json` and assets to a new ZIP.
53. Verify the output.
54. Delete the output if verification fails.

The actual stage progress bar reflects only enabled stages, so disabled transforms do not appear as completed work.

## Always-on repairs and invariants

Several operations happen independently of the optional transform flags:

- `topLevel: false`, `shadow: false`, and string-valued `mutation.warp` are normalized.
- Sound `rate` and `sampleCount` are removed unless `--keep-sound-metadata` is set.
- Dangling `next`/`parent` block links are repaired.
- Inputs pointing to missing blocks are removed or reduced to their surviving shadow value.
- Unambiguous missing parent links may be restored.
- Comments pointing to missing blocks are deleted.
- A referenced-but-undefined broadcast can be repaired into the appropriate broadcast table. Conflicting names for the same missing ID are rejected rather than guessed.
- Verification always runs after a successful non-lossless write.

## JSON and archive optimization

### Normal ZIP writing

The output contains `project.json` first, followed by assets.

The normal archive writer uses DEFLATE with `--compression-level` (default `9`).

### `--optimize-json`

The JSON optimizer considers multiple valid representations, including:

- original property order;
- alternative block-property orders;
- shorter exact numeric spellings;
- compact block/default representations when enabled;
- block-ID relabeling when enabled;
- bounded layout/window searches;
- Zopfli when requested.

The optimizer compares candidates using the requested objective and keeps the best candidate.

For large `project.json` files, the search is deliberately reduced to prevent compression-search cost from dominating processing time. The large-project thresholds in the script are approximately 1.5 MB of raw JSON or 8,000 blocks.

### Asset optimization

When `--optimize-assets` is enabled:

- already internally compressed formats are preserved without redundant recompression;
- unchanged assets can be compared against their original compressed streams;
- Zopfli can be used when enabled;
- asset data itself is unchanged unless an explicit transform such as WAV-to-MP3 or deduplication changes references.

`--preserve-asset-compression` is useful when an existing DEFLATE stream is already no larger than recompression.

## Lossless mode

Lossless mode is separate from the normal semantic-minification pipeline.

```bash
python minify_sb3.py project.sb3 --lossless
```

It optimizes:

- `project.json` serialization/compression;
- selected representation-only block defaults;
- selected costume references;
- asset DEFLATE streams.

The lossless verifier checks:

- archive entry names and order;
- ZIP metadata;
- every non-`project.json` asset byte-for-byte;
- exact JSON values;
- collection order.

The only permitted representation changes are the ones explicitly selected by the lossless options, such as approved block-flag/default compaction or block-ID relabeling.

`--all-lossless`, `--all-safe`, and `--all-safe-flags` enable the strongest built-in lossless bundle and require Zopfli.

## Verification

The normal verifier reloads the original and output archives and checks the archive, project structure, references, and approved transformations.

It validates, among other things:

### Archive integrity

- ZIP entry names are valid and non-duplicated.
- `project.json` exists and can be parsed.
- Asset references resolve to archive entries.
- Asset MD5s and metadata are consistent.
- WAV-to-MP3 conversions have updated asset IDs/references correctly.
- Unchanged assets remain byte-identical when the transform requires it.

### Block graph

- Block opcodes and record structure are valid.
- `next`/`parent` relationships are coherent.
- Inputs and shadow references resolve correctly.
- Top-level/shadow relationships are valid.
- No unexpected dangling block references remain.

### Data and procedure integrity

- Variable/list/broadcast references are consistent.
- Approved removed data is absent only where the corresponding transform allows it.
- Renamed IDs are reversed before comparison.
- Procedure rewrites, inlining, specialization, argument changes, and other approved graph edits are checked against the transform's recorded edits.

### Monitors, comments, costumes, and sounds

- Monitor order and non-variable/list monitors are preserved.
- Removed monitors must satisfy the transform's orphan/default-layout rules.
- Comments and comment links are checked against the selected comment policy.
- Costume and sound metadata changes are restricted to approved transforms.

If verification fails, the output archive is deleted and the process returns status `2`.

## Output and progress reporting

The normal pipeline prints a live progress bar with stages such as:

```text
Normalize block defaults
...
Optimize project.json
Process assets and write archive
Verify output
```

Asset optimization also reports an asset counter while processing a large archive.

At completion, the script prints:

```text
Input : "my_project.sb3"
Output: "my_project_minified.sb3"

Transforms applied:
  ...
project.json : ... MiB -> ... MiB  (...)
archive      : ... MiB -> ... MiB
```

The transform report includes counters for metadata normalization, comments, positions, shadows, monitors, list clearing, dead-code/procedure removal, script rewrites, identifier changes, compaction, asset work, JSON optimization, and verification-related repairs.

Lossless mode additionally reports JSON encoding trials, the selected JSON lower bound, JSON DEFLATE savings, and asset DEFLATE savings.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success; output was written and verified. |
| `1` | Invalid usage, missing input, invalid archive, unavailable required compressor, or another pre-write/runtime error. |
| `2` | Output verification failed; the output was deleted. |
| `130` | Interactive prompt was aborted with Ctrl-C; nothing was written. |

## Behavior-changing and potentially lossy transforms

These transforms deserve review before shipping the result:

- `--clear-large-lists` can change runtime behavior when selected lists are referenced.
- `--remove-unreachable` can remove scripts that the reachability model does not consider live.
- `--remove-unused-procedures` relies on the procedure-call model implemented by the script.
- `--fold-constant-variables` uses the stored initial value and can remove a setter in approved cases.
- `--fold-constant-expressions` uses the script's modeled Scratch coercion/evaluation rules.
- `--script-constant-propagation` and control-flow rewrites change the serialized program graph.
- Procedure argument optimization, specialization, duplicate merging, and inlining change procedure structure.
- `--group-similar-sequences` creates procedures and replaces repeated sequences.
- `--normalize-numbers` can change near-integer floating-point values by design.
- `--compact-numeric-inputs` changes JSON number/string representation and can matter to consumers that distinguish the two outside Scratch.
- `--convert-wav-to-mp3` is lossy audio conversion.
- `--deduplicate-assets` changes archive/reference structure while retaining the shared asset data.
- `--strip-reference-names` and similar metadata compaction can affect tools that depend on serialized names rather than Scratch behavior.
- `--remove-unused-extensions` assumes the script's reference analysis is sufficient for the target project.

Lossless mode is the appropriate choice when semantic and byte-level preservation are the priority.

## Known limitations

- The normal pipeline loads all assets into memory.
- `--clear-large-lists` and `--fold-constant-variables` are interactive; they are not enabled by `--all-optimizations`.
- `--all-optimizations` is intentionally aggressive and can substantially rewrite a project.
- `--all-flags` adds lossy WAV-to-MP3 conversion.
- Explicit Zopfli options require the `zopfli` package.
- `ffmpeg` failures during WAV conversion leave the WAV in place rather than invalidating the whole project.
- Some transformations intentionally model only the serialized constructs the script understands. Hand-edited or extension-specific project structures should be tested in the intended Scratch/TurboWarp environment.
- JSON optimization can be CPU-intensive. Large projects use bounded search paths specifically to prevent the compression/layout search from exploding in cost.
- The script's lossless lower bound is a bound under its selected fixed representation model; it is not a proof of the globally smallest possible Scratch JSON representation.

## Programmatic use

The module exposes:

```python
from minify_sb3 import Options, minify_sb3

opts = Options(
    lists=False,
    rename_block_ids=True,
    remove_unreachable=True,
    normalize_numbers=True,
    compression_level=9,
)

status = minify_sb3("in.sb3", "out.sb3", opts)
```

`minify_sb3()` returns the same status codes used by the CLI.

### `Options` keyword arguments

The `Options` constructor currently accepts:

```text
comments
positions
covered
monitors
lists

rename_block_ids
rename_variable_ids
rename_list_ids
rename_broadcast_ids
rename_argument_ids

rename_identifiers
rename_variable_names
rename_list_names
rename_broadcast_names
rename_argument_names
rename_procedure_names

remove_unused_variables
remove_unused_lists
remove_unused_broadcasts
remove_unreachable
remove_unused_procedures

normalize_numbers
remove_empty_fields
remove_empty_inputs
remove_costume_metadata
remove_default_target_properties
remove_empty_containers
remove_empty_target_containers
remove_project_meta

convert_wav_to_mp3
compress_assets
sort_keys
compression_level
list_bytes
list_items
normalize_epsilon
keep_sound_metadata
preserve_asset_compression

frequency_block_ids
frequency_data_ids

compact_numeric_inputs
compact_field_ids
compact_mutation_hasnext
compact_mutation_metadata

fold_constant_variables
fold_constant_expressions
simplify_boolean_control
simplify_blocks

deduplicate_assets

optimize_procedure_arguments
merge_duplicate_procedures
inline_single_use_procedures
procedure_inline_passes

specialize_procedures
procedure_specialization_passes
procedure_specialization_min_calls

branch_swapping
trivial_loops
nested_conditionals
associative_constant_merging
script_constant_propagation

strip_reference_names
compact_data_literals
remove_unused_extensions

group_similar_sequences
sequence_threshold

lossless
all_lossless

optimize_json
optimize_assets
compact_block_defaults
zopfli
zopfli_assets
zopfli_iterations
compact_costume_references
compact_block_flags
minimum_json
json_search_rounds
relabel_block_ids
auto_zopfli
```

Important programmatic defaults:

- `compression_level=9`
- `list_bytes=4096`
- `list_items=1000`
- `normalize_epsilon=1e-8`
- `zopfli_iterations=5`
- `procedure_inline_passes=8`
- `procedure_specialization_passes=4`
- `procedure_specialization_min_calls=2`
- `sequence_threshold=3`

Unlike the CLI, a directly constructed `Options()` has `lists=True`, so programmatic callers should set `lists=False` if they do not want an interactive large-list prompt.

`Options` also accumulates internal state used by verification, including rename maps, cleared-list selections, asset conversions, and recorded graph edits. Use a fresh `Options` object for each independent run.

## Examples

```bash
# Default minification
python minify_sb3.py project.sb3

# Keep comments and monitors
python minify_sb3.py project.sb3 --keep-comments --keep-monitors

# Remove dead data and dead scripts
python minify_sb3.py project.sb3 \
    --remove-unused-variables \
    --remove-unused-lists \
    --remove-unused-broadcasts \
    --remove-unreachable \
    --remove-unused-procedures

# Rename all supported user-facing identifiers
python minify_sb3.py project.sb3 --rename-identifiers

# Rename IDs by reference frequency
python minify_sb3.py project.sb3 \
    --frequency-block-ids \
    --frequency-data-ids

# Fold constant expressions
python minify_sb3.py project.sb3 --fold-constant-expressions

# Optimize custom procedures
python minify_sb3.py project.sb3 \
    --optimize-procedure-arguments \
    --merge-duplicate-procedures \
    --inline-single-use-procedures \
    --specialize-procedures

# Group repeated sequences
python minify_sb3.py project.sb3 \
    --group-similar-sequences \
    --sequence-threshold=4

# Optimize JSON encoding without enabling all semantic transforms
python minify_sb3.py project.sb3 \
    --optimize-json \
    --json-search-rounds=2

# Optimize assets while preserving unchanged asset streams when possible
python minify_sb3.py project.sb3 \
    --optimize-assets \
    --preserve-asset-compression

# Lossless optimization
python minify_sb3.py project.sb3 project_lossless.sb3 --all-lossless

# Maximum built-in optimization, including lossy WAV conversion
python minify_sb3.py project.sb3 project_max.sb3 --all-flags
```
