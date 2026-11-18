"""哈希磁力（没有 dn=）：下到 未分类/<任务号>，下完按真实文件自动分类再移到 待整理/电影|电视剧。"""
import posixpath
import tempfile

import pytest

from app import downloads as D
from app.config import Settings
from app.downloads import DownloadManager
from app.fake_openlist import FakeOpenList
from app.jobs import Store
from app.release import classify_release, inspect
from app.tmdb import demo_fake_tmdb

HASH_ONLY = "magnet:?xt=urn:btih:66b53d1f4933851e1a867e7dc21b0382457ff4f4"
HASH_ONLY_2 = "magnet:?xt=urn:btih:77b53d1f4933851e1a867e7dc21b0382457ff4f4"
GB = 1_000_000_000


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
    return Settings(rename_interval=0, move_wait=20, data_dir=tempfile.mkdtemp(),
                    download_poll_interval=0, download_refresh_wait=0)


@pytest.fixture
def fake(clock):
    f = FakeOpenList({
        "/guangdong/娱乐/影视/待整理/电影": [],
        "/guangdong/娱乐/影视/待整理/电视剧": [],
        "/guangdong/娱乐/影视/电影": [],
        "/guangdong/娱乐/影视/电视剧": [],
    })
    f.clock = clock
    f.move_busy_seconds = 15
    return f


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(D.notify, "telegram",
                        lambda settings, text, reply_markup=None: out.append((text, reply_markup)) or True)
    return out


@pytest.fixture
def executor_busy():
    return {"busy": False}


@pytest.fixture
def mgr(settings, fake, clock, executor_busy):
    tmdb = demo_fake_tmdb()
    m = DownloadManager(settings.data_dir, settings, lambda: fake, lambda: tmdb,
                        can_write_preview=lambda: not executor_busy["busy"],
                        sleep=clock.sleep, clock=clock)
    m.bind_store(Store(settings.data_dir))
    return m


def _submit(mgr, fake, url=HASH_ONLY):
    job = mgr.ingest(url, source="telegram")
    assert job["status"] == "downloading" and job["auto"] and job["kind"] is None
    return job, job["dest"]


def _ops(fake, name):
    return [c for c in fake.calls if c[0] == name]


