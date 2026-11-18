"""全自动入库：下完 → 分类 → 移到待整理 → 扫描 → （全部安全条件满足时）改名 → 移进正式片库。

- 只处理“这个下载任务自己的那一项”（下到 未分类/<任务号> 再移到待整理的那个文件夹），
  从不碰待整理里其它东西。
- 改名 / 移动用现有的 Executor（改名间隔 RENAME_INTERVAL，移动一次一个、间隔 MOVE_WAIT，
  超时不重试、最后核对），并在 JobManager 里单飞执行：同一时间只有一个扫描/执行任务，
  忙的时候排队，下一轮再试。
- 任何一条安全条件不满足：不改名不移动，退回到“扫描 + 通知你去网页确认”。
- 从不删除任何东西。
"""
from __future__ import annotations

import json
import os
import posixpath
import time
from typing import Callable, Optional

from . import naming as N
from . import notify
from .executor import Executor
from .openlist import join
from .scanner import Scanner

# 下载任务（downloads/*.json）里 organize.state 的取值
QUEUED = "queued"          # 等 JobManager 空出来
RUNNING = "running"
DONE = "done"              # 已进片库（或新季已并入）
SKIPPED = "skipped"        # 安全条件不满足，退回人工确认
UNVERIFIED = "unverified"  # 移动超时 / 核对时还没看到，后面只核对，不重试
FAILED = "failed"
INTERRUPTED = "interrupted"  # 服务重启等原因中断：不自动重来，请人工核对

# 不影响结果的提示：允许自动执行
BENIGN_PLAN_FLAGS = ("原名只有 UHD/4K，分辨率按 2160p 写入", "：文件名没有标签，标签取自文件夹名")
MAX_VERIFY_CHECKS = 5
_KIND_ZH = {"movie": "电影", "tv": "电视剧"}


