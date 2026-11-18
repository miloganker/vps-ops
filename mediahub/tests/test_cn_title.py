"""TMDB 缺简体中文标题 / 文件夹中文名和 TMDB 中文名不一致时的处理（假 TMDB，不访问网络）。"""
import tempfile

import pytest

from app import autoorg as A
from app import notify
from app.config import Settings
from app.downloads import DownloadManager
from app.fake_openlist import FakeOpenList
from app.jobs import JobManager, Store
from app.naming import cn_title_match
from app.scanner import Scanner
from app.tmdb import TMDB, Candidate, FakeTMDB, _norm, apply_chinese_title

GB = 1_000_000_000
SM = "/guangdong/娱乐/影视/待整理/电影"
ST = "/guangdong/娱乐/影视/待整理/电视剧"
LM = "/guangdong/娱乐/影视/电影"
LT = "/guangdong/娱乐/影视/电视剧"
TG_FOLDER = ("【高清影视之家发布 www.WHATMV.com】壮志凌云2：独行侠[中文字幕].Top.Gun.Maverick.2022."
             "UHD.BluRay.REMUX.2160p.HEVC.Atmos.TrueHD7.1-DreamHD")
TG_FILE = "Top.Gun.Maverick.2022.UHD.BluRay.REMUX.2160p.HEVC.Atmos.TrueHD7.1-DreamHD.mkv"
# TMDB 361743 的真实情况（2026-11-16 查到）：zh-CN 译名是空的，搜索结果标题退回英文
TG_ALTS = [("zh-HK", "壯志凌雲：獨行俠"), ("zh-TW", "捍衛戰士：獨行俠"), ("zh-SG", "捍卫战士：独行侠"),
           ("alt-TW", "壮志凌云2：独行侠"), ("alt-CN", "壮志凌云2：独行侠"), ("alt-SG", "壮志凌云2：独行侠")]


def topgun(alts=TG_ALTS, year=2022):
    return Candidate(361743, "Top Gun: Maverick", "Top Gun: Maverick", year, 80.0,
                     en_title="Top Gun: Maverick", zh_alts=list(alts))


def fake_tmdb(extra=(), alts=TG_ALTS, year=2022, cn_query="壮志凌云2：独行侠"):
    t = {("movie", _norm("Top Gun Maverick")): topgun(alts, year)}
    if cn_query:
        t[("movie", _norm(cn_query))] = topgun(alts, year)
    for q, c in extra:
        t[("movie", _norm(q))] = c
    return FakeTMDB(t)


def settings():
    return Settings(rename_interval=2, move_wait=20, data_dir=tempfile.mkdtemp(),
                    download_poll_interval=0, download_refresh_wait=0)


def scan(tmdb, folder=TG_FOLDER, file=TG_FILE):
    f = FakeOpenList({SM: [], ST: [], LM: [], LT: []})
    f.put_file(SM + "/" + folder, file, 60 * GB)
    data = Scanner(f, tmdb, settings(), log=lambda *a: None).scan()
    assert len(data["items"]) == 1, data
    return data, data["items"][0]


def gates(data, it, classify=None):
    org = A.AutoOrganizer.__new__(A.AutoOrganizer)
    dl = {"kind": "movie", "classify": classify or {"confidence": "high"}}
    return org.gates(dl, [it], data)


# ---------------------------------------------------------------- 模糊比对
@pytest.mark.parametrize("a,b", [
    ("壮志凌云2：独行侠", "壮志凌云2:独行侠"),
    ("壮志凌云2：独行侠", "壮志凌云二 独行侠"),
    ("壮志凌云2：独行侠", "壮志凌云：独行侠"),
    ("沙丘2", "沙丘二"),
    ("请回答1988", "请回答 1988"),
])
def test_cn_fuzzy_agree(a, b):
    assert cn_title_match(a, b)


def test_cn_fuzzy_disagree_and_strict():
    assert not cn_title_match("壮志凌云2：独行侠", "捍卫战士：独行侠")
    assert not cn_title_match("壮志凌云2：独行侠", "九龙城寨之围城")
    assert not cn_title_match("壮志凌云", "壮志凌云2独行侠", strict=True)
    assert cn_title_match("壮志凌云二：独行侠", "壮志凌云2:独行侠", strict=True)


