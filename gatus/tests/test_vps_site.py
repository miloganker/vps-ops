"""VPS_SITE 切换：不连 Telegram，不读真实 /opt/gatus/.env。

直接运行：python3 gatus/tests/test_vps_site.py
"""
import builtins
import importlib.util
import io
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOT = Path(__file__).resolve().parents[1] / "status_bot.py"
REAL_OPEN = builtins.open
STATE = {"env": ""}

GLOBS = {
    "/etc/sing-box/*": ["/etc/sing-box/config.json"],
    "/etc/caddy/*": ["/etc/caddy/Caddyfile", "/etc/caddy/ip.crt", "/etc/caddy/ip.key"],
    "/opt/gatus/*.py": ["/opt/gatus/status_bot.py", "/opt/gatus/traffic_collector.py"],
    "/etc/systemd/system/sing-box.service.d/*": ["/etc/systemd/system/sing-box.service.d/override.conf"],
    "/etc/ssh/sshd_config.d/*.conf": ["/etc/ssh/sshd_config.d/00-no-password.conf"],
    "/etc/systemd/journald.conf.d/*.conf": ["/etc/systemd/journald.conf.d/limits.conf"],
    "/opt/gatus/handoff/**/*": ["/opt/gatus/handoff/note.md"],
}
EXPLICIT = [
    "/opt/gatus/config.yaml",
    "/opt/gatus/docker-compose.yml",
    "/opt/gatus/.env",
    "/etc/systemd/system/gatus-status-bot.service",
    "/etc/systemd/system/traffic-collector.service",
    "/etc/systemd/system/sing-box.service",
    "/root/.ssh/authorized_keys",
    "/opt/openlist/data/config.json",
]
SINGBOX_PATHS = {
    "/etc/sing-box/config.json",
    "/etc/systemd/system/traffic-collector.service",
    "/etc/systemd/system/sing-box.service",
    "/etc/systemd/system/sing-box.service.d/override.conf",
}


def _open(file, *args, **kwargs):
    path = os.fspath(file)
    if path == "/opt/gatus/.env":
        return io.StringIO(STATE["env"])
    if path == "/opt/mediahub/.env":
        raise FileNotFoundError(path)
    return REAL_OPEN(file, *args, **kwargs)


