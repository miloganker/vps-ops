import json
import os
import tempfile
import time

import pytest

from app.config import Settings
from app.downloads import (MAX_LINKS_PER_SUBMIT, DownloadManager, can_dismiss,
                          classify, collect_links, extract_links,
                          format_batch_banner, is_open_job, parse_link)
from app.fake_openlist import FakeOpenList
from app.jobs import Store
from app.openlist import OpenListClient, OpenListError
from app.tmdb import demo_fake_tmdb

MOVIE_MAGNET = "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=Raging.Bull.1980.2160p.BluRay"
TV_MAGNET = "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=Breaking.Bad.S01E01.2160p"
UNCLEAR_MAGNET = "magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc&dn=Some.Unknown.Release.Name"
ED2K = "ed2k://|file|Raging.Bull.1980.mkv|12345|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|/"


@pytest.fixture
def settings():
    d = tempfile.mkdtemp()
    return Settings(rename_interval=0, move_wait=0, data_dir=d,
                    download_poll_interval=0, download_refresh_wait=0)


@pytest.fixture
def fake():
    # 空的待整理 + 空片库，避免和演示树里已有条目搅在一起
    tree = {
        "/guangdong/娱乐/影视/待整理/电影": [],
        "/guangdong/娱乐/影视/待整理/电视剧": [],
        "/guangdong/娱乐/影视/电影": [],
        "/guangdong/娱乐/影视/电视剧": [],
    }
    return FakeOpenList(tree)


@pytest.fixture
def tmdb():
    return demo_fake_tmdb()


@pytest.fixture
def mgr(settings, fake, tmdb):
    store = Store(settings.data_dir)
    m = DownloadManager(settings.data_dir, settings, lambda: fake, lambda: tmdb,
                        can_write_preview=lambda: True, sleep=lambda _s: None, clock=time.time)
    m.bind_store(store)
    return m


def test_parse_magnet_and_ed2k():
    links = extract_links("看这个 " + MOVIE_MAGNET + " 谢谢")
    assert links == [MOVIE_MAGNET]
    info = parse_link(MOVIE_MAGNET)
    assert info["display_name"] == "Raging.Bull.1980.2160p.BluRay"
    assert info["key"] == "btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    info = parse_link(ED2K)
    assert "Raging.Bull.1980.mkv" in info["display_name"]
    links = extract_links("no links here")
    assert links == []
    # 普通聊天里的 http 默认不抓，避免误伤
    assert extract_links("see https://example.com/a.mkv") == []
    assert extract_links("see https://example.com/a.mkv", allow_http=True)[0].startswith("https://")
    # 一段文字里多条，按出现顺序去重
    blob = "\n".join([MOVIE_MAGNET, TV_MAGNET, ED2K, MOVIE_MAGNET, "https://example.com/a.mkv"])
    assert extract_links(blob) == [MOVIE_MAGNET, TV_MAGNET, ED2K]
    assert extract_links(blob, allow_http=True) == [
        MOVIE_MAGNET, TV_MAGNET, ED2K, "https://example.com/a.mkv"]
    assert collect_links(MOVIE_MAGNET) == [MOVIE_MAGNET]
    assert collect_links("没有链接") == []
    msg = format_batch_banner(ok=2, exists=1, fail=1, auto=1, need=0, skip=3, cap=20)
    assert "已处理 4 条链接：新提交 2 条，已在队列 1 条，失败 1 条。" in msg
    assert "另有 3 条超出每次 20 条的上限" in msg
    assert "其中 1 条名字看不出类型" in msg
    assert MAX_LINKS_PER_SUBMIT == 20


def test_classify_high_movie_and_tv(tmdb):
    movie = classify("Raging.Bull.1980.2160p.BluRay", tmdb)
    assert movie["kind"] == "movie" and movie["confidence"] == "high"
    tv = classify("Breaking.Bad.S01E01.2160p", tmdb)
    assert tv["kind"] == "tv" and tv["confidence"] == "high"
    unclear = classify("Some.Unknown.Release.Name", tmdb)
    assert unclear["kind"] is None
    forced = classify("Some.Unknown.Release.Name", tmdb, forced="movie")
    assert forced["kind"] == "movie"


