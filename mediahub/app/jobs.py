"""后台任务（一次只允许一个）和状态持久化（JSON 文件，放在 DATA_DIR）。"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Optional


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(os.path.join(data_dir, "jobs"), exist_ok=True)

    def _p(self, *a) -> str:
        return os.path.join(self.dir, *a)

    def save(self, name: str, obj: Any) -> None:
        tmp = self._p(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._p(name))

    def load(self, name: str, default=None):
        try:
            with open(self._p(name), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def job_ids(self) -> list[str]:
        files = [f for f in os.listdir(self._p("jobs")) if f.endswith(".json")]
        files.sort(key=lambda f: os.path.getmtime(self._p("jobs", f)), reverse=True)
        return [f[:-5] for f in files]


class JobManager:
    def __init__(self, store: Store, threaded: bool = True):
        self.store = store
        self.threaded = threaded   # 测试里同步执行
        self.lock = threading.Lock()
        self.current: Optional[dict] = None
        self._stop = threading.Event()

    def busy(self) -> bool:
        return self.current is not None and self.current["status"] == "running"

    def get(self, job_id: str) -> Optional[dict]:
        if self.current and self.current["id"] == job_id:
            return self.current
        return self.store.load(f"jobs/{job_id}.json")

    def recent(self, n: int = 10) -> list[dict]:
        out = []
        for jid in self.store.job_ids()[:n]:
            j = self.get(jid)
            if j:
                out.append(j)
        return out

    def stop(self) -> None:
        self._stop.set()

    def start(self, kind: str, fn: Callable[[dict], Any], meta: Optional[dict] = None) -> Optional[dict]:
        """fn(job) 在后台线程里运行；job['log'] / job['progress'] 由 fn 更新。忙时返回 None。"""
        with self.lock:
            if self.busy():
                return None
            self._stop.clear()
            job = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4], "type": kind,
                   "status": "running", "started": time.time(), "finished": None,
                   "log": [], "progress": {"done": 0, "total": 0}, "result": None, "error": None}
            if meta:
                job.update(meta)
            self.current = job
        self._persist(job)

        def runner():
            try:
                job["result"] = fn(job)
                job["status"] = "done"
            except Exception as e:
                job["status"] = "error"
                job["error"] = f"{type(e).__name__}: {e}"
                job["log"].append("出错：" + job["error"])
                job["trace"] = traceback.format_exc()[-3000:]
            finally:
                job["finished"] = time.time()
                self._persist(job)

        if not self.threaded:
            runner()
            return job
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        job["_thread"] = t
        return job

    def log(self, job: dict, msg: str) -> None:
        job["log"].append(time.strftime("%H:%M:%S ") + msg)
        if len(job["log"]) % 5 == 0:
            self._persist(job)

    def _persist(self, job: dict) -> None:
        self.store.save(f"jobs/{job['id']}.json", {k: v for k, v in job.items() if not k.startswith("_")})

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()