class AutoOrganizer:
    def __init__(self, settings, store, jobs, client_factory: Callable, tmdb_factory: Callable,
                 downloads=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.s = settings
        self.store = store
        self.jobs = jobs
        self.client_factory = client_factory
        self.tmdb_factory = tmdb_factory
        self.downloads = downloads
        self.sleep = sleep
        self.clock = clock
        self.log = log
        self._override_path = os.path.join(settings.data_dir, "auto_organize.json")

    # ------------------------------------------------------------ 开关
    def _override(self) -> Optional[bool]:
        try:
            with open(self._override_path, encoding="utf-8") as f:
                v = json.load(f).get("enabled")
            return v if isinstance(v, bool) else None
        except (FileNotFoundError, ValueError, OSError):
            return None

    def enabled(self) -> bool:
        o = self._override()
        return bool(self.s.auto_organize) if o is None else o

    def set_enabled(self, on: bool, by: str = "web") -> dict:
        tmp = self._override_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(on), "by": by, "at": self.clock()}, f)
        os.replace(tmp, self._override_path)
        return self.status()

    def status(self) -> dict:
        o = self._override()
        out = {"enabled": self.enabled(), "env_default": bool(self.s.auto_organize),
               "source": "env" if o is None else "override", "queued": [], "recent": []}
        if self.downloads:
            for j in self.downloads.list_jobs(40):
                org = j.get("organize") or {}
                if not org:
                    continue
                row = {"id": j["id"], "name": org.get("target") or j.get("display_name"),
                       "state": org.get("state"), "reason": "；".join(org.get("reasons") or [])}
                if org.get("state") == QUEUED:
                    out["queued"].append(row)
                elif len(out["recent"]) < 5:
                    out["recent"].append(row)
        return out

    # ------------------------------------------------------------ 入口（DownloadManager 调用）
    def wants(self, job: dict) -> bool:
        """这个下载任务能不能走自动入库（只有能准确认出“自己那一项”的任务才行）。"""
        return self.enabled() and bool(job.get("moved_to")) and job.get("kind") in ("movie", "tv")

    def request(self, dl_id: str) -> bool:
        """尝试马上在 JobManager 里执行；忙就保持排队（pump 会再试）。"""
        dl = self.downloads.get(dl_id)
        if not dl or (dl.get("organize") or {}).get("state") != QUEUED:
            return False
        name = dl.get("display_name") or dl_id
        job = self.jobs.start("auto", lambda j: self._run(j, dl_id),
                              meta={"download_id": dl_id, "title": name})
        if job is None:
            self._dl_log(dl_id, "整理任务正忙，自动入库排队等待")
            return False
        return True

    def pump(self) -> None:
        """每轮轮询调用：排队的再试一次；中断的标出来；移动待核实的再核对。"""
        if not self.downloads:
            return
        for dl in sorted(self.downloads.list_jobs(80), key=lambda j: j.get("created") or 0):
            org = dl.get("organize") or {}
            st = org.get("state")
            if st == QUEUED:
                if self.jobs.busy():
                    return
                self.request(dl["id"])
            elif st == RUNNING:
                cur = self.jobs.current
                if not (self.jobs.busy() and cur and cur.get("id") == org.get("exec_job")):
                    self._finish(dl["id"], INTERRUPTED,
                                 reasons=["自动入库执行中断（服务重启？），没有自动重来，请打开网页核对"],
                                 notify_text=True)
            elif st == UNVERIFIED and int(org.get("checks") or 0) < MAX_VERIFY_CHECKS:
                self._verify_later(dl)

    # ------------------------------------------------------------ 执行（JobManager 线程里）
    def _run(self, job: dict, dl_id: str) -> dict:
        jlog = lambda m: self.jobs.log(job, m)
        dl = self.downloads.get(dl_id) or {}
        self._update_org(dl_id, state=RUNNING, exec_job=job["id"])
        moved_to = dl.get("moved_to") or ""
        staging, item_name = posixpath.split(moved_to)
        kind = dl.get("kind")
        jlog(f"自动入库：{item_name}（{_KIND_ZH.get(kind, kind)}）")
        if not self.enabled():
            return self._fallback(job, dl_id, None, ["自动入库已关闭"], jlog)
        c = self.client_factory()
        start = getattr(c, "requests_115", 0)
        sc = Scanner(c, self.tmdb_factory(), self.s, log=jlog)
        data = sc.scan()
        data["scanned_at"] = self.clock()
        data["requests_115"] = getattr(c, "requests_115", 0) - start
        data["job_id"] = job["id"]
        data["from_download"] = True
        self.store.save("preview.json", data)
        jlog(f"扫描完成：{len(data.get('items') or [])} 项（只处理这个下载的那一项）")
        mine = [it for it in data.get("items") or []
                if it.get("staging") == staging and it.get("folder_old") == item_name]
        reasons = self.gates(dl, mine, data)
        if reasons:
            return self._fallback(job, dl_id, data, reasons, jlog)
        it = mine[0]
        it["include"] = True
        ex = Executor(c, self.s, log=jlog, sleep=self.sleep,
                      progress=lambda d, t: job["progress"].update(done=d, total=t),
                      should_stop=lambda: self.jobs.stop_requested)
        jlog(f"安全检查全部通过，开始改名并移入片库：{it['target_name']}")
        result = ex.run([it])
        result["auto"] = True
        result["download_id"] = dl_id
        res = (result.get("items") or [{}])[0]
        st = res.get("status")
        lib = self.s.library_movies if it["kind"] == "movie" else self.s.library_tv
        if it.get("merge") is not None:
            dest = join(lib, it["merge"]["show"])
            seasons = "、".join(N.season_folder(n) for n in it["merge"]["seasons"])
        else:
            dest = join(lib, it["target_name"])
            seasons = ""
        state = {"done": DONE, "merged": DONE, "move_unverified": UNVERIFIED,
                 "moved": UNVERIFIED}.get(st, FAILED)
        if st == "conflict":
            state = SKIPPED
        # 这一项已经处理过：从预览里拿掉，免得网页里再执行一次
        data2 = self.store.load("preview.json") or {}
        if data2.get("job_id") == job["id"] and state in (DONE, UNVERIFIED):
            data2["items"] = [x for x in data2.get("items") or [] if x.get("id") != it["id"]]
            data2["auto_executed"] = {"job": job["id"], "item": it["target_name"]}
            self.store.save("preview.json", data2)
        renamed = sum(1 for o in res.get("ops") or [] if o["op"] == "rename" and o["result"] in ("ok", "timeout"))
        org = dict(state=state, target=it["target_name"], dest=dest, seasons=seasons,
                   exec_status=st, renamed=renamed, notes=res.get("notes") or [],
                   errors=res.get("errors") or [], kind=it["kind"],
                   merge=it.get("merge") is not None, checks=0)
        self._update_org(dl_id, **org)
        dl = self.downloads.get(dl_id) or dl
        self._dl_log(dl_id, f"自动入库结果：{notify.STATUS_ZH.get(st, st)} {it['target_name']}")
        notify.telegram(self.s, notify.auto_organized(self.s, dl, job["id"]))
        jlog(f"完成：{notify.STATUS_ZH.get(st, st)}，115 请求 {result['requests_115']} 次")
        return result

    def gates(self, dl: dict, mine: list, data: dict) -> list[str]:
        """返回不能自动入库的原因；空列表 = 全部通过。"""
        r: list[str] = []
        kind = dl.get("kind")
        c = dl.get("classify") or {}
        if kind not in ("movie", "tv") or c.get("confidence") != "high":
            r.append("电影/电视剧分类不是高置信")
        if not mine:
            return r + ["扫描结果里找不到这个下载对应的那一项"]
        if len(mine) > 1:
            return r + ["扫描结果里有多项同名，无法确定是哪一项"]
        it = mine[0]
        name = it.get("folder_old") or ""
        if it.get("kind") != kind:
            r.append("扫描时的类型和分类结果不一致")
        for e in data.get("errors") or []:
            if name in e or "片库" in e:
                r.append("扫描出错：" + e)
        if it.get("manual"):
            r.append("这一项被手动改过")
        tm = it.get("tmdb")
        if not tm:
            r.append("TMDB 没有匹配")
        elif it.get("confidence") != "high":
            r.append(f"TMDB 匹配不是高置信（{it.get('confidence')}）：{tm.get('title')} ({tm.get('year')})")
        if tm:
            ck = c.get(kind) if isinstance(c.get(kind), dict) else None
            # 有 TMDB ID 时按 ID 比（中文标题的取法可能变过）；旧记录没有 ID 时按标题比
            same = (ck.get("id") == tm.get("id")) if ck and ck.get("id") and tm.get("id") \
                else (ck and (ck.get("title") == tm.get("title")
                              or (ck.get("title") or "") in {tm.get("original"), tm.get("en_title")}))
            if ck and ck.get("title") and ck.get("confidence") == "high" and \
                    (not same or (ck.get("year") and ck.get("year") != tm.get("year"))):
                r.append(f"分类时 TMDB 对上 {ck.get('title')} ({ck.get('year')})，扫描时对上 "
                         f"{tm.get('title')} ({tm.get('year')})，不一致")
        for x in (it.get("blocking") or []) + (it.get("plan_blocking") or []):
            r.append(x)
        for x in it.get("flags") or []:
            r.append("提示：" + x)
        for x in it.get("plan_flags") or []:
            if not any(b in x for b in BENIGN_PLAN_FLAGS):
                r.append("提示：" + x)
        m = it.get("merge")
        if it.get("library_conflict"):
            if kind == "movie":
                r.append(f"片库已有同名「{it.get('target_name')}」，没有移动（冲突）")
            elif m is not None:
                r.append(f"片库里「{m['show']}」已有 " + "、".join(N.season_folder(n) for n in m.get("skip") or [])
                         + "，没有移动（冲突）")
            else:
                r.append(f"片库已有同名「{it.get('target_name')}」，没有移动（冲突）")
        elif m is not None and m.get("skip"):
            r.append(f"片库里「{m['show']}」已有 " + "、".join(N.season_folder(n) for n in m["skip"])
                     + "，没有移动（冲突）")
        if not r and not it.get("executable"):
            r.append("这一项不可执行")
        if not r and not it.get("include"):
            r.append("这一项默认不勾选")
        # 去重保持顺序
        seen, out = set(), []
        for x in r:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def _fallback(self, job: dict, dl_id: str, data: Optional[dict], reasons: list[str], jlog) -> dict:
        jlog("不自动入库：" + "；".join(reasons))
        count = len((data or {}).get("items") or []) if data else None
        if data is None:
            # 开关被关掉：照旧扫描生成预览
            try:
                c = self.client_factory()
                data = Scanner(c, self.tmdb_factory(), self.s, log=jlog).scan()
                data["scanned_at"] = self.clock()
                data["job_id"] = job["id"]
                data["from_download"] = True
                self.store.save("preview.json", data)
                count = len(data.get("items") or [])
            except Exception as e:
                jlog(f"扫描失败：{type(e).__name__}: {e}")
        self._finish(dl_id, SKIPPED, reasons=reasons, scan_count=count, notify_text=True)
        return {"items": [], "requests_115": 0, "auto": True, "download_id": dl_id, "skipped": reasons}

    # ------------------------------------------------------------ 辅助
    def _finish(self, dl_id: str, state: str, reasons: list[str], scan_count=None, notify_text=True) -> None:
        self._update_org(dl_id, state=state, reasons=reasons)
        dl = self.downloads.get(dl_id)
        if not dl:
            return
        if scan_count is not None:
            dl["scan_count"] = scan_count
        self._dl_log(dl_id, ("不自动入库：" if state == SKIPPED else "自动入库：") + "；".join(reasons), dl=dl)
        dl = self.downloads.get(dl_id) or dl
        if notify_text:
            notify.telegram(self.s, notify.download_complete(self.s, dl, dl.get("scan_count"),
                                                             auto_reasons=reasons))

    def _update_org(self, dl_id: str, **kw) -> None:
        dl = self.downloads.get(dl_id)
        if not dl:
            return
        org = dl.get("organize") or {}
        org.update(kw)
        org["updated"] = self.clock()
        dl["organize"] = org
        if org.get("state") not in (QUEUED, RUNNING):
            dl["status"] = "complete"
            dl["finished"] = dl.get("finished") or self.clock()
        dl["updated"] = self.clock()
        self.downloads.save(dl)

    def _dl_log(self, dl_id: str, msg: str, dl: Optional[dict] = None) -> None:
        dl = dl or self.downloads.get(dl_id)
        if not dl:
            return
        dl.setdefault("log", []).append(time.strftime("%H:%M:%S ") + msg)
        dl["updated"] = self.clock()
        self.downloads.save(dl)
        self.log(msg)

    def _verify_later(self, dl: dict) -> None:
        """移动超时 / 核对时没看到：只核对片库，不重试移动。"""
        org = dl["organize"]
        c = self.client_factory()
        try:
            if org.get("merge"):
                names = {e.name for e in c.list(org["dest"], refresh=True)}
                want = [s for s in (org.get("seasons") or "").split("、") if s]
                ok = bool(want) and all(s in names for s in want)
            else:
                parent, name = posixpath.split(org["dest"])
                ok = name in {e.name for e in c.list(parent, refresh=True)}
        except Exception:
            ok = False
        if ok:
            self._update_org(dl["id"], state=DONE, exec_status="done",
                             notes=(org.get("notes") or []) + ["后续核对：已在片库里看到"])
            dl = self.downloads.get(dl["id"]) or dl
            self._dl_log(dl["id"], "后续核对：已在片库里看到 " + org["dest"])
            notify.telegram(self.s, notify.auto_organized(self.s, dl, org.get("exec_job")))
            return
        checks = int(org.get("checks") or 0) + 1
        self._update_org(dl["id"], checks=checks)
        if checks >= MAX_VERIFY_CHECKS:
            dl = self.downloads.get(dl["id"]) or dl
            self._dl_log(dl["id"], "多次核对仍没在片库里看到，没有重试移动，请人工核对")
            notify.telegram(self.s, f"❓ 自动入库待核实：{org.get('target')}\n"
                                    f"移动后多次核对仍没在 {org.get('dest')} 看到，没有重试（避免重复操作），请到网页或 115 里看一下。")
