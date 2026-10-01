# minify_sb3

`minify_sb3.py` removes redundant Scratch project metadata and dead data while preserving project behavior, editor compatibility, and asset integrity. It is designed to shrink `.sb3` archives without breaking the Scratch VM or the block editor.

## Usage

```bash
python minify_sb3.py input.sb3 [output.sb3]
    [--keep-comments] [--keep-positions] [--keep-covered] [--keep-monitors]
    [--keep-lists] [--rename-block-ids] [--rename-variable-ids] [--rename-list-ids]
    [--rename-broadcast-ids] [--rename-argument-ids]
    [--remove-unused-variables] [--remove-unused-lists]
    [--remove-unreachable] [--remove-unused-procedures]
    [--normalize-numbers] [--compact-numeric-inputs] [--compact-field-ids]
    [--compact-mutation-hasnext] [--compact-mutation-metadata]
    [--remove-empty-fields] [--remove-empty-inputs]
    [--remove-costume-metadata] [--remove-default-target-properties]
    [--remove-empty-containers] [--remove-project-meta] [--sort-keys]
    [--keep-sound-metadata] [--convert-wav-to-mp3] [--preserve-asset-compression]
    [--frequency-block-ids] [--frequency-data-ids]
    [--fold-constant-variables] [--fold-constant-expressions]
    [--list-bytes=N] [--list-items=N] [--compression-level=N] [--normalize-epsilon=N]
    [--all-optimizations]
```

The default behavior applies the core minifications and prompts before clearing large lists. The `--keep-*` flags disable individual default transforms. `--all-optimizations` and its alias `--all-flags` enable the batch of non-interactive optional transforms, but skip the large-list prompt, constant-variable folding, and mutation-metadata compaction. Those three require separate choices or flags.

## What the script does by default

These are the transformations that are applied automatically unless explicitly disabled:

### Metadata cleanup

- `topLevel: false` is removed from blocks. Scratch only checks whether `block.topLevel` is truthy, so the `false` value is redundant.
- `shadow: false` is removed for the same reason.
- `mutation.warp` values are normalized from the string forms `"true"` and `"false"` to real booleans.
- The script keeps required empty container objects intact by default and only removes them when you explicitly enable `--remove-empty-fields` or `--remove-empty-inputs`.
- WAV sounds are left unchanged by default. `--convert-wav-to-mp3` converts them with `ffmpeg`; `--all-optimizations` also enables conversion. If `ffmpeg` is unavailable, the WAV assets are retained.
- The `rate` and `sampleCount` metadata of each sound asset are removed, unless the flag `--keep-sound-metadata` is set.

### Sprite comments and block comment links

- Every sprite comment object is removed from non-Stage targets.
- Block comment links are removed to avoid dangling references.
- The Stage is left alone because TurboWarp stores `_twconfig_` metadata there, and some projects rely on it.

This is enabled by default and can be skipped with `--keep-comments`.

### Position rounding

- Block coordinates and primitive positions are rounded to whole numbers.
- Comment positions and sizes are rounded.
- Costume rotation centers are rounded when they are editor-only layout values.

This is safe because these values are editor/layout metadata. Disable it with `--keep-positions`.

### Covered shadow values

If an input is covered by a reporter, the obscured shadow still needs to be a valid Scratch value object for the editor to reconstruct the workspace, but the actual value is never used at runtime. The script resets numeric shadows to `0` and text shadows to `""`. Other shadows are ignored.

This transform is enabled by default and can be disabled with `--keep-covered`.

### Monitor cleanup

The script removes unnecessary monitor metadata while keeping monitor semantics intact:

- hidden, orphaned variable/list monitors are dropped
- hidden monitors with no block that could show them and a default layout are removed
- list-monitor `params` are cleared because the loader re-derives them from the monitor id
- list and variable monitor `value` placeholders are normalized to their empty/default states

This is enabled by default and can be skipped with `--keep-monitors`.

### Large-list prompt

The script looks for large lists and offers an interactive choice of which ones to clear.

- Default threshold: at least 4096 bytes of JSON or 1000 items
- Can be adjusted with `--list-bytes=N` and `--list-items=N`
- Pressing Enter, EOF, or a closed stdin keeps all lists
- `a` clears all large lists
- `u` clears only lists that are unreferenced / safe to empty
- `s N` prints a sample of a list
- `1,3,5-7` selects specific list numbers

Clearing a list only empties its contents. The list itself stays defined, which avoids breaking script references and monitor IDs.

## Optional transforms

These are not applied unless requested with the corresponding flag or `--all-optimizations`.

### ID renaming

- `--rename-block-ids`: renames block IDs using Scratch's compact character set and rewrites every block reference.
- `--rename-variable-ids`: renames variable IDs while keeping ownership and references coherent.
- `--rename-list-ids`: renames list IDs while keeping ownership and references coherent.
- `--rename-broadcast-ids`: renames broadcast IDs while preserving their names.
- `--rename-argument-ids`: renames custom block argument IDs inside procedure mutation metadata and inputs.
- `--frequency-block-ids` (alias `--order-block-ids-by-frequency`): renames block IDs, assigning shorter IDs to more frequently referenced blocks.
- `--frequency-data-ids` (alias `--order-data-ids-by-frequency`): renames variable, list, and broadcast IDs, assigning shorter IDs to more frequently referenced data.

