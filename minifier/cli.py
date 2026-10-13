def minify_sb3(src, dst, opts=None):
	opts = opts or Options()

	if os.path.normcase(os.path.realpath(src)) == os.path.normcase(
		os.path.realpath(dst)
	) or (os.path.exists(src) and os.path.exists(dst) and os.path.samefile(src, dst)):
		print(Ansi.error("Error: output path must differ from input path."))
		return 1
	
	if not os.path.isfile(src):
		print(Ansi.error(f'Error: file not found: "{src}"'))
		return 1
	
	try:
		zopfli = (
			get_zopfli(opts.zopfli_required)
			if (opts.zopfli or opts.zopfli_assets)
			else None
		)
	except ValueError as error:
		print(Ansi.error(f"Error: {error}"))
		return 1

	with zipfile.ZipFile(src) as zin:
		if "project.json" not in zin.namelist():
			print(Ansi.error("Error: no project.json in archive"))
			return 1
		
		raw = zin.read("project.json")
		project = json.loads(raw.decode("utf-8"))
		
		if opts.lists:
			candidates = find_large_lists(project, opts.list_bytes, opts.list_items)
			picked = prompt_for_lists(project, candidates, len(raw))
			if picked is None:
				print(Ansi.warning("Nothing was written."))
				return 130
			opts.cleared_lists = frozenset(picked)
		
		if opts.fold_constant_variables:
			candidates = get_constant_variables(project)
			picked = prompt_for_constant_variables(project, candidates)
			
			if picked is None:
				print(Ansi.warning("Nothing was written."))
				return 130
			
			opts.folded_constant_variables = picked
			opts.folded_constant_variable_literals = dict(picked)
			opts.folded_constant_variable_setters = {
				(c["ti"], c["id"]): (c["setter_ti"], c["setter"])
				for c in candidates
				if (c["ti"], c["id"]) in picked
			}

		asset_infos = {
			item.filename: item
			for item in zin.infolist()
			if item.filename != "project.json"
		}
		assets = {
			item.filename: zin.read(item.filename)
			for item in zin.infolist()
			if item.filename != "project.json"
		}

		print("\n" + Ansi.prompt("--- BEGIN ---") + "\n")
		progress = ProgressBar(progress_stage_count(opts))
		stats = apply_transforms(project, opts, assets, progress)
		
		if opts.relabel_block_ids:
			progress.next("Minimize block IDs")
			project, label_maps, label_cost = relabel_blocks(project)
			previous_maps = getattr(opts, "renamed_block_ids", {})
			opts.renamed_block_ids = {
				ti: (
					{
						old: label_maps.get(ti, {}).get(new, new)
						for old, new in previous_maps[ti].items()
					}
					if ti in previous_maps
					else mapping
				)
				for ti, mapping in label_maps.items()
			}
			opts.rename_block_ids = True
			stats["block_id_bytes_saved"] = label_cost["before"] - label_cost["after"]
		
		out_json = dumps_compact(project, opts.sort_keys).encode("utf-8")
		compressed_json = None
		
		if opts.optimize_json:
			progress.next("Optimize project.json")
			out_json, compressed_json, encoding_stats = optimize_project_json(
				loads_exact(out_json),
				opts.compression_level,
				opts.zopfli,
				opts.zopfli_iterations,
				opts.zopfli_required,
				opts.compact_block_defaults,
				opts.compact_costume_references,
				opts.compact_block_flags,
				opts.minimum_json,
				opts.json_search_rounds,
				False,
				opts.strip_reference_names,
				opts.prune_orphan_arguments,
				opts.compact_procedure_symbols,
				opts.compact_reporter_defaults,
				opts.fast_json,
				opts.strip_covered_shadows,
				opts.strip_editor_comments,
				opts.strip_script_positions,
				opts.prune_unused_data,
				opts.rebuild_procedure_displays,
				opts.compact_terminal_links,
			)
			stats.update(
				{
					key: value
					for key, value in encoding_stats.items()
					if isinstance(value, int)
				}
			)

		progress.next(
			"Process assets and write archive"
			if opts.optimize_assets
			else "Write archive"
		)
		with zipfile.ZipFile(
			dst, "w", zipfile.ZIP_DEFLATED, compresslevel=opts.compression_level
		) as zout:
			if compressed_json is None:
				zout.writestr("project.json", out_json)
			else:
				json_info = zipfile.ZipInfo("project.json")
				json_info.compress_type = zipfile.ZIP_DEFLATED
				json_info.file_size = len(out_json)
				json_info.CRC = zlib.crc32(out_json)
				write_compressed_entry(zout, json_info, compressed_json)
			asset_count = len(assets)
			last_progress_render = time.monotonic()
			for asset_index, (name, data) in enumerate(assets.items(), 1):
				if (
					opts.optimize_assets
					and time.monotonic() - last_progress_render >= 0.15
				):
					progress.detail(f"{asset_index:,}/{asset_count:,} assets")
					last_progress_render = time.monotonic()
				if opts.optimize_assets:
					_ext = os.path.splitext(name)[1].lower()
					if (
						_ext
						in (
							".png",
							".jpg",
							".jpeg",
							".gif",
							".webp",
							".mp3",
							".ogg",
							".m4a",
							".aac",
						)
						and name in asset_infos
					):
						info = copy.copy(asset_infos[name])
						previous = read_compressed_entry(zin, info)
						write_compressed_entry(zout, info, previous)
						continue
					info = (
						copy.copy(asset_infos[name])
						if name in asset_infos
						else zipfile.ZipInfo(name)
					)
					original = None
					if name in asset_infos and zin.read(name) == data:
						original = (
							info.compress_type,
							read_compressed_entry(zin, info),
						)
					asset_zopfli = (
						zopfli
						if opts.zopfli_assets
						and (not opts.auto_zopfli or opts.zopfli_required)
						else False
					)
					method, compressed = optimize_asset(
						data,
						opts.compression_level,
						original,
						asset_zopfli,
						opts.zopfli_iterations,
					)
					info.compress_type = method
					info.file_size = len(data)
					info.CRC = zlib.crc32(data)
					write_compressed_entry(zout, info, compressed)
					continue
				written = False
				if (
					opts.preserve_asset_compression
					and name in asset_infos
					and zin.read(name) == data
				):
					orig_info = asset_infos[name]
					if orig_info.compress_type == zipfile.ZIP_DEFLATED:
						py_comp_size = (
							len(zlib.compress(data, opts.compression_level)) - 6
						)
						if orig_info.compress_size <= py_comp_size:
							zin.fp.seek(orig_info.header_offset)
							header = zin.fp.read(30)
							fn_len = int.from_bytes(header[26:28], "little")
							extra_len = int.from_bytes(header[28:30], "little")
							zin.fp.seek(
								orig_info.header_offset + 30 + fn_len + extra_len
							)
							raw_comp = zin.fp.read(orig_info.compress_size)

							zinfo = zipfile.ZipInfo(name)
							zinfo.date_time = orig_info.date_time
							zinfo.compress_type = zipfile.ZIP_DEFLATED
							zinfo.CRC = orig_info.CRC
							zinfo.file_size = orig_info.file_size
							zinfo.compress_size = orig_info.compress_size
							zinfo.header_offset = zout.fp.tell()

							zout.fp.write(zinfo.FileHeader())
							zout.fp.write(raw_comp)
							zout.filelist.append(zinfo)
							zout.NameToInfo[name] = zinfo
							zout.start_dir = zout.fp.tell()
							written = True
				if not written:
					zout.writestr(
						name,
						data,
						compress_type=zipfile.ZIP_DEFLATED,
						compresslevel=opts.compression_level,
					)

	progress.next("Verify output")
	ok, msg = verify(src, dst, opts)
	
	if not ok:
		progress.detail(f"failed: {msg}")
		progress.close()
		print(Ansi.error(f"Verification failed: {msg}"))
		os.remove(dst)
		print(Ansi.muted("Output deleted."))
		return 2
	
	progress.finish("Complete")
	print(Ansi.success("Verified successfully."))

	print(f'Input : "{src}"\nOutput: "{dst}"\n')
	print_transform_stats(stats, opts)

	b, a = len(raw), len(out_json)
	print()

	for line in format_size_report(b, a, os.path.getsize(src), os.path.getsize(dst)):
		print(line)
	return 0


