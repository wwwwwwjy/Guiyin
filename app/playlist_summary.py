"""歌单只读关联与总览口径；配置、执行台账和飞牛现存歌单各有独立含义。"""


def resolve_target(user, name, guid, playlists):
    """优先使用同账户 GUID，缺少有效 GUID 时才按同账户唯一名称关联。"""
    rows = playlists.get(user)
    if rows is None:
        return None
    if guid:
        found = next((p for p in rows if p.get("guid") == guid), None)
        if found is not None:
            return found
    matches = [p for p in rows if p.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def coverage_row(merged_tracks, match_results):
    """把一次「来源去重 + 本地曲库匹配」压成总览要的两个数。

    merged = 融合去重后的唯一曲目数，作为占比的分母；
    local  = 其中能在本地曲库找到的唯一歌曲数，作为分子。
    多个来源匹配到同一首本地歌曲时只算一次，所以要用 guid 去重。
    未匹配（miss）和疑似翻唱/伴奏（cover）都不算“本地已有”。
    """
    guids = set()
    for row in match_results or []:
        if not isinstance(row, dict) or row.get("status") not in ("auto", "version"):
            continue
        # 歌手明确不同（同名不同人）不算“本地已有”：那是另一首歌，
        # 只是标题和时长撞上了，不能算进分子。缺歌手信息时无法判断，仍按已有处理。
        if row.get("artist") == "differ":
            continue
        guid = (row.get("local") or {}).get("guid")
        if guid:
            guids.add(guid)
    missing = sum(1 for row in match_results or []
                  if isinstance(row, dict) and row.get("status") == "miss")
    cover = sum(1 for row in match_results or []
                if isinstance(row, dict) and (row.get("status") == "cover"
                    or (row.get("status") in ("auto", "version") and row.get("artist") == "differ")))
    return {"merged": len(merged_tracks or []), "local": len(guids),
            "missing": missing, "cover": cover}


def coverage_plan(items, sigs, now, retry=300, ttl=3600):
    """决定哪些融合组能直接用缓存、哪些要重算、要不要让前端继续轮询。

    items: {key: row}，row 含 sig / merged / ok_at / try_at / error
    sigs:  {key: 当前来源配置与统计口径版本的指纹}
    返回 (ready, todo, pending)：
      ready 只放指纹与当前配置一致的旧结果 —— 换了来源还在重算的组，
            不能在重算期间继续显示上一套百分比；
      todo  本轮要重算的 key；
      pending 为真表示还有「第一次算/配置变了」的组在等结果，前端据此轮询；
            一直失败、正在退避的组不算，免得每 2 秒白转一次。
    """
    ready, todo, waiting = {}, [], set()
    for key, sig in (sigs or {}).items():
        row = items.get(key) if isinstance(items, dict) and isinstance(items.get(key), dict) else {}
        # 换配置连上次失败的退避也失效；不能拿旧来源的错误阻止新来源计算。
        if row.get("sig") != sig:
            row = {}
        fresh = row.get("merged") is not None and row.get("local") is not None
        if row.get("error"):
            ready[key] = row  # 失败原因也要给前端，但数字未知仍保留 None。
        attempted = float(row.get("try_at") or 0)
        if fresh:
            ready[key] = row
            if row.get("error"):
                if now - attempted >= retry:
                    todo.append(key)
                else:
                    waiting.add(key)
            elif now - float(row.get("ok_at") or 0) >= ttl:
                todo.append(key)
        elif now - attempted >= (retry if row.get("error") else 0):
            # 从没算过、或配置/曲库变了：立即重算；失败过的等退避时间到。
            todo.append(key)
        else:
            waiting.add(key)
    return ready, todo, any(key not in waiting for key in todo)


def merge_comparisons(provenance, groups, playlists, source_counts, coverage=None):
    """以融合配置为主补齐总览，并保留有执行台账的旧目标。

    coverage 有值（来自一次来源抓取 + 本地曲库匹配）时，分母用融合去重后的
    唯一曲目数、分子用其中本地曲库已有的唯一歌曲数；没有 coverage 才退回
    「来源合计 / 飞牛歌单实际」的旧口径，并用 coverage_ready=False 明确标注，
    免得把未去重的来源之和当成完成度。
    """
    records = {}
    for row in provenance:
        if not isinstance(row, dict):
            continue
        user, name = row.get("fn_user") or "", row.get("name") or ""
        if not user or not name:
            continue
        key = (user, name)
        # 同名目标从单源升级融合后，以融合执行台账为准。
        if key not in records or row.get("source") == "merge":
            records[key] = row
    configured = {(g.get("fn_user") or "", g.get("name") or ""): g
                  for g in groups if isinstance(g, dict) and g.get("fn_user") and g.get("name")}
    keys = list(configured) + [k for k, r in records.items() if k not in configured
                              and (r.get("导入曲目") is not None or r.get("实际曲目") is not None)]
    result = []
    for user, name in keys:
        record = records.get((user, name), {})
        group = configured.get((user, name))
        live = resolve_target(user, name, record.get("guid"), playlists)
        sources = (group or record).get("sources") or []
        counts = [source_counts.get((user, s.get("platform"), str(s.get("id")))) for s in sources]
        source_count = sum(counts) if counts and all(c is not None for c in counts) else record.get("导入曲目")
        # 当前飞牛存在且已经读取到的数量优先；读取失败不会回填成零。
        actual = live.get("count") if live is not None else None
        if actual is None and record.get("实际曲目") is not None:
            actual = record["实际曲目"]
        state = "present" if live is not None else "unknown" if user not in playlists else "pending"
        cov = coverage.get(f"{user}|{name}") if isinstance(coverage, dict) else None
        ready = bool(isinstance(cov, dict) and cov.get("merged") is not None
                     and cov.get("local") is not None)
        if ready:
            # 分母/分子都换成“去重后唯一曲目”，跨平台重复不再算进缺口。
            source_count, actual = int(cov["merged"]), int(cov["local"])
        result.append({"user": user, "name": name, "source_count": source_count,
                       "actual_count": actual, "state": state,
                       "executed": bool(record), "coverage_ready": ready,
                       "library_missing": cov.get("missing") if ready else None,
                       "library_cover": cov.get("cover") if ready else None,
                       "coverage_at": cov.get("ok_at") if ready else None,
                       "library_at": cov.get("library_at") if ready else None,
                       "coverage_error": (cov.get("error") or "") if isinstance(cov, dict) else "",
                       "target_guid": (live or {}).get("guid") or record.get("guid") or ""})
    return result
