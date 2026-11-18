"""可选：任务结束后发 Telegram 通知（默认关闭，需同时设置 TELEGRAM_TOKEN 和 TELEGRAM_CHAT_ID）。"""
from __future__ import annotations

import requests


def telegram(settings, text: str, reply_markup: dict | None = None) -> bool:
    if not (settings.telegram_token and settings.telegram_chat_id):
        return False
    body = {"chat_id": settings.telegram_chat_id, "text": text[:4000]}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        r = requests.post(f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                          json=body, timeout=15)
        return r.ok
    except Exception:
        return False


STATUS_ZH = {
    "done": "✅ 已进片库", "conflict": "⚠️ 片库有同名，未移动", "failed": "❌ 失败",
    "move_failed": "❌ 移动失败", "move_unverified": "❓ 移动待核实", "renamed": "改名完成，未移动",
    "cancelled": "已取消", "pending": "未执行", "moved": "已移动", "merged": "✅ 新季已并入片库", "partial": "⚠️ 部分季已并入",
}


def summary(result: dict) -> str:
    lines = [f"媒体整理完成，115 请求 {result.get('requests_115', 0)} 次"]
    for it in result.get("items", []):
        lines.append(f"{STATUS_ZH.get(it['status'], it['status'])}  {it['target']}")
    return "\n".join(lines)


def _base_url(settings) -> str:
    return (getattr(settings, "public_url", None) or "https://media.sumax.duckdns.org:8443").rstrip("/")


def download_complete(settings, job: dict, scan_count, auto_reasons=None) -> str:
    title = job.get("display_name") or job.get("id")
    kind = {"movie": "电影", "tv": "电视剧"}.get(job.get("kind") or "", "")
    url = (getattr(settings, "public_url", None) or "https://media.sumax.duckdns.org:8443").rstrip("/") + "/preview"
    lines = [f"✅ 云下载完成：{title}" + (f"（{kind}）" if kind else "")]
    c = job.get("classify") or {}
    if job.get("auto") and kind and c.get("reason"):
        who = "你选择了" if str(c.get("reason", "")).startswith("你选择了") else f"识别为{kind}："
        lines.append(c["reason"] if who == "你选择了" else who + c["reason"])
    if job.get("moved_to"):
        lines.append(f"已移到 {job['moved_to']}")
    if auto_reasons:
        lines.append("⚠️ 没有自动入库，原因：" + "；".join(auto_reasons))
        lines.append("已扫描待整理，请打开网页确认后再入库。")
    else:
        lines.append("已扫描待整理，请打开网页确认后再入库（不会自动改名或移动）。")
    if scan_count is not None:
        lines.append(f"扫描到 {scan_count} 项。")
    lines.append(url)
    return "\n".join(lines)


def download_failed(settings, job: dict) -> str:
    title = job.get("display_name") or job.get("id")
    err = job.get("error") or "未知错误"
    return f"❌ 云下载失败：{title}\n{err}"


def kind_keyboard(job_id: str) -> dict:
    """和 Telegram 机器人用同一个 callback 格式：机器人收到后调用 mediahub 设置类型。"""
    return {"inline_keyboard": [[
        {"text": "电影", "callback_data": f"dlkind:{job_id}:movie"},
        {"text": "电视剧", "callback_data": f"dlkind:{job_id}:tv"},
    ]]}


def download_needs_kind(settings, job: dict) -> str:
    title = job.get("display_name") or job.get("id")
    reason = (job.get("classify") or {}).get("reason") or "无法判断"
    c = job.get("contents") or {}
    lines = [f"⬇️ 云下载完成：{title}", f"还是判断不了是电影还是电视剧：{reason}"]
    vids = c.get("videos") or []
    if vids:
        more = f" 等 {c.get('video_count')} 个" if (c.get("video_count") or 0) > len(vids[:3]) else ""
        lines.append("视频：" + "、".join(vids[:3]) + more)
    lines.append(f"现在在 {job.get('dest')}。点下面的按钮，我就移到待整理对应的目录并扫描（不会自动入库）。")
    return "\n".join(lines)


def auto_organized(settings, job: dict, exec_job_id=None) -> str:
    """自动改名入库之后的通知。"""
    org = job.get("organize") or {}
    kind = {"movie": "电影", "tv": "电视剧"}.get(org.get("kind") or job.get("kind") or "", "")
    target = org.get("target") or job.get("display_name") or job.get("id")
    st = org.get("state")
    head = {
        "done": "✅ 已自动入库",
        "unverified": "❓ 已自动改名并移动，待核实",
        "failed": "❌ 自动入库没有完成",
        "skipped": "⚠️ 没有自动入库",
    }.get(st, "自动入库：" + str(st))
    lines = [f"{head}：{target}" + (f"（{kind}）" if kind else "")]
    c = job.get("classify") or {}
    if c.get("reason"):
        lines.append(("" if str(c["reason"]).startswith("你选择了") else "识别：") + c["reason"])
    n = org.get("renamed") or 0
    if org.get("merge"):
        where = f"新季 {org.get('seasons')} 并入片库 {org.get('dest')}"
    else:
        where = f"片库 {org.get('dest')}"
    if st == "done":
        lines.append(f"改名 {n} 项，已移到{where}")
    elif st == "unverified":
        lines.append(f"改名 {n} 项，已发出移动到{where}，但还没核对到结果；不会重试移动，稍后自动再核对。")
    elif st == "failed":
        lines.append(f"状态：{STATUS_ZH.get(org.get('exec_status'), org.get('exec_status'))}；文件还在待整理，没有删除任何东西。")
    for x in (org.get("errors") or [])[:3]:
        lines.append("错误：" + str(x))
    for x in (org.get("notes") or [])[:3]:
        lines.append(str(x))
    if exec_job_id or org.get("exec_job"):
        lines.append(_base_url(settings) + "/job/" + str(exec_job_id or org.get("exec_job")))
    return "\n".join(lines)
