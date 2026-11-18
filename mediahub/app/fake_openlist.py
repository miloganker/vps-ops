"""内存里的假 OpenList，用于单元测试和本地演示，不访问任何网络。"""
from __future__ import annotations

import json
import posixpath
from typing import Callable, Optional

from .openlist import Entry, OpenListError, OpenListTimeout


class FakeOpenList:
    def __init__(self, tree: dict[str, list[dict]] | None = None):
        # tree: {目录路径: [{"name","is_dir","size"}]}
        self.dirs: dict[str, dict[str, Entry]] = {}
        for d, items in (tree or {}).items():
            self._ensure(d)
            for it in items:
                e = Entry(it["name"], bool(it.get("is_dir", it.get("dir"))), int(it.get("size") or 0))
                self.dirs[d][e.name] = e
                if e.is_dir:
                    self._ensure(posixpath.join(d, e.name))
        self.calls: list[tuple] = []
        self.requests_115 = 0
        # 注入故障：{("rename", 新名字): "timeout"|"timeout_fail"|"error"}
        self.faults: dict[tuple, str] = {}
        self.move_busy_until: float = -1.0
        self.clock: Callable[[], float] = lambda: 0.0
        self.move_busy_seconds: float = 0.0
        self.offline_tasks: dict[str, dict] = {}
        self.offline_add_fault: Optional[str] = None  # "10008"

    @classmethod
    def from_file(cls, path: str) -> "FakeOpenList":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def _ensure(self, d: str) -> None:
        d = d.rstrip("/") or "/"
        if d not in self.dirs:
            self.dirs[d] = {}
            parent, name = posixpath.split(d)
            if name and parent != d:
                self._ensure(parent)
                self.dirs[parent].setdefault(name, Entry(name, True, 0))

    def login(self) -> None:
        pass

    def _count(self, *call) -> None:
        self.requests_115 += 1
        self.calls.append(call)

    def list(self, path: str, refresh: bool = False) -> list[Entry]:
        self._count("list", path, refresh)
        if path not in self.dirs:
            raise OpenListError("object not found")
        return [Entry(e.name, e.is_dir, e.size) for e in self.dirs[path].values()]

    def rename(self, path: str, name: str) -> None:
        self._count("rename", path, name)
        fault = self.faults.get(("rename", name))
        if fault == "timeout_fail":
            raise OpenListTimeout("rename 超时")
        if fault == "error":
            raise OpenListError("rename failed")
        parent, old = posixpath.split(path)
        if parent not in self.dirs or old not in self.dirs[parent]:
            raise OpenListError("object not found")
        if name in self.dirs[parent]:
            raise OpenListError("file exists")
        e = self.dirs[parent].pop(old)
        self.dirs[parent][name] = Entry(name, e.is_dir, e.size)
        if e.is_dir:
            self._move_subtree(path, posixpath.join(parent, name))
        if fault == "timeout":   # 115 的典型行为：超时但实际改成功了
            raise OpenListTimeout("rename 超时")

    def _move_subtree(self, old: str, new: str) -> None:
        for d in sorted([d for d in self.dirs if d == old or d.startswith(old + "/")]):
            self.dirs[new + d[len(old):]] = self.dirs.pop(d)

    def mkdir(self, path: str) -> None:
        self._count("mkdir", path)
        self._ensure(path)

    def move(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        self._count("move", src_dir, dst_dir, tuple(names))
        if len(names) != 1:
            raise OpenListError("测试约束：一次只能移动一个")
        now = self.clock()
        if now < self.move_busy_until:
            raise OpenListError("errno 990009 移动操作尚未执行完成，请稍后再试")
        name = names[0]
        fault = self.faults.get(("move", name))
        if fault == "timeout_fail":
            raise OpenListTimeout("move 超时")
        if name not in self.dirs.get(src_dir, {}):
            raise OpenListError("object not found")
        if name in self.dirs.get(dst_dir, {}):
            raise OpenListError("file exists")
        e = self.dirs[src_dir].pop(name)
        self._ensure(dst_dir)
        self.dirs[dst_dir][name] = e
        if e.is_dir:
            self._move_subtree(posixpath.join(src_dir, name), posixpath.join(dst_dir, name))
        self.move_busy_until = now + self.move_busy_seconds
        if fault == "timeout":   # 超时但实际移动成功
            raise OpenListTimeout("move 超时")


    def normalize_offline_tool(self, tool: str) -> str:
        from .openlist import OpenListClient
        return OpenListClient.normalize_offline_tool(tool)

    def add_offline_download(self, urls: list[str], path: str, tool: str = "115 Cloud",
                             delete_policy: str = "delete_never") -> dict:
        tool = self.normalize_offline_tool(tool)
        self._count("add_offline_download", tuple(urls), path, tool, delete_policy)
        if self.offline_add_fault == "10008":
            from .openlist import OpenListError
            raise OpenListError("failed to add offline download task: code: 10008, message: 任务已存在，请勿输入重复的链接地址。", 500)
        tid = "task-" + str(len(self.offline_tasks) + 1)
        task = {"id": tid, "name": f"download {urls[0]} to ({path})", "state": "pending",
                "status": "Ready to start offline download", "progress": 0, "error": "", "path": path, "url": urls[0]}
        self.offline_tasks[tid] = task
        return {"tasks": [dict(task)]}

    def offline_task_info(self, tid: str):
        self._count("offline_task_info", tid)
        t = self.offline_tasks.get(tid)
        return dict(t) if t else None

    def offline_tasks_undone(self) -> list:
        self._count("offline_tasks_undone")
        return [dict(t) for t in self.offline_tasks.values() if t.get("state") not in ("succeeded", "failed", "canceled")]

    def offline_tasks_done(self) -> list:
        self._count("offline_tasks_done")
        return [dict(t) for t in self.offline_tasks.values() if t.get("state") in ("succeeded", "failed", "canceled")]

    def complete_offline(self, tid: str, names: list[str] | None = None) -> None:
        """测试辅助：把任务标为成功，并在目标目录放入 names（文件夹）。"""
        t = self.offline_tasks[tid]
        t["state"] = "succeeded"
        t["status"] = "Offline download completed"
        t["progress"] = 100
        dest = t.get("path")
        if dest and names:
            self._ensure(dest)
            for name in names:
                self.dirs[dest][name] = Entry(name, True, 0)
                self._ensure(posixpath.join(dest, name))

    def fail_offline(self, tid: str, error: str = "Offline download failed") -> None:
        t = self.offline_tasks[tid]
        t["state"] = "failed"
        t["status"] = error
        t["error"] = error

    def remove(self, *a, **k):  # pragma: no cover
        raise AssertionError("永远不应该删除任何东西")

    # 方便测试
    def put_file(self, folder: str, name: str, size: int = 2_000_000_000) -> None:
        self._ensure(folder)
        self.dirs[folder][name] = Entry(name, False, size)

    def names(self, path: str) -> list[str]:
        return sorted(self.dirs.get(path, {}).keys())
