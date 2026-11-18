"""TMDB 查询：支持 v3 API Key（api_key 参数）和 v4 读取令牌（Authorization: Bearer）。"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import requests

API = "https://api.themoviedb.org/3"


def key_type(key: str) -> str:
    return "v4" if key.startswith("eyJ") else "v3"


@dataclass
class Candidate:
    tmdb_id: int
    title: str           # zh-CN 标题
    original_title: str
    year: Optional[int]
    popularity: float = 0.0
    score: float = 0.0
    en_title: str = ""
    exact: bool = False
    # TMDB 上的各种中文名：[(来源, 标题)]，来源如 "zh-CN" "alt-CN" "zh-SG" "zh-HK" "zh-TW" "alt-TW"
    zh_alts: list = field(default_factory=list)
    title_source: str = "zh-CN"   # 显示标题来自哪里；zh-CN = TMDB 简体中文标题


@dataclass
class Match:
    best: Optional[Candidate]
    confidence: str      # high / medium / low / none
    score: float
    alternatives: list[Candidate]
    note: str = ""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\W_]+", "", s)
    return s


CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# 选显示标题时中文名的优先级：简体（大陆别名、新加坡译名）优先，繁体其次
_ZH_ORDER = ["zh-CN", "alt-CN", "zh-SG", "alt-SG", "zh-HK", "alt-HK", "zh-TW", "alt-TW"]
_SIMPLIFIED = {"zh-CN", "alt-CN", "zh-SG", "alt-SG"}
SOURCE_ZH = {"zh-CN": "TMDB 简体中文标题", "alt-CN": "TMDB 大陆别名", "zh-SG": "TMDB 新加坡中文标题",
             "alt-SG": "TMDB 新加坡别名", "zh-HK": "TMDB 香港中文标题", "alt-HK": "TMDB 香港别名",
             "zh-TW": "TMDB 台湾中文标题", "alt-TW": "TMDB 台湾别名", "folder": "文件夹中文名"}


KANA_RE = re.compile(r"[\u3040-\u30ff]")


def has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s or ""))


def is_chinese_title(s: str) -> bool:
    """有汉字、且不是日文假名标题（TMDB 缺简体名时会退回日文原名，如“七人の侍”）。"""
    return has_cjk(s) and not KANA_RE.search(s or "")


def apply_chinese_title(c: "Candidate", cn: str = "") -> "Candidate":
    """zh-CN 标题是空的时（TMDB 会退回英文原名），按优先级换成别的中文名。

    简体（大陆别名 > 新加坡）> 文件夹里的中文名（如果和 TMDB 的某个中文名对得上，或 TMDB 没有简体名）
    > 繁体（香港 > 台湾）> 保持原样。"""
    if is_chinese_title(c.title) or not c.zh_alts:
        if not is_chinese_title(c.title) and cn and has_cjk(cn):
            c.title, c.title_source = cn, "folder"   # TMDB 完全没有中文名：用文件夹中文名
        return c
    from .naming import cn_title_match
    by_src: dict[str, str] = {}
    for src, t in c.zh_alts:
        if t and is_chinese_title(t):
            by_src.setdefault(src, t)
    for src in _ZH_ORDER:
        if src in _SIMPLIFIED and src in by_src:
            c.title, c.title_source = by_src[src], src
            return c
    if cn and has_cjk(cn):
        c.title, c.title_source = cn, "folder"
        return c
    for src in _ZH_ORDER:
        if src in by_src:
            c.title, c.title_source = by_src[src], src
            return c
    return c


def similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class TMDB:
    def __init__(self, key: str, timeout: float = 15.0, session: Optional[requests.Session] = None):
        self.key = key
        self.timeout = timeout
        self.s = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, **params) -> dict:
        headers = {"accept": "application/json"}
        if key_type(self.key) == "v4":
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        r = self.s.get(API + path, params=params, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def check(self) -> bool:
        try:
            self._get("/configuration")
            return True
        except Exception:
            return False

    # ---- 搜索
    def search(self, kind: str, query: str, year: Optional[int]) -> list[Candidate]:
        """zh-CN 拿中文标题，en-US 拿英文标题（用于和英文文件名比对），按 id 合并。"""
        path = "/search/movie" if kind == "movie" else "/search/tv"
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        zh = self._get(path, language="zh-CN", **params).get("results", [])[:10]
        if not zh:
            return []
        en = {r["id"]: r for r in self._get(path, language="en-US", **params).get("results", [])[:20]}
        out = []
        for r in zh:
            c = self._cand(kind, r)
            e = en.get(r["id"])
            if e:
                c.en_title = e.get("title") or e.get("name") or ""
            out.append(c)
        return out

    def details(self, kind: str, tmdb_id: int, cn: str = "") -> Candidate:
        path = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        c = self._cand(kind, self._get(path, language="zh-CN"))
        c.score, c.exact = 1.0, True
        self.enrich(kind, c, cn)
        return c

    def chinese_titles(self, kind: str, tmdb_id: int) -> list[tuple[str, str]]:
        """translations 里的 zh-* 标题 + alternative_titles 里 CN/SG/HK/TW 的别名。"""
        base = f"/movie/{tmdb_id}" if kind == "movie" else f"/tv/{tmdb_id}"
        out: list[tuple[str, str]] = []
        tr = self._get(base + "/translations").get("translations", [])
        for x in tr:
            if x.get("iso_639_1") == "zh":
                d = x.get("data") or {}
                t = (d.get("title") or d.get("name") or "").strip()
                if t:
                    out.append((f"zh-{x.get('iso_3166_1')}", t))
        alt = self._get(base + "/alternative_titles")
        for x in alt.get("titles") or alt.get("results") or []:
            reg = x.get("iso_3166_1")
            t = (x.get("title") or "").strip()
            if reg in ("CN", "SG", "HK", "TW") and t:
                out.append((f"alt-{reg}", t))
        return out

    def enrich(self, kind: str, c: Candidate, cn: str = "") -> Candidate:
        """zh-CN 标题缺失（退回成英文）时，补查其它中文名。只在需要时多发 2 个请求。"""
        if is_chinese_title(c.title):
            return c
        try:
            c.zh_alts = self.chinese_titles(kind, c.tmdb_id)
        except Exception:
            c.zh_alts = []
        return apply_chinese_title(c, cn)

    @staticmethod
    def _cand(kind: str, r: dict) -> Candidate:
        if kind == "movie":
            title, orig, date = r.get("title", ""), r.get("original_title", ""), r.get("release_date", "")
        else:
            title, orig, date = r.get("name", ""), r.get("original_name", ""), r.get("first_air_date", "")
        y = int(date[:4]) if date and date[:4].isdigit() else None
        return Candidate(int(r["id"]), title or orig, orig, y, float(r.get("popularity") or 0))

    def match(self, kind: str, en: str, cn: str, year: Optional[int]) -> Match:
        """按英文名/中文名 + 年份查询并打分。"""
        pool: dict[int, Candidate] = {}
        queries = [q for q in (en, cn) if q]
        for q in queries:
            for y in ([year, None] if year else [None]):
                try:
                    res = self.search(kind, q, y)
                except Exception as e:  # 网络问题：不猜
                    return Match(None, "none", 0.0, [], f"TMDB 查询失败：{type(e).__name__}")
                for c in res:
                    pool.setdefault(c.tmdb_id, c)
                if res:
                    break
        m = score_candidates(list(pool.values()), en, cn, year, kind)
        if m.best:
            self.enrich(kind, m.best, cn)
        return m


def score_candidates(cands: list[Candidate], en: str, cn: str, year: Optional[int], kind: str = "movie") -> Match:
    if not cands:
        return Match(None, "none", 0.0, [], "TMDB 没有找到结果")
    maxpop = max((c.popularity for c in cands), default=1.0) or 1.0
    for c in cands:
        sim = max(similarity(en, c.original_title), similarity(en, c.title), similarity(en, c.en_title),
                  similarity(cn, c.title), similarity(cn, c.original_title))
        c.exact = sim >= 0.999
        cn_exact = bool(cn) and similarity(cn, c.title) >= 0.999
        en_sim = max(similarity(en, c.original_title), similarity(en, c.en_title), similarity(en, c.title))
        cn_sim = max(similarity(cn, c.title), similarity(cn, c.original_title))
        c.both = bool(en and cn) and en_sim >= 0.9 and cn_sim >= 0.9  # type: ignore[attr-defined]
        pop = 0.05 * (c.popularity / maxpop)
        if year:
            ys = 1.0 if c.year == year else (0.6 if c.year and abs(c.year - year) == 1 else 0.0)
            c.score = round(0.65 * sim + 0.3 * ys + pop, 3)
        else:
            c.score = round(0.95 * sim + pop, 3)
        c.cn_exact = cn_exact  # type: ignore[attr-defined]
    cands.sort(key=lambda c: (c.score, c.exact), reverse=True)
    best = cands[0]
    second = cands[1] if len(cands) > 1 else None
    margin = best.score - (second.score if second else 0.0)
    unique_exact = best.exact and not (second and second.exact)
    if best.score >= 0.85 and (margin >= 0.1 or second is None or unique_exact):
        conf = "high"
    elif best.score >= 0.7 and (margin >= 0.05 or unique_exact):
        conf = "medium"
    elif best.exact and second is not None and getattr(best, "cn_exact", False) \
            and not getattr(second, "cn_exact", False):
        conf = "medium"  # 例如“请回答1988”和“请回答1988（国语版）”：中文名完全一致的优先，但要人工确认"
    else:
        conf = "low"
    note = ""
    if kind == "movie" and not year and conf == "high" and not getattr(best, "both", False):
        # 电影文件名里没有年份，又只有一个名字对得上（例如“新建文件夹”恰好是某部片名）：要人工确认
        conf = "medium"
        note = "文件名里没有年份，请确认"
    if conf != "high" and second is not None:
        note = (note + "；" if note else "") + f"有相近条目：{second.title} ({second.year})"
    if year and best.year and best.year != year:
        note = (note + "；" if note else "") + f"年份不一致：文件名 {year}，TMDB {best.year}"
    return Match(best, conf, best.score, cands[1:4], note)


class FakeTMDB:
    """测试/演示用：从字典返回固定结果。"""

    def __init__(self, table: dict[tuple[str, str], Candidate] | None = None):
        self.table = table or {}
        self.key = "fake"

    enabled = True

    def match(self, kind: str, en: str, cn: str, year: Optional[int]) -> Match:
        found: dict[int, Candidate] = {}
        for (k, q), c in self.table.items():
            if k == kind and q in (_norm(en), _norm(cn)):
                found.setdefault(c.tmdb_id, Candidate(**vars(c)))
        cands = list(found.values())
        m = score_candidates(cands, en, cn, year, kind)
        if m.best:
            apply_chinese_title(m.best, cn)
        return m

    def details(self, kind: str, tmdb_id: int, cn: str = "") -> Candidate:
        for (k, _), c in self.table.items():
            if k == kind and c.tmdb_id == tmdb_id:
                return apply_chinese_title(Candidate(**vars(c)), cn)
        raise KeyError(tmdb_id)


def demo_fake_tmdb() -> FakeTMDB:
    rows = [
        ("movie", "Raging Bull", 1578, "愤怒的公牛", "Raging Bull", 1980),
        ("movie", "Seven Samurai", 346, "七武士", "七人の侍", 1954),
        ("movie", "Dune Part Two", 693134, "沙丘2", "Dune: Part Two", 2024),
        ("movie", "Echoes Of The Rainbow", 39693, "岁月神偷", "歲月神偷", 2010),
        ("tv", "Breaking Bad", 1396, "绝命毒师", "Breaking Bad", 2008),
        ("tv", "Reply 1988", 64010, "请回答1988", "응답하라 1988", 2015),
    ]
    t = {}
    for kind, q, i, zh, orig, y in rows:
        t[(kind, _norm(q))] = Candidate(i, zh, orig, y, 50.0, en_title=q)
        t[(kind, _norm(zh))] = Candidate(i, zh, orig, y, 50.0, en_title=q)
    return FakeTMDB(t)