def test_hash_only_tv_folder_auto_tv(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    assert jd == settings.staging_unsorted + "/" + job["id"]
    assert ("mkdir", jd) in fake.calls
    show = posixpath.join(jd, "Breaking.Bad.S01.1080p.BluRay")
    for e in range(1, 11):
        fake.put_file(show, f"Breaking.Bad.S01E{e:02d}.1080p.mkv", 2 * GB)
    fake.put_file(show, "更多资源 www.example.com.mp4", 100)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete", job["log"]
    assert job["kind"] == "tv"
    assert job["moved_to"] == settings.staging_tv + "/Breaking.Bad.S01.1080p.BluRay"
    assert "Breaking.Bad.S01.1080p.BluRay" in fake.names(settings.staging_tv)
    reason = job["classify"]["reason"]
    assert "S01E01–E10" in reason and "绝命毒师" in reason
    assert len(_ops(fake, "move")) == 1
    assert not _ops(fake, "rename")
    # 广告文件跟着文件夹一起走，没有被删除
    assert "更多资源 www.example.com.mp4" in fake.names(settings.staging_tv + "/Breaking.Bad.S01.1080p.BluRay")
    text, kb = sent[-1]
    assert "识别为电视剧" in text and "S01E01–E10" in text and "绝命毒师" in text
    assert "已移到" in text and kb is None
    # 只在 115 Cloud 下载，从不使用会落到服务器的工具
    assert all(c[3] == "115 Cloud" for c in _ops(fake, "add_offline_download"))


def test_hash_only_single_movie_auto_movie(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    folder = posixpath.join(jd, "Raging.Bull.1980.2160p.BluRay.x265")
    fake.put_file(folder, "Raging.Bull.1980.2160p.BluRay.x265.mkv", 40 * GB)
    fake.put_file(folder, "Raging.Bull.1980.sample.mkv", 50_000_000)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete" and job["kind"] == "movie"
    assert "Raging.Bull.1980.2160p.BluRay.x265" in fake.names(settings.staging_movies)
    assert "只有 1 个正片文件" in job["classify"]["reason"]
    assert "愤怒的公牛 (1980)" in job["classify"]["reason"]
    assert "识别为电影" in sent[-1][0]


def test_single_loose_file_moved_directly(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    fake.put_file(jd, "Dune.Part.Two.2024.2160p.WEB-DL.mkv", 30 * GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["kind"] == "movie" and job["status"] == "complete"
    assert "Dune.Part.Two.2024.2160p.WEB-DL.mkv" in fake.names(settings.staging_movies)
    # 空的任务文件夹留着（从不删除）
    assert fake.names(jd) == []


def test_loose_episode_files_folder_renamed_then_moved(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    for e in (1, 2, 3):
        fake.put_file(jd, f"Breaking.Bad.S02E{e:02d}.720p.mkv", GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete" and job["kind"] == "tv", job["log"]
    assert job["moved_to"] == settings.staging_tv + "/Breaking Bad"
    assert sorted(fake.names(settings.staging_tv + "/Breaking Bad")) == [
        "Breaking.Bad.S02E01.720p.mkv", "Breaking.Bad.S02E02.720p.mkv", "Breaking.Bad.S02E03.720p.mkv"]
    assert len(_ops(fake, "rename")) == 1 and len(_ops(fake, "move")) == 1


def test_chinese_episode_names(fake):
    folder = "/x/job"
    for e in range(1, 5):
        fake.put_file(folder + "/请回答1988 全20集", f"请回答1988 第{e:02d}集.mp4", GB)
    info = inspect(fake, folder)
    r = classify_release(info, demo_fake_tmdb())
    assert r["kind"] == "tv"
    assert "S01E01–E04" in r["reason"]


def test_season_folders_mean_tv(fake):
    folder = "/x/job2"
    fake.put_file(folder + "/Some Show/Season 1", "a.mkv", GB)
    fake.put_file(folder + "/Some Show/Season 2", "b.mkv", GB)
    r = classify_release(inspect(fake, folder), None)
    assert r["kind"] == "tv" and "季文件夹" in r["reason"]


def test_unclear_asks_with_buttons_then_choice_moves(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    folder = posixpath.join(jd, "Random.Stuff")
    fake.put_file(folder, "part one.mkv", GB)
    fake.put_file(folder, "part two.mkv", GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "needs_kind" and job["downloaded"]
    assert not _ops(fake, "move")
    text, kb = sent[-1]
    assert "判断不了" in text and "Random.Stuff" in text
    assert kb["inline_keyboard"][0][0]["callback_data"] == f"dlkind:{job['id']}:movie"
    assert kb["inline_keyboard"][0][1]["callback_data"] == f"dlkind:{job['id']}:tv"
    job = mgr.set_kind(job["id"], "tv")
    assert job["status"] == "complete" and job["kind"] == "tv"
    assert "Random.Stuff" in fake.names(settings.staging_tv)
    assert len(_ops(fake, "add_offline_download")) == 1   # 没有重新提交
    assert "你选择了电视剧" in sent[-1][0]


def test_kind_chosen_during_download_skips_classification(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    mgr.set_kind(job["id"], "movie")
    assert mgr.get(job["id"])["status"] == "downloading"
    fake.put_file(posixpath.join(jd, "Whatever"), "x.mkv", GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    assert "Whatever" in fake.names(settings.staging_movies)


def test_busy_990009_waits_and_retries(mgr, fake, settings, sent, clock):
    job, jd = _submit(mgr, fake)
    fake.put_file(posixpath.join(jd, "Raging.Bull.1980.1080p"), "Raging.Bull.1980.1080p.mkv", 10 * GB)
    fake.complete_offline(job["tid"])
    fake.move_busy_until = clock() + 5   # 别的移动还在处理
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    moves = _ops(fake, "move")
    assert len(moves) == 2      # 第一次 990009，等 move_wait 后第二次成功
    assert 20 in clock.sleeps


def test_two_jobs_moves_are_spaced(mgr, fake, settings, sent, clock):
    a, ja = _submit(mgr, fake, HASH_ONLY)
    b, jb = _submit(mgr, fake, HASH_ONLY_2)
    fake.put_file(posixpath.join(ja, "Raging.Bull.1980.1080p"), "Raging.Bull.1980.1080p.mkv", 10 * GB)
    fake.put_file(posixpath.join(jb, "Seven.Samurai.1954.1080p"), "Seven.Samurai.1954.1080p.mkv", 10 * GB)
    fake.complete_offline(a["tid"])
    fake.complete_offline(b["tid"])
    mgr.tick()
    assert mgr.get(a["id"])["status"] == "complete"
    assert mgr.get(b["id"])["status"] == "complete"
    # 两次移动都一次成功，没有撞上 990009
    assert len(_ops(fake, "move")) == 2


def test_move_timeout_but_succeeded_is_not_retried(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    name = "Raging.Bull.1980.1080p"
    fake.put_file(posixpath.join(jd, name), name + ".mkv", 10 * GB)
    fake.faults[("move", name)] = "timeout"
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "resolving" and job["move_pending"]
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    assert len(_ops(fake, "move")) == 1
    assert name in fake.names(settings.staging_movies)


def test_move_timeout_not_moved_checked_before_one_retry(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    name = "Raging.Bull.1980.1080p"
    fake.put_file(posixpath.join(jd, name), name + ".mkv", 10 * GB)
    fake.faults[("move", name)] = "timeout_fail"
    fake.complete_offline(job["tid"])
    mgr.tick()
    assert len(_ops(fake, "move")) == 1
    for _ in range(2):
        mgr.tick()
        assert len(_ops(fake, "move")) == 1    # 先核实，不盲目重试
    del fake.faults[("move", name)]
    mgr.tick()                                  # 第 3 次核实仍在原处 → 才再移动一次
    job = mgr.get(job["id"])
    assert len(_ops(fake, "move")) == 2
    assert job["status"] == "complete"


def test_name_collision_not_moved(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    name = "Raging.Bull.1980.1080p"
    fake.put_file(posixpath.join(jd, name), name + ".mkv", 10 * GB)
    fake.put_file(posixpath.join(settings.staging_movies, name), "old.mkv", GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete" and "同名" in job["error"]
    assert not _ops(fake, "move")
    assert fake.names(posixpath.join(jd, name)) == [name + ".mkv"]


def test_empty_after_complete_keeps_checking_then_fails(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    fake.complete_offline(job["tid"])
    mgr.tick()
    assert mgr.get(job["id"])["status"] == "resolving"
    for _ in range(D.MAX_EMPTY_CHECKS):
        mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "failed" and "空的" in job["error"]
    assert not _ops(fake, "move")


def test_waits_for_executor_before_moving(mgr, fake, settings, sent, executor_busy):
    job, jd = _submit(mgr, fake)
    fake.put_file(posixpath.join(jd, "Raging.Bull.1980.1080p"), "Raging.Bull.1980.1080p.mkv", 10 * GB)
    fake.complete_offline(job["tid"])
    executor_busy["busy"] = True
    mgr.tick()
    assert mgr.get(job["id"])["status"] == "resolving"
    assert not _ops(fake, "move")
    executor_busy["busy"] = False
    mgr.tick()
    assert mgr.get(job["id"])["status"] == "complete"


def test_sync_10008_watches_existing_task_and_uses_its_dest(mgr, fake, settings, sent):
    fake.offline_add_fault = "10008"
    job = mgr.ingest(HASH_ONLY, source="telegram")
    assert job["status"] == "already_exists"
    fake.offline_add_fault = None
    fake.offline_tasks["pre"] = {"id": "pre", "name": f"download {HASH_ONLY} to ({settings.staging_tv})",
                                 "state": "running", "status": "", "progress": 40, "error": ""}
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "already_exists" and job["kind"] == "tv" and job["auto"] is False
    fake.offline_tasks["pre"]["state"] = "succeeded"
    fake.put_file(settings.staging_tv + "/Some.Show.S01", "Some.Show.S01E01.mkv", GB)
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "complete"
    assert len(_ops(fake, "add_offline_download")) == 1
    assert not _ops(fake, "move")


def test_async_10008_in_task_error_not_resubmitted(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    fake.fail_offline(job["tid"], "failed to add offline download task: code: 10008, message: 任务已存在")
    mgr.tick()
    job = mgr.get(job["id"])
    assert job["status"] == "already_exists"
    for _ in range(3):
        mgr.tick()
    assert mgr.get(job["id"])["status"] == "already_exists"
    assert len(_ops(fake, "add_offline_download")) == 1


def test_dn_with_clear_name_keeps_fast_path(mgr, fake, settings, sent):
    url = "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=Breaking.Bad.S01E01.2160p"
    job = mgr.ingest(url)
    assert job["auto"] is False and job["kind"] == "tv" and job["dest"] == settings.staging_tv
    assert not any(c[0] == "mkdir" for c in fake.calls)


def test_never_deletes(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    fake.put_file(posixpath.join(jd, "Breaking.Bad.S01"), "Breaking.Bad.S01E01.mkv", GB)
    fake.complete_offline(job["tid"])
    mgr.tick()
    assert not any(c[0] in ("remove", "delete") for c in fake.calls)


def test_stale_poller_copy_does_not_drop_user_choice(mgr, fake, settings, sent):
    job, jd = _submit(mgr, fake)
    stale = mgr.get(job["id"])
    mgr.set_kind(job["id"], "tv")
    stale["progress"] = 50
    mgr.save(stale)          # 轮询线程用旧副本保存
    assert mgr.get(job["id"])["kind"] == "tv"
