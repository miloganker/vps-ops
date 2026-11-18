"""磁力 / ed2k 投递、分类、115 云下载盯梢。与整理任务的 JobManager 分开，可并行多个下载。

完成之后：AUTO_ORGANIZE 开着时交给 autoorg.AutoOrganizer（安全条件都满足才自动改名入库，
否则扫描 + 通知你确认）；关着时只扫描生成预览，不自动改名或移动。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import naming as N
from . import notify
from . import release
from .openlist import OpenListClient, OpenListError, OpenListTimeout, join
from .scanner import Scanner

# 任务状态
NEEDS_KIND = "needs_kind"
SUBMITTING = "submitting"
DOWNLOADING = "downloading"
ALREADY_EXISTS = "already_exists"
COMPLETE = "complete"
FAILED = "failed"
RESOLVING = "resolving"   # 已下完（在 未分类/<任务号>），正在看文件分类 / 移到电影或电视剧
ORGANIZING = "organizing"  # 已在待整理，等待 / 正在自动改名入库

ACTIVE = {NEEDS_KIND, SUBMITTING, DOWNLOADING, ALREADY_EXISTS, RESOLVING, ORGANIZING}
# 已完成但仍需你处理的入库状态（首页继续展示，直到自动完成或你手动隐藏）
OPEN_ORGANIZE = {"skipped", "unverified", "failed", "interrupted", "queued", "running"}
DISMISSABLE_ORGANIZE = {"skipped", "unverified", "failed", "interrupted"}
TASK_DEST_RE = re.compile(r" to \((.*)\)\s*$")
MAX_EMPTY_CHECKS = 10        # 下完后目录一直是空的：查这么多轮后放弃
MAX_PENDING_CHECKS = 3       # 移动超时后：核实这么多轮仍没到位，才再移动一次
SUBMIT_GAP = 1.2             # 连续向 115 提交离线任务的间隔（秒），避免一下压太多
MAX_LINKS_PER_SUBMIT = 20    # 网页一次粘贴 / Telegram 一条消息最多处理这么多条

MAGNET_RE = re.compile(r"(magnet:\?xt=urn:[a-z0-9]+:[A-Za-z0-9]+)([^\s<>\"\']*)", re.I)
ED2K_RE = re.compile(r"(ed2k://\|file\|[^\s<>\"\']+)", re.I)
HTTP_RE = re.compile(r"(https?://[^\s<>\"\']+)", re.I)

# OpenList 任务完成 / 失败（字符串或旧版整数）
_DONE_OK = {"succeeded", "success", "complete", "completed", "2", 2}
_DONE_FAIL = {"failed", "canceled", "cancelled", "errored", "error", "-1", -1}
_RUNNING = {
    "pending", "running", "canceling", "cancelling", "failing",
    "waiting_retry", "before_retry", "0", "1", 0, 1,
}

_KIND_ZH = {"movie": "电影", "tv": "电视剧"}


def extract_links(text: str, allow_http: bool = False) -> list[str]:
    """从一段文字里抽出磁力 / ed2k（以及可选的 http 链接）。"""
    text = text or ""
    found: list[str] = []
    for rx in (MAGNET_RE, ED2K_RE):
        for m in rx.finditer(text):
            url = (m.group(0) or "").rstrip(".,;）)】]>")
            if url and url not in found:
                found.append(url)
    if allow_http:
        for m in HTTP_RE.finditer(text):
            url = (m.group(0) or "").rstrip(".,;）)】]>")
            if url and url not in found and not url.lower().startswith("magnet:"):
                found.append(url)
    return found


def collect_links(text: str, allow_http: bool = True) -> list[str]:
    """抽出文字里的链接；若整段本身就是一条链接但正则没吃到，也收进来。已按原文去重。"""
    text = (text or "").strip()
    links = extract_links(text, allow_http=allow_http)
    if not links:
        low = text.lower()
        if low.startswith(("magnet:", "ed2k://", "http://", "https://")):
            links = [text]
    return links


def format_batch_banner(ok: int, exists: int, fail: int, auto: int = 0, need: int = 0,
                        skip: int = 0, cap: int = MAX_LINKS_PER_SUBMIT) -> str:
    """网页一次提交多条后的横幅说明。"""
    n = ok + exists + fail + need
    bits = []
    if ok:
        bits.append(f"新提交 {ok} 条")
    if exists:
        bits.append(f"已在队列 {exists} 条")
    if fail:
        bits.append(f"失败 {fail} 条")
    if need:
        bits.append(f"待选类型 {need} 条")
    head = f"已处理 {n} 条链接"
    if bits:
        head += "：" + "，".join(bits)
    head += "。"
    extras = []
    if skip:
        extras.append(f"另有 {skip} 条超出每次 {cap} 条的上限，这次没有提交，请再贴一次。")
    if auto:
        extras.append(f"其中 {auto} 条名字看不出类型，先下到“待整理/未分类”，下完按文件自动分类；实在判断不了才会问你。")
    if need:
        extras.append("请在下面的任务里选一下类型再继续。")
    return head + "".join(extras)


def parse_link(url: str) -> dict:
    """解析展示名和去重键。"""
    url = (url or "").strip()
    low = url.lower()
    info: dict = {"url": url, "display_name": "", "key": low, "kind_hint": None, "type": "unknown"}
    if low.startswith("magnet:"):
        info["type"] = "magnet"
        q = url.split("?", 1)[-1] if "?" in url else url
        qs = parse_qs(q)
        xt = (qs.get("xt") or [""])[0]
        dn = (qs.get("dn") or [""])[0]
        name = unquote(dn.replace("+", " ")) if dn else ""
        # btih / btmh hash
        hash_ = ""
        m = re.search(r"urn:bt[im]h:([A-Za-z0-9]+)", xt, re.I)
        if m:
            hash_ = m.group(1).lower()
        if not hash_:
            m = re.search(r"urn:bt[im]h:([A-Za-z0-9]+)", url, re.I)
            if m:
                hash_ = m.group(1).lower()
        info["key"] = f"btih:{hash_}" if hash_ else low
        info["display_name"] = name or (hash_[:16] if hash_ else "磁力链接")
        info["hash"] = hash_
    elif low.startswith("ed2k://"):
        info["type"] = "ed2k"
        parts = url.split("|")
        name = unquote(parts[2]) if len(parts) > 2 else ""
        hash_ = (parts[4] if len(parts) > 4 else "").rstrip("/").lower()
        info["key"] = f"ed2k:{hash_}" if hash_ else low
        info["display_name"] = name or "ed2k 链接"
        info["hash"] = hash_
    elif low.startswith("http://") or low.startswith("https://"):
        info["type"] = "http"
        path = unquote(urlparse(url).path)
        info["display_name"] = os.path.basename(path.rstrip("/")) or url
        info["key"] = low
    else:
        raise ValueError("不是磁力、ed2k 或 http 链接")
    parsed = N.parse_name(info["display_name"])
    if parsed.season is not None or parsed.episode is not None:
        info["kind_hint"] = "tv"
    info["parsed"] = {"cn": parsed.cn_title, "en": parsed.en_title, "year": parsed.year,
                      "season": parsed.season, "episode": parsed.episode}
    return info


def classify(display_name: str, tmdb, forced: Optional[str] = None) -> dict:
    """判断电影 / 电视剧。高置信才自动选定；否则需要人工选择。"""
    if forced in ("movie", "tv"):
        return {"kind": forced, "confidence": "high", "reason": "用户指定", "movie": None, "tv": None}
    parsed = N.parse_name(display_name or "")
    tv_signal = parsed.season is not None or parsed.episode is not None
    movie_m = tv_m = None
    if tmdb is not None and getattr(tmdb, "enabled", True):
        try:
            movie_m = tmdb.match("movie", parsed.en_title, parsed.cn_title, parsed.year)
        except Exception as e:
            movie_m = None
            movie_err = f"{type(e).__name__}"
        else:
            movie_err = None
        try:
            tv_m = tmdb.match("tv", parsed.en_title, parsed.cn_title, parsed.year)
        except Exception:
            tv_m = None
    else:
        movie_err = None

    def _sum(m):
        if not m or not m.best:
            return None
        return {"id": m.best.tmdb_id, "title": m.best.title, "year": m.best.year,
                "confidence": m.confidence, "score": m.score}

    movie_c = movie_m.confidence if movie_m and movie_m.best else "none"
    tv_c = tv_m.confidence if tv_m and tv_m.best else "none"
    reason = ""
    kind = None
    conf = "low"

    if tv_signal and movie_c != "high":
        kind, conf = "tv", "high" if tv_c == "high" else "high"
        reason = "文件名含季/集" + ("，TMDB 高置信电视剧" if tv_c == "high" else "")
    elif movie_c == "high" and tv_c != "high":
        kind, conf, reason = "movie", "high", "TMDB 高置信电影"
    elif tv_c == "high" and movie_c != "high":
        kind, conf, reason = "tv", "high", "TMDB 高置信电视剧"
    elif movie_c == "high" and tv_c == "high":
        reason = "电影和电视剧都是高置信，需要确认"
    else:
        reason = "无法可靠判断是电影还是电视剧"
        if movie_err:
            reason += f"（TMDB 电影查询失败：{movie_err}）"

    return {"kind": kind, "confidence": conf if kind else "low", "reason": reason,
            "movie": _sum(movie_m), "tv": _sum(tv_m), "tv_signal": tv_signal}


def task_outcome(task: Optional[dict]) -> str:
    """返回 running / succeeded / failed / unknown。"""
    if not task:
        return "unknown"
    state = task.get("state")
    err = str(task.get("error") or "")
    status = str(task.get("status") or "")
    if state in _DONE_FAIL or "10008" in err and state in _DONE_FAIL:
        return "failed"
    if state in _DONE_OK:
        return "failed" if err and "10008" not in err and "任务已存在" not in err else "succeeded"
    if state in _RUNNING:
        # 个别版本把 10008 写在仍在执行的 task.error 里：当作已在队列里，继续盯
        return "running"
    # 文本兜底
    low = (status + " " + err).lower()
    if "completed" in low or "成功" in status:
        return "succeeded"
    if "fail" in low or "失败" in status or "deleted" in low:
        return "failed"
    if state is None:
        return "unknown"
    return "running"


def _task_matches(task: dict, job: dict) -> bool:
    if str(task.get("id") or "") in (job.get("ignore_tids") or []):
        return False
    blob = " ".join(str(task.get(k) or "") for k in ("id", "name", "status", "error")).lower()
    key = (job.get("key") or "").lower()
    url = (job.get("url") or "").lower()
    h = (job.get("hash") or "").lower()
    tid = (job.get("tid") or "").lower()
    if tid and str(task.get("id") or "").lower() == tid:
        return True
    if h and h in blob:
        return True
    if key and key.split(":")[-1] and key.split(":")[-1] in blob:
        return True
    if url and url[:80] in blob:
        return True
    return False


def is_open_job(job: Optional[dict]) -> bool:
    """首页“下载任务”只列还需要处理的：进行中、失败、或下完但入库未了结。"""
    if not job or job.get("dismissed") or job.get("archived"):
        return False
    status = job.get("status")
    if status in ACTIVE or status == FAILED:
        return True
    if status == COMPLETE:
        state = (job.get("organize") or {}).get("state")
        return state in OPEN_ORGANIZE
    return False


def can_dismiss(job: Optional[dict]) -> bool:
    """失败，或已完成但仍停在需确认/未自动入库，可以从首页隐藏（不删记录）。"""
    if not job or job.get("dismissed") or job.get("archived"):
        return False
    status = job.get("status")
    if status == FAILED:
        return True
    if status == COMPLETE:
        state = (job.get("organize") or {}).get("state")
        return state in DISMISSABLE_ORGANIZE
    return False


class DownloadManager:

    def __init__(self, data_dir: str, settings, client_factory: Callable, tmdb_factory: Callable,
                 can_write_preview: Optional[Callable[[], bool]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.dir = os.path.join(data_dir, "downloads")
        os.makedirs(self.dir, exist_ok=True)
        self.settings = settings
        self.client_factory = client_factory
        self.tmdb_factory = tmdb_factory
        self.can_write_preview = can_write_preview or (lambda: True)
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._last_move: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_submit: Optional[float] = None
        self._submit_gate = threading.Lock()
        self._preview_store_save: Optional[Callable] = None
        self._preview_store_load: Optional[Callable] = None
        self.organizer = None

    def bind_organizer(self, organizer) -> None:
        self.organizer = organizer
        organizer.downloads = self

    def bind_store(self, store) -> None:
        self._preview_store_save = store.save
        self._preview_store_load = store.load

    # ---- 持久化
    def _path(self, job_id: str) -> str:
        return os.path.join(self.dir, job_id + ".json")

    def save(self, job: dict) -> None:
        if job.get("auto") and not job.get("kind"):
            # 轮询线程拿着旧副本时，用户可能刚在按钮上选了类型：别把选择覆盖掉
            disk = self.get(job["id"]) if job.get("id") else None
            if disk and disk.get("kind") in ("movie", "tv"):
                job["kind"] = disk["kind"]
                job["classify"] = disk.get("classify") or job.get("classify")
        job = {k: v for k, v in job.items() if not k.startswith("_")}
        tmp = self._path(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path(job["id"]))

    def get(self, job_id: str) -> Optional[dict]:
        try:
            with open(self._path(job_id), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _iter_jobs_by_mtime(self):
        files = [f for f in os.listdir(self.dir) if f.endswith(".json")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(self.dir, f)), reverse=True)
        for fn in files:
            j = self.get(fn[:-5])
            if j:
                yield j

    def list_jobs(self, n: int = 30) -> list[dict]:
        """按修改时间倒序的完整任务列表（内部 API / 历史用，含已了结）。"""
        out = []
        for j in self._iter_jobs_by_mtime():
            out.append(j)
            if len(out) >= n:
                break
        return out

    def list_open_jobs(self, n: int = 30) -> list[dict]:
        """首页用：只返回还需要处理的任务，扫过已了结的记录，避免把更早的待办挤掉。"""
        out = []
        for j in self._iter_jobs_by_mtime():
            if is_open_job(j):
                out.append(j)
                if len(out) >= n:
                    break
        return out

    def dismiss(self, job_id: str) -> dict:
        job = self.get(job_id)
        if not job:
            raise KeyError(job_id)
        if not can_dismiss(job):
            raise ValueError("当前状态不能从首页隐藏")
        job["dismissed"] = True
        job["dismissed_at"] = self.clock()
        self.save(job)
        return job

    def find_active(self, key: str) -> Optional[dict]:
        for j in self.list_jobs(80):
            if j.get("key") == key and j.get("status") in ACTIVE:
                return j
        return None

    # ---- 投递
    def ingest(self, url: str, kind: Optional[str] = None, source: str = "web",
               submit: bool = True) -> dict:
        info = parse_link(url)
        url = info["url"]
        existing = self.find_active(info["key"])
        if existing:
            return {**existing, "reused": True, "duplicate_of": existing["id"]}
        if kind not in ("movie", "tv"):
            kind = None
        c = classify(info["display_name"], self.tmdb_factory(), forced=kind)
        sure = bool(c["kind"] and c["confidence"] == "high")
        # 自动入库开着：所有链接都下到自己的 未分类/<任务号>，这样下完能准确认出“这个任务的那一项”
        own_dir = (not sure) or bool(self.organizer and self.organizer.enabled())
        job = {
            "id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4],
            "url": url,
            "key": info["key"],
            "hash": info.get("hash") or "",
            "display_name": info["display_name"],
            "link_type": info["type"],
            "source": source,
            "kind": c["kind"] if sure else None,
            "classify": c,
            # 名字看不出类型：不再先问，直接下到 未分类/<任务号>，下完看文件再分
            # （自动入库开着时，名字看得出类型的也下到自己的文件夹，下完直接移到对应待整理目录）
            "auto": own_dir,
            "status": SUBMITTING,
            "tid": None,
            "dest": "",
            "progress": 0,
            "task_status": "",
            "error": None,
            "created": self.clock(),
            "updated": self.clock(),
            "finished": None,
            "scan_count": None,
            "log": [],
        }
        if job["auto"]:
            job["dest"] = self._unsorted_dir(job["id"])
        else:
            job["dest"] = self._dest(job["kind"])
        self._log(job, f"收到 {info['type']}：{job['display_name']}")
        if job["auto"] and job["kind"]:
            self._log(job, f"按名字识别为{_KIND_ZH[job['kind']]}（{c.get('reason') or ''}），先下到 "
                      + job["dest"] + f"，下完移到待整理/{_KIND_ZH[job['kind']]}")
        elif job["auto"]:
            self._log(job, "链接名字看不出电影还是电视剧（" + (c.get("reason") or "") +
                      "），先下到 " + job["dest"] + "，下完按文件自动分类")
        self.save(job)
        if submit:
            self._submit_with_gap(job)
        return job

    def ingest_many(self, urls: list[str], kind: Optional[str] = None, source: str = "web") -> list[dict]:
        """一次收多条链接。本地先建任务并去重；向 115 的提交稍稍错开。
        测试里（轮询间隔为 0）同步提交，方便立刻断言；线上多条时在后台提交，以免网页请求卡住。"""
        results: list[dict] = []
        pending: list[str] = []
        for url in urls:
            try:
                job = self.ingest(url, kind=kind, source=source, submit=False)
                results.append(job)
                if not job.get("reused") and job.get("status") == SUBMITTING and not job.get("tid"):
                    pending.append(job["id"])
            except ValueError as e:
                results.append({"url": url, "status": FAILED, "error": str(e)})
            except Exception as e:
                results.append({"url": url, "status": FAILED, "error": f"{type(e).__name__}: {e}"})
        self._submit_jobs(pending)
        # 同步提交后把最终状态写回（后台提交时仍是 submitting）
        out = []
        for job in results:
            jid = job.get("id")
            if jid and not job.get("reused"):
                fresh = self.get(jid)
                if fresh:
                    out.append(fresh)
                    continue
            out.append(job)
        return out

    def _submit_jobs(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        interval = float(getattr(self.settings, "download_poll_interval", 90) or 0)
        if interval <= 0:
            for jid in job_ids:
                job = self.get(jid)
                if job and job.get("status") == SUBMITTING and not job.get("tid"):
                    self._submit_with_gap(job)
            return
        threading.Thread(target=self._submit_jobs_bg, args=(list(job_ids),),
                         daemon=True, name="download-submit").start()

    def _submit_jobs_bg(self, job_ids: list[str]) -> None:
        for jid in job_ids:
            try:
                job = self.get(jid)
                if job and job.get("status") == SUBMITTING and not job.get("tid"):
                    self._submit_with_gap(job)
            except Exception as e:
                self.log(f"submit {jid}: {type(e).__name__}: {e}")

    def _submit_with_gap(self, job: dict) -> None:
        """同一时刻只向 115 提交一个离线任务，两次之间至少隔 SUBMIT_GAP 秒。"""
        with self._submit_gate:
            last = self._last_submit
            now = self.clock()
            if last is not None:
                wait = SUBMIT_GAP - (now - last)
                if wait > 0:
                    self.sleep(wait)
            self._last_submit = self.clock()
            self._submit(job)

    def set_kind(self, job_id: str, kind: str) -> dict:
        if kind not in ("movie", "tv"):
            raise ValueError("kind 必须是 movie 或 tv")
        action = None
        with self._lock:
            job = self.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.get("auto") and not job.get("moved_to") and job["status"] in (
                    DOWNLOADING, ALREADY_EXISTS, SUBMITTING, RESOLVING, NEEDS_KIND):
                # 自动分类任务：只记下类型；下完（或已下完）后直接移到对应待整理目录
                job["kind"] = kind
                job["classify"] = {"kind": kind, "confidence": "high", "reason": "你选择了" + _KIND_ZH[kind]}
                job["updated"] = self.clock()
                if job["status"] == NEEDS_KIND:
                    job["status"] = RESOLVING
                    self._log(job, f"已选择{_KIND_ZH[kind]}，移到待整理/{_KIND_ZH[kind]}并扫描")
                    action = "resolve"
                else:
                    self._log(job, f"已选择{_KIND_ZH[kind]}，下完后直接移到待整理/{_KIND_ZH[kind]}")
                self.save(job)
            elif job["status"] not in (NEEDS_KIND, FAILED):
                # 已经在下或已完成：只允许在等待分类时设置
                if job.get("kind") == kind:
                    return job
                raise ValueError(f"当前状态 {job['status']} 不能再改类型")
            else:
                # 旧流程（升级前留下的“先选类型再提交”任务，或失败后重选）
                job["kind"] = kind
                job["auto"] = False
                job["dest"] = self._dest(kind)
                job["status"] = SUBMITTING
                job["error"] = None
                job["updated"] = self.clock()
                self._log(job, f"已选择{_KIND_ZH[kind]}，开始提交 115 云下载")
                self.save(job)
                action = "submit"
        if action == "submit":
            self._submit_with_gap(job)
            return job
        if action == "resolve":
            self.kick(job["id"])
        return self.get(job["id"]) or job

    def _unsorted_dir(self, job_id: str) -> str:
        return join(self.settings.staging_unsorted, job_id)

    def _dest(self, kind: str) -> str:
        return self.settings.staging_movies if kind == "movie" else self.settings.staging_tv

    def _log(self, job: dict, msg: str) -> None:
        job.setdefault("log", []).append(time.strftime("%H:%M:%S ") + msg)
        self.log(msg)

    def _submit(self, job: dict) -> None:
        auto = bool(job.get("auto"))
        if not job.get("kind") and not auto:
            job["status"] = NEEDS_KIND
            self.save(job)
            return
        job["status"] = SUBMITTING
        job["dest"] = self._unsorted_dir(job["id"]) if auto else self._dest(job["kind"])
        job["updated"] = self.clock()
        self.save(job)
        try:
            c = self.client_factory()
            if auto:
                # 每个任务一个空文件夹：下完里面出现的东西就是这条链接的内容（只建文件夹，不删任何东西）
                c.mkdir(job["dest"])
            data = c.add_offline_download([job["url"]], job["dest"], tool="115 Cloud",
                                          delete_policy="delete_never")
        except OpenListError as e:
            if OpenListClient.is_already_exists(e):
                job["status"] = ALREADY_EXISTS
                job["error"] = str(e)
                job["updated"] = self.clock()
                self._log(job, "115 提示任务已存在（10008），不再重复提交，改为盯梢已有任务")
                self.save(job)
                return
            job["status"] = FAILED
            job["error"] = str(e)
            job["finished"] = self.clock()
            job["updated"] = self.clock()
            self._log(job, "提交失败：" + str(e))
            self.save(job)
            return
        except Exception as e:
            job["status"] = FAILED
            job["error"] = f"{type(e).__name__}: {e}"
            job["finished"] = self.clock()
            job["updated"] = self.clock()
            self._log(job, "提交失败：" + job["error"])
            job["trace"] = traceback.format_exc()[-2000:]
            self.save(job)
            return
        tasks = (data or {}).get("tasks") or []
        if tasks:
            job["tid"] = tasks[0].get("id")
            job["task_status"] = str(tasks[0].get("status") or tasks[0].get("state") or "")
        already = bool((data or {}).get("already_exists"))
        if already:
            job["status"] = ALREADY_EXISTS
            self._log(job, "115 提示任务已存在（10008），不再重复提交，改为盯梢已有任务")
        else:
            job["status"] = DOWNLOADING
            self._log(job, "已提交 115 云下载" + (f"，任务 {job['tid']}" if job.get("tid") else ""))
        job["updated"] = self.clock()
        self.save(job)

    # ---- 盯梢
    def kick(self, job_id: Optional[str] = None) -> None:
        """选完类型后尽快处理（测试里同步执行；线上起一个后台线程）。"""
        if float(getattr(self.settings, "download_poll_interval", 90) or 0) <= 0:
            self.tick()
            return
        threading.Thread(target=self._safe_tick, daemon=True, name="download-kick").start()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as e:
            self.log(f"download tick: {e}")

    def tick(self) -> None:
        # 轮询线程和“选完类型”的线程可能同时进来：同一时间只跑一轮，避免两个移动叠在一起（990009）
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
            if self.organizer is not None:
                try:
                    self.organizer.pump()
                except Exception as e:
                    self.log(f"auto organize pump: {type(e).__name__}: {e}")
        finally:
            self._tick_lock.release()

    def _tick(self) -> None:
        for job in self.list_jobs(80):
            if job.get("status") == RESOLVING:
                try:
                    self._resolve(self.client_factory(), job)
                except Exception as e:
                    self._log(job, f"自动分类出错：{type(e).__name__}: {e}")
                    job["updated"] = self.clock()
                    self.save(job)
                continue
            if job.get("status") in (DOWNLOADING, ALREADY_EXISTS, SUBMITTING):
                # submitting 残留：上次进程在提交中途退出。没有 tid 则再提交一次（10008 是安全的）。
                if job["status"] == SUBMITTING and not job.get("tid"):
                    # 避免刚刚写入还没提交完的任务被立刻再提交：created 后至少过 1 秒
                    if self.clock() - job.get("updated", 0) < 1:
                        continue
                    self._submit(job)
                    continue
                try:
                    self._poll_one(job)
                except Exception as e:
                    self._log(job, f"查询状态出错：{type(e).__name__}: {e}")
                    job["updated"] = self.clock()
                    self.save(job)

    def _poll_one(self, job: dict) -> None:
        c = self.client_factory()
        task = None
        if job.get("tid"):
            try:
                task = c.offline_task_info(job["tid"])
            except OpenListError:
                task = None
        if task is None:
            try:
                pool = (c.offline_tasks_undone() or []) + (c.offline_tasks_done() or [])
            except OpenListError:
                pool = []
            task = next((t for t in pool if _task_matches(t, job)), None)
            if task and not job.get("tid"):
                job["tid"] = task.get("id")
                self._adopt_task_dest(job, task)
        outcome = task_outcome(task)
        if outcome == "failed" and task and OpenListClient.is_already_exists(
                Exception(str(task.get("error") or "") + " " + str(task.get("status") or ""))):
            # 115 拒绝了（10008 任务已存在）：不重新提交，改为盯已有的那条任务
            job.setdefault("ignore_tids", []).append(str(task.get("id") or ""))
            job["tid"] = None
            job["status"] = ALREADY_EXISTS
            job["error"] = None
            job["updated"] = self.clock()
            self._log(job, "115 提示任务已存在（10008），不再重复提交，改为盯梢已有任务")
            self.save(job)
            return
        if task:
            job["progress"] = task.get("progress") or 0
            job["task_status"] = str(task.get("status") or task.get("state") or "")
            if task.get("error") and "10008" not in str(task.get("error")):
                job["error"] = str(task.get("error"))
        if outcome == "running" or (outcome == "unknown" and not self._staging_has_new(c, job)):
            job["updated"] = self.clock()
            self.save(job)
            return
        if outcome == "failed" and not self._staging_has_new(c, job):
            job["status"] = FAILED
            job["error"] = job.get("error") or job.get("task_status") or "离线下载失败"
            job["finished"] = self.clock()
            job["updated"] = self.clock()
            self._log(job, "下载失败：" + job["error"])
            self.save(job)
            notify.telegram(self.settings, notify.download_failed(self.settings, job))
            return
        # succeeded，或列表尚未给出任务但目录里已经出现了文件
        if job.get("auto") and not job.get("moved_to"):
            job["status"] = RESOLVING
            job["progress"] = 100
            job["updated"] = self.clock()
            self._log(job, "115 云下载完成，查看下载内容自动判断电影 / 电视剧")
            self.save(job)
            self._resolve(c, job)
            return
        self._on_complete(c, job)

    def _adopt_task_dest(self, job: dict, task: dict) -> None:
        """盯的是已有任务（10008）时，按它真正的目标目录来处理。"""
        m = TASK_DEST_RE.search(str(task.get("name") or ""))
        if not m:
            return
        dest = m.group(1).rstrip("/")
        if not dest or dest == job.get("dest"):
            return
        self._log(job, f"已有任务的目标目录是 {dest}")
        job["dest"] = dest
        if dest == self.settings.staging_movies:
            job.update(kind="movie", auto=False)
        elif dest == self.settings.staging_tv:
            job.update(kind="tv", auto=False)
        elif dest.startswith(self.settings.staging_unsorted + "/"):
            job["auto"] = True
        else:
            job["auto"] = False

    def _staging_has_new(self, client, job: dict) -> bool:
        dest = job.get("dest") or (self._dest(job["kind"]) if job.get("kind") else "")
        if not dest:
            return False
        if job.get("auto") and dest.startswith(self.settings.staging_unsorted + "/"):
            # 自己的空文件夹里出现了东西 = 下完了
            try:
                return bool(client.list(dest, refresh=True))
            except Exception:
                return False
        try:
            entries = client.list(dest, refresh=True)
        except Exception:
            return False
        names = [e.name for e in entries]
        job["_staging_names"] = names
        hint = (job.get("display_name") or "").strip()
        if not hint:
            return False
        # 宽松：目录里出现和展示名相关的项（下载完成后 115 列表可能稍晚刷新，调用方会再刷一次）
        stem = N.parse_name(hint)
        keys = [k.lower() for k in (hint, stem.en_title, stem.cn_title) if k and len(k) >= 2]
        for n in names:
            nl = n.lower()
            if any(k in nl or nl in k for k in keys):
                return True
        return False

    # ---- 下完之后：看文件分类 → 移到 待整理/电影 或 待整理/电视剧
    def _resolve(self, client, job: dict) -> None:
        job = self.get(job["id"]) or job
        if job.get("status") != RESOLVING:
            return
        src = job.get("dest") or self._unsorted_dir(job["id"])
        if job.get("move_pending"):
            self._check_pending_move(client, job)
            return
        if not job.get("kind") or not job.get("contents"):
            try:
                info = release.inspect(client, src, int(float(self.settings.ad_max_size_mb) * 1024 * 1024))
            except OpenListError as e:
                info = {"top": [], "files": [], "season_dirs": [], "error": str(e)}
            if not info["top"]:
                # 115 列表偶尔滞后：等一下再刷一次
                wait = float(getattr(self.settings, "download_refresh_wait", 30) or 0)
                if wait > 0:
                    self.sleep(wait)
                try:
                    info = release.inspect(client, src, int(float(self.settings.ad_max_size_mb) * 1024 * 1024))
                except OpenListError:
                    pass
            if not info["top"]:
                job["empty_checks"] = int(job.get("empty_checks") or 0) + 1
                job["updated"] = self.clock()
                if job["empty_checks"] >= MAX_EMPTY_CHECKS:
                    job["status"] = FAILED
                    job["error"] = f"115 显示下载完成，但 {src} 里一直是空的（没有移动或删除任何东西）"
                    job["finished"] = self.clock()
                    self._log(job, job["error"])
                    self.save(job)
                    notify.telegram(self.settings, notify.download_failed(self.settings, job))
                    return
                self._log(job, f"{src} 里暂时还看不到文件，下一轮再查")
                self.save(job)
                return
            job["contents"] = {
                "top": info["top"][:20],
                "videos": [f["name"] for f in info["files"] if f["video"] and not f["ad"]][:12],
                "video_count": len([f for f in info["files"] if f["video"] and not f["ad"]]),
            }
            if len(info["top"]) == 1:
                job["display_name"] = info["top"][0]["name"]
            if not job.get("kind"):
                r = release.classify_release(info, self.tmdb_factory())
                job["classify"] = {"kind": r["kind"], "confidence": "high" if r["kind"] else "low",
                                   "reason": r["reason"], "auto": True,
                                   "movie": r["tmdb_movie"], "tv": r["tmdb_tv"],
                                   "title_from": r["title_from"]}
                if not r["kind"]:
                    job["status"] = NEEDS_KIND
                    job["downloaded"] = True
                    job["updated"] = self.clock()
                    self._log(job, "看了下载内容还是判断不了：" + r["reason"] + "，等你选择")
                    self.save(job)
                    notify.telegram(self.settings, notify.download_needs_kind(self.settings, job),
                                    reply_markup=notify.kind_keyboard(job["id"]))
                    return
                job["kind"] = r["kind"]
                self._log(job, f"识别为{_KIND_ZH[r['kind']]}：{r['reason']}")
            self.save(job)
        self._move_into_staging(client, job)

    def _wait_executor_idle(self, limit: float = 600) -> bool:
        deadline = self.clock() + limit
        while not self.can_write_preview():
            if self.clock() >= deadline:
                return False
            self.sleep(2)
        return True

    def _space_moves(self) -> None:
        gap = float(self.settings.move_wait or 0)
        if self._last_move is not None and gap > 0:
            left = gap - (self.clock() - self._last_move)
            if left > 0:
                self.sleep(left)

    def _move_into_staging(self, client, job: dict) -> None:
        kind = job["kind"]
        src = job.get("dest") or self._unsorted_dir(job["id"])
        dst = self._dest(kind)
        if not self._wait_executor_idle():
            self._log(job, "正在执行入库，等它结束后再移动")
            job["updated"] = self.clock()
            self.save(job)
            return
        top = client.list(src, refresh=True)
        if not top:
            self._log(job, f"{src} 是空的，下一轮再查")
            job["updated"] = self.clock()
            self.save(job)
            return
        parent, folder = src.rsplit("/", 1)
        if len(top) == 1:
            src_dir, item = src, top[0].name
        else:
            # 多个散文件：把任务文件夹改成片名再整个移走（只在 未分类 里改名，不动片库）
            title = (job.get("classify") or {}).get("title_from") or job.get("display_name") or folder
            p = N.parse_name(title)
            want = N.folder_name(p.cn_title or p.en_title, p.year) if (p.cn_title or p.en_title) else folder
            want = N.safe_title(want) or folder
            if want != folder:
                try:
                    siblings = [e.name for e in client.list(parent, refresh=True)]
                except OpenListError:
                    siblings = []
                if want in siblings:
                    want = f"{want} {job['id'][-4:]}"
                try:
                    client.rename(src, want)
                except OpenListTimeout:
                    pass   # 115 改名常超时但已成功：下面核实，不重试
                names = [e.name for e in client.list(parent, refresh=True)]
                if want in names:
                    job["dest"] = src = join(parent, want)
                    self._log(job, f"散文件较多，把任务文件夹改名为 {want}")
                elif folder not in names:
                    self._log(job, "改名后找不到任务文件夹，下一轮再查")
                    job["updated"] = self.clock()
                    self.save(job)
                    return
                else:
                    want = folder
            src_dir, item = parent, want
            self.save(job)
        try:
            existing = [e.name for e in client.list(dst, refresh=True)]
        except OpenListError:
            existing = []
        if item in existing:
            job["status"] = COMPLETE
            job["finished"] = self.clock()
            job["updated"] = self.clock()
            job["error"] = f"待整理/{_KIND_ZH[kind]} 里已经有同名的 {item}，没有移动，文件留在 {join(src_dir, item)}"
            self._log(job, job["error"])
            self.save(job)
            notify.telegram(self.settings, notify.download_failed(self.settings, job))
            return
        if self._do_move(client, job, src_dir, dst, item):
            self._after_move(client, job, src_dir, dst, item)

    def _do_move(self, client, job: dict, src_dir: str, dst: str, item: str) -> bool:
        """一次只移动一个；遇到 990009 等一会儿再试；超时不重试，下一轮先核实。"""
        tries = 3 if self.settings.move_retry_on_busy else 1
        for i in range(tries):
            self._space_moves()
            try:
                client.move(src_dir, dst, [item])
                self._last_move = self.clock()
                self._log(job, f"已移动 {item} → {dst}")
                return True
            except OpenListTimeout:
                self._last_move = self.clock()
                job["move_pending"] = {"src_dir": src_dir, "dst": dst, "item": item, "checks": 0}
                job["updated"] = self.clock()
                self._log(job, "移动请求超时：不重试，下一轮先核实是否已经移过去")
                self.save(job)
                return False
            except OpenListError as e:
                self._last_move = self.clock()
                if e.busy and i + 1 < tries:
                    self._log(job, "115 上一个移动还没处理完（990009），稍后再试")
                    continue
                job["updated"] = self.clock()
                self._log(job, f"移动失败：{e}，下一轮再试")
                job["move_errors"] = int(job.get("move_errors") or 0) + 1
                if job["move_errors"] >= 5:
                    job["status"] = FAILED
                    job["error"] = f"移动到待整理失败：{e}（文件仍在 {join(src_dir, item)}）"
                    job["finished"] = self.clock()
                    self.save(job)
                    notify.telegram(self.settings, notify.download_failed(self.settings, job))
                    return False
                self.save(job)
                return False
        return False

    def _check_pending_move(self, client, job: dict) -> None:
        mp = job["move_pending"]
        try:
            at_dst = mp["item"] in [e.name for e in client.list(mp["dst"], refresh=True)]
        except OpenListError:
            at_dst = False
        if at_dst:
            job.pop("move_pending", None)
            self._log(job, "核实：已经移过去了")
            self._after_move(client, job, mp["src_dir"], mp["dst"], mp["item"], verified=True)
            return
        try:
            at_src = mp["item"] in [e.name for e in client.list(mp["src_dir"], refresh=True)]
        except OpenListError:
            at_src = True
        mp["checks"] = int(mp.get("checks") or 0) + 1
        job["updated"] = self.clock()
        if at_src and mp["checks"] >= MAX_PENDING_CHECKS:
            job.pop("move_pending", None)
            self._log(job, "核实：还在原处，没有移过去，再移动一次")
            self.save(job)
            if self._do_move(client, job, mp["src_dir"], mp["dst"], mp["item"]):
                self._after_move(client, job, mp["src_dir"], mp["dst"], mp["item"])
            return
        self._log(job, "核实：还没看到移动结果，下一轮再查")
        self.save(job)

    def _after_move(self, client, job: dict, src_dir: str, dst: str, item: str,
                    verified: bool = False) -> None:
        if not verified:
            # 等 115 处理完这次移动再核实（也给下一个移动留出间隔）
            gap = float(self.settings.move_wait or 0)
            if gap > 0:
                self.sleep(gap)
            try:
                ok = item in [e.name for e in client.list(dst, refresh=True)]
            except OpenListError:
                ok = False
            if not ok:
                job["move_pending"] = {"src_dir": src_dir, "dst": dst, "item": item, "checks": 0}
                job["updated"] = self.clock()
                self._log(job, "移动后暂时没在目标目录看到，下一轮再核实（不会重复移动）")
                self.save(job)
                return
        job["moved_to"] = join(dst, item)
        job["dest"] = dst
        self.save(job)
        self._on_complete(client, job)

    def _on_complete(self, client, job: dict) -> None:
        dest = job.get("dest") or self._dest(job["kind"])
        org = self.organizer
        if org is not None and org.wants(job):
            # 交给自动入库：在 JobManager 里单飞执行（扫描 → 安全检查 → 改名 → 移进片库），忙就排队
            job["status"] = ORGANIZING
            job["organize"] = {"state": "queued", "queued_at": self.clock()}
            job["updated"] = self.clock()
            job.pop("_staging_names", None)
            self._log(job, "已在待整理，排队自动改名入库（安全检查不通过就改为通知你确认）")
            self.save(job)
            org.request(job["id"])
            return
        auto_reasons = None
        if org is not None:
            if not org.enabled():
                auto_reasons = ["自动入库已关闭"]
            else:
                auto_reasons = ["这个任务不是下到自己的文件夹（例如 115 里已有的旧任务），无法确定待整理里哪一项属于它"]
            job["organize"] = {"state": "skipped", "reasons": auto_reasons, "updated": self.clock()}
        self._log(job, "115 云下载完成，刷新待整理目录并扫描（不自动入库）")
        try:
            client.list(dest, refresh=True)
        except Exception as e:
            self._log(job, f"刷新目录失败：{e}")
        # 列表有时会滞后，再给一次刷新机会（测试里把等待设成 0）
        wait = float(getattr(self.settings, "download_refresh_wait", 30) or 0)
        if wait > 0:
            self.sleep(wait)
            try:
                client.list(dest, refresh=True)
            except Exception:
                pass
        # 等整理执行结束后再覆盖预览，避免和入库任务打架
        deadline = self.clock() + 120
        while not self.can_write_preview() and self.clock() < deadline:
            self.sleep(1)
        count = None
        try:
            sc = Scanner(client, self.tmdb_factory(), self.settings,
                         log=lambda m: self._log(job, m))
            data = sc.scan()
            data["scanned_at"] = self.clock()
            data["requests_115"] = getattr(client, "requests_115", 0)
            data["job_id"] = job["id"]
            data["from_download"] = True
            if self._preview_store_save:
                self._preview_store_save("preview.json", data)
            count = len(data.get("items") or [])
            self._log(job, f"扫描完成：{count} 项，请打开网页确认后再入库")
        except Exception as e:
            self._log(job, f"扫描失败：{type(e).__name__}: {e}")
        job["status"] = COMPLETE
        job["scan_count"] = count
        job["finished"] = self.clock()
        job["updated"] = self.clock()
        job.pop("_staging_names", None)
        self.save(job)
        notify.telegram(self.settings, notify.download_complete(self.settings, job, count,
                                                                auto_reasons=auto_reasons))

    # ---- 后台线程
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        interval = float(getattr(self.settings, "download_poll_interval", 90) or 0)
        if interval <= 0:
            return
        self._stop.clear()
        def loop():
            # 重启后先把进行中的任务盯一轮，再按间隔轮询
            try:
                self.tick()
            except Exception as e:
                self.log(f"download poller start tick: {e}")
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception as e:
                    self.log(f"download poller: {e}")
        self._thread = threading.Thread(target=loop, daemon=True, name="download-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