def load_mod(name, dotenv, site="__unset__"):
    if site == "__unset__":
        os.environ.pop("VPS_SITE", None)
    else:
        os.environ["VPS_SITE"] = site
    os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
    STATE["env"] = dotenv
    spec = importlib.util.spec_from_file_location(name, BOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def install_fs(mod):
    present = set(EXPLICIT)
    for paths in GLOBS.values():
        present.update(paths)

    def isfile(path):
        return path in present

    def globber(pattern, recursive=False):
        if pattern not in GLOBS:
            raise AssertionError(f"unexpected glob {pattern!r} recursive={recursive}")
        return list(GLOBS[pattern])

    mod.os.path.isfile = isfile
    mod.glob.glob = globber
    return present


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    builtins.open = _open
    try:
        return _main()
    finally:
        builtins.open = REAL_OPEN


def _main():
    osaka = load_mod("status_bot_osaka", "TELEGRAM_TOKEN=from-file\nTELEGRAM_CHAT_ID=1\n")
    check(osaka.VPS_SITE == "osaka" and not osaka.IS_HANGZHOU, "unset site should be osaka")
    check(osaka.SERVICES == ["sing-box", "caddy", "docker", "containerd", "ssh", "gatus-status-bot", "traffic-collector"],
          f"osaka services changed: {osaka.SERVICES}")
    check(osaka.CONTAINERS == ["gatus", "openlist", "mediahub"], "containers changed")
    check([c["command"] for c in osaka.MENU] ==
          ["status", "server", "services", "traffic", "backup", "dl", "auto", "115qr", "help"],
          f"osaka menu changed: {osaka.MENU}")
    check("/traffic yesterday" in osaka.HELP and "不可用" not in osaka.HELP, "osaka help changed")
    check(osaka.cmd_traffic("nope").startswith("用法：/traffic"), "osaka traffic usage changed")
    check("还没有流量数据" in osaka.cmd_traffic(""), "osaka missing-db message changed")
    check(osaka.MEDIAHUB_URL == "http://127.0.0.1:8095", "mediahub url changed")
    check(osaka.OPENLIST == "http://127.0.0.1:5244", "openlist url changed")

    hang = load_mod(
        "status_bot_hangzhou",
        "VPS_SITE=hangzhou\nTELEGRAM_TOKEN=file-token\nTELEGRAM_CHAT_ID=1\n",
    )
    # .env wins only when the process environment does not already set VPS_SITE.
    check(hang.IS_HANGZHOU, "VPS_SITE=hangzhou in .env was ignored")
    check(hang.SERVICES == ["caddy", "docker", "containerd", "ssh", "gatus-status-bot"],
          f"hangzhou services: {hang.SERVICES}")
    check("traffic" not in [c["command"] for c in hang.MENU], "hangzhou menu still lists traffic")
    check(all(c in [x["command"] for x in hang.MENU] for c in
              ["status", "server", "services", "backup", "dl", "auto", "115qr", "help"]),
          "hangzhou menu dropped a kept command")
    check("不可用" in hang.HELP and "大阪" in hang.HELP, "hangzhou help should mark /traffic unavailable")
    opened = {"n": 0}
    orig_connect = sqlite3.connect

    def boom(*a, **k):
        opened["n"] += 1
        raise AssertionError("hangzhou /traffic queried sqlite")

    hang.sqlite3.connect = boom
    real_exists = hang.os.path.exists

    def exists(path):
        if path == hang.TRAFFIC_DB:
            raise AssertionError("hangzhou /traffic touched TRAFFIC_DB")
        return real_exists(path)

    hang.os.path.exists = exists
    try:
        reply = hang.cmd_traffic("7")
        check("杭州" in reply and "大阪" in reply, f"unexpected /traffic reply: {reply}")
        check(opened["n"] == 0, "sqlite connect was called")
        check("流量" in hang.cmd_traffic("yesterday"), "arg should not change the hangzhou reply")
    finally:
        sqlite3.connect = orig_connect

    spaced = load_mod("status_bot_case", "", site="  Hangzhou ")
    check(spaced.IS_HANGZHOU, "VPS_SITE should strip and ignore case")
    unknown = load_mod("status_bot_unknown", "", site="hz")
    check(unknown.VPS_SITE == "osaka" and not unknown.IS_HANGZHOU, "unknown site should fall back to osaka")
    empty = load_mod("status_bot_empty", "", site="")
    check(not empty.IS_HANGZHOU, "empty VPS_SITE should be osaka")

    install_fs(osaka)
    install_fs(hang)
    osaka_paths = set(osaka.backup_plain_paths())
    hang_paths = set(hang.backup_plain_paths())
    check(SINGBOX_PATHS <= osaka_paths, f"osaka backup dropped sing-box paths: {osaka_paths}")
    check(SINGBOX_PATHS.isdisjoint(hang_paths), f"hangzhou backup still packs sing-box: {hang_paths & SINGBOX_PATHS}")
    for required in [
        "/etc/caddy/Caddyfile", "/etc/caddy/ip.crt", "/etc/caddy/ip.key",
        "/opt/gatus/config.yaml", "/opt/gatus/docker-compose.yml", "/opt/gatus/.env",
        "/opt/gatus/status_bot.py", "/etc/systemd/system/gatus-status-bot.service",
        "/root/.ssh/authorized_keys", "/opt/openlist/data/config.json",
        "/etc/ssh/sshd_config.d/00-no-password.conf",
        "/etc/systemd/journald.conf.d/limits.conf", "/opt/gatus/handoff/note.md",
    ]:
        check(required in hang_paths, f"hangzhou backup missing {required}")
        check(required in osaka_paths, f"osaka backup missing {required}")

    now = datetime(2026, 11, 18, 9, 4, tzinfo=timezone(timedelta(hours=8)))
    osaka_text = "\n".join(osaka.backup_readme_lines(now, ["etc/caddy/Caddyfile"], ["note-a"]))
    check("  1. apt 安装 sing-box、caddy、docker.io、docker-compose、python3、7zip。" in osaka_text, "osaka readme step 1")
    check("systemctl enable --now sing-box caddy gatus-status-bot traffic-collector。" in osaka_text, "osaka enable line")
    check("流量统计数据库" in osaka_text, "osaka readme should still mention the traffic db")
    check("  note-a" in osaka_text, "notes were dropped")
    check("  etc/caddy/Caddyfile" in osaka_text, "file list missing")

    hang_text = "\n".join(hang.backup_readme_lines(now, ["etc/caddy/ip.crt"], ["note-b"]))
    check("OpenList" in hang_text and "mediahub" in hang_text and "Gatus" in hang_text, "hangzhou restore scope")
    check("VPS_SITE=hangzhou" in hang_text, "hangzhou readme should require VPS_SITE")
    check("systemctl enable --now caddy gatus-status-bot。" in hang_text, "hangzhou enable line")
    check("systemctl enable --now sing-box" not in hang_text, "hangzhou readme still enables sing-box")
    check("流量统计数据库" not in hang_text, "hangzhou readme still describes the osaka traffic db")
    check("127.0.0.1" in hang_text, "hangzhou readme should point at local services")
    check("  note-b" in hang_text, "hangzhou notes were dropped")

    # sqlite3 import still exists for /backup and /115qr; just don't let /traffic use it above.
    check(hang.sqlite3 is sqlite3 or hang.sqlite3.__name__ == "sqlite3", "sqlite3 missing")

    hz_cfg = (BOT.parent / "config.hangzhou.yaml").read_text()
    osaka_cfg = (BOT.parent / "config.yaml").read_text()
    for snippet in [
        "http://127.0.0.1:5244/ping",
        "http://127.0.0.1:5244/dav/",
        "http://127.0.0.1:5244/dav/guangdong/",
        "http://127.0.0.1:5244/dav/guangdong/EXAMPLE_DIR/example-episode.mp4",
        "http://127.0.0.1:8095/healthz",
        "[STATUS] == 200",
        "[BODY] == pong",
        "[STATUS] == 207",
        "[STATUS] == 302",
        "ignore-redirect: true",
        "timeout: 20s",
        'Range: "bytes=0-0"',
        'Authorization: "Basic ${WEBDAV_BASIC}"',
        'token: "${TELEGRAM_TOKEN}"',
        'id: "${TELEGRAM_CHAT_ID}"',
        "address: 127.0.0.1",
        "port: 8080",
        "path: /data/gatus.db",
        "method: PROPFIND",
    ]:
        check(snippet in hz_cfg, f"hangzhou config missing {snippet}")
    check(hz_cfg.count("method: PROPFIND") == 2, "expected two PROPFIND checks")
    check("EXAMPLE_DIR" in hz_cfg, "302 probe placeholder missing")
    urls = [line.split("url:", 1)[1] for line in hz_cfg.splitlines() if line.strip().startswith("url:")]
    check(urls and all("127.0.0.1" in u for u in urls), f"hangzhou checks must stay on localhost: {urls}")
    blob = "\n".join(urls).lower()
    for banned in ["duckdns", "sing-box", "166.88.", "sumax"]:
        check(banned not in blob, f"hangzhou url contains {banned}")
    check("sing-box" in osaka_cfg and "sumax.duckdns.org" in osaka_cfg, "osaka config template was altered")
    print("ok")


def test_vps_site():
    main()


if __name__ == "__main__":
    sys.exit(main() or 0)
