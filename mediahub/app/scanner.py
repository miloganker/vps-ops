"""扫描待整理目录，生成预览（旧名 → 新名、识别置信度、标记）。

只调用 list。每个目录 refresh 一次：待整理根目录、每个条目文件夹、剧集的季文件夹、两个片库根目录。
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from . import naming as N
from .tmdb import SOURCE_ZH, similarity
from .openlist import Entry, join


def _id(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:10]


def _ents(es: list[Entry]) -> list[dict]:
    return [{"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in es]


class Scanner:
    def __init__(self, client, tmdb, settings, log=print):
        self.c = client
        self.tmdb = tmdb
        self.s = settings
        self.log = log
        self.ad_bytes = int(settings.ad_max_size_mb * 1024 * 1024)

    # ------------------------------------------------------------ 入口
    def scan(self) -> dict[str, Any]:
        items: list[dict] = []
        errors: list[str] = []
        lib_names: dict[str, list[str]] = {}
        for kind, staging, library in (("movie", self.s.staging_movies, self.s.library_movies),
                                       ("tv", self.s.staging_tv, self.s.library_tv)):
            try:
                root = self.c.list(staging, refresh=True)
            except Exception as e:
                errors.append(f"无法列出 {staging}：{e}")
                continue
            if not root:
                continue
            try:
                lib_names[kind] = [e.name for e in self.c.list(library, refresh=True)]
            except Exception as e:
                errors.append(f"无法列出片库 {library}：{e}")
                lib_names[kind] = []
            root_names = [e.name for e in root]
            for e in root:
                if not e.is_dir:
                    if N.is_ad(e.name, e.size, False, self.ad_bytes) or not N.is_video(e.name):
                        continue
                try:
                    if kind == "movie":
                        items.append(self._scan_movie(staging, e))
                    else:
                        items.append(self._scan_tv(staging, e, root_names))
                except Exception as ex:  # 单个条目失败不影响其它
                    errors.append(f"{e.name}：{type(ex).__name__}: {ex}")
        for it in items:
            lib = lib_names.get(it["kind"], [])
            if it["kind"] == "tv":
                # 先按识别结果算出剧名，片库里已有这部剧 → 列一下它有哪些季（只为合并新季用）
                plan(it, self.s)
                if it["target_name"] in lib:
                    try:
                        ents = self.c.list(join(self.s.library_tv, it["target_name"]), refresh=True)
                        it["library_seasons"] = sorted(
                            n for n in (N.is_season_folder_name(e.name) for e in ents if e.is_dir) if n is not None)
                        it["library_show"] = it["target_name"]
                    except Exception as ex:
                        errors.append(f"无法列出片库剧集 {it['target_name']}：{ex}")
                it.pop("include", None)
            plan(it, self.s)
            apply_library(it, lib)
        return {"items": items, "errors": errors, "library": lib_names}

    # ------------------------------------------------------------ 识别
    def _identify(self, it: dict, kind: str, folder: str, sample: Optional[str]) -> None:
        pf = N.parse_name(folder)
        pv = N.parse_name(sample) if sample else N.Parsed()
        cn = pf.cn_title or pv.cn_title
        en = pv.en_title or pf.en_title
        if kind == "tv":
            en = pf.en_title or pv.en_title
        year = pf.year or pv.year
        if pf.year and pv.year and pf.year != pv.year:
            it["flags"].append(f"文件夹年份 {pf.year} 与文件年份 {pv.year} 不一致")
        it["parsed"] = {"cn": cn, "en": en, "year": year}
        it["tmdb"] = None
        it["confidence"] = "none"
        if not (cn or en):
            it["flags"].append("无法从文件名解析出片名")
            return
        m = self.tmdb.match(kind, en, cn, year)
        if m.best:
            it["tmdb"] = {"id": m.best.tmdb_id, "title": m.best.title, "year": m.best.year,
                          "original": m.best.original_title, "en_title": m.best.en_title, "score": m.score,
                          "title_source": m.best.title_source,
                          "zh_alts": [t for _, t in m.best.zh_alts],
                          "alternatives": [{"id": a.tmdb_id, "title": a.title, "year": a.year}
                                           for a in m.alternatives]}
        it["confidence"] = m.confidence
        it.setdefault("notes", [])
        if m.note:
            it["flags"].append(m.note)
        if m.best:
            title = m.best.title
            if self.s.title_source == "folder" and cn:
                title = cn
            elif cn:
                self._check_cn(it, m, cn, en, year)
            if m.best.title_source != "zh-CN" and title == m.best.title:
                it["notes"].append(f"TMDB 没有简体中文标题，片名用的是{SOURCE_ZH.get(m.best.title_source, m.best.title_source)}：{title}")
            it["title"] = title
            # 电影用 TMDB 上映年份；剧集用首播年份
            it["year"] = m.best.year or year
        else:
            it["title"] = cn or en
            it["year"] = year

    @staticmethod
    def _check_cn(it: dict, m, cn: str, en: str, year) -> None:
        """文件夹中文名和 TMDB 中文名（含别名/译名）对不上时：
        - 中文名精确对应另一条 TMDB 结果 → 阻止自动入库；
        - 英文名 + 年份都对上、高置信 → 只做提示（notes），不阻止；
        - 其它情况 → 阻止（flags）。"""
        b = m.best
        names = [b.title, b.original_title] + [t for _, t in b.zh_alts]
        if any(N.cn_title_match(cn, n) for n in names if n):
            return
        msg = f"文件夹中文名是“{cn}”，TMDB 中文名是“{b.title}”"
        for a in m.alternatives:
            if a.tmdb_id != b.tmdb_id and any(N.cn_title_match(cn, n, strict=True)
                                              for n in [a.title] + [t for _, t in a.zh_alts] if n):
                it["flags"].append(msg + f"；这个中文名对应的是另一部：{a.title} ({a.year})")
                return
        en_sim = max(similarity(en, b.original_title), similarity(en, b.en_title), similarity(en, b.title)) if en else 0
        if m.confidence == "high" and year and b.year == year and en_sim >= 0.9:
            it["notes"].append(msg + "（英文名和年份都对得上，只做提示，不影响自动入库）")
        else:
            it["flags"].append(msg)

    # ------------------------------------------------------------ 电影
    def _scan_movie(self, staging: str, e: Entry) -> dict:
        it: dict[str, Any] = {"id": _id("movie", e.name), "kind": "movie", "staging": staging,
                              "folder_old": e.name, "flags": [], "blocking": [], "ignored": []}
        if not e.is_dir:
            it["files"] = []
            it["blocking"].append("视频不在单独的文件夹里（按规则不为电影建文件夹），请手动处理")
            self._identify(it, "movie", e.name, e.name)
            return it
        kids = self.c.list(join(staging, e.name), refresh=True)
        it["files"] = _ents(kids)
        videos = [k for k in kids if not k.is_dir and N.is_video(k.name)
                  and not N.is_ad(k.name, k.size, False, self.ad_bytes)]
        videos.sort(key=lambda k: k.size, reverse=True)
        sample = videos[0].name if videos else None
        self._identify(it, "movie", e.name, sample)
        return it

    # ------------------------------------------------------------ 剧集
    def _scan_tv(self, staging: str, e: Entry, root_names: list[str]) -> dict:
        it: dict[str, Any] = {"id": _id("tv", e.name), "kind": "tv", "staging": staging,
                              "folder_old": e.name, "flags": [], "blocking": [], "ignored": [],
                              "root_names": root_names}
        if not e.is_dir:
            it["files"], it["seasons_raw"] = [], []
            it["blocking"].append("单个剧集文件不在文件夹里，请手动处理")
            self._identify(it, "tv", e.name, e.name)
            return it
        kids = self.c.list(join(staging, e.name), refresh=True)
        it["files"] = _ents(kids)
        it["seasons_raw"] = []
        subdirs = [k for k in kids if k.is_dir]
        sample = None
        for k in kids:
            if not k.is_dir and N.is_video(k.name) and not N.is_ad(k.name, k.size, False, self.ad_bytes):
                sample = k.name
                break
        flat_eps = sample is not None
        if subdirs:
            for d in subdirs:
                sn = N.is_season_folder_name(d.name)
                pn = N.parse_name(d.name)
                if sn is None and pn.season is None and not flat_eps:
                    # 也许是 “Season 1” 以外的命名，列一下看看有没有剧集
                    pass
                if sn is None and pn.season is None and flat_eps:
                    it["ignored"].append(d.name + "/")
                    continue
                sub = self.c.list(join(staging, e.name, d.name), refresh=True)
                it["seasons_raw"].append({"folder": d.name, "season_hint": sn or pn.season,
                                          "files": _ents(sub)})
                if sample is None:
                    for k in sub:
                        if not k.is_dir and N.is_video(k.name) and not N.is_ad(k.name, k.size, False, self.ad_bytes):
                            sample = k.name
                            break
        if flat_eps and it["seasons_raw"]:
            it["blocking"].append("文件夹里同时有散放的剧集和季文件夹，请先手动整理")
        self._identify(it, "tv", e.name, sample)
        return it


# ---------------------------------------------------------------- 片库对照

def apply_library(it: dict, lib_names: list[str]) -> None:
    """根据片库现状决定：正常移入 / 合并新季（电视剧）/ 同名冲突跳过。"""
    it.pop("library_conflict", None)
    it["merge"] = None
    it["lib_flags"] = []
    if it["target_name"] not in lib_names:
        return
    ops = it.get("tv_ops")
    if it["kind"] == "tv" and ops and it.get("library_show") == it["target_name"]:
        have = set(it.get("library_seasons") or [])
        mine = [ops["season"]] if ops["layout"] == "flat" else [s["season"] for s in ops["seasons"]]
        new = [n for n in mine if n not in have]
        dup = [n for n in mine if n in have]
        # 片库已有这部剧：不在待整理里建剧名文件夹，季文件夹直接移进片库的剧集文件夹
        it["merge"] = {"show": it["target_name"], "seasons": new, "skip": dup}
        if ops["layout"] == "flat" and ops["season_folder"] in it.get("root_names", []) \
                and ops["season_folder"] != it["folder_old"]:
            it["plan_blocking"].append(f"待整理根目录里已有“{ops['season_folder']}”，请先手动处理")
            it["executable"] = False
            it["include"] = False
        if new:
            msg = "片库已有这部剧：" + "、".join(N.season_folder(n) for n in new) + " 会直接移进片库里的剧集文件夹"
            if dup:
                msg += "；" + "、".join(N.season_folder(n) for n in dup) + " 片库里已有，会跳过（不合并文件）"
            it["lib_flags"].append(msg)
            return
        it["lib_flags"].append("片库里这部剧已有相同的季：默认不勾选；勾选后只改名、不移动（不合并、不覆盖）")
    else:
        it["lib_flags"].append("片库已有同名文件夹：默认不勾选；勾选后只改名、不移动（不合并、不覆盖）")
    it["library_conflict"] = True
    it["include"] = False


def stage_name(it: dict) -> str:
    """改名阶段结束后，这一项在待整理根目录里应该叫什么。"""
    m = it.get("merge")
    if m is not None and it["kind"] == "tv":
        ops = it["tv_ops"]
        return ops["season_folder"] if ops["layout"] == "flat" else it["folder_old"]
    return it["target_name"]


# ---------------------------------------------------------------- 计划（可在用户修改片名/年份后重新计算）

def plan(it: dict, settings) -> dict:
    """根据 it['title'] / it['year'] 计算所有新名字和操作。会重置自动生成的标记。"""
    ad_bytes = int(settings.ad_max_size_mb * 1024 * 1024)
    it["plan_flags"] = []
    it["plan_blocking"] = []
    title, year = it.get("title"), it.get("year")
    if not title:
        it["plan_blocking"].append("缺少片名")
        title = it["folder_old"]
    it["target_name"] = N.folder_name(title, year)
    if not year:
        it["plan_blocking"].append("缺少年份")
    if it["kind"] == "movie":
        _plan_movie(it, title, year, ad_bytes)
    else:
        _plan_tv(it, title, year, ad_bytes)
    conf = it.get("confidence")
    manual = it.get("manual")
    it["executable"] = not (it["blocking"] or it["plan_blocking"]) and (manual or conf in ("high", "medium"))
    if not manual and conf in ("low", "none"):
        it["plan_blocking"].append("识别置信度低：请核对并填写片名/年份或 TMDB ID 后再执行")
        it["executable"] = False
    if "include" not in it:
        it["include"] = it["executable"] and (manual or conf == "high") and not it.get("library_conflict")
    return it


def _classify(files: list[dict], ad_bytes: int):
    videos, subs, ignored = [], [], []
    for f in files:
        if f["is_dir"]:
            continue
        n, sz = f["name"], f["size"]
        if N.is_video(n):
            (ignored if N.is_ad(n, sz, False, ad_bytes) else videos).append(f)
        elif N.is_subtitle(n):
            (ignored if N.AD_NAME_RE.search(n) else subs).append(f)
        else:
            ignored.append(f)
    return videos, subs, ignored


def _plan_movie(it: dict, title: str, year, ad_bytes: int) -> None:
    videos, subs, ignored = _classify(it.get("files", []), ad_bytes)
    it["ignored"] = [f["name"] for f in ignored] + [f["name"] + "/" for f in it.get("files", []) if f["is_dir"]]
    renames: list[dict] = []
    if not videos:
        dirs = {f["name"].upper() for f in it.get("files", []) if f["is_dir"]}
        if "BDMV" in dirs:
            it["plan_flags"].append("原盘目录结构（BDMV）：只改文件夹名")
        elif it.get("files"):
            it["plan_blocking"].append("文件夹里没有找到视频文件")
    videos.sort(key=lambda f: f["size"], reverse=True)
    folder_tags = N.parse_tags(it["folder_old"])
    main_stem = None
    for v in videos:
        tags = N.parse_tags(v["name"])
        if tags.empty() and not folder_tags.empty():
            tags = folder_tags
            it["plan_flags"].append(f"{v['name']}：文件名没有标签，标签取自文件夹名")
        if tags.resolution_inferred:
            it["plan_flags"].append("原名只有 UHD/4K，分辨率按 2160p 写入")
        ext = N.split_ext(v["name"])[1]
        new = N.movie_file_name(title, year, tags, ext)
        renames.append({"old": v["name"], "new": new, "type": "video"})
        if main_stem is None:
            main_stem = N.split_ext(new)[0]
    if main_stem is None:
        main_stem = N.folder_name(title, year)
    for s in subs:
        stem, ext = N.split_ext(s["name"])
        renames.append({"old": s["name"], "new": f"{main_stem}{N.subtitle_suffix(s['name'])}.{ext}", "type": "subtitle"})
    _check_dupes(it, renames)
    it["renames"] = [r for r in renames if r["old"] != r["new"]]
    it["folder_new"] = it["target_name"]
    it["tv_ops"] = None


def _episode_renames(it, files, title, year, season_default, ad_bytes, label=""):
    videos, subs, ignored = _classify(files, ad_bytes)
    it["ignored"] += [label + f["name"] for f in ignored]
    out, seasons, eps = [], set(), {}
    for v in videos:
        p = N.parse_name(v["name"])
        if p.episode is None:
            it["plan_blocking"].append(f"识别不出集数：{label}{v['name']}")
            continue
        season = p.season or season_default or 1
        seasons.add(season)
        tags = N.parse_tags(v["name"])
        ext = N.split_ext(v["name"])[1]
        new = N.episode_file_name(title, year, season, p.episode, tags, ext, p.episode_end)
        out.append({"old": v["name"], "new": new, "type": "video", "episode": p.episode})
        eps.setdefault((season, p.episode), []).append(N.split_ext(new)[0])
    for s in subs:
        p = N.parse_name(s["name"])
        season = p.season or season_default or 1
        stems = eps.get((season, p.episode)) if p.episode is not None else None
        if not stems:
            it["plan_flags"].append(f"字幕对不上集数，保持原名：{label}{s['name']}")
            continue
        ext = N.split_ext(s["name"])[1]
        out.append({"old": s["name"], "new": f"{stems[0]}{N.subtitle_suffix(s['name'])}.{ext}", "type": "subtitle"})
    out.sort(key=lambda r: (r["type"] != "video", r.get("episode") or 0, r["old"]))
    return out, seasons


def _plan_tv(it: dict, title: str, year, ad_bytes: int) -> None:
    it["ignored"] = [x for x in it.get("ignored", []) if x.endswith("/")]
    pf = N.parse_name(it["folder_old"])
    seasons_raw = it.get("seasons_raw") or []
    show_new = it["target_name"]
    ops: dict[str, Any] = {"show_new": show_new}
    if not seasons_raw:
        renames, seasons = _episode_renames(it, it.get("files", []), title, year, pf.season, ad_bytes)
        if len(seasons) > 1:
            it["plan_blocking"].append(f"同一文件夹里有多季剧集（{sorted(seasons)}），请先按季分开")
        season = (sorted(seasons) or [pf.season or 1])[0]
        ops["layout"] = "flat"
        ops["season"] = season
        ops["season_folder"] = N.season_folder(season)
        if show_new in it.get("root_names", []) and show_new != it["folder_old"]:
            it["plan_blocking"].append(f"待整理里已有“{show_new}”文件夹，请先手动处理")
        if show_new == it["folder_old"] and ops["season_folder"] in it.get("root_names", []):
            it["plan_blocking"].append(f"待整理根目录里已有“{ops['season_folder']}”，请先手动处理")
        if not renames and not it["plan_blocking"]:
            it["plan_blocking"].append("没有找到剧集文件")
        ops["renames"] = renames
        ops["seasons"] = []
        it["episode_count"] = sum(1 for r in renames if r["type"] == "video")
    else:
        ops["layout"] = "seasons"
        ops["seasons"] = []
        total = 0
        used = set()
        for sr in seasons_raw:
            renames, seasons = _episode_renames(it, sr["files"], title, year, sr["season_hint"], ad_bytes,
                                                label=sr["folder"] + "/")
            sn = sr["season_hint"] or (sorted(seasons)[0] if seasons else None)
            if sn is None:
                it["plan_blocking"].append(f"识别不出季号：{sr['folder']}")
                sn = 0
            if len(seasons) > 1:
                it["plan_blocking"].append(f"{sr['folder']} 里混有多季剧集")
            if sn in used:
                it["plan_blocking"].append(f"重复的季：{sr['folder']}")
            used.add(sn)
            total += sum(1 for r in renames if r["type"] == "video")
            ops["seasons"].append({"folder_old": sr["folder"], "season": sn,
                                   "folder_new": N.season_folder(sn),
                                   "renames": [r for r in renames if r["old"] != r["new"]]})
        ops["renames"] = []
        it["episode_count"] = total
    for grp in [ops.get("renames", [])] + [s["renames"] for s in ops["seasons"]]:
        _check_dupes(it, grp)
    ops["renames"] = [r for r in ops.get("renames", []) if r["old"] != r["new"]]
    it["tv_ops"] = ops
    it["renames"] = []
    it["folder_new"] = show_new


def _check_dupes(it: dict, renames: list[dict]) -> None:
    seen: dict[str, str] = {}
    for r in renames:
        if r["new"] in seen:
            it["plan_blocking"].append(f"新文件名重复：{r['new']}（{seen[r['new']]} / {r['old']}）")
        seen[r["new"]] = r["old"]


def estimate_requests(it: dict) -> int:
    """估算执行这一条需要多少次 115 请求（不含最终核对）。"""
    if it["kind"] == "movie":
        n = len(it["renames"]) + (1 if it["folder_new"] != it["folder_old"] else 0)
        return n + 1  # + 移动
    ops = it["tv_ops"] or {}
    m = it.get("merge")
    if m is not None:
        if ops.get("layout") == "flat":
            return len(ops["renames"]) + 1 + 1 + 2   # 季文件夹改名 + 移动 + 移动前/后各列一次片库剧集
        n = sum(len(s["renames"]) + (1 if s["folder_new"] != s["folder_old"] else 0) for s in ops["seasons"])
        return n + len(m["seasons"]) + 2
    if ops.get("layout") == "flat":
        return len(ops["renames"]) + 3 + 1  # mkdir + move + 季文件夹改名 + 移入片库
    n = sum(len(s["renames"]) + (1 if s["folder_new"] != s["folder_old"] else 0) for s in ops.get("seasons", []))
    return n + (1 if it["folder_new"] != it["folder_old"] else 0) + 1
