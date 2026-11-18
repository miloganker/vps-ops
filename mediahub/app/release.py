"""下完之后看 115 里的真实文件，判断电影 / 电视剧。

只读：只调用 list。种子名（顶层文件夹 / 文件名）+ 文件列表给出结构信号，
再用 TMDB 确认或补充。判断不了就返回 kind=None，由调用方让用户点按钮。
"""
from __future__ import annotations

import re
from typing import Optional

from . import naming as N
from .openlist import join

_COMPLETE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(complete|全\s*\d+\s*集|全集)(?![A-Za-z0-9])")
_SAMPLE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(sample|trailer|预告)(?![A-Za-z0-9])")
_EXTRA_DIR_RE = re.compile(r"(?i)^(sample|samples|extras?|featurettes?|花絮|特典|sp|sps|bonus)$")

MAX_SUBDIRS = 8      # 最多再往下列几个子文件夹（控制 115 请求数）


def inspect(client, folder: str, ad_max_bytes: int = 5 * 1024 * 1024) -> dict:
    """列出 folder 下的顶层条目，以及（最多 2 层）里面的视频文件。只读。"""
    top = client.list(folder, refresh=True)
    files: list[dict] = []
    season_dirs: list[str] = []
    listed = 0
    queue = [(folder, e, 0) for e in top]
    while queue:
        parent, e, depth = queue.pop(0)
        p = join(parent, e.name)
        if e.is_dir:
            if N.is_season_folder_name(e.name) is not None:
                season_dirs.append(e.name)
            if depth >= 2 or listed >= MAX_SUBDIRS or _EXTRA_DIR_RE.match(e.name.strip()):
                continue
            listed += 1
            try:
                kids = client.list(p, refresh=False)
            except Exception:
                continue
            queue.extend((p, k, depth + 1) for k in kids)
        else:
            files.append({"name": e.name, "size": e.size, "path": p,
                          "video": N.is_video(e.name),
                          "ad": N.is_ad(e.name, e.size, False, ad_max_bytes)})
    return {
        "folder": folder,
        "top": [{"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in top],
        "files": files,
        "season_dirs": season_dirs,
    }


def _main_videos(files: list[dict]) -> list[dict]:
    vids = [f for f in files if f["video"] and not f["ad"] and not _SAMPLE_RE.search(f["name"])]
    if not vids:
        return []
    biggest = max(f["size"] for f in vids)
    if biggest <= 0:
        return vids
    # 正片：至少是最大文件的 30%（排除花絮、预告）
    return [f for f in vids if f["size"] >= 0.3 * biggest]


def _ep_range(eps: set[tuple[int, int]]) -> str:
    by_season: dict[int, list[int]] = {}
    for s, e in eps:
        by_season.setdefault(s, []).append(e)
    parts = []
    for s in sorted(by_season):
        es = sorted(by_season[s])
        if len(es) == 1:
            parts.append(f"S{s:02d}E{es[0]:02d}")
        else:
            parts.append(f"S{s:02d}E{es[0]:02d}–E{es[-1]:02d}")
    return "、".join(parts)


def _tmdb_best(tmdb, kind: str, parsed) -> Optional[dict]:
    if tmdb is None or not getattr(tmdb, "enabled", True):
        return None
    if not (parsed.en_title or parsed.cn_title):
        return None
    try:
        m = tmdb.match(kind, parsed.en_title, parsed.cn_title, parsed.year)
    except Exception:
        return None
    if not m or not m.best:
        return None
    return {"title": m.best.title, "year": m.best.year, "confidence": m.confidence, "score": m.score}


def _fmt_match(m: Optional[dict]) -> str:
    if not m:
        return ""
    y = f" ({m['year']})" if m.get("year") else ""
    return f"TMDB 匹配 {m['title']}{y}"


def title_source(info: dict) -> str:
    """用来查 TMDB 的名字：唯一的顶层文件夹名，否则最大的视频文件名。"""
    top = info.get("top") or []
    dirs = [t for t in top if t["is_dir"] and not _EXTRA_DIR_RE.match(t["name"].strip())]
    if len(top) == 1 and dirs:
        name = dirs[0]["name"]
        p = N.parse_name(name)
        if p.en_title or p.cn_title:
            return name
    mains = _main_videos(info.get("files") or [])
    if mains:
        return max(mains, key=lambda f: f["size"])["name"]
    return top[0]["name"] if top else ""


def classify_release(info: dict, tmdb) -> dict:
    """返回 {kind, reason, signals, tmdb_movie, tmdb_tv, title_from}。kind 为 None 表示判断不了。"""
    top = info.get("top") or []
    files = info.get("files") or []
    mains = _main_videos(files)
    names = [t["name"] for t in top]

    eps: set[tuple[int, int]] = set()
    for f in mains:
        p = N.parse_name(f["name"])
        if p.episode is not None:
            eps.add((p.season or 1, p.episode))
            if p.episode_end:
                for e in range(p.episode + 1, p.episode_end + 1):
                    eps.add((p.season or 1, e))

    season_names = []
    for n in names:
        p = N.parse_name(n)
        if p.season is not None and p.episode is None:
            season_names.append(f"S{p.season:02d}")
        elif _COMPLETE_RE.search(n):
            season_names.append(_COMPLETE_RE.search(n).group(1))

    src = title_source(info)
    parsed = N.parse_name(src)
    t_movie = _tmdb_best(tmdb, "movie", parsed)
    t_tv = _tmdb_best(tmdb, "tv", parsed)
    mv_hi = bool(t_movie and t_movie["confidence"] == "high")
    tv_hi = bool(t_tv and t_tv["confidence"] == "high")
    tv_ok = bool(t_tv and t_tv["confidence"] in ("high", "medium"))
    mv_ok = bool(t_movie and t_movie["confidence"] in ("high", "medium"))

    out = {"kind": None, "reason": "", "title_from": src, "tmdb_movie": t_movie, "tmdb_tv": t_tv,
           "main_videos": len(mains), "episodes": len(eps)}

    tv_bits = []
    if eps:
        tv_bits.append(f"种子内容含 {_ep_range(eps)}（{len(eps)} 集）")
    if season_names:
        tv_bits.append(f"文件夹名含 {season_names[0]}")
    if info.get("season_dirs"):
        tv_bits.append(f"有季文件夹 {info['season_dirs'][0]}")
    many_unnumbered = len(mains) >= 3 and not eps

    if tv_bits:
        reason = "，".join(tv_bits)
        if tv_ok:
            reason += "，" + _fmt_match(t_tv)
        out.update(kind="tv", reason=reason)
        return out

    if len(mains) == 1:
        p = N.parse_name(mains[0]["name"])
        year = p.year or parsed.year
        if tv_hi and not mv_ok:
            out["reason"] = f"只有 1 个正片文件，但 TMDB 只对上电视剧（{_fmt_match(t_tv)}），需要确认"
            return out
        reason = "只有 1 个正片文件" + (f"，名字含年份 {year}" if year else "")
        if mv_ok:
            reason += "，" + _fmt_match(t_movie)
        elif not year:
            # 单文件、无年份、TMDB 也没找到电影：太弱，问一下
            out["reason"] = "只有 1 个正片文件，但名字里没有年份，TMDB 也没对上电影，需要确认"
            return out
        out.update(kind="movie", reason=reason)
        return out

    if not mains:
        out["reason"] = "下载内容里没有找到视频文件"
        return out

    # 多个正片但没有集号：看 TMDB
    if tv_hi and not mv_hi:
        out.update(kind="tv", reason=f"有 {len(mains)} 个正片文件，" + _fmt_match(t_tv) + "（电视剧）")
        return out
    if mv_hi and not tv_hi and not many_unnumbered:
        out.update(kind="movie", reason=f"有 {len(mains)} 个正片文件（可能是分段），" + _fmt_match(t_movie))
        return out
    out["reason"] = f"有 {len(mains)} 个视频文件但没有集号，TMDB 也没有唯一结果，需要确认"
    return out
