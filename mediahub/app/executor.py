"""执行改名和移动。

顺序（每一步之间按 RENAME_INTERVAL 间隔）：
1. 改名阶段（逐条）：先改文件，再改文件夹。电视剧散放结构：先在原文件夹里改好每一集 → 在待整理里建
   `剧名 (年份)` → 把原文件夹移进去 → 等 MOVE_WAIT → 把原文件夹改名为 `Season NN`。
2. 核对阶段：refresh 列出待整理目录，确认改名结果（超时的改名不重试，只在这里核对）。
3. 移动阶段：refresh 列出片库一次；同名就跳过（不合并、不覆盖、不删除）；一次只移一个文件夹，
   每次移动后等 MOVE_WAIT。
4. 最终核对：refresh 列出片库和待整理。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .openlist import OpenListError, OpenListTimeout, join
from . import naming as N


class Executor:
    def __init__(self, client, settings, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep,
                 progress: Optional[Callable[[int, int], None]] = None,
                 should_stop: Callable[[], bool] = lambda: False):
        self.c = client
        self.s = settings
        self.log = log
        self.sleep = sleep
        self.progress = progress or (lambda done, total: None)
        self.should_stop = should_stop
        self._last_write = False
        self.done = 0
        self.total = 0

    # ---------------------------------------------------------------- 底层
    def _pace(self) -> None:
        if self._last_write:
            self.sleep(self.s.rename_interval)
        self._last_write = True

    def _tick(self) -> None:
        self.done += 1
        self.progress(self.done, self.total)

    def _rename(self, res: dict, path: str, new: str) -> str:
        """返回 ok / timeout / error。超时不重试。"""
        self._pace()
        try:
            self.c.rename(path, new)
            r = "ok"
            self.log(f"改名 {path.rsplit('/', 1)[-1]} → {new}")
        except OpenListTimeout:
            r = "timeout"
            self.log(f"改名超时（不重试，最后核对）：{new}")
        except OpenListError as e:
            r = "error"
            self.log(f"改名失败：{new}：{e}")
            res["errors"].append(f"改名失败 {new}：{e}")
        res["ops"].append({"op": "rename", "path": path, "new": new, "result": r})
        self._tick()
        return r

    def _move(self, res: dict, src: str, dst: str, name: str) -> str:
        self._pace()
        attempts = 2 if self.s.move_retry_on_busy else 1
        r = "error"
        for i in range(attempts):
            try:
                self.c.move(src, dst, [name])
                r = "ok"
                self.log(f"移动 {name} → {dst}")
                break
            except OpenListTimeout:
                r = "timeout"
                self.log(f"移动超时（不重试，最后核对）：{name}")
                break
            except OpenListError as e:
                if e.busy and i + 1 < attempts:
                    # 115 明确拒绝（没有执行），等更久再试一次
                    self.log(f"115 上一个移动还没完成，等 {self.s.move_wait * 2:.0f} 秒再试一次：{name}")
                    self.sleep(self.s.move_wait * 2)
                    continue
                r = "error"
                self.log(f"移动失败：{name}：{e}")
                res["errors"].append(f"移动失败 {name}：{e}")
                break
        res["ops"].append({"op": "move", "src": src, "dst": dst, "name": name, "result": r})
        self._tick()
        # 无论成功与否都等 MOVE_WAIT，避免下一个移动与它重叠
        self.log(f"等待 {self.s.move_wait:.0f} 秒让 115 完成移动")
        self.sleep(self.s.move_wait)
        self._last_write = False
        return r

    def _mkdir(self, res: dict, path: str) -> str:
        self._pace()
        try:
            self.c.mkdir(path)
            r = "ok"
            self.log(f"新建文件夹 {path}")
        except OpenListTimeout:
            r = "timeout"
        except OpenListError as e:
            r = "error"
            res["errors"].append(f"新建文件夹失败 {path}：{e}")
        res["ops"].append({"op": "mkdir", "path": path, "result": r})
        self._tick()
        return r

    def _list(self, path: str) -> Optional[list]:
        try:
            return self.c.list(path, refresh=True)
        except Exception as e:
            self.log(f"列目录失败 {path}：{e}")
            return None

    # ---------------------------------------------------------------- 主流程
    def run(self, items: list[dict]) -> dict:
        start = getattr(self.c, "requests_115", 0)
        items = [it for it in items if it.get("include") and it.get("executable")]
        results = {it["id"]: {"id": it["id"], "kind": it["kind"], "folder_old": it["folder_old"],
                              "target": it["target_name"], "status": "pending", "ops": [],
                              "errors": [], "notes": []} for it in items}
        from .scanner import estimate_requests
        self.total = sum(estimate_requests(it) for it in items) + 4
        self.progress(0, self.total)

        # 1. 改名
        for it in items:
            if self.should_stop():
                results[it["id"]]["status"] = "cancelled"
                continue
            res = results[it["id"]]
            if it["kind"] == "movie":
                self._do_movie(it, res)
            else:
                self._do_tv(it, res)

        # 2. 核对改名
        staged: dict[str, list] = {}
        for it in items:
            res = results[it["id"]]
            if res["status"] in ("cancelled", "failed"):
                continue
            st = it["staging"]
            if st not in staged:
                staged[st] = self._list(st) or []
                self._tick()
            names = {e.name for e in staged[st]}
            from .scanner import stage_name
            if stage_name(it) in names:
                res["status"] = "renamed"
            else:
                res["status"] = "failed"
                res["errors"].append("核对时在待整理里找不到改名后的文件夹，跳过移动")
                continue
            # 只有出现过超时/失败的条目才深入核对文件（省请求）
            if any(o["result"] != "ok" and o["op"] == "rename" for o in res["ops"]):
                self._verify_inner(it, res)

        # 3. 移动进片库
        libs: dict[str, set] = {}
        movable = [it for it in items if results[it["id"]]["status"] == "renamed"]
        for it in movable:
            if self.should_stop():
                results[it["id"]]["status"] = "cancelled"
                continue
            res = results[it["id"]]
            lib = self.s.library_movies if it["kind"] == "movie" else self.s.library_tv
            if lib not in libs:
                ents = self._list(lib)
                self._tick()
                if ents is None:
                    res["status"] = "failed"
                    res["errors"].append("无法列出片库，为安全起见不移动")
                    continue
                libs[lib] = {e.name for e in ents}
            if it.get("merge") is not None:
                self._merge_move(it, res, lib, libs[lib])
                continue
            if it["target_name"] in libs[lib]:
                res["status"] = "conflict"
                res["notes"].append(f"片库已有同名文件夹“{it['target_name']}”，已跳过移动（未合并、未覆盖）")
                self.log(f"片库已有同名，跳过：{it['target_name']}")
                continue
            r = self._move(res, it["staging"], lib, it["target_name"])
            if r == "ok":
                libs[lib].add(it["target_name"])
                res["status"] = "moved"
            elif r == "timeout":
                res["status"] = "move_unverified"
            else:
                res["status"] = "move_failed"

        # 4. 最终核对
        final_lib: dict[str, set] = {}
        plain = [it for it in movable if it.get("merge") is None]
        for lib in {self.s.library_movies if it["kind"] == "movie" else self.s.library_tv for it in plain}:
            ents = self._list(lib)
            final_lib[lib] = {e.name for e in ents} if ents is not None else set()
        final_stage: dict[str, set] = {}
        for st in {it["staging"] for it in plain}:
            ents = self._list(st)
            final_stage[st] = {e.name for e in ents} if ents is not None else set()
        for it in movable:
            res = results[it["id"]]
            if it.get("merge") is not None:
                if res.get("_moved_seasons"):
                    self._merge_verify(it, res)
                continue
            if res["status"] not in ("moved", "move_unverified", "move_failed"):
                continue
            lib = self.s.library_movies if it["kind"] == "movie" else self.s.library_tv
            in_lib = it["target_name"] in final_lib.get(lib, set())
            in_stage = it["target_name"] in final_stage.get(it["staging"], set())
            if in_lib and not in_stage:
                res["status"] = "done"
            elif res["status"] == "moved":
                res["status"] = "move_unverified"
                res["notes"].append("移动接口返回成功，但最终核对还没看到它进片库（115 可能还在处理）")
        self.progress(self.total, self.total)
        used = getattr(self.c, "requests_115", 0) - start
        for r in results.values():
            r.pop("_moved_seasons", None)
        return {"items": list(results.values()), "requests_115": used}

    # ---------------------------------------------------------------- 电影
    def _do_movie(self, it: dict, res: dict) -> None:
        st, old = it["staging"], it["folder_old"]
        for r in it["renames"]:
            self._rename(res, join(st, old, r["old"]), r["new"])
        if it["folder_new"] != old:
            if self._rename(res, join(st, old), it["folder_new"]) == "error":
                res["status"] = "failed"

    # ---------------------------------------------------------------- 电视剧
    def _do_tv(self, it: dict, res: dict) -> None:
        st, old = it["staging"], it["folder_old"]
        ops = it["tv_ops"]
        show = ops["show_new"]
        if it.get("merge") is not None:
            # 片库里已有这部剧：不建剧名文件夹，只把季文件夹准备好，移动阶段直接移进片库的剧集文件夹
            if ops["layout"] == "flat":
                for r in ops["renames"]:
                    self._rename(res, join(st, old, r["old"]), r["new"])
                if old != ops["season_folder"]:
                    if self._rename(res, join(st, old), ops["season_folder"]) == "error":
                        res["status"] = "failed"
                return
            want = set(it["merge"]["seasons"])
            for sn in ops["seasons"]:
                if sn["season"] not in want:
                    continue   # 片库已有的季：不动
                for r in sn["renames"]:
                    self._rename(res, join(st, old, sn["folder_old"], r["old"]), r["new"])
                if sn["folder_new"] != sn["folder_old"]:
                    self._rename(res, join(st, old, sn["folder_old"]), sn["folder_new"])
            return
        if ops["layout"] == "flat" and old == show:
            # 原文件夹已经叫“剧名 (年份)”：先改成 Season NN，再建剧名文件夹，把季文件夹移进去
            for r in ops["renames"]:
                self._rename(res, join(st, old, r["old"]), r["new"])
            if self._rename(res, join(st, old), ops["season_folder"]) == "error":
                res["status"] = "failed"
                return
            if self._mkdir(res, join(st, show)) == "error":
                res["status"] = "failed"
                return
            if self._move(res, st, join(st, show), ops["season_folder"]) == "error":
                res["status"] = "failed"
            return
        if ops["layout"] == "flat":
            for r in ops["renames"]:
                self._rename(res, join(st, old, r["old"]), r["new"])
            if self._mkdir(res, join(st, show)) == "error":
                res["status"] = "failed"
                return
            r = self._move(res, st, join(st, show), old)
            if r == "error":
                res["status"] = "failed"
                return
            if self._rename(res, join(st, show, old), ops["season_folder"]) == "error":
                res["notes"].append("季文件夹改名失败，原文件夹已在剧名文件夹里")
            return
        for s in ops["seasons"]:
            for r in s["renames"]:
                self._rename(res, join(st, old, s["folder_old"], r["old"]), r["new"])
            if s["folder_new"] != s["folder_old"]:
                self._rename(res, join(st, old, s["folder_old"]), s["folder_new"])
        if show != old:
            if self._rename(res, join(st, old), show) == "error":
                res["status"] = "failed"

    def _verify_inner(self, it: dict, res: dict) -> None:
        """有超时的条目：列出条目内部，确认文件名都改好了。"""
        st = it["staging"]
        checks: list[tuple[str, set]] = []
        if it["kind"] == "movie":
            checks.append((join(st, it["target_name"]), {r["new"] for r in it["renames"]}))
        elif it.get("merge") is not None:
            ops = it["tv_ops"]
            if ops["layout"] == "flat":
                checks.append((join(st, ops["season_folder"]), {r["new"] for r in ops["renames"]}))
            else:
                for sn in ops["seasons"]:
                    if sn["season"] in it["merge"]["seasons"]:
                        checks.append((join(st, it["folder_old"], sn["folder_new"]), {r["new"] for r in sn["renames"]}))
        else:
            ops = it["tv_ops"]
            if ops["layout"] == "flat":
                checks.append((join(st, ops["show_new"], ops["season_folder"]), {r["new"] for r in ops["renames"]}))
            else:
                for s in ops["seasons"]:
                    checks.append((join(st, ops["show_new"], s["folder_new"]), {r["new"] for r in s["renames"]}))
        for path, want in checks:
            ents = self._list(path)
            if ents is None:
                res["status"] = "failed"
                res["errors"].append(f"核对失败：无法列出 {path}")
                return
            have = {e.name for e in ents}
            missing = sorted(want - have)
            if missing:
                res["status"] = "failed"
                res["errors"].append(f"有 {len(missing)} 个文件改名未生效（未重试）：{missing[:3]}")
            else:
                res["notes"].append("有改名超时，但核对确认已生效")

    # ---------------------------------------------------------------- 新季并入片库已有剧集
    def _merge_move(self, it: dict, res: dict, lib: str, lib_names: set) -> None:
        m = it["merge"]
        show_dir = join(lib, m["show"])
        res["_moved_seasons"] = []
        if m["show"] not in lib_names:
            res["status"] = "failed"
            res["errors"].append(f"片库里找不到“{m['show']}”了，为安全起见不移动")
            return
        ents = self._list(show_dir)
        self._tick()
        if ents is None:
            res["status"] = "failed"
            res["errors"].append("无法列出片库里的剧集文件夹，为安全起见不移动")
            return
        have = {e.name for e in ents}
        for sk in m.get("skip") or []:
            res["notes"].append(f"{N.season_folder(sk)} 片库里已有，跳过（未合并、未覆盖）")
        ops = it["tv_ops"]
        src = it["staging"] if ops["layout"] == "flat" else join(it["staging"], it["folder_old"])
        any_ok = False
        for sn in m["seasons"]:
            name = N.season_folder(sn)
            if name in have:
                res["notes"].append(f"{name} 片库里已有，跳过（未合并、未覆盖）")
                continue
            r = self._move(res, src, show_dir, name)
            res["_moved_seasons"].append((name, r))
            any_ok = any_ok or r in ("ok", "timeout")
        res["status"] = "moved" if any_ok else ("conflict" if not res["_moved_seasons"] else "move_failed")

    def _merge_verify(self, it: dict, res: dict) -> None:
        lib = self.s.library_tv
        ents = self._list(join(lib, it["merge"]["show"]))
        have = {e.name for e in ents} if ents is not None else set()
        moved = res["_moved_seasons"]
        ok = [n for n, r in moved if n in have]
        bad = [n for n, r in moved if n not in have]
        if ok and not bad:
            res["status"] = "merged" if not (it["merge"].get("skip") or len(ok) < len(it["merge"]["seasons"])) else "partial"
        elif ok:
            res["status"] = "partial"
            res["notes"].append("还没在片库里看到：" + "、".join(bad))
        elif moved:
            res["status"] = "move_unverified" if any(r == "timeout" for _, r in moved) else "move_failed"
        if ok:
            res["notes"].append("已并入片库：" + "、".join(ok))