def test_refuse_simplehttp_and_friends(fake):
    with pytest.raises(OpenListError, match="拒绝"):
        fake.add_offline_download(["http://example.com/a.mkv"], "/tmp", tool="SimpleHttp")
    with pytest.raises(OpenListError, match="拒绝"):
        OpenListClient.normalize_offline_tool("aria2")
    with pytest.raises(OpenListError, match="拒绝"):
        OpenListClient.normalize_offline_tool("qBittorrent")
    with pytest.raises(OpenListError, match="拒绝"):
        OpenListClient.normalize_offline_tool("transmission")
    assert OpenListClient.normalize_offline_tool("115") == "115 Cloud"
    assert OpenListClient.normalize_offline_tool("115 Cloud") == "115 Cloud"


def test_unclear_name_submits_to_unsorted_without_asking(mgr, fake):
    job = mgr.ingest(UNCLEAR_MAGNET, source="web")
    assert job["status"] == "downloading"
    assert job["kind"] is None and job["auto"] is True
    adds = [c for c in fake.calls if c[0] == "add_offline_download"]
    assert len(adds) == 1
    assert adds[0][2] == mgr.settings.staging_unsorted + "/" + job["id"]
    assert adds[0][3] == "115 Cloud"
    assert adds[0][4] == "delete_never"


def test_legacy_needs_kind_job_still_submits_on_choice(mgr, fake):
    # 升级前留下的“先选类型再提交”任务：选完照旧提交到对应目录
    job = mgr.ingest(UNCLEAR_MAGNET, source="web")
    job.update(status="needs_kind", auto=False, tid=None, dest="")
    mgr.save(job)
    job = mgr.set_kind(job["id"], "movie")
    assert job["status"] == "downloading" and job["kind"] == "movie"
    adds = [c for c in fake.calls if c[0] == "add_offline_download"]
    assert adds[-1][2] == mgr.settings.staging_movies


