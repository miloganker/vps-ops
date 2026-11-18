"""OpenList v4 API 客户端（只用 list / rename / mkdir / move；从不调用 remove）。

每次 /api/fs/* 调用都会落到 115，计入 `requests_115`。登录不计（不访问 115）。
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Optional

import requests


class OpenListError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code

    @property
    def busy(self) -> bool:
        """115 的“操作尚未执行完成”(990009)：上一个移动还没处理完。"""
        m = str(self)
        return "990009" in m or "尚未执行完成" in m


class OpenListTimeout(OpenListError):
    """请求超时。115 改名经常超时但实际已成功——调用方不得重试。"""


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int = 0


def join(*parts: str) -> str:
    return posixpath.join(*parts)


class OpenListClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: Optional[str] = None
        self.requests_115 = 0
        self.s = requests.Session()

    # ---- 基础
    def login(self) -> None:
        r = self.s.post(self.base + "/api/auth/login",
                        json={"username": self.username, "password": self.password}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 200:
            raise OpenListError(f"登录失败：{j.get('message')}", j.get("code"))
        self.token = j["data"]["token"]

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 count: bool = True, params: Optional[dict] = None) -> dict:
        if self.token is None:
            self.login()
        if count:
            self.requests_115 += 1
        try:
            r = self.s.request(method, self.base + path, json=body, params=params,
                               timeout=self.timeout,
                               headers={"Authorization": self.token or ""})
        except requests.Timeout as e:
            raise OpenListTimeout(f"{path} 超时") from e
        try:
            j = r.json()
        except ValueError as e:
            raise OpenListError(f"{path} 返回非 JSON（HTTP {r.status_code}）") from e
        if j.get("code") != 200:
            raise OpenListError(str(j.get("message")), j.get("code"))
        return j.get("data") or {}

    def _post(self, path: str, body: dict, count: bool = True) -> dict:
        return self._request("POST", path, body, count=count)

    def _get(self, path: str, count: bool = True, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, count=count, params=params)

    # ---- fs
    def list(self, path: str, refresh: bool = False) -> list[Entry]:
        d = self._post("/api/fs/list", {"path": path, "refresh": refresh, "per_page": 0, "page": 1})
        return [Entry(x["name"], bool(x.get("is_dir")), int(x.get("size") or 0)) for x in (d.get("content") or [])]

    def rename(self, path: str, name: str) -> None:
        self._post("/api/fs/rename", {"path": path, "name": name})

    def mkdir(self, path: str) -> None:
        self._post("/api/fs/mkdir", {"path": path})

    def move(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        if len(names) != 1:
            raise ValueError("每次只移动一个文件夹（115 并发移动会报 990009）")
        self._post("/api/fs/move", {"src_dir": src_dir, "dst_dir": dst_dir, "names": names})

    # ---- 离线下载（只允许 115 Cloud，绝不使用会落到本机的工具）
    ALLOWED_OFFLINE_TOOLS = frozenset({"115", "115 Cloud"})
    REFUSED_OFFLINE_TOOLS = frozenset({
        "simplehttp", "aria2", "qbittorrent", "qbit", "transmission", "115 open",
    })

    @staticmethod
    def normalize_offline_tool(tool: str) -> str:
        """OpenList 4.2.9 里 115  cookie 驱动的工具名是 '115 Cloud'。
        必须用这个名字，下载才会直接进 115 目标目录；写成别的可能落到服务器本地再转存。
        """
        name = (tool or "").strip()
        low = name.lower()
        if low in OpenListClient.REFUSED_OFFLINE_TOOLS or low.startswith("simplehttp"):
            raise OpenListError(f"拒绝使用离线下载工具 {name!r}（会在服务器本机下载）")
        if name in ("115", "115 Cloud"):
            return "115 Cloud"
        raise OpenListError(f"只允许 115 云下载，收到的工具是 {name!r}")

    @staticmethod
    def is_already_exists(err: Exception) -> bool:
        m = str(err)
        return "10008" in m or "任务已存在" in m

    def add_offline_download(self, urls: list[str], path: str, tool: str = "115 Cloud",
                             delete_policy: str = "delete_never") -> dict:
        tool = self.normalize_offline_tool(tool)
        if delete_policy != "delete_never":
            delete_policy = "delete_never"
        try:
            data = self._post("/api/fs/add_offline_download", {
                "urls": urls, "path": path, "tool": tool, "delete_policy": delete_policy,
            })
        except OpenListError as e:
            if self.is_already_exists(e):
                return {"already_exists": True, "tasks": [], "error": str(e)}
            raise
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if tasks is None:
            tasks = data if isinstance(data, list) else []
            data = {"tasks": tasks}
        # 个别版本会返回 200 但 task.error 里带 10008（任务其实已在 115 队列里）
        for t in tasks or []:
            err = str((t or {}).get("error") or "")
            if "10008" in err or "任务已存在" in err:
                data["already_exists"] = True
                break
        return data

    def offline_task_info(self, tid: str) -> Optional[dict]:
        data = self._request("POST", "/api/task/offline_download/info", params={"tid": tid})
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    def offline_tasks_undone(self) -> list[dict]:
        data = self._get("/api/task/offline_download/undone")
        return data if isinstance(data, list) else []

    def offline_tasks_done(self) -> list[dict]:
        data = self._get("/api/task/offline_download/done")
        return data if isinstance(data, list) else []
