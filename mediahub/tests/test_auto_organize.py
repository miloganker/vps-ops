"""全自动入库：下完 → 分类 → 待整理 → 扫描 → 安全检查 → 改名 → 移进片库（假 OpenList，不访问网络）。"""
import posixpath
import tempfile

import pytest

from app import notify
from app import autoorg as A
from app.config import Settings
from app.downloads import DownloadManager
from app.fake_openlist import FakeOpenList
from app.jobs import JobManager, Store
from app.tmdb import demo_fake_tmdb

HASH = "magnet:?xt=urn:btih:66b53d1f4933851e1a867e7dc21b0382457ff4f4"
HASH2 = "magnet:?xt=urn:btih:77b53d1f4933851e1a867e7dc21b0382457ff4f4"
TV_DN = "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=Breaking.Bad.S01.1080p.BluRay"
GB = 1_000_000_000
SM = "/guangdong/娱乐/影视/待整理/电影"
ST = "/guangdong/娱乐/影视/待整理/电视剧"
LM = "/guangdong/娱乐/影视/电影"
LT = "/guangdong/娱乐/影视/电视剧"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings():
    return Settings(rename_interval=2, move_wait=20, data_dir=tempfile.mkdtemp(),
                    download_poll_interval=0, download_refresh_wait=0)


@pytest.fixture
def fake(clock):
    f = FakeOpenList({SM: [], ST: [], LM: [], LT: []})
    f.clock = clock
    f.move_busy_seconds = 15
    return f


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "telegram",
                        lambda settings, text, reply_markup=None: out.append(text) or True)
    return out


@pytest.fixture
def env(settings, fake, clock):
    store = Store(settings.data_dir)
    jobs = JobManager(store, threaded=False)
    tmdb = demo_fake_tmdb()
    busy = lambda: not (jobs.busy() and jobs.current.get("type") in ("execute", "auto"))
    mgr = DownloadManager(settings.data_dir, settings, lambda: fake, lambda: tmdb,
                          can_write_preview=busy, sleep=clock.sleep, clock=clock)
    mgr.bind_store(store)
    org = A.AutoOrganizer(settings, store, jobs, lambda: fake, lambda: tmdb, sleep=clock.sleep, clock=clock)
    mgr.bind_organizer(org)
    return {"mgr": mgr, "org": org, "jobs": jobs, "store": store}


def _ops(fake, name):
    return [c for c in fake.calls if c[0] == name]


def _download(env, fake, url=HASH, files=()):
    mgr = env["mgr"]
    job = mgr.ingest(url, source="telegram")
    jd = job["dest"]
    assert jd.startswith("/guangdong/娱乐/影视/待整理/未分类/")
    for folder, name, size in files:
        fake.put_file(posixpath.join(jd, folder) if folder else jd, name, size)
    fake.complete_offline(job["tid"])
    mgr.tick()
    return mgr.get(job["id"])


def test_hash_only_movie_fully_automatic(env, fake, settings, sent, clock):
    fake.put_file(SM + "/Seven.Samurai.1954.1080p", "Seven.Samurai.1954.1080p.mkv", 10 * GB)  # 别人的，不能碰
    folder = "Raging.Bull.1980.2160p.BluRay.x265"
    job = _download(env, fake, files=[(folder, "Raging.Bull.1980.2160p.BluRay.x265.mkv", 40 * GB),
                                      (folder, "更多资源 www.example.com.mp4", 100)])
    assert job["status"] == "complete", job["log"]
    org = job["organize"]
    assert org["state"] == "done" and org["target"] == "愤怒的公牛 (1980)"
    assert org["dest"] == LM + "/愤怒的公牛 (1980)"
    assert fake.names(LM) == ["愤怒的公牛 (1980)"]
    lib = fake.names(LM + "/愤怒的公牛 (1980)")
    assert "愤怒的公牛 (1980) 2160p.x265.mkv" in lib or any(n.startswith("愤怒的公牛 (1980) 2160p") for n in lib)
    assert "更多资源 www.example.com.mp4" in lib          # 广告文件保留，没有删除
    # 别的待整理条目原样不动
    assert fake.names(SM) == ["Seven.Samurai.1954.1080p"]
    assert not [c for c in fake.calls if c[0] == "rename" and "Seven" in c[1]]
    # 两次移动（未分类→待整理，待整理→片库）之间至少隔 MOVE_WAIT
    assert len(_ops(fake, "move")) == 2 and 20 in clock.sleeps
    assert not any(c[0] in ("remove", "delete") for c in fake.calls)
    text = sent[-1]
    assert "已自动入库" in text and "愤怒的公牛 (1980)" in text and LM in text and "/job/" in text
    # 网页历史里能看到
    recent = env["jobs"].recent(5)
    assert recent[0]["type"] == "auto" and recent[0]["download_id"] == job["id"]
    assert recent[0]["result"]["items"][0]["status"] == "done"
    # 预览里不再留着这一项（免得再执行一次），别的项还在
    pv = env["store"].load("preview.json")
    assert [it["folder_old"] for it in pv["items"]] == ["Seven.Samurai.1954.1080p"]