These can reduce the size of a project, but they are more invasive and may make the project harder to compare by eye.

### Removing dead data

- `--remove-unused-variables`: removes variables that no block or monitor references.
- `--remove-unused-lists`: removes lists that no block or monitor references.
- `--remove-unreachable`: removes blocks that are not reachable from a top-level/root script.
- `--remove-unused-procedures`: removes procedure definitions whose implementation is never called.

These are good for aggressively shrinking projects, but they can change behavior if a project relies on idling or hidden data that is no longer referenced.

### Normalization

- `--normalize-numbers`: rewrites integer-valued floats such as `1.0` to integers and rounds eligible near-integer values under a tolerance (default `1e-8`, adjustable with `--normalize-epsilon=N`).
- `--compact-numeric-inputs`: converts numeric input strings to JSON numbers only when their decimal text has an exact canonical representation.
- `--compact-field-ids`: removes explicit null IDs from block fields.
- `--compact-mutation-hasnext`: removes `mutation.hasnext` only when it is explicitly false.
- `--compact-mutation-metadata`: compacts JSON-encoded custom-block mutation arrays without removing `tagName` or `children`. This flag is not enabled by `--all-optimizations`.
- `--fold-constant-expressions`: folds nested numeric `+`, `-`, `*`, `/`, and `%` reporter expressions when every operand is a finite numeric literal. Decimal and `0b`, `0o`, and `0x` integer strings are accepted. Expressions with variables, unsupported operators, division by zero, or non-finite results are left unchanged.
- `--fold-constant-variables`: interactively offers variable reporters for replacement by each variable's initial project value. Candidates must have one literal `set variable to` block, at least one reporter use, no `change variable by` block, and must not be cloud variables. Review selections carefully: the transform substitutes the initial value, which may differ from the value assigned by the setter.
- `--remove-empty-fields`: drops empty `fields` objects.
- `--remove-empty-inputs`: drops empty `inputs` objects.
- `--remove-costume-metadata`: strips redundant costume metadata like default `md5ext` values and SVG `bitmapResolution` defaults.
- `--remove-default-target-properties`: removes target properties only when they exactly match Scratch defaults.
- `--remove-empty-containers` (alias `--remove-empty-target-containers`): removes empty `lists`, `broadcasts`, and `comments` containers.
- `--remove-project-meta`: removes project metadata keys such as `meta.agent` and `meta.platform`.
- `--keep-sound-metadata`: preserves `rate` and `sampleCount` on sounds; otherwise, they are removed.
- `--sort-keys`: canonicalizes object key ordering for compression experiments.

### Archive compression and output control

- `--compression-level=N`: sets the ZIP DEFLATE compression level from 0 to 9.
- `--preserve-asset-compression`: reuses an unchanged asset's original DEFLATE data when it is no larger than recompressing it.
- `--convert-wav-to-mp3`: converts WAV sounds to MP3 with `ffmpeg`; converted assets receive IDs matching their new data.
- `--list-bytes=N` and `--list-items=N`: adjust the threshold used when scanning for large lists.
- `--compress-assets` is accepted by the CLI but currently does not run a separate asset-compression transform.

## Behavior that must be preserved

The script is deliberately conservative. Certain fields cannot be removed without breaking compatibility with the editor, the loader, or the VM.

These are the invariants it protects:

- `next` and `parent` must remain non-null where the VM expects them.
- `mutation.tagName` and `mutation.children` must be retained so block XML conversion works when the project is opened in the editor.
- sound `md5ext` values and asset IDs must remain consistent so audio still loads.
- project assets, variable/list definitions, comments, costumes, sounds, and monitor entries must not be removed wholesale unless a transform explicitly does so.
- Numeric strings are preserved by default. `--compact-numeric-inputs` explicitly converts eligible canonical decimal input strings, and `--fold-constant-expressions` evaluates numeric literals only inside selected constant arithmetic expressions. These transformations do not globally convert arbitrary strings.

## Verification

After writing the output archive, the script reloads both the original and minified `.sb3` files and verifies that:

- the archive entry set matches the expected result, including any requested WAV-to-MP3 conversions
- unchanged non-`project.json` assets are byte-for-byte identical; converted assets are checked against their new IDs
- the `project.json` structure differs only in the intended ways
- any optional renames or removals are still consistent with Scratch’s reference system
- monitor, block, and procedural state remain valid

If a mismatch is detected, the output file is deleted, and the original archive is left untouched.

## Example workflow

```bash
python minify_sb3.py my_project.sb3
python minify_sb3.py my_project.sb3 my_project_minified.sb3 --all-optimizations
python minify_sb3.py my_project.sb3 --keep-comments --keep-monitors --sort-keys
python minify_sb3.py my_project.sb3 --fold-constant-expressions
python minify_sb3.py my_project.sb3 --convert-wav-to-mp3
```

## Notes for programmatic use

The script exposes an `Options` object and `minify_sb3(src, dst, opts)` entry point. This can be used from Python code when more control is needed than what the CLI offers.
