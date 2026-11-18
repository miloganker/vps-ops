"""Web 界面：登录 → 扫描 → 预览/修改 → 执行 → 结果。"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import notify
from .autoorg import AutoOrganizer
from .config import Settings
from .downloads import (DownloadManager, MAX_LINKS_PER_SUBMIT, collect_links,
                        format_batch_banner)
from .executor import Executor
from .fake_openlist import FakeOpenList
from .jobs import JobManager, Store
from .openlist import OpenListClient
from .scanner import Scanner, apply_library, estimate_requests, plan
from .tmdb import TMDB, demo_fake_tmdb

HERE = os.path.dirname(__file__)
settings = Settings.from_env()
store = Store(settings.data_dir)
jobs = JobManager(store)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals["STATUS_ZH"] = notify.STATUS_ZH


def _session_secret() -> str:
    if settings.session_secret:
        return settings.session_secret
    p = os.path.join(settings.data_dir, ".session_secret")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    return open(p).read().strip()


_fake_client: Optional[FakeOpenList] = None


def make_client():
    global _fake_client
    if settings.fake_openlist:
        if _fake_client is None:
            _fake_client = FakeOpenList.from_file(settings.fake_openlist)
        return _fake_client
    return OpenListClient(settings.openlist_url, settings.openlist_user, settings.openlist_password,
                          timeout=settings.request_timeout)


def make_tmdb():
    if settings.fake_tmdb:
        return demo_fake_tmdb()
    return TMDB(settings.tmdb_api_key)


def _can_write_preview() -> bool:
    # 入库执行期间不要覆盖预览；扫描只读冲突相对可接受
    return not (jobs.busy() and jobs.current and jobs.current.get("type") in ("execute", "auto"))


downloads_mgr = DownloadManager(
    settings.data_dir, settings, make_client, make_tmdb,
    can_write_preview=_can_write_preview,
)
downloads_mgr.bind_store(store)
organizer = AutoOrganizer(settings, store, jobs, make_client, make_tmdb)
downloads_mgr.bind_organizer(organizer)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    downloads_mgr.start()
    yield
    downloads_mgr.stop()


app = FastAPI(title="mediahub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.add_middleware(SessionMiddleware, secret_key=_session_secret(), session_cookie="mediahub_session",
                   max_age=7 * 86400, same_site="lax",
                   https_only=os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes"))
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

# ------------------------------------------------------------------ 登录
_fails: dict[str, list[float]] = {}


def _digest(s: str) -> bytes:
    return hashlib.sha256(s.encode("utf-8")).digest()


def _authed(request: Request) -> bool:
    return bool(request.session.get("ok"))


def _csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(16)
        request.session["csrf"] = tok
    return tok


def _check_csrf(request: Request, token: str) -> None:
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(400, "表单已过期，请刷新页面重试")


def _guard(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return None


def render(request: Request, name: str, **ctx):
    ctx.setdefault("busy", jobs.busy())
    ctx.setdefault("current", jobs.current)
    ctx["csrf"] = _csrf(request)
    ctx["s"] = settings
    return templates.TemplateResponse(request, name, ctx)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 10:
        return render(request, "login.html", error="失败次数过多，请 10 分钟后再试")
    if settings.app_password and hmac.compare_digest(_digest(password), _digest(settings.app_password)):
        request.session.clear()
        request.session["ok"] = True
        _fails.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    recent.append(now)
    _fails[ip] = recent
    time.sleep(1)
    msg = "密码错误" if settings.app_password else "服务端没有设置 APP_PASSWORD，无法登录"
    return render(request, "login.html", error=msg)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ 页面
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if (r := _guard(request)):
        return r
    preview = store.load("preview.json")
    def _qint(name: str) -> int:
        try:
            return max(0, int(request.query_params.get(name) or 0))
        except ValueError:
            return 0

    dl_msg = request.query_params.get("dl")
    dl_batch = ""
    if dl_msg == "batch":
        dl_batch = format_batch_banner(
            ok=_qint("ok"), exists=_qint("exists"), fail=_qint("fail"),
            auto=_qint("auto"), need=_qint("need"), skip=_qint("skip"),
            cap=MAX_LINKS_PER_SUBMIT,
        )
    return render(request, "home.html", preview=preview, recent=jobs.recent(8),
                  dls=downloads_mgr.list_open_jobs(15), dl_msg=dl_msg,
                  dl_id=request.query_params.get("id"), dl_batch=dl_batch,
                  auto_org=organizer.status())


@app.post("/auto-organize")
def auto_organize_toggle(request: Request, enabled: str = Form(""), csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)
    organizer.set_enabled(enabled in ("1", "on", "true"), by="web")
    return RedirectResponse("/", status_code=303)


@app.post("/scan")
def scan(request: Request, csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)

    def fn(job):
        c = make_client()
        start = getattr(c, "requests_115", 0)
        sc = Scanner(c, make_tmdb(), settings, log=lambda m: jobs.log(job, m))
        jobs.log(job, "开始扫描待整理目录")
        data = sc.scan()
        data["requests_115"] = getattr(c, "requests_115", 0) - start
        data["scanned_at"] = time.time()
        data["job_id"] = job["id"]
        store.save("preview.json", data)
        jobs.log(job, f"扫描完成：{len(data['items'])} 项，115 请求 {data['requests_115']} 次")
        return {"requests_115": data["requests_115"], "count": len(data["items"])}

    job = jobs.start("scan", fn)
    if job is None:
        return RedirectResponse("/?busy=1", status_code=303)
    return RedirectResponse(f"/job/{job['id']}", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    if (r := _guard(request)):
        return r
    data = store.load("preview.json")
    if not data:
        return RedirectResponse("/", status_code=303)
    sel = [it for it in data["items"] if it.get("include") and it.get("executable")]
    # + 核对：每类各列一次待整理、一次片库，最后各再列一次
    est = sum(estimate_requests(it) for it in sel) + 4 * len({it["kind"] for it in sel})
    return render(request, "preview.html", data=data, est=est)


@app.post("/item/{item_id}")
async def edit_item(request: Request, item_id: str):
    if (r := _guard(request)):
        return r
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    data = store.load("preview.json")
    if not data or jobs.busy():
        return RedirectResponse("/preview", status_code=303)
    it = next((x for x in data["items"] if x["id"] == item_id), None)
    if not it:
        raise HTTPException(404)
    tmdb_id = str(form.get("tmdb_id", "")).strip()
    title = str(form.get("title", "")).strip()
    year = str(form.get("year", "")).strip()
    old_tmdb = str((it.get("tmdb") or {}).get("id", ""))
    if tmdb_id and tmdb_id != old_tmdb and tmdb_id.isdigit():
        try:
            c = make_tmdb().details(it["kind"], int(tmdb_id), (it.get("parsed") or {}).get("cn") or "")
            it["tmdb"] = {"id": c.tmdb_id, "title": c.title, "year": c.year, "original": c.original_title,
                          "score": 1.0, "alternatives": []}
            title, year = c.title, str(c.year or "")
        except Exception as e:
            it.setdefault("flags", []).append(f"TMDB ID {tmdb_id} 查询失败：{type(e).__name__}")
    if title:
        it["title"] = title
    if year.isdigit():
        it["year"] = int(year)
    it["manual"] = True
    it.pop("include", None)
    plan(it, settings)
    it["include"] = it["executable"]
    apply_library(it, (data.get("library") or {}).get(it["kind"], []))
    store.save("preview.json", data)
    return RedirectResponse(f"/preview#it-{item_id}", status_code=303)


@app.post("/execute")
async def execute(request: Request):
    if (r := _guard(request)):
        return r
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    data = store.load("preview.json")
    if not data:
        return RedirectResponse("/", status_code=303)
    chosen = set(form.getlist("sel"))
    for it in data["items"]:
        it["include"] = it["id"] in chosen and it.get("executable", False)
    store.save("preview.json", data)
    items = [it for it in data["items"] if it["include"]]
    if not items:
        return RedirectResponse("/preview?none=1", status_code=303)

    def fn(job):
        c = make_client()
        ex = Executor(c, settings, log=lambda m: jobs.log(job, m),
                      progress=lambda d, t: job["progress"].update(done=d, total=t),
                      should_stop=lambda: jobs.stop_requested)
        jobs.log(job, f"开始执行 {len(items)} 项")
        result = ex.run(items)
        jobs.log(job, f"完成，115 请求 {result['requests_115']} 次")
        data2 = store.load("preview.json") or {}
        data2["executed_job"] = job["id"]
        store.save("preview.json", data2)
        notify.telegram(settings, notify.summary(result))
        return result

    job = jobs.start("execute", fn)
    if job is None:
        return RedirectResponse("/preview?busy=1", status_code=303)
    return RedirectResponse(f"/job/{job['id']}", status_code=303)


@app.post("/stop")
def stop(request: Request, csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)
    jobs.stop()
    return RedirectResponse(f"/job/{jobs.current['id']}" if jobs.current else "/", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    if (r := _guard(request)):
        return r
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return render(request, "job.html", job=job)


@app.get("/api/job/{job_id}")
def job_api(request: Request, job_id: str):
    if not _authed(request):
        raise HTTPException(401)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return JSONResponse({"status": job["status"], "progress": job["progress"], "log": job["log"][-40:]})



# ------------------------------------------------------------------ 投递下载（与整理任务分开）
_KIND_ZH = {"movie": "电影", "tv": "电视剧"}


def _download_redirect_one(job: dict) -> RedirectResponse:
    if job["status"] == "needs_kind":
        return RedirectResponse(f"/?dl=needs_kind&id={job['id']}", status_code=303)
    if job.get("auto") and not job.get("kind"):
        return RedirectResponse(f"/?dl=auto&id={job['id']}", status_code=303)
    return RedirectResponse(f"/?dl=ok&id={job['id']}", status_code=303)


@app.post("/download")
def download_submit(request: Request, url: str = Form(""), kind: str = Form(""), csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)
    url = (url or "").strip()
    if not url:
        return RedirectResponse("/?dl=empty", status_code=303)
    links = collect_links(url, allow_http=True)
    if not links:
        return RedirectResponse("/?dl=bad", status_code=303)
    skipped = max(0, len(links) - MAX_LINKS_PER_SUBMIT)
    links = links[:MAX_LINKS_PER_SUBMIT]
    kind_arg = kind if kind in ("movie", "tv") else None
    try:
        if len(links) == 1 and not skipped:
            job = downloads_mgr.ingest(links[0], kind=kind_arg, source="web")
            return _download_redirect_one(job)
        results = downloads_mgr.ingest_many(links, kind=kind_arg, source="web")
    except ValueError:
        return RedirectResponse("/?dl=bad", status_code=303)
    except Exception:
        return RedirectResponse("/?dl=error", status_code=303)
    ok = exists = fail = auto = need = 0
    for job in results:
        st = job.get("status")
        if st == "failed" and not job.get("id"):
            fail += 1
            continue
        if job.get("reused") or st == "already_exists":
            exists += 1
            continue
        if st == "failed":
            fail += 1
            continue
        if st == "needs_kind":
            need += 1
            continue
        ok += 1
        if job.get("auto") and not job.get("kind"):
            auto += 1
    return RedirectResponse(
        f"/?dl=batch&ok={ok}&exists={exists}&fail={fail}&auto={auto}&need={need}&skip={skipped}",
        status_code=303,
    )


@app.post("/download/{job_id}/kind")
def download_kind(request: Request, job_id: str, kind: str = Form(...), csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)
    try:
        downloads_mgr.set_kind(job_id, kind)
    except (KeyError, ValueError):
        return RedirectResponse("/?dl=error", status_code=303)
    return RedirectResponse(f"/?dl=ok&id={job_id}", status_code=303)


@app.post("/download/{job_id}/dismiss")
def download_dismiss(request: Request, job_id: str, csrf: str = Form("")):
    if (r := _guard(request)):
        return r
    _check_csrf(request, csrf)
    try:
        downloads_mgr.dismiss(job_id)
    except (KeyError, ValueError):
        return RedirectResponse("/?dl=error", status_code=303)
    return RedirectResponse("/?dl=hidden", status_code=303)


# ------------------------------------------------------------------ 内部 API（只给本机 Telegram 机器人用）
def _check_internal(request: Request) -> None:
    tok = settings.mediahub_internal_token
    if not tok:
        raise HTTPException(503, "未配置内部令牌")
    got = (request.headers.get("x-internal-token") or "").strip()
    auth = (request.headers.get("authorization") or "").strip()
    if not got and auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    elif not got and auth:
        got = auth
    if not got or not hmac.compare_digest(got, tok):
        raise HTTPException(401, "unauthorized")


@app.post("/internal/downloads")
async def internal_ingest(request: Request):
    _check_internal(request)
    body = await request.json()
    url = str(body.get("url") or "").strip()
    kind = body.get("kind") or None
    if kind not in ("movie", "tv", None):
        kind = None
    if not url:
        raise HTTPException(400, "缺少 url")
    try:
        job = downloads_mgr.ingest(url, kind=kind, source="telegram")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(_job_public(job))


@app.post("/internal/downloads/{job_id}/kind")
async def internal_kind(request: Request, job_id: str):
    _check_internal(request)
    body = await request.json()
    kind = body.get("kind")
    try:
        job = downloads_mgr.set_kind(job_id, kind)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(_job_public(job))


@app.get("/internal/downloads/{job_id}")
def internal_get(request: Request, job_id: str):
    _check_internal(request)
    job = downloads_mgr.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return JSONResponse(_job_public(job))


@app.get("/internal/auto-organize")
def internal_auto_status(request: Request):
    _check_internal(request)
    return JSONResponse(organizer.status())


@app.post("/internal/auto-organize")
async def internal_auto_set(request: Request):
    _check_internal(request)
    body = await request.json()
    v = body.get("enabled")
    if not isinstance(v, bool):
        raise HTTPException(400, "enabled 必须是 true 或 false")
    return JSONResponse(organizer.set_enabled(v, by="telegram"))


@app.get("/internal/downloads")
def internal_list(request: Request):
    _check_internal(request)
    return JSONResponse({"jobs": [_job_public(j) for j in downloads_mgr.list_jobs(20)]})


def _job_public(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "kind": job.get("kind"),
        "display_name": job.get("display_name"),
        "dest": job.get("dest"),
        "tid": job.get("tid"),
        "progress": job.get("progress"),
        "error": job.get("error"),
        "reason": (job.get("classify") or {}).get("reason"),
        "auto": bool(job.get("auto")),
        "moved_to": job.get("moved_to"),
        "scan_count": job.get("scan_count"),
        "organize": {k: (job.get("organize") or {}).get(k) for k in ("state", "target", "dest", "reasons")}
        if job.get("organize") else None,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}