def _render_layout(parts, order=None, shapes=None):
	if shapes is None:
		shapes = {}
	return b"".join(
		part if isinstance(part, bytes) else part.render(order, shapes)
		for part in parts
	)


def _dangling_block_ids(target):
	blocks = target.get("blocks", {})
	ids = set(blocks)
	dangling = set()
	for block in blocks.values():
		if not isinstance(block, dict):
			continue
		for key in ("next", "parent"):
			v = block.get(key)
			if isinstance(v, str) and v not in ids:
				dangling.add(v)
		for value in block.get("inputs", {}).values():
			collect_dangling_in_input(value, ids, dangling)
	for comment in target.get("comments", {}).values():
		if isinstance(comment, dict):
			bid = comment.get("blockId")
			if isinstance(bid, str) and bid not in ids:
				dangling.add(bid)
	return dangling


if __name__ == "__main__":
	raw_flags: list[str] = [a for a in sys.argv[1:] if a.startswith("--")]
	args: list[str] = [a for a in sys.argv[1:] if not a.startswith("--")]

	opts: Options = build_options(raw_flags, args)
	dst: str = args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + "_minified.sb3"
	if os.path.abspath(args[0]) == os.path.abspath(dst):
		print(Ansi.error("Error: output path must differ from input path."))
		sys.exit(1)
	sys.exit(minify_sb3(args[0], dst, opts))