# ---------------------------------------------------------------- 选中文标题
def test_prefers_mainland_alt_title_when_zh_cn_missing():
    c = apply_chinese_title(topgun(), "随便")
    assert (c.title, c.title_source) == ("壮志凌云2：独行侠", "alt-CN")


def test_folder_cn_beats_traditional_when_no_simplified():
    c = apply_chinese_title(topgun([("zh-HK", "壯志凌雲：獨行俠"), ("zh-TW", "捍衛戰士：獨行俠")]), "壮志凌云2：独行侠")
    assert (c.title, c.title_source) == ("壮志凌云2：独行侠", "folder")
    c = apply_chinese_title(topgun([("zh-HK", "壯志凌雲：獨行俠"), ("zh-TW", "捍衛戰士：獨行俠")]), "")
    assert (c.title, c.title_source) == ("壯志凌雲：獨行俠", "zh-HK")


def test_no_chinese_anywhere_uses_folder_cn_else_keeps_english():
    c = apply_chinese_title(topgun([]), "壮志凌云2：独行侠")
    assert (c.title, c.title_source) == ("壮志凌云2：独行侠", "folder")
    c = apply_chinese_title(topgun([]), "")
    assert (c.title, c.title_source) == ("Top Gun: Maverick", "zh-CN")


def test_existing_zh_cn_title_untouched():
    c = apply_chinese_title(Candidate(1578, "愤怒的公牛", "Raging Bull", 1980, zh_alts=[("alt-CN", "蛮牛")]), "")
    assert (c.title, c.title_source) == ("愤怒的公牛", "zh-CN")


class _Resp:
    def __init__(self, d):
        self.d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self.d


class _Session:
    """模拟 TMDB 真实接口返回（361743 的 zh-CN 译名为空）。"""

    def __init__(self):
        self.paths = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.split("/3", 1)[1]
        self.paths.append((path, (params or {}).get("language")))
        r = {"id": 361743, "title": "Top Gun: Maverick", "original_title": "Top Gun: Maverick",
             "release_date": "2022-05-21", "popularity": 80}
        if path.startswith("/search/movie"):
            return _Resp({"results": [r]})
        if path == "/movie/361743/translations":
            return _Resp({"translations": [
                {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"title": ""}},
                {"iso_639_1": "zh", "iso_3166_1": "HK", "data": {"title": "壯志凌雲：獨行俠"}},
                {"iso_639_1": "zh", "iso_3166_1": "TW", "data": {"title": "捍衛戰士：獨行俠"}},
                {"iso_639_1": "en", "iso_3166_1": "US", "data": {"title": ""}}]})
        if path == "/movie/361743/alternative_titles":
            return _Resp({"titles": [{"iso_3166_1": "CN", "title": "壮志凌云2：独行侠", "type": "动作"},
                                     {"iso_3166_1": "US", "title": "Top Gun 2", "type": ""}]})
        if path == "/movie/361743":
            return _Resp(r)
        raise AssertionError(path)


def test_real_tmdb_client_fetches_translations_only_when_needed():
    s = _Session()
    m = TMDB("0123456789abcdef0123456789abcdef", session=s).match("movie", "Top Gun Maverick", "壮志凌云2：独行侠", 2022)
    assert m.best.tmdb_id == 361743 and m.confidence == "high"
    assert (m.best.title, m.best.title_source) == ("壮志凌云2：独行侠", "alt-CN")
    assert sum(1 for p, _ in s.paths if p.endswith("/translations")) == 1
    d = TMDB("0123456789abcdef0123456789abcdef", session=_Session()).details("movie", 361743)
    assert d.title == "壮志凌云2：独行侠"


# ---------------------------------------------------------------- 扫描 + 自动入库检查
def test_topgun_real_case_now_passes():
    data, it = scan(fake_tmdb())
    assert it["confidence"] == "high"
    assert it["flags"] == [], it["flags"]
    assert it["target_name"] == "壮志凌云2：独行侠 (2022)"
    assert any("大陆别名" in n for n in it["notes"])
    assert gates(data, it) == []