def test_tv_new_show_named_magnet(env, fake, settings, sent):
    job = _download(env, fake, url=TV_DN, files=[
        ("Breaking.Bad.S01.1080p.BluRay", f"Breaking.Bad.S01E{e:02d}.1080p.BluRay.x264.AAC.2.0.mkv", 2 * GB)
        for e in (1, 2, 3)])
    assert job["organize"]["state"] == "done", job["log"] + [str(job.get("organize"))]
    assert fake.names(LT) == ["绝命毒师 (2008)"]
    assert fake.names(LT + "/绝命毒师 (2008)") == ["Season 01"]
    eps = fake.names(LT + "/绝命毒师 (2008)/Season 01")
    assert len(eps) == 3 and all(n.startswith("绝命毒师 (2008) S01E0") for n in eps)
    assert fake.names(ST) == []


def test_tv_new_season_goes_into_existing_show(env, fake, settings, sent):
    fake.put_file(LT + "/绝命毒师 (2008)/Season 01", "绝命毒师 (2008) S01E01 1080p.mkv", GB)
    job = _download(env, fake, files=[
        ("Breaking.Bad.S02.1080p", f"Breaking.Bad.S02E{e:02d}.1080p.mkv", GB) for e in (1, 2)])
    org = job["organize"]
    assert org["state"] == "done", job["log"] + [str(org)]
    assert org["merge"] and org["seasons"] == "Season 02"
    assert fake.names(LT) == ["绝命毒师 (2008)"]
    assert fake.names(LT + "/绝命毒师 (2008)") == ["Season 01", "Season 02"]
    assert fake.names(LT + "/绝命毒师 (2008)/Season 01") == ["绝命毒师 (2008) S01E01 1080p.mkv"]
    assert "并入片库" in sent[-1]


def test_tv_existing_season_is_skipped(env, fake, settings, sent):
    fake.put_file(LT + "/绝命毒师 (2008)/Season 01", "old.mkv", GB)
    job = _download(env, fake, files=[
        ("Breaking.Bad.S01.1080p", f"Breaking.Bad.S01E{e:02d}.1080p.mkv", GB) for e in (1, 2)])
    org = job["organize"]
    assert org["state"] == "skipped" and any("Season 01" in r and "冲突" in r for r in org["reasons"])
    assert fake.names(LT + "/绝命毒师 (2008)/Season 01") == ["old.mkv"]
    assert not _ops(fake, "rename")
    assert fake.names(ST) == ["Breaking.Bad.S01.1080p"]
    assert "没有自动入库" in sent[-1] and "冲突" in sent[-1]