def test_10008_no_retry_watch(mgr, fake):
    fake.offline_add_fault = "10008"
    job = mgr.ingest(MOVIE_MAGNET, kind="movie")
    assert job["status"] == "already_exists"
    adds = [c for c in fake.calls if c[0] == "add_offline_download"]
    assert len(adds) == 1
    # 再 tick 不得重新提交
    fake.offline_add_fault = None
    fake.offline_tasks["pre"] = {
        "id": "pre", "name": f"download {MOVIE_MAGNET} to ({job['dest']})",
        "state": "pending", "status": "", "progress": 10, "error": "",
        "path": job["dest"], "url": MOVIE_MAGNET,
    }
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "already_exists"
    assert len([c for c in fake.calls if c[0] == "add_offline_download"]) == 1
    fake.complete_offline("pre", ["Raging Bull (1980)"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    assert job["scan_count"] is not None


def test_poll_complete_scans_but_does_not_execute(mgr, fake, settings):
    job = mgr.ingest(TV_MAGNET, kind="tv")
    assert job["status"] == "downloading"
    tid = job["tid"]
    assert tid
    # 模拟 115 下完并出现在待整理
    fake.complete_offline(tid, ["Breaking Bad (2008)"])
    # 给剧集文件夹里放一集，扫描才能较完整
    from app.openlist import Entry
    import posixpath
    show = posixpath.join(settings.staging_tv, "Breaking Bad (2008)")
    fake.dirs[show]["Breaking.Bad.S01E01.2160p.mkv"] = Entry("Breaking.Bad.S01E01.2160p.mkv", False, 5_000_000_000)
    before = list(fake.calls)
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    preview = os.path.join(settings.data_dir, "preview.json")
    assert os.path.isfile(preview)
    data = json.loads(open(preview, encoding="utf-8").read())
    assert data.get("from_download") is True
    assert isinstance(data.get("items"), list)
    new_ops = [c[0] for c in fake.calls[len(before):]]
    assert "rename" not in new_ops and "move" not in new_ops and "mkdir" not in new_ops
    # 预览项存在，但没有执行入库
    assert all(it.get("status") in (None, "pending") or "status" not in it or it.get("status") != "done"
               for it in data["items"])


def test_duplicate_active_not_resubmitted(mgr, fake):
    a = mgr.ingest(MOVIE_MAGNET, kind="movie")
    b = mgr.ingest(MOVIE_MAGNET, kind="movie")
    assert a["id"] == b["id"]
    assert len([c for c in fake.calls if c[0] == "add_offline_download"]) == 1


def test_is_open_job_hides_settled_complete():
    # 用户已手动入库、任务只是 complete、没有 organize：应隐藏
    leftover = {"id": "leftover", "status": "complete", "display_name": "九龙城寨之围城 (2024)",
                "kind": "movie", "progress": 100}
    assert is_open_job(leftover) is False
    assert can_dismiss(leftover) is False
    assert is_open_job({"id": "x", "status": "complete", "organize": {}}) is False
    assert is_open_job({"id": "x", "status": "complete", "organize": {"state": "done"}}) is False
    # 下完但仍需处理
    for state in ("skipped", "unverified", "failed", "interrupted", "queued", "running"):
        j = {"id": "x", "status": "complete", "organize": {"state": state}}
        assert is_open_job(j) is True, state
        assert can_dismiss(j) is (state != "queued" and state != "running"), state
    # 进行中 / 失败
    for st in ("needs_kind", "submitting", "downloading", "already_exists", "resolving", "organizing"):
        assert is_open_job({"id": "x", "status": st}) is True, st
        assert can_dismiss({"id": "x", "status": st}) is False, st
    failed = {"id": "x", "status": "failed"}
    assert is_open_job(failed) is True and can_dismiss(failed) is True
    assert is_open_job({"id": "x", "status": "failed", "dismissed": True}) is False
    assert can_dismiss({"id": "x", "status": "failed", "dismissed": True}) is False
    assert is_open_job({"id": "x", "status": "complete", "organize": {"state": "skipped"},
                        "archived": True}) is False


def test_list_open_jobs_and_dismiss(mgr):
    jobs = [
        {"id": "settled-manual", "status": "complete", "display_name": "九龙城寨之围城 (2024)",
         "kind": "movie", "progress": 100},
        {"id": "settled-auto", "status": "complete", "display_name": "已自动入库片",
         "kind": "movie", "organize": {"state": "done", "dest": "/电影/Foo (2020)"}},
        {"id": "open-skipped", "status": "complete", "display_name": "需确认的剧",
         "kind": "tv", "organize": {"state": "skipped", "reasons": ["有提示"]}},
        {"id": "open-kind", "status": "needs_kind", "display_name": "待选类型"},
        {"id": "open-org", "status": "organizing", "display_name": "正在入库",
         "kind": "movie", "organize": {"state": "running"}},
        {"id": "open-fail", "status": "failed", "display_name": "失败任务", "error": "x"},
    ]
    for j in jobs:
        mgr.save(j)
    all_ids = {j["id"] for j in mgr.list_jobs(30)}
    open_ids = {j["id"] for j in mgr.list_open_jobs(30)}
    assert all_ids == {j["id"] for j in jobs}
    assert open_ids == {"open-skipped", "open-kind", "open-org", "open-fail"}
    job = mgr.dismiss("open-skipped")
    assert job["dismissed"] is True and job.get("dismissed_at")
    assert mgr.get("open-skipped")["dismissed"] is True  # 文件还在，只是打了标记
    assert "open-skipped" not in {j["id"] for j in mgr.list_open_jobs(30)}
    assert any(j["id"] == "open-skipped" for j in mgr.list_jobs(30))
    try:
        mgr.dismiss("settled-manual")
        raise AssertionError("should not dismiss settled complete")
    except ValueError:
        pass
    try:
        mgr.dismiss("open-org")
        raise AssertionError("should not dismiss in-progress organize")
    except ValueError:
        pass
    try:
        mgr.dismiss("missing-id")
        raise AssertionError("should KeyError")
    except KeyError:
        pass


def test_ingest_many_dedupes_and_applies_kind(mgr, fake):
    # 同一哈希出现两次：第二条算已在队列，不会再向 115 提交
    other = "magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd&dn=Other.Movie.2020"
    results = mgr.ingest_many([MOVIE_MAGNET, MOVIE_MAGNET, other], kind="movie", source="web")
    assert len(results) == 3
    assert results[0]["status"] in ("downloading", "already_exists")
    assert results[0]["kind"] == "movie" and not results[0].get("reused")
    assert results[1].get("reused") is True and results[1]["duplicate_of"] == results[0]["id"]
    assert results[2]["id"] != results[0]["id"] and results[2]["kind"] == "movie"
    adds = [c for c in fake.calls if c[0] == "add_offline_download"]
    assert len(adds) == 2
    # 轮询间隔为 0 时同步提交，两次提交之间会走间隔（测试里 sleep 是空操作）

