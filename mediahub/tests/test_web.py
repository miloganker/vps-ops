import importlib
import os
import re
import sys
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(__file__)


@pytest.fixture
def client(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", d)
    monkeypatch.setenv("APP_PASSWORD", "test-pass")
    monkeypatch.setenv("FAKE_OPENLIST", os.path.join(HERE, "..", "fixtures", "demo_tree.json"))
    monkeypatch.setenv("FAKE_TMDB", "1")
    monkeypatch.setenv("RENAME_INTERVAL", "0")
    monkeypatch.setenv("MOVE_WAIT", "0")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("DOWNLOAD_POLL_INTERVAL", "0")
    monkeypatch.setenv("DOWNLOAD_REFRESH_WAIT", "0")
    monkeypatch.setenv("MEDIAHUB_INTERNAL_TOKEN", "test-internal")
    sys.modules.pop("app.main", None)
    m = importlib.import_module("app.main")
    return TestClient(m.app), m


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def wait(m):
    for _ in range(200):
        if not m.jobs.busy():
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_login_required(client):
    c, _ = client
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = c.post("/login", data={"password": "wrong"})
    assert "密码错误" in r.text


def test_scan_preview_execute(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    assert "扫描并生成预览" in r.text
    r = c.post("/scan", data={"csrf": csrf(r.text)})
    wait(m)
    r = c.get("/preview")
    assert "绝命毒师 (2008)" in r.text and "未识别" in r.text
    sels = re.findall(r'name="sel" value="([^"]+)" checked', r.text)
    assert len(sels) == 4
    # 修改未识别的条目
    item = next(it for it in m.store.load("preview.json")["items"] if it["folder_old"] == "新建文件夹")
    r = c.post(f"/item/{item['id']}", data={"csrf": csrf(r.text), "title": "测试片", "year": "2020", "tmdb_id": ""})
    item = next(it for it in m.store.load("preview.json")["items"] if it["folder_old"] == "新建文件夹")
    assert item["executable"] and item["target_name"] == "测试片 (2020)"
    r = c.get("/preview")
    r = c.post("/execute", data={"csrf": csrf(r.text), "sel": sels})
    wait(m)
    job = m.jobs.recent(1)[0]
    assert job["type"] == "execute" and job["status"] == "done"
    assert all(i["status"] == "done" for i in job["result"]["items"])
    r = c.get(f"/job/{job['id']}")
    assert "115 请求" in r.text


def test_csrf_enforced(client):
    c, _ = client
    c.post("/login", data={"password": "test-pass"})
    r = c.post("/scan", data={"csrf": "nope"})
    assert r.status_code == 400


MOVIE_MAGNET = "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=Raging.Bull.1980.2160p.BluRay"


def test_internal_requires_token(client):
    c, m = client
    r = c.post("/internal/downloads", json={"url": MOVIE_MAGNET})
    assert r.status_code == 401
    r = c.post("/internal/downloads", json={"url": MOVIE_MAGNET},
               headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401
    old = m.settings.mediahub_internal_token
    m.settings.mediahub_internal_token = ""
    try:
        r = c.post("/internal/downloads", json={"url": MOVIE_MAGNET})
        assert r.status_code == 503
    finally:
        m.settings.mediahub_internal_token = old
    r = c.post("/internal/downloads", json={"url": MOVIE_MAGNET},
               headers={"X-Internal-Token": "test-internal"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("downloading", "needs_kind", "already_exists")
    assert body["display_name"]


def test_web_download_form_and_kind(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    assert "投递云下载" in r.text
    unclear = "magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc&dn=Some.Unknown.Release.Name"
    r = c.post("/download", data={"csrf": csrf(r.text), "url": unclear, "kind": ""}, follow_redirects=True)
    assert r.status_code == 200
    jobs = m.downloads_mgr.list_jobs(5)
    # 名字看不出类型：不再先问，直接下到 未分类/<任务号>，下完再按文件自动分类
    assert jobs and jobs[0]["status"] == "downloading" and jobs[0]["auto"] is True
    jid = jobs[0]["id"]
    assert jobs[0]["dest"].endswith("/待整理/未分类/" + jid)
    assert "未分类" in r.text or "下载中" in r.text
    # 下载中也可以提前指定类型：只记下，下完直接移到对应目录
    r = c.post(f"/download/{jid}/kind", data={"csrf": csrf(r.text), "kind": "movie"}, follow_redirects=True)
    job = m.downloads_mgr.get(jid)
    assert job["status"] == "downloading" and job["kind"] == "movie"


def test_auto_organize_toggle_web_and_internal(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    assert "自动入库：开" in r.text and "关闭自动入库" in r.text
    r = c.get("/internal/auto-organize")
    assert r.status_code == 401
    h = {"X-Internal-Token": "test-internal"}
    r = c.get("/internal/auto-organize", headers=h)
    assert r.json()["enabled"] is True and r.json()["source"] == "env"
    r = c.post("/auto-organize", data={"csrf": csrf(c.get("/").text),
                                       "enabled": "off"}, follow_redirects=True)
    assert "自动入库：关" in r.text and "开启自动入库" in r.text
    assert c.get("/internal/auto-organize", headers=h).json()["enabled"] is False
    r = c.post("/internal/auto-organize", headers=h, json={"enabled": "yes"})
    assert r.status_code == 400
    r = c.post("/internal/auto-organize", headers=h, json={"enabled": True})
    assert r.json()["enabled"] is True and r.json()["source"] == "override"
    # 名字清楚的链接，自动入库开着时也下到自己的文件夹
    r = c.post("/internal/downloads", json={"url": MOVIE_MAGNET}, headers=h)
    body = r.json()
    assert "/待整理/未分类/" in body["dest"] and body["kind"] == "movie"


def test_auto_job_shows_in_history(client):
    c, m = client
    m.jobs.store.save("jobs/20990101-000000-aaaa.json", {
        "id": "20990101-000000-aaaa", "type": "auto", "status": "done", "title": "Raging.Bull.1980",
        "download_id": "x", "started": 0, "finished": 1, "log": ["x"], "progress": {"done": 1, "total": 1},
        "error": None, "result": {"items": [{"id": "i", "kind": "movie", "folder_old": "Raging.Bull.1980",
                                             "target": "愤怒的公牛 (1980)", "status": "done", "ops": [],
                                             "errors": [], "notes": []}], "requests_115": 3, "auto": True}})
    r = c.post("/login", data={"password": "test-pass"})
    assert "自动入库 · Raging.Bull.1980" in r.text and "愤怒的公牛 (1980)" in r.text
    r = c.get("/job/20990101-000000-aaaa")
    assert "自动入库" in r.text and "已进片库" in r.text


def test_home_hides_settled_complete_jobs(client):
    c, m = client
    c.post("/login", data={"password": "test-pass"})
    m.downloads_mgr.save({
        "id": "settled-manual", "status": "complete",
        "display_name": "九龙城寨之围城-首页残留", "kind": "movie", "progress": 100,
    })
    m.downloads_mgr.save({
        "id": "settled-auto", "status": "complete",
        "display_name": "已自动入库-测试片", "kind": "movie",
        "organize": {"state": "done", "dest": "/电影/Foo (2020)"},
    })
    m.downloads_mgr.save({
        "id": "open-skipped", "status": "complete",
        "display_name": "需确认-测试剧", "kind": "tv",
        "organize": {"state": "skipped", "reasons": ["有提示"]},
    })
    m.downloads_mgr.save({
        "id": "open-kind", "status": "needs_kind",
        "display_name": "待选类型-测试",
    })
    m.downloads_mgr.save({
        "id": "open-org", "status": "organizing",
        "display_name": "正在入库-测试", "kind": "movie",
        "organize": {"state": "running"},
    })
    r = c.get("/")
    section = r.text.split("下载任务", 1)[1].split("扫描待整理", 1)[0]
    assert "九龙城寨之围城-首页残留" not in section
    assert "已自动入库-测试片" not in section
    assert "需确认-测试剧" in section
    assert "待选类型-测试" in section
    assert "正在入库-测试" in section
    assert "没有自动入库" in section or "没有自动入库" in r.text
    assert "隐藏" in section
    # 内部 API / list_jobs 仍返回已了结的完整列表
    h = {"X-Internal-Token": "test-internal"}
    ids = {j["id"] for j in c.get("/internal/downloads", headers=h).json()["jobs"]}
    assert {"settled-manual", "settled-auto", "open-skipped", "open-kind", "open-org"} <= ids
    r = c.post("/download/open-skipped/dismiss", data={"csrf": csrf(r.text)}, follow_redirects=True)
    assert r.status_code == 200
    assert "已从首页隐藏" in r.text
    section = r.text.split("下载任务", 1)[1].split("扫描待整理", 1)[0]
    assert "需确认-测试剧" not in section
    leftover = m.downloads_mgr.get("open-skipped")
    assert leftover and leftover.get("dismissed") is True
    # 进行中的任务不能隐藏
    r = c.post("/download/open-org/dismiss", data={"csrf": csrf(c.get("/").text)}, follow_redirects=True)
    assert m.downloads_mgr.get("open-org").get("dismissed") is not True
    assert "正在入库-测试" in r.text.split("下载任务", 1)[1].split("扫描待整理", 1)[0]


TV_MAGNET = "magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb&dn=Breaking.Bad.S01E01.2160p"
ED2K = "ed2k://|file|Raging.Bull.1980.mkv|12345|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|/"


def test_web_download_multiple_links(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    # 同一哈希、不同 tracker 的磁力算同一部，应记为已在队列（原文完全相同的会在提取时去重）
    dup = MOVIE_MAGNET + "&tr=udp://tracker.example.com:80"
    blob = "\n".join([MOVIE_MAGNET, TV_MAGNET, ED2K, dup])
    r = c.post("/download", data={"csrf": csrf(r.text), "url": blob, "kind": ""}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/?dl=batch&")
    # 电影磁力、剧集磁力、ed2k 各一条新任务；同哈希的第二条算已在队列
    assert "ok=3" in loc and "exists=1" in loc and "fail=0" in loc and "skip=0" in loc
    r = c.get(loc)
    assert r.status_code == 200
    assert "已处理 4 条链接：新提交 3 条，已在队列 1 条。" in r.text
    jobs = [j for j in m.downloads_mgr.list_jobs(10) if j.get("source") == "web"]
    kinds = sorted(j.get("kind") or "" for j in jobs if not j.get("reused"))
    # list_jobs 来自磁盘，没有 reused 字段；去重后应有电影 + 电视剧各一
    keys = {j["key"] for j in jobs}
    assert "btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in keys
    assert "btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in keys
    assert any(k.startswith("ed2k:") for k in keys)
    assert any(j.get("kind") == "movie" for j in jobs)
    assert any(j.get("kind") == "tv" for j in jobs)


def test_web_download_kind_applies_to_whole_paste(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    unclear = "magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc&dn=Some.Unknown.Release.Name"
    other = "magnet:?xt=urn:btih:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee&dn=Another.Unknown.Name"
    blob = unclear + " " + other
    r = c.post("/download", data={"csrf": csrf(r.text), "url": blob, "kind": "tv"}, follow_redirects=True)
    assert "新提交 2 条" in r.text
    jobs = m.downloads_mgr.list_jobs(10)
    fresh = [j for j in jobs if j.get("hash") in (
        "cccccccccccccccccccccccccccccccccccccccc",
        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    )]
    assert len(fresh) == 2
    assert all(j["kind"] == "tv" for j in fresh)


def test_web_download_caps_and_reports_skipped(client):
    c, m = client
    r = c.post("/login", data={"password": "test-pass"})
    links = [
        f"magnet:?xt=urn:btih:{i:040x}&dn=Batch.Item.{i:02d}.2020"
        for i in range(1, 23)
    ]
    r = c.post("/download", data={"csrf": csrf(r.text), "url": "\n".join(links), "kind": "movie"},
               follow_redirects=True)
    assert "已处理 20 条链接：新提交 20 条。" in r.text
    assert "另有 2 条超出每次 20 条的上限，这次没有提交，请再贴一次。" in r.text
    jobs = [j for j in m.downloads_mgr.list_jobs(80) if (j.get("display_name") or "").startswith("Batch.Item.")]
    assert len(jobs) == 20
    assert all(j.get("kind") == "movie" for j in jobs)