def test_movie_name_exists_in_library_skipped(env, fake, settings, sent):
    fake.put_file(LM + "/愤怒的公牛 (1980)", "old.mkv", GB)
    job = _download(env, fake, files=[("Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)])
    org = job["organize"]
    assert org["state"] == "skipped" and "冲突" in "".join(org["reasons"])
    assert not _ops(fake, "rename")
    assert fake.names(LM + "/愤怒的公牛 (1980)") == ["old.mkv"]
    assert fake.names(SM) == ["Raging.Bull.1980.1080p"]


def test_no_tmdb_match_falls_back(env, fake, settings, sent):
    job = _download(env, fake, files=[("Totally.Unknown.Film.2020.1080p", "Totally.Unknown.Film.2020.1080p.mkv", 10 * GB)])
    assert job["kind"] == "movie"
    org = job["organize"]
    assert org["state"] == "skipped" and any("TMDB" in r for r in org["reasons"])
    assert not _ops(fake, "rename") and fake.names(LM) == []
    assert "请打开网页确认" in sent[-1]


def test_scan_warning_falls_back(env, fake, settings, sent):
    # 年份差一年：TMDB 仍是高置信，但扫描有“年份不一致”提示 → 不自动
    job = _download(env, fake, files=[("Raging.Bull.1981.1080p", "Raging.Bull.1981.1080p.mkv", 10 * GB)])
    org = job["organize"]
    assert org["state"] == "skipped", org
    assert any("年份不一致" in r for r in org["reasons"])
    assert fake.names(LM) == []


def test_switch_off_keeps_phase1_behaviour(env, fake, settings, sent):
    env["org"].set_enabled(False)
    mgr = env["mgr"]
    named = "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=Raging.Bull.1980.2160p.BluRay"
    j = mgr.ingest(named)
    assert j["auto"] is False and j["dest"] == SM      # 关着时名字清楚的照旧直接下到待整理
    job = _download(env, fake, files=[("Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)])
    assert job["status"] == "complete" and job["organize"]["state"] == "skipped"
    assert job["organize"]["reasons"] == ["自动入库已关闭"]
    assert not _ops(fake, "rename") and fake.names(LM) == []
    assert "自动入库已关闭" in sent[-1]
    assert env["org"].status()["enabled"] is False and env["org"].status()["source"] == "override"
    env["org"].set_enabled(True)
    assert env["org"].enabled()


def test_env_default_respected(settings, env):
    settings.auto_organize = False
    assert not env["org"].enabled()
    env["org"].set_enabled(True)
    assert env["org"].enabled()


def test_queued_while_jobmanager_busy(env, fake, settings, sent):
    jobs = env["jobs"]
    jobs.current = {"id": "manual", "type": "scan", "status": "running", "log": []}
    job = _download(env, fake, files=[("Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)])
    assert job["status"] == "organizing" and job["organize"]["state"] == "queued"
    assert not _ops(fake, "rename")
    assert env["org"].status()["queued"]
    env["mgr"].tick()
    assert env["mgr"].get(job["id"])["organize"]["state"] == "queued"
    jobs.current["status"] = "done"
    env["mgr"].tick()
    job = env["mgr"].get(job["id"])
    assert job["organize"]["state"] == "done" and job["status"] == "complete"


def test_two_downloads_organized_one_at_a_time(env, fake, settings, sent):
    mgr = env["mgr"]
    a = mgr.ingest(HASH)
    b = mgr.ingest(HASH2)
    fake.put_file(a["dest"] + "/Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)
    fake.put_file(b["dest"] + "/Seven.Samurai.1954.1080p", "Seven.Samurai.1954.1080p.mkv", 10 * GB)
    fake.complete_offline(a["tid"])
    fake.complete_offline(b["tid"])
    mgr.tick()
    assert mgr.get(a["id"])["organize"]["state"] == "done"
    assert mgr.get(b["id"])["organize"]["state"] == "done"
    assert fake.names(LM) == ["七武士 (1954)", "愤怒的公牛 (1980)"]
    # 4 次移动，都没撞上 990009
    assert len(_ops(fake, "move")) == 4
    autos = [j for j in env["jobs"].recent(5) if j["type"] == "auto"]
    assert len(autos) == 2


def test_library_move_timeout_not_retried_then_verified(env, fake, settings, sent):
    fake.faults[("move", "愤怒的公牛 (1980)")] = "timeout_fail"
    job = _download(env, fake, files=[("Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)])
    assert job["organize"]["state"] == "unverified", job["organize"]
    lib_moves = lambda: [c for c in _ops(fake, "move") if c[2] == LM]
    assert len(lib_moves()) == 1
    env["mgr"].tick()
    assert env["mgr"].get(job["id"])["organize"]["state"] == "unverified"
    assert len(lib_moves()) == 1                       # 只核对，不重试
    # 115 后来其实移过去了
    del fake.faults[("move", "愤怒的公牛 (1980)")]
    e = fake.dirs[SM].pop("愤怒的公牛 (1980)")
    fake.dirs[LM]["愤怒的公牛 (1980)"] = e
    fake._move_subtree(SM + "/愤怒的公牛 (1980)", LM + "/愤怒的公牛 (1980)")
    env["mgr"].tick()
    job = env["mgr"].get(job["id"])
    assert job["organize"]["state"] == "done"
    assert len(lib_moves()) == 1
    assert "已自动入库" in sent[-1]


def test_unverified_gives_up_after_checks(env, fake, settings, sent):
    fake.faults[("move", "愤怒的公牛 (1980)")] = "timeout_fail"
    job = _download(env, fake, files=[("Raging.Bull.1980.1080p", "Raging.Bull.1980.1080p.mkv", 10 * GB)])
    for _ in range(A.MAX_VERIFY_CHECKS + 2):
        env["mgr"].tick()
    assert len([c for c in _ops(fake, "move") if c[2] == LM]) == 1
    assert "待核实" in sent[-1]
    assert fake.names(SM) == ["愤怒的公牛 (1980)"]   # 文件还在，没有删除


def test_interrupted_run_is_not_retried(env, fake, settings, sent):
    mgr = env["mgr"]
    job = mgr.ingest(HASH)
    job = mgr.get(job["id"])
    job["status"] = "organizing"
    job["moved_to"] = SM + "/x"
    job["kind"] = "movie"
    job["organize"] = {"state": "running", "exec_job": "gone"}
    mgr.save(job)
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["organize"]["state"] == "interrupted" and job["status"] == "complete"
    assert not _ops(fake, "rename")


def test_user_button_choice_then_auto(env, fake, settings, sent):
    mgr = env["mgr"]
    job = mgr.ingest(HASH)
    fake.put_file(job["dest"] + "/Raging.Bull.1980.1080p", "part1 Raging.Bull.1980.mkv", 5 * GB)
    fake.put_file(job["dest"] + "/Raging.Bull.1980.1080p", "part2 Raging.Bull.1980.mkv", 5 * GB)
    fake.put_file(job["dest"] + "/Raging.Bull.1980.1080p", "part3 Raging.Bull.1980.mkv", 5 * GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    assert mgr.get(job["id"])["status"] == "needs_kind"
    mgr.set_kind(job["id"], "movie")
    job = mgr.get(job["id"])
    assert job["organize"]["state"] in ("done", "skipped")
    assert job["classify"]["reason"] == "你选择了电影"
