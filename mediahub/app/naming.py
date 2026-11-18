"""文件名解析与新文件名生成。

规则（与手动整理时一致）：
- 电影文件夹：`中文片名 (年份)`
- 电影文件：`中文片名 (年份) <分辨率>.<视频质量>.<音频编码+声道>.<ext>`
- 剧集：`剧名 (年份)/Season 01/剧名 (年份) S01E01 <标签>.<ext>`
- 只保留原名里真实出现的标签；去掉片源、视频编码、发布组、网站广告。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

VIDEO_EXTS = {"mkv", "mp4", "m2ts", "ts", "avi", "mov", "wmv", "iso", "rmvb", "flv", "webm", "m4v"}
SUB_EXTS = {"srt", "ass", "ssa", "sup", "vtt", "idx", "sub"}

AD_NAME_RE = re.compile(
    r"(?i)(www\.|\.com\b|\.net\b|\.xyz\b|\.cc\b|\.org\b|更多.{0,12}(下载|请访问|访问)|请访问|BBQDDQ)"
)

CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SEP_RE = re.compile(r"[.\s_]+")
_B = r"(?<![A-Za-z0-9])"   # 左边界
_E = r"(?![A-Za-z0-9])"    # 右边界

RES_RE = re.compile(_B + r"(4320p|2160p|1440p|1080p|1080i|720p|576p|480p)" + _E, re.I)
RES_ALIAS_RE = re.compile(_B + r"(4K|UHD)" + _E, re.I)
FPS_RE = re.compile(_B + r"(\d{2,3})\s?FPS" + _E, re.I)
DV_RE = re.compile(_B + r"(DV|DoVi|Dolby[ .]?Vision)" + _E, re.I)
HDRP_RE = re.compile(_B + r"(HDR10\+|HDR10Plus)(?![A-Za-z0-9+])", re.I)
HDR_RE = re.compile(_B + r"(HDR10|HDR)" + r"(?![A-Za-z0-9+])", re.I)
SDR_RE = re.compile(_B + r"SDR" + _E, re.I)
ATMOS_RE = re.compile(_B + r"Atmos" + _E, re.I)

# 音频编码（顺序很重要：长的写在前面），后面可以紧跟声道数
_AUDIO_ALTS = [
    (r"DTS[-.]?HD[-. ]?MA", "DTS-HD.MA"),
    (r"DTS[-.]?HD[-. ]?HRA", "DTS-HD.HRA"),
    (r"DTS[-:.]?X", "DTS-X"),
    (r"DTS[-.]?HD", "DTS-HD"),
    (r"DTS", "DTS"),
    (r"TrueHD", "TrueHD"),
    (r"DDP|DD\+|E-?AC-?3", "DDP"),
    (r"AC-?3", "AC3"),
    (r"DD", "DD"),
    (r"FLAC", "FLAC"),
    (r"AAC", "AAC"),
    (r"L?PCM", "LPCM"),
    (r"OPUS", "OPUS"),
    (r"MP3", "MP3"),
]
AUDIO_RE = re.compile(
    _B + r"(" + "|".join(f"(?:{p})" for p, _ in _AUDIO_ALTS) + r")(?:[ .]?(\d\.\d))?(?![A-Za-z0-9+])",
    re.I,
)
_AUDIO_CANON = [(re.compile(r"^(?:" + p + r")$", re.I), c) for p, c in _AUDIO_ALTS]

EP_RE = re.compile(r"(?i)" + _B + r"S(\d{1,2})[ .]?E(\d{1,3})(?:-?E(\d{1,3}))?" + _E)
EP_ONLY_RE = re.compile(r"(?i)" + _B + r"(?:E|EP)(\d{1,3})" + _E)
EP_CN_RE = re.compile(r"第\s*(\d{1,3})\s*[集话話]")
SEASON_RE = re.compile(r"(?i)" + _B + r"S(\d{1,2})" + _E)
SEASON_WORD_RE = re.compile(r"(?i)" + _B + r"Season[ ._]?(\d{1,2})" + _E)
SEASON_CN_RE = re.compile(r"第\s*([一二三四五六七八九十百\d]+)\s*季")

YEAR_RE = re.compile(r"^(19[0-9]{2}|20[0-9]{2})$")

# 出现这些 token 说明“片名部分”已经结束
HARD_TECH_RE = re.compile(
    r"(?i)^(\d{3,4}[pi]|4K|UHD|HDR10\+?|HDR|SDR|DV|DoVi|\d{2,3}FPS|WEB-?DL|WEB|WEBRip|BluRay|Blu-ray|BDRip|BDRemux|"
    r"Remux|HDTV|DVDRip|NF|AMZN|Amazon|DSNP|ATVP|HMAX|iT|x26[45]|H26[45]|HEVC|AVC|10bit|"
    r"REPACK|PROPER|EXTENDED|IMAX|Criterion|CC|USA|JAPANESE|CHINESE|KOREAN|V\d|"
    r"DTS.*|TrueHD.*|Atmos|DDP.*|AAC.*|FLAC.*|AC3.*|\d+Audios?)$"
)
SEASON_TOKEN_RE = re.compile(r"(?i)^(S\d{1,2}(-S?\d{1,2})?|Season\d{0,2}|第.+季|全\d+集|Complete)$")
EP_TOKEN_RE = re.compile(r"(?i)^(S\d{1,2}E\d{1,3}(-?E\d{1,3})?|E\d{1,3}|EP\d{1,3}|第\d+[集话話])$")

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

LANG_TOKENS = {
    "chs", "cht", "chi", "zh", "zho", "zh-cn", "zh-hans", "zh-hant", "zh-tw", "zh-hk", "sc", "tc",
    "eng", "en", "jpn", "ja", "jp", "kor", "ko", "fre", "fr", "ger", "de", "spa", "es", "ita", "rus",
    "简体", "繁体", "简中", "繁中", "简英", "繁英", "中英", "双语", "chs&eng", "cht&eng", "chs_eng", "cht_eng",
    "forced", "default", "sdh", "cc",
}

ILLEGAL_RE = re.compile(r'[\\/:*?"<>|]')


def cn_number(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return CN_NUM.get(a, 0) * 10 + (CN_NUM.get(b, 0) if b else 0)
    return CN_NUM.get(s)


def split_ext(name: str) -> tuple[str, str]:
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        if 1 <= len(ext) <= 5 and re.fullmatch(r"[A-Za-z0-9]+", ext):
            return stem, ext
    return name, ""


def is_video(name: str) -> bool:
    return split_ext(name)[1].lower() in VIDEO_EXTS


def is_subtitle(name: str) -> bool:
    return split_ext(name)[1].lower() in SUB_EXTS


def is_ad(name: str, size: int, is_dir: bool = False, ad_max_bytes: int = 5 * 1024 * 1024) -> bool:
    """广告文件：文件名带网址/“更多下载”，或视频扩展名但体积很小。永远不改名、不删除、不计入集数。"""
    if is_dir:
        return False
    stem, ext = split_ext(name)
    # 只看去掉【】后的部分以外是否“全是广告”——带网址的文件名本身就当广告
    if AD_NAME_RE.search(name):
        return True
    if ext.lower() in VIDEO_EXTS and 0 <= size < ad_max_bytes:
        return True
    return False


def strip_brackets(s: str) -> str:
    s = re.sub(r"【[^】]*】", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"［[^］]*］", " ", s)
    s = re.sub(r"「[^」]*」", " ", s)
    return s.strip(" .-_")


# ---------------------------------------------------------------- 技术标签

@dataclass
class Tags:
    resolution: str = ""
    resolution_inferred: bool = False   # 只有 UHD/4K 没写 2160p 时推断
    video: list[str] = field(default_factory=list)   # 60FPS / DV / HDR / SDR
    audio: list[str] = field(default_factory=list)   # Atmos / TrueHD / 7.1 ...

    def fps(self) -> list[str]:
        return [v for v in self.video if v.upper().endswith("FPS")]

    def format(self) -> str:
        """`60FPS.2160p.DV.HDR.Atmos.TrueHD.7.1`（帧率放在分辨率前，与片库现有命名一致）"""
        parts: list[str] = []
        parts += self.fps()
        if self.resolution:
            parts.append(self.resolution)
        parts += [v for v in self.video if not v.upper().endswith("FPS")]
        parts += self.audio
        return ".".join(parts)

    def empty(self) -> bool:
        return not (self.resolution or self.video or self.audio)


def _canon_audio(raw: str) -> str:
    for rx, c in _AUDIO_CANON:
        if rx.match(raw):
            return c
    return raw


def parse_tags(name: str) -> Tags:
    """从文件名（不含扩展名也可）中抽取需要保留的标签。"""
    stem, ext = split_ext(name)
    if ext.lower() not in VIDEO_EXTS | SUB_EXTS:
        stem = name
    s = strip_brackets(stem) or stem
    t = Tags()
    m = RES_RE.search(s)
    if m:
        r = m.group(1).lower()
        t.resolution = r
    elif RES_ALIAS_RE.search(s):
        t.resolution = "2160p"
        t.resolution_inferred = True
    fm = FPS_RE.search(s)
    if fm:
        t.video.append(f"{fm.group(1)}FPS")
    if DV_RE.search(s):
        t.video.append("DV")
    if HDRP_RE.search(s):
        t.video.append("HDR10+")
    elif HDR_RE.search(s):
        t.video.append("HDR")
    if SDR_RE.search(s) and "HDR" not in t.video:
        t.video.append("SDR")

    if ATMOS_RE.search(s):
        t.audio.append("Atmos")
    seen: set[str] = set()
    for am in AUDIO_RE.finditer(s):
        codec = _canon_audio(am.group(1))
        ch = am.group(2)
        if codec in seen:
            continue
        # DD 太短容易误伤（例如组名），只有带声道时才认
        if codec == "DD" and not ch:
            continue
        seen.add(codec)
        t.audio.append(codec)
        if ch:
            t.audio.append(ch)
    return t


# ---------------------------------------------------------------- 片名 / 年份 / 集数

@dataclass
class Parsed:
    cn_title: str = ""
    en_title: str = ""
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None

    @property
    def query(self) -> str:
        return self.en_title or self.cn_title


def _tokens(s: str) -> list[str]:
    return [t for t in _SEP_RE.split(s) if t]


def parse_name(name: str) -> Parsed:
    """解析文件夹名或文件名中的中文名、英文名、年份、季、集。"""
    stem, ext = split_ext(name)
    if ext.lower() not in VIDEO_EXTS | SUB_EXTS:
        stem = name
    p = Parsed()

    # 季 / 集（在去掉方括号之前，因为 [第一季] 之类可能在括号里）
    m = SEASON_CN_RE.search(stem)
    if m:
        p.season = cn_number(m.group(1))
    m = EP_RE.search(stem)
    if m:
        p.season = int(m.group(1))
        p.episode = int(m.group(2))
        if m.group(3):
            p.episode_end = int(m.group(3))
    else:
        m = SEASON_WORD_RE.search(stem) or SEASON_RE.search(stem)
        if m:
            p.season = int(m.group(1))
        m = EP_ONLY_RE.search(stem) or EP_CN_RE.search(stem)
        if m:
            p.episode = int(m.group(1))

    s = strip_brackets(stem)
    toks = _tokens(s)

    # 片名部分在第一个“硬技术标签”之前
    hard = len(toks)
    for i, t in enumerate(toks):
        if i > 0 and HARD_TECH_RE.match(t):
            hard = i
            break
    # 年份：硬标签之前最后一个 19xx/20xx（不能是第一个 token，比如 “2001 A Space Odyssey 1968”）
    year_idx = None
    for i in range(1, hard):
        if YEAR_RE.match(toks[i]):
            year_idx = i
    if year_idx is not None:
        p.year = int(toks[year_idx])
    elif toks and YEAR_RE.match(toks[0]) and len(toks) > 1 and hard <= 1:
        p.year = int(toks[0])

    end = year_idx if year_idx is not None else hard
    title_toks: list[str] = []
    for i, t in enumerate(toks[:end]):
        if EP_TOKEN_RE.match(t):
            break   # 集号之后是单集标题，不是剧名
        if SEASON_TOKEN_RE.match(t) or SEASON_CN_RE.fullmatch(t):
            continue
        title_toks.append(t)

    cn: list[str] = []
    en: list[str] = []
    for t in title_toks:
        if CJK_RE.search(t):
            t2 = SEASON_CN_RE.sub("", t).strip()
            t2 = re.sub(r"全\d+集", "", t2).strip()
            if t2:
                cn.append(t2)
        else:
            en.append(t)
    p.cn_title = " ".join(cn).strip()
    p.en_title = " ".join(en).strip(" -")
    return p


def subtitle_suffix(name: str) -> str:
    """字幕的语言后缀，例如 `xxx.chs.srt` -> `.chs`，`xxx.zh-cn.forced.ass` -> `.zh-cn.forced`。"""
    stem, _ = split_ext(name)
    parts = stem.split(".")
    suf: list[str] = []
    while len(parts) > 1 and parts[-1].lower() in LANG_TOKENS:
        suf.insert(0, parts.pop())
    return ("." + ".".join(suf)) if suf else ""


def safe_title(title: str) -> str:
    t = ILLEGAL_RE.sub(" ", title)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t


def folder_name(title: str, year: Optional[int]) -> str:
    t = safe_title(title)
    return f"{t} ({year})" if year else t


def movie_file_name(title: str, year: Optional[int], tags: Tags, ext: str) -> str:
    base = folder_name(title, year)
    tag = tags.format()
    return f"{base} {tag}.{ext}" if tag else f"{base}.{ext}"


def episode_file_name(title: str, year: Optional[int], season: int, episode: int,
                      tags: Tags, ext: str, episode_end: Optional[int] = None) -> str:
    base = folder_name(title, year)
    se = f"S{season:02d}E{episode:02d}" + (f"-E{episode_end:02d}" if episode_end else "")
    tag = tags.format()
    return f"{base} {se} {tag}.{ext}" if tag else f"{base} {se}.{ext}"


def season_folder(season: int) -> str:
    return f"Season {season:02d}"


def is_season_folder_name(name: str) -> Optional[int]:
    """`Season 1` / `S01` / `第一季` / `Season01` -> 季号；否则 None。"""
    n = name.strip()
    m = re.fullmatch(r"(?i)season[ ._]?(\d{1,2})", n) or re.fullmatch(r"(?i)s(\d{1,2})", n)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"第\s*([一二三四五六七八九十\d]+)\s*季", n)
    if m:
        return cn_number(m.group(1))
    return None


def _cn_norm(s: str) -> str:
    """去掉标点/空格，全角转半角，中文数字转阿拉伯数字（壮志凌云二 → 壮志凌云2）。"""
    import unicodedata as _u
    s = re.sub(r"[\W_]+", "", _u.normalize("NFKC", s or "")).lower()
    return re.sub(r"[零一二三四五六七八九十]+",
                  lambda m: str(cn_number(m.group(0))) if cn_number(m.group(0)) is not None else m.group(0), s)


def cn_title_match(a: str, b: str, strict: bool = False) -> bool:
    """两个中文名是否算“对得上”（模糊）：忽略标点、：和 :、空格，2 和 二 视为相同；
    strict=True 时只认这一种；
    否则去掉数字后相同（壮志凌云2：独行侠 ≈ 壮志凌云：独行侠）、一方包含另一方、或相似度 ≥ 0.85 也算。"""
    import difflib
    na, nb = _cn_norm(a), _cn_norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if strict:
        return False
    da, db = re.sub(r"\d+", "", na), re.sub(r"\d+", "", nb)
    if len(da) >= 2 and da == db:
        return True
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


def similarity_cn(a: str, b: str):
    """中文名是否“基本一致”：完全一致或一方包含另一方 → True；否则 False。"""
    import unicodedata as _u
    na = re.sub(r"[\W_]+", "", _u.normalize("NFKC", a or ""))
    nb = re.sub(r"[\W_]+", "", _u.normalize("NFKC", b or ""))
    if not na or not nb:
        return True
    return na == nb or na in nb or nb in na