def test_mismatch_with_strong_match_is_only_a_note():
    # TMDB 的中文名都和文件夹不一样，但英文名 + 年份 + 高置信都对上
    data, it = scan(fake_tmdb(alts=[("alt-CN", "捍卫战士：独行侠")], cn_query=None))
    assert it["flags"] == []
    assert any("只做提示" in n for n in it["notes"])
    assert it["target_name"] == "捍卫战士：独行侠 (2022)"
    assert gates(data, it) == []


def test_old_classify_record_with_english_title_still_matches():
    data, it = scan(fake_tmdb())
    ck = {"confidence": "high", "movie": {"title": "Top Gun: Maverick", "year": 2022, "confidence": "high"}}
    assert gates(data, it, ck) == []
    ck["movie"] = {"id": 361743, "title": "Top Gun: Maverick", "year": 2022, "confidence": "high"}
    assert gates(data, it, ck) == []
    ck["movie"] = {"id": 744, "title": "壮志凌云", "year": 1986, "confidence": "high"}
    assert any("不一致" in r for r in gates(data, it, ck))


def test_year_differs_still_blocks():
    data, it = scan(fake_tmdb(alts=[("alt-CN", "捍卫战士：独行侠")], year=2021, cn_query=None))
    assert any("年份不一致" in f for f in it["flags"])
    assert any("文件夹中文名" in f for f in it["flags"])
    assert gates(data, it)


def test_cn_name_points_to_another_tmdb_entry_blocks():
    other = Candidate(9999, "壮志凌云2：独行侠", "Another Film", 2022, 5.0, en_title="Another Film")
    data, it = scan(fake_tmdb(alts=[("alt-CN", "捍卫战士：独行侠")], cn_query=None,
                              extra=[("壮志凌云2：独行侠", other)]))
    assert it["tmdb"]["id"] == 361743
    assert any("对应的是另一部" in f for f in it["flags"]), it["flags"]
    assert gates(data, it)


def test_weak_match_with_mismatch_blocks():
    # 文件名里没有英文名：只有中文名对上一条 TMDB 结果、且中文名不一致 → 不是高置信，照样拦住
    folder = "独行侠归来.2022.2160p"
    c = Candidate(361743, "Top Gun: Maverick", "Top Gun: Maverick", 2022, 80.0, en_title="Top Gun: Maverick",
                  zh_alts=[("alt-CN", "捍卫战士：独行侠")])
    c2 = Candidate(5, "独行侠", "Lone Ranger", 2022, 70.0, en_title="Lone Ranger")
    data, it = scan(FakeTMDB({("movie", _norm("独行侠归来")): c, ("movie", _norm("独行侠")): c2}),
                    folder=folder, file=folder + ".mkv")
    assert it["confidence"] != "high"
    assert any("文件夹中文名" in f for f in it["flags"])
    assert gates(data, it)


# ---------------------------------------------------------------- 端到端：只有哈希的磁力链
def test_topgun_hash_only_download_fully_automatic(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "telegram", lambda s, text, reply_markup=None: sent.append(text) or True)

    class Clock:
        t = 1000.0

        def sleep(self, s):
            self.t += s

        def __call__(self):
            return self.t

    clock, st = Clock(), settings()
    fake = FakeOpenList({SM: [], ST: [], LM: [], LT: []})
    fake.clock = clock
    store = Store(st.data_dir)
    jobs = JobManager(store, threaded=False)
    tmdb = fake_tmdb()
    mgr = DownloadManager(st.data_dir, st, lambda: fake, lambda: tmdb, can_write_preview=lambda: True,
                          sleep=clock.sleep, clock=clock)
    mgr.bind_store(store)
    mgr.bind_organizer(A.AutoOrganizer(st, store, jobs, lambda: fake, lambda: tmdb, sleep=clock.sleep, clock=clock))
    job = mgr.ingest("magnet:?xt=urn:btih:66b53d1f4933851e1a867e7dc21b0382457ff4f4", source="telegram")
    fake.put_file(job["dest"] + "/" + TG_FOLDER, TG_FILE, 60 * GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    org = job["organize"]
    assert org["state"] == "done", (org, job["log"])
    assert fake.names(LM) == ["壮志凌云2：独行侠 (2022)"]
    assert not any(c[0] in ("remove", "delete") for c in fake.calls)
    assert "已自动入库" in sent[-1]
