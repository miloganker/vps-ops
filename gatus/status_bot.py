#!/usr/bin/env python3
"""Telegram 查询机器人：/status /server /services /traffic /backup /dl /auto /115qr /help，只回应指定 chat。"""
import json, os, re, sys, time, shutil, subprocess, urllib.request, urllib.parse, urllib.error
import socket, http.client
from datetime import datetime, timezone, timedelta

# 这台 VPS 访问 api.telegram.org 的 IPv6 会 SYN 黑洞，urllib 又不做 Happy Eyeballs，会卡满超时。
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and host.endswith("telegram.org"):
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _getaddrinfo

def log(*a):
    print(datetime.now().strftime("%F %T"), *a, file=sys.stderr, flush=True)

def load_env(p="/opt/gatus/.env"):
    for line in open(p):
        if "=" in line:
            k, v = line.strip().split("=", 1); os.environ.setdefault(k, v)
load_env()

def load_one(path, key):
    """只读一个键，不把整个文件载入环境（避免把 mediahub 的其它密钥带进进程）。"""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        return ""
    return ""

TOKEN = os.environ["TELEGRAM_TOKEN"]
MEDIAHUB_URL = "http://127.0.0.1:8095"
MEDIAHUB_TOKEN = load_one("/opt/mediahub/.env", "MEDIAHUB_INTERNAL_TOKEN")
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}/"
GATUS = "http://127.0.0.1:8080/api/v1/endpoints/statuses"
TZ = timezone(timedelta(hours=8))
SERVICES = ["sing-box", "caddy", "docker", "containerd", "ssh", "gatus-status-bot", "traffic-collector"]
CONTAINERS = ["gatus", "openlist", "mediahub"]

def call(method, timeout=40, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=timeout) as r:
        return json.load(r)

def send(text, reply_markup=None):
    for i in range(3):
        try:
            kw = {"timeout": 15, "chat_id": CHAT_ID, "text": text[:4000]}
            if reply_markup:
                kw["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            call("sendMessage", **kw); return
        except Exception as e:
            log("send failed:", e); time.sleep(2 * (i + 1))

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()

def fmt_bytes(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024: return f"{n:.0f}{u}" if u in ("B", "KB") else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"

def cmd_status():
    with urllib.request.urlopen(GATUS, timeout=10) as r:
        eps = json.load(r)
    lines, bad = [], 0
    for e in eps:
        res = e.get("results") or []
        if not res:
            lines.append(f"⏳ {e['name']}：暂无结果"); continue
        last = res[-1]; ok = last.get("success"); bad += 0 if ok else 1
        ms = last.get("duration", 0) // 1_000_000
        try:
            t = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00")).astimezone(TZ).strftime("%m-%d %H:%M")
        except Exception:
            t = "?"
        lines.append(f"{'✅' if ok else '❌'} {e['name']}：{ms}ms，{t}")
        if not ok:
            for c in last.get("conditionResults") or []:
                if not c.get("success"): lines.append(f"    ↳ {c.get('condition')}")
            for err in last.get("errors") or []: lines.append(f"    ↳ {err}")
    head = "全部正常" if bad == 0 else f"{bad} 项异常"
    return f"监控状态（{head}，北京时间）\n" + "\n".join(lines)

def cmd_server():
    load1, load5, load15 = os.getloadavg()
    cpus = os.cpu_count() or 1
    with open("/proc/stat") as f: a = list(map(int, f.readline().split()[1:]))
    time.sleep(1)
    with open("/proc/stat") as f: b = list(map(int, f.readline().split()[1:]))
    idle = (b[3] + b[4]) - (a[3] + a[4]); total = sum(b) - sum(a)
    cpu = 100 * (1 - idle / total) if total else 0
    mi = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":"); mi[k] = int(v.split()[0]) * 1024
    mt, ma = mi["MemTotal"], mi["MemAvailable"]
    st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    du = shutil.disk_usage("/")
    up = float(open("/proc/uptime").read().split()[0])
    d, rem = divmod(int(up), 86400); h, rem = divmod(rem, 3600); m = rem // 60
    return "\n".join([
        f"服务器状态（{datetime.now(TZ).strftime('%m-%d %H:%M')} 北京时间）",
        f"🖥 CPU：{cpu:.0f}%（{cpus} 核），负载 {load1:.2f} / {load5:.2f} / {load15:.2f}",
        f"🧠 内存：已用 {fmt_bytes(mt-ma)} / {fmt_bytes(mt)}（{100*(mt-ma)/mt:.0f}%），可用 {fmt_bytes(ma)}",
        f"🔄 交换：已用 {fmt_bytes(st-sf)} / {fmt_bytes(st)}" if st else "🔄 交换：未启用",
        f"💾 硬盘 /：已用 {fmt_bytes(du.used)} / {fmt_bytes(du.total)}（{100*du.used/du.total:.0f}%），剩余 {fmt_bytes(du.free)}",
        f"⏱ 开机：{d} 天 {h} 小时 {m} 分",
    ])

def cmd_services():
    lines = ["服务状态"]
    for s in SERVICES:
        st = run(["systemctl", "is-active", s]) or "unknown"
        lines.append(f"{'✅' if st == 'active' else '❌'} {s}：{st}")
    lines.append("容器状态")
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"])
    got = dict(l.split("\t", 1) for l in out.splitlines() if "\t" in l)
    for c in CONTAINERS:
        st = got.get(c, "不存在")
        lines.append(f"{'✅' if st.startswith('Up') else '❌'} {c}：{st}")
    return "\n".join(lines)

# ---------- /traffic：按类型统计经代理的流量（数据来自 traffic-collector） ----------
import sqlite3, ipaddress
TRAFFIC_DB = "/opt/gatus/data_traffic/traffic.db"
CATEGORIES = [
    ("🎬 视频", ["googlevideo.com", "youtube.com", "ytimg.com", "youtu.be", "youtube-nocookie.com", "netflix.com", "nflxvideo.net", "nflximg.net", "nflxso.net", "nflxext.com", "disneyplus.com", "disney-plus.net", "dssott.com", "bamgrid.com", "hbo.com", "hbomax.com", "max.com", "primevideo.com", "aiv-cdn.net", "twitch.tv", "ttvnw.net", "jtvnw.net", "bilibili.com", "bilivideo.com", "hdslb.com", "tiktok.com", "tiktokcdn.com", "tiktokv.com", "byteoversea.com", "ibytedtos.com", "vimeo.com", "vimeocdn.com", "dailymotion.com", "spotify.com", "scdn.co", "spotifycdn.com", "hulu.com", "pornhub.com", "phncdn.com"]),
    ("💬 社交通讯", ["telegram.org", "t.me", "telegram.me", "telesco.pe", "tdesktop.com", "whatsapp.com", "whatsapp.net", "instagram.com", "cdninstagram.com", "facebook.com", "facebook.net", "fbcdn.net", "fb.com", "messenger.com", "twitter.com", "x.com", "twimg.com", "t.co", "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media", "line.me", "line-apps.com", "line-scdn.net", "naver.jp", "reddit.com", "redditmedia.com", "redd.it", "redditstatic.com", "threads.net", "signal.org", "snapchat.com", "sc-cdn.net", "linkedin.com", "licdn.com", "kakao.com", "pinterest.com", "pinimg.com", "tumblr.com"]),
    ("🍎 苹果服务", ["apple.com", "icloud.com", "icloud-content.com", "mzstatic.com", "aaplimg.com", "apple-cloudkit.com", "cdn-apple.com", "apple.news", "apple-dns.net", "me.com", "itunes.com", "appstore.com"]),
    ("🤖 AI", ["openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com", "anthropic.com", "claude.ai", "claude.com", "x.ai", "grok.com", "perplexity.ai", "pplx.ai", "poe.com", "cursor.sh", "cursor.com", "huggingface.co", "mistral.ai", "deepseek.com"]),
    ("🔍 谷歌", ["google.com", "gstatic.com", "googleapis.com", "gmail.com", "googleusercontent.com", "googlesyndication.com", "googletagmanager.com", "google-analytics.com", "doubleclick.net", "ggpht.com", "gvt1.com", "gvt2.com", "1e100.net", "app-measurement.com", "googleadservices.com", "withgoogle.com", "android.com", "firebaseio.com", "crashlytics.com", "goo.gl", "blogger.com", "google.com.hk", "google.co.jp"]),
    ("📦 下载更新", ["github.com", "githubusercontent.com", "githubassets.com", "github.io", "ghcr.io", "steampowered.com", "steamcontent.com", "steamstatic.com", "steamserver.net", "steamcommunity.com", "docker.io", "docker.com", "microsoft.com", "windowsupdate.com", "msftconnecttest.com", "pypi.org", "pythonhosted.org", "npmjs.org", "npmjs.com", "dropbox.com", "dropboxusercontent.com", "mega.nz", "playstation.net", "nintendo.net", "epicgames.com", "debian.org", "ubuntu.com", "sourceforge.net"]),
]
CAT_NETS = [  # 有些 App 直接按 IP 连接
    ("💬 社交通讯", ["91.108.4.0/22", "91.108.8.0/21", "91.108.16.0/21", "91.108.56.0/22", "95.161.64.0/20", "149.154.160.0/20", "185.76.151.0/24", "2001:b28:f23d::/48", "2001:b28:f23f::/48", "2001:67c:4e8::/48"]),
    ("🍎 苹果服务", ["17.0.0.0/8"]),
]
OTHER = "🌐 其他"
_DOM2CAT = {d: c for c, ds in CATEGORIES for d in ds}
_NETS = [(c, ipaddress.ip_network(n)) for c, ns in CAT_NETS for n in ns]

def categorize(dom):
    try:
        ip = ipaddress.ip_address(dom)
        for c, n in _NETS:
            if ip.version == n.version and ip in n: return c
        return OTHER
    except ValueError:
        pass
    p = dom.split(".")
    for i in range(len(p)):
        c = _DOM2CAT.get(".".join(p[i:]))
        if c: return c
    return OTHER

def cmd_traffic(arg=""):
    today = datetime.now(TZ).date()
    arg = (arg or "").strip().lower()
    if arg in ("", "today", "今天"):
        start, end, title = today, today, f"今天 {today:%m-%d}"
    elif arg in ("yesterday", "y", "昨天"):
        y = today - timedelta(days=1); start, end, title = y, y, f"昨天 {y:%m-%d}"
    elif arg.isdigit() and 1 <= int(arg) <= 60:
        n = int(arg); start, end = today - timedelta(days=n - 1), today
        title = f"近 {n} 天 {start:%m-%d}～{end:%m-%d}"
    else:
        return "用法：/traffic（今天）、/traffic yesterday（昨天）、/traffic 7（近 7 天，最多 60）"
    if not os.path.exists(TRAFFIC_DB):
        return "还没有流量数据（采集服务 traffic-collector 尚未写入）。"
    db = sqlite3.connect(f"file:{TRAFFIC_DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute("SELECT domain, SUM(up), SUM(down) FROM traffic WHERE day BETWEEN ? AND ? GROUP BY domain",
                          (start.isoformat(), end.isoformat())).fetchall()
    finally:
        db.close()
    if not rows:
        return f"📊 代理流量（{title}，北京时间）\n暂无数据。"
    cats, tu, td = {}, 0, 0
    for dom, u, d in rows:
        tu += u; td += d
        cats.setdefault(categorize(dom), []).append((dom, u + d))
    total = tu + td
    order = sorted(cats.items(), key=lambda kv: -sum(t for _, t in kv[1]))
    lines = [f"📊 代理流量（{title}，北京时间）",
             f"合计 {fmt_bytes(total)}（↓{fmt_bytes(td)} ↑{fmt_bytes(tu)}）", ""]
    for cat, doms in order:
        ct = sum(t for _, t in doms); doms.sort(key=lambda x: -x[1])
        pct = 100 * ct / total if total else 0
        lines.append(f"{cat}  {fmt_bytes(ct)}（{pct:.0f}%）")
        limit = 10 if cat == OTHER else 8
        for dom, t in doms[:limit]:
            lines.append(f"  · {dom}  {fmt_bytes(t)}")
        if len(doms) > limit:
            rest = sum(t for _, t in doms[limit:])
            lines.append(f"  · 其余 {len(doms) - limit} 个域名  {fmt_bytes(rest)}")
        lines.append("")
    lines.append("注：只统计经过本代理（sing-box）的流量，11-14 21:19 起开始统计；按域名归类，不能区分具体 App；“(未归属)”为两次采样间已关闭的短连接流量。")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3850].rsplit("\n", 1)[0] + "\n…（内容过长已截断）"
    return text

# ---------- /backup：打包关键配置，7z AES-256 加密（含文件名）后发到 Telegram，服务器不留副本 ----------
import tempfile, threading, uuid, glob
BACKUP_LOCK = threading.Lock()
TG_LIMIT = 50 * 1024 * 1024
OPENLIST_DATA = "/opt/openlist/data"

def _copy(src, dst_root, rel):
    dst = os.path.join(dst_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return rel

def build_backup():
    """返回 (tmpdir, archive_path, file_count, size, notes)。调用方负责删除 tmpdir。"""
    pw = os.environ.get("BACKUP_PASSWORD", "")
    if not pw:
        raise RuntimeError("未设置 BACKUP_PASSWORD")
    now = datetime.now(TZ)
    tmp = tempfile.mkdtemp(prefix="vps-backup-", dir="/tmp")
    os.chmod(tmp, 0o700)
    stage = os.path.join(tmp, "vps-backup")
    files, notes = [], []
    # 1) 普通文件
    plain = [p for p in glob.glob("/etc/sing-box/*") if os.path.isfile(p)]
    plain += [p for p in glob.glob("/etc/caddy/*") if os.path.isfile(p)]
    plain += [p for p in ["/opt/gatus/config.yaml", "/opt/gatus/docker-compose.yml", "/opt/gatus/.env"] if os.path.isfile(p)]
    plain += sorted(glob.glob("/opt/gatus/*.py"))
    plain += [p for p in ["/etc/systemd/system/gatus-status-bot.service", "/etc/systemd/system/traffic-collector.service",
                          "/etc/systemd/system/sing-box.service", "/root/.ssh/authorized_keys"] if os.path.isfile(p)]
    plain += [p for p in [f"{OPENLIST_DATA}/config.json"] if os.path.isfile(p)]
    plain += [p for p in glob.glob("/etc/systemd/system/sing-box.service.d/*") if os.path.isfile(p)]
    plain += [p for p in glob.glob("/etc/ssh/sshd_config.d/*.conf") if os.path.isfile(p)]
    plain += [p for p in glob.glob("/etc/systemd/journald.conf.d/*.conf") if os.path.isfile(p)]
    # 对接说明书与迁移脚本
    plain += [p for p in glob.glob("/opt/gatus/handoff/**/*", recursive=True) if os.path.isfile(p)]
    for p in plain:
        files.append(_copy(p, stage, p.lstrip("/")))
    # 2) OpenList 数据库：用 sqlite 在线备份 API 得到一致副本，失败则直接复制
    db = f"{OPENLIST_DATA}/data.db"
    if os.path.isfile(db):
        rel = db.lstrip("/"); dst = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            s = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            d = sqlite3.connect(dst)
            try:
                s.backup(d)
            finally:
                d.close(); s.close()
            files.append(rel)
        except Exception as e:
            log("sqlite backup failed, copying raw:", e); notes.append(f"data.db 在线备份失败，改为直接复制（{e}）")
            if os.path.exists(dst): os.remove(dst)
            for p in [db, db + "-wal", db + "-shm"]:
                if os.path.isfile(p): files.append(_copy(p, stage, p.lstrip("/")))
    # 3) OpenList 容器定义（docker run 启动，无 compose）
    for p in glob.glob("/opt/openlist/*.yml") + glob.glob("/opt/openlist/*.yaml"):
        files.append(_copy(p, stage, p.lstrip("/")))
    r = subprocess.run(["docker", "inspect", "openlist"], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        with open(os.path.join(stage, "openlist-inspect.json"), "w") as f: f.write(r.stdout)
        files.append("openlist-inspect.json")
    else:
        notes.append("docker inspect openlist 失败：" + (r.stderr.strip()[:200] or "未知"))
    # 3b) mediahub（影视整理服务）：整个目录含 .env，跳过可重建的缓存
    MEDIAHUB = "/opt/mediahub"
    for root, dirs, fnames in os.walk(MEDIAHUB):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", ".venv", ".git")]
        for fn in fnames:
            p = os.path.join(root, fn)
            if os.path.isfile(p) and not os.path.islink(p): files.append(_copy(p, stage, p.lstrip("/")))
    # 4) README
    readme = [
        f"VPS 配置备份  {now:%Y-%m-%d %H:%M} 北京时间  主机 {os.uname().nodename}",
        "",
        "内容（路径与服务器上的原路径一致）：",
        *[f"  {f}" for f in sorted(files)],
        "",
        "恢复提示：",
        "  1. apt 安装 sing-box、caddy、docker.io、docker-compose、python3、7zip。",
        "  2. 把 etc/、opt/、root/ 下的文件复制回 / 对应位置（注意 .env 和 sing-box 配置权限 600，authorized_keys 600）。",
        "  3. OpenList：数据目录 /opt/openlist/data 归属 1001:1001（chown -R 1001:1001），然后按 openlist-inspect.json 重建，原来大致是：",
        "     docker run -d --name openlist --restart unless-stopped -p 127.0.0.1:5244:5244 -v /opt/openlist/data:/opt/openlist/data openlistteam/openlist:latest",
        "     （以 inspect 里的 Config.Env / HostConfig 为准）。",
        "  4. Gatus：cd /opt/gatus && docker-compose up -d（监控历史 data/ 未备份，会重新开始）。",
        "  4b. mediahub：opt/mediahub 复制回 /opt/mediahub（.env 权限 600，data/ 归属 10001:10001），cd /opt/mediahub && docker compose up -d --build。",
        "  5. systemctl daemon-reload && systemctl enable --now sing-box caddy gatus-status-bot traffic-collector。",
        "  - SSH 只允许密钥登录（etc/ssh/sshd_config.d/）和日志上限（etc/systemd/journald.conf.d/）：复制回去后 systemctl reload ssh、systemctl restart systemd-journald。",
        "  6. sing-box 用的是软件包自带的 systemd 单元（/usr/lib/systemd/system/sing-box.service），装包即有，未单独备份。",
        "  未备份：Gatus 历史数据库、流量统计数据库、OpenList 日志/临时目录（都可重新生成）。",
    ]
    if notes: readme += ["", "备注：", *[f"  {n}" for n in notes]]
    with open(os.path.join(stage, "README.txt"), "w") as f: f.write("\n".join(readme) + "\n")
    files.append("README.txt")
    out = os.path.join(tmp, f"vps-backup-{now:%Y%m%d-%H%M}.7z")
    # 密码走 stdin，不出现在命令行；-mhe=on 加密文件名；小字典控制内存
    r = subprocess.run(["7z", "a", "-t7z", "-p", "-mhe=on", "-m0=lzma2", "-mx=5", "-md=1m", "-mmt=1", "-bd", out, "."],
                       cwd=stage, input=pw + "\n", capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        raise RuntimeError("7z 打包失败：" + (r.stderr.strip() or r.stdout.strip())[-300:])
    shutil.rmtree(stage, ignore_errors=True)
    return tmp, out, len(files), os.path.getsize(out), notes

def send_document(path, caption):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f: blob = f.read()
    parts = []
    for k, v in (("chat_id", str(CHAT_ID)), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"\r\n'
                  f"Content-Type: application/x-7z-compressed\r\n\r\n").encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API + "sendDocument", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    if not res.get("ok"): raise RuntimeError(f"Telegram 返回：{res}")

def do_backup():
    tmp = None
    try:
        tmp, out, n, size, notes = build_backup()
        if size > TG_LIMIT:
            send(f"⚠️ 备份包 {fmt_bytes(size)}，超过 Telegram 机器人 50MB 上传上限，未发送（已删除）。"); return
        cap = (f"🗄 VPS 配置备份 {os.path.basename(out)}\n大小 {fmt_bytes(size)}，共 {n} 个文件（含 README.txt）\n"
               "🔐 已用你设置的备份密码加密（AES-256，文件名也加密），打开时需要输入该密码。")
        if notes: cap += "\n备注：" + "；".join(notes)
        send_document(out, cap[:1000])
        log(f"backup sent: {size} bytes, {n} files")
    except Exception as e:
        log("backup failed:", e); send(f"⚠️ /backup 失败：{str(e)[:300]}")
    finally:
        if tmp: shutil.rmtree(tmp, ignore_errors=True)
        BACKUP_LOCK.release()

def start_backup():
    if not BACKUP_LOCK.acquire(blocking=False):
        send("⏳ 已有一个备份正在打包/发送，请稍候。"); return
    send("🗄 正在打包备份，稍等…")
    threading.Thread(target=do_backup, daemon=True).start()


# ---------- /dl：把磁力或 ed2k 交给 mediahub，由 115 云下载（mediahub 只发通知，不轮询 Telegram）----------
MAGNET_RE = re.compile(r"(magnet:\?xt=urn:[a-z0-9]+:[A-Za-z0-9]+)([^\s<>\"']*)", re.I)
ED2K_RE = re.compile(r"(ed2k://\|file\|[^\s<>\"']+)", re.I)
HTTP_RE = re.compile(r"(https?://[^\s<>\"']+)", re.I)
MAX_LINKS_PER_MESSAGE = 20  # 一条消息最多处理这么多条，多出来的会告知，不会悄悄丢掉
DL_STATUS = {
    "needs_kind": "还不能判断是电影还是电视剧，请点下面的按钮（或回复「电影」「电视剧」）。",
    "submitting": "正在提交到 115 云下载…",
    "downloading": "已提交 115 云下载，盯梢中。完成后会通知你。",
    "already_exists": "115 里已经有这条任务，不会重复提交，改为盯梢。完成后会通知你。",
    "resolving": "已下完，正在按文件判断类型并移到待整理，完成后会通知你。",
    "organizing": "已下完并移到待整理，正在自动改名入库，完成后会通知你。",
    "complete": "已经完成。",
    "failed": "提交或下载失败。",
}

def extract_links(text, allow_http=False):
    text = text or ""
    found = []
    for rx in (MAGNET_RE, ED2K_RE):
        for m in rx.finditer(text):
            url = m.group(0).rstrip(".,;）)】]>")
            if url and url not in found:
                found.append(url)
    if allow_http:
        for m in HTTP_RE.finditer(text):
            url = m.group(0).rstrip(".,;）)】]>")
            if url and url not in found:
                found.append(url)
    return found

def mh_call(method, path, body=None):
    if not MEDIAHUB_TOKEN:
        raise RuntimeError("还没有配置 MEDIAHUB_INTERNAL_TOKEN")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        MEDIAHUB_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Internal-Token": MEDIAHUB_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:300]
        try:
            j = json.loads(raw)
            msg = j.get("detail") or j.get("message") or raw
        except Exception:
            msg = f"HTTP {e.code}"
        raise RuntimeError(str(msg))

def kind_keyboard(jid):
    return {"inline_keyboard": [[
        {"text": "电影", "callback_data": f"dlkind:{jid}:movie"},
        {"text": "电视剧", "callback_data": f"dlkind:{jid}:tv"},
    ]]}

def fmt_job(job):
    name = job.get("display_name") or job.get("id")
    kind = {"movie": "电影", "tv": "电视剧"}.get(job.get("kind") or "", "")
    st = job.get("status") or ""
    lines = [f"🎬 {name}" + (f"（{kind}）" if kind else "")]
    if job.get("auto") and not job.get("kind") and st in ("downloading", "already_exists", "submitting"):
        lines.append(DL_STATUS.get(st, st).split("完成后")[0].rstrip("，。") + "。")
        lines.append("从链接名字看不出电影还是电视剧：下完我会按种子里的文件自动识别再通知你，实在判断不了才会问你。")
    elif job.get("auto") and job.get("kind") and st in ("downloading", "already_exists", "submitting"):
        lines.append(f"已记下类型：{kind}。下完直接移到待整理/{kind}并扫描，完成后通知你。")
    else:
        lines.append(DL_STATUS.get(st, st))
    if st == "needs_kind" and job.get("reason"):
        lines.append(job["reason"])
    if st == "failed" and job.get("error"):
        lines.append(str(job["error"])[:300])
    org = job.get("organize") or {}
    if st == "complete" and org.get("state"):
        lines.append(ORG_STATE.get(org["state"], org["state"]) + (f"：{org['dest']}" if org.get("dest") and org["state"] in ("done", "unverified") else ""))
        if org.get("reasons"):
            lines.append("原因：" + "；".join(org["reasons"])[:300])
    return "\n".join(lines)

ORG_STATE = {"done": "✅ 已自动入库", "skipped": "⚠️ 没有自动入库，请打开网页确认", "unverified": "❓ 已移动，待核实",
             "failed": "❌ 自动入库没有完成", "interrupted": "⚠️ 自动入库中断，请打开网页核对",
             "queued": "排队等待自动入库", "running": "正在自动入库"}

def cmd_auto(arg=""):
    """/auto 查看自动入库开关；/auto on|off 开关（写在 mediahub 的 data 里，不改 .env）。"""
    a = (arg or "").strip().lower()
    if a in ("on", "开", "1", "true", "off", "关", "0", "false"):
        st = mh_call("POST", "/internal/auto-organize", {"enabled": a in ("on", "开", "1", "true")})
    elif a:
        return "用法：/auto 查看状态，/auto on 开启，/auto off 关闭"
    else:
        st = mh_call("GET", "/internal/auto-organize")
    lines = [f"自动入库：{'开' if st.get('enabled') else '关'}"
             + ("（配置默认）" if st.get("source") == "env" else f"（手动设置；配置默认{'开' if st.get('env_default') else '关'}）")]
    lines.append("开着时：下完、分类、扫描后，识别高置信且没有提示/冲突就自动改名并移进片库；否则通知你去网页确认。")
    if st.get("queued"):
        lines.append("排队中：" + "、".join(q.get("name") or q["id"] for q in st["queued"][:5]))
    for r in (st.get("recent") or [])[:5]:
        lines.append(f"· {ORG_STATE.get(r.get('state'), r.get('state'))} {r.get('name') or r.get('id')}")
    return "\n".join(lines)

def _link_label(url):
    url = url or ""
    m = re.search(r"urn:bt[im]h:([A-Za-z0-9]+)", url, re.I)
    if m:
        return "磁力 " + m.group(1)[:16]
    if url.lower().startswith("ed2k://"):
        parts = url.split("|")
        return (parts[2][:40] if len(parts) > 2 else "ed2k")
    return url[:48]


def short_job_line(job):
    name = job.get("display_name") or job.get("id") or ""
    kind = {"movie": "电影", "tv": "电视剧"}.get(job.get("kind") or "", "")
    st = job.get("status") or ""
    label = {
        "needs_kind": "待选类型",
        "submitting": "正在提交",
        "downloading": "已提交，盯梢中",
        "already_exists": "已在队列，盯梢中",
        "resolving": "已下完，正在分类",
        "organizing": "正在自动入库",
        "complete": "已完成",
        "failed": "失败",
    }.get(st, st)
    extra = ""
    if job.get("auto") and not job.get("kind") and st in ("downloading", "already_exists", "submitting"):
        extra = "；先下到未分类，下完按文件识别"
    err = ""
    if st == "failed" and job.get("error"):
        err = "：" + str(job["error"])[:80]
    return f"· {name}" + (f"（{kind}）" if kind else "") + f"：{label}{extra}{err}"


def submit_links(links):
    if not links:
        send("用法：直接把磁力或 ed2k 链接发给我，或 /dl <链接>。一条消息可以多条，最多 20 条。"); return
    total = len(links)
    skipped = max(0, total - MAX_LINKS_PER_MESSAGE)
    links = links[:MAX_LINKS_PER_MESSAGE]
    results = []
    for url in links:
        try:
            job = mh_call("POST", "/internal/downloads", {"url": url})
            log("dl submitted:", job.get("id"), job.get("status"), job.get("display_name"))
            results.append(("job", job))
        except Exception as e:
            log("dl failed:", e)
            results.append(("err", url, str(e)))

    def emit_one(item):
        if item[0] == "err":
            send(f"⚠️ {_link_label(item[1])} 投递失败：{item[2]}")
            return
        job = item[1]
        kb = kind_keyboard(job["id"]) if job.get("status") == "needs_kind" else None
        send(fmt_job(job), reply_markup=kb)

    # 3 条及以下：每条单独一条回复（需要选类型的还是带按钮）
    if len(links) <= 3 and not skipped:
        for item in results:
            emit_one(item)
        return

    n_ok = n_exists = n_fail = 0
    for item in results:
        if item[0] == "err":
            n_fail += 1
            continue
        st = (item[1] or {}).get("status")
        if st == "already_exists":
            n_exists += 1
        elif st == "failed":
            n_fail += 1
        else:
            n_ok += 1
    lines = []
    if skipped:
        lines.append(f"消息里有 {total} 条链接，先处理 {len(links)} 条，其余 {skipped} 条这次没提交，请再发一次。")
    else:
        lines.append(f"已收到 {len(links)} 条链接。")
    bits = []
    if n_ok:
        bits.append(f"新提交 {n_ok} 条")
    if n_exists:
        bits.append(f"已在队列 {n_exists} 条")
    if n_fail:
        bits.append(f"失败 {n_fail} 条")
    if bits:
        lines.append("，".join(bits) + "。")
    for item in results:
        if item[0] == "err":
            lines.append(f"· {_link_label(item[1])}：投递失败：{item[2][:80]}")
        else:
            lines.append(short_job_line(item[1]))
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    send(text)
    # 需要选类型的另外单独发一条，带按钮
    for item in results:
        if item[0] == "job" and item[1].get("status") == "needs_kind":
            send(fmt_job(item[1]), reply_markup=kind_keyboard(item[1]["id"]))

def cmd_dl(arg=""):
    arg = (arg or "").strip()
    if not arg:
        send("用法：/dl <磁力或 ed2k 链接>，也可以直接把链接发给我。一条消息可以多条，最多 20 条。"); return
    links = extract_links(arg, allow_http=True)
    if not links:
        low = arg.lower()
        if low.startswith(("magnet:", "ed2k://", "http://", "https://")):
            links = [arg]
    submit_links(links)

def set_job_kind(jid, kind):
    job = mh_call("POST", f"/internal/downloads/{jid}/kind", {"kind": kind})
    send(fmt_job(job))

def handle_kind_text(text):
    kind = "movie" if "电影" in text else "tv"
    data = mh_call("GET", "/internal/downloads")
    jobs = data.get("jobs") if isinstance(data, dict) else data
    pending = next((j for j in (jobs or []) if j.get("status") == "needs_kind"), None)
    if not pending:
        send("现在没有等待选择类型的下载任务。"); return
    set_job_kind(pending["id"], kind)

def handle_callback(cq):
    chat = (cq.get("message") or {}).get("chat") or {}
    frm = cq.get("from") or {}
    if chat.get("id") != CHAT_ID and frm.get("id") != CHAT_ID:
        return
    data = cq.get("data") or ""
    try:
        call("answerCallbackQuery", timeout=10, callback_query_id=cq.get("id", ""))
    except Exception as e:
        log("answerCallbackQuery failed:", e)
    if not data.startswith("dlkind:"):
        return
    try:
        _, jid, kind = data.split(":", 2)
        if kind not in ("movie", "tv"):
            return
        set_job_kind(jid, kind)
    except Exception as e:
        log("dl callback failed:", e); send(f"⚠️ 设置类型失败：{e}")


# ---------- /115qr：扫码刷新 OpenList 里 115 存储的登录（令牌只留在内存，结束后丢弃） ----------
OPENLIST = "http://127.0.0.1:5244"
_QR_LOCK = threading.Lock()
_QR_CANCEL = threading.Event()
_QR_SESSION_SEC = 240       # 单张二维码最多等 4 分钟；长轮询要一直挂着，不能短超时切断
_QR_REISSUE = 1             # 过期后自动再发一张，之后停止

def _safe_err(e):
    """给日志和聊天用的错误摘要，去掉可能的令牌、Cookie、长串。"""
    s = str(e)
    s = re.sub(r"(UID|CID|SEID|KID)=[^;\s&]+", r"\1=<redacted>", s, flags=re.I)
    s = re.sub(r"(cookie|qrcode_token|authorization|sign|uid|token|password)[=: ]+\S+", r"\1=<redacted>", s, flags=re.I)
    s = re.sub(r"[A-Za-z0-9+/=_\-]{24,}", "<redacted>", s)
    return s[:200]

def _ol_site_token():
    con = sqlite3.connect("file:/opt/openlist/data/data.db?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute("SELECT value FROM x_setting_items WHERE key=?", ("token",)).fetchone()
        return (row[0] or "") if row else ""
    finally:
        con.close()

def _ol_api(path, method="GET", payload=None, timeout=60):
    token = _ol_site_token()
    if not token:
        raise RuntimeError("读不到 OpenList 管理令牌")
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(OPENLIST + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            obj = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            raise RuntimeError(f"OpenList HTTP {e.code}") from None
    if not isinstance(obj, dict) or obj.get("code") != 200:
        msg = (obj or {}).get("message") if isinstance(obj, dict) else ""
        raise RuntimeError(msg or "OpenList 接口返回异常")
    return obj

def _send_photo(blob, caption):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", str(CHAT_ID)), ("caption", caption[:1024])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="115-qrcode.png"\r\n'
                  f"Content-Type: image/png\r\n\r\n").encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(API + "sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError("发送二维码图片失败")
    return (res.get("result") or {}).get("message_id")

def _edit_caption(message_id, caption):
    if not message_id:
        send(caption); return
    try:
        call("editMessageCaption", timeout=15, chat_id=CHAT_ID, message_id=message_id, caption=caption[:1024])
    except Exception as e:
        log("editMessageCaption failed:", _safe_err(e)); send(caption)

class _QRStop(Exception):
    def __init__(self, reason):
        self.reason = reason  # aborted / timeout

def _qr_token():
    # 用 tv 端点申请，手机确认时才会显示电视/TV，而不是「网页登录」。
    with urllib.request.urlopen("https://qrcodeapi.115.com/api/1.0/tv/1.0/token/", timeout=15) as r:
        obj = json.load(r)
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict) or not data.get("uid") or not data.get("sign"):
        raise RuntimeError("115 没有返回有效的二维码")
    return data

def _qr_image(uid):
    url = "https://qrcodeapi.115.com/api/1.0/tv/1.0/qrcode?uid=" + urllib.parse.quote(str(uid))
    with urllib.request.urlopen(url, timeout=15) as r:
        blob = r.read()
    if not blob:
        raise RuntimeError("二维码图片是空的")
    return blob

def _http_get_json_until(url, deadline):
    """保持一次 GET 直到有响应、被取消或到截止时间。取消时关掉连接以立刻结束。"""
    u = urllib.parse.urlparse(url)
    remaining = max(1.0, deadline - time.time())
    conn = http.client.HTTPSConnection(u.hostname, 443, timeout=remaining)
    stop = threading.Event()
    def _abort_if_needed():
        while not stop.wait(0.5):
            if _QR_CANCEL.is_set() or time.time() >= deadline:
                try:
                    conn.close()
                except Exception:
                    pass
                return
    watcher = threading.Thread(target=_abort_if_needed, daemon=True)
    watcher.start()
    try:
        path = u.path + (("?" + u.query) if u.query else "")
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"115 状态接口 HTTP {resp.status}")
        return json.loads(raw)
    except (OSError, http.client.HTTPException, TimeoutError, socket.timeout, ValueError) as e:
        if _QR_CANCEL.is_set():
            raise _QRStop("aborted") from None
        if time.time() >= deadline:
            raise _QRStop("timeout") from None
        raise
    finally:
        stop.set()
        try:
            conn.close()
        except Exception:
            pass

def _qr_status_wait(uid, tm, sign, deadline):
    q = urllib.parse.urlencode({"uid": uid, "time": tm, "sign": sign, "_": int(time.time())})
    obj = _http_get_json_until("https://qrcodeapi.115.com/get/status/?" + q, deadline)
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, dict) and "status" in data:
        try:
            return int(data["status"])
        except (TypeError, ValueError):
            pass
    # 部分失败响应没有 data.status，state 为 0/False 表示已失效
    state = obj.get("state") if isinstance(obj, dict) else None
    if state in (0, False, "0"):
        return -1
    raise RuntimeError("115 状态接口返回异常")

_QR_CAPTION = ("请用 115 APP 扫码，然后在手机上点确认。确认时设备名应类似电视/TV，不要是「网页登录」。"
               "这边会自动接着更新，不用把令牌或 Cookie 发过来。二维码几分钟内有效。要取消的话发送 /115qr cancel。")

def _issue_qr():
    data = _qr_token()
    blob = _qr_image(data["uid"])
    mid = _send_photo(blob, _QR_CAPTION)
    return data, mid

def _verify_dav():
    basic = os.environ.get("WEBDAV_BASIC", "")
    if not basic:
        raise RuntimeError("没有 WebDAV 认证配置，无法复查目录")
    auth = basic if basic.lower().startswith("basic ") else "Basic " + basic
    req = urllib.request.Request(OPENLIST + "/dav/guangdong/", method="PROPFIND",
                                 headers={"Authorization": auth, "Depth": "1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

def _update_openlist(uid):
    lst = _ol_api("/api/admin/storage/list")
    content = ((lst.get("data") or {}).get("content") if isinstance(lst.get("data"), dict) else None) or []
    found = [s for s in content if s.get("mount_path") == "/guangdong" and s.get("driver") == "115 Cloud"]
    if len(found) != 1:
        raise RuntimeError("没有找到唯一的 /guangdong 115 存储，未做修改")
    st = _ol_api("/api/admin/storage/get?id=%s" % found[0]["id"]).get("data") or {}
    if st.get("mount_path") != "/guangdong" or st.get("driver") != "115 Cloud":
        raise RuntimeError("存储信息不匹配，未做修改")
    raw = st.get("addition") or "{}"
    add = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if add.get("page_size") != 1500 or add.get("limit_rate") != 4:
        raise RuntimeError("当前 page_size 或 limit_rate 不是预期值，未做修改")
    if "cookie" not in add or "qrcode_token" not in add:
        raise RuntimeError("存储字段不完整，未做修改")
    add["qrcode_token"] = uid
    add["qrcode_source"] = "tv"
    add["cookie"] = ""
    if add.get("page_size") != 1500 or add.get("limit_rate") != 4:
        raise RuntimeError("准备写入的速率参数异常，未做修改")
    payload = dict(st)
    payload["addition"] = json.dumps(add, ensure_ascii=False)
    _ol_api("/api/admin/storage/update", method="POST", payload=payload, timeout=60)
    # 保存后再确认关键字段没被改掉（cookie/令牌本身不读取内容）
    again = _ol_api("/api/admin/storage/get?id=%s" % st["id"]).get("data") or {}
    raw2 = again.get("addition") or "{}"
    add2 = json.loads(raw2) if isinstance(raw2, str) else dict(raw2)
    if add2.get("page_size") != 1500 or add2.get("limit_rate") != 4:
        raise RuntimeError("更新后速率参数发生变化，请检查 OpenList 存储设置")
    if again.get("webdav_policy") != "302_redirect":
        raise RuntimeError("更新后 webdav_policy 发生了变化，请检查 OpenList 存储设置")
    return again.get("status")

def _wait_qr(data, mid, deadline):
    """挂起长轮询直到状态变化。返回 'confirmed' / 'expired' / 'canceled' / 'aborted' / 'timeout'。"""
    scanned = False
    while time.time() < deadline:
        if _QR_CANCEL.is_set():
            return "aborted"
        try:
            status = _qr_status_wait(data["uid"], data["time"], data["sign"], deadline)
        except _QRStop as e:
            return e.reason
        except Exception as e:
            log("115 status poll failed:", _safe_err(e))
            if _QR_CANCEL.wait(2):
                return "aborted"
            continue
        log("115 qr status:", status)
        if status == 1:
            if not scanned:
                scanned = True
                _edit_caption(mid, "已扫码，请在 115 APP 里点确认（设备名应类似电视/TV）。")
        elif status == 2:
            return "confirmed"
        elif status == -1:
            return "expired"
        elif status == -2:
            return "canceled"
        elif status != 0:
            log("115 unexpected qr status:", status)
        if status == 0 and _QR_CANCEL.wait(2):
            return "aborted"
    return "timeout"

def _do_115qr():
    mid = None
    try:
        send("正在向 115 申请二维码…")
        log("115qr session started")
        reissued = 0
        while True:
            if _QR_CANCEL.is_set():
                send("已取消，没有改 OpenList 里的登录信息。"); return
            try:
                data, mid = _issue_qr()
            except Exception as e:
                log("115 qr issue failed:", _safe_err(e)); send(f"⚠️ 申请二维码失败：{_safe_err(e)}。稍后再试 /115qr。"); return
            result = _wait_qr(data, mid, time.time() + _QR_SESSION_SEC)
            if result == "aborted":
                send("已取消，没有改 OpenList 里的登录信息。"); return
            if result == "canceled":
                send("这次扫码已取消。需要的话再发送 /115qr。"); return
            if result == "timeout":
                send("等了几分钟还没确认，我先停了。需要的话再发送 /115qr。"); return
            if result == "expired":
                if reissued < _QR_REISSUE and not _QR_CANCEL.is_set():
                    reissued += 1
                    send("这张二维码过期了，我再发一张新的。还是用 115 APP 扫码并确认。")
                    continue
                send("二维码过期了。想继续的话再发送一次 /115qr。"); return
            # confirmed：即使用户这时点了 cancel，也把已经确认的登录更新上去
            log("115qr confirmed, updating OpenList")
            send("扫码已确认，正在更新 OpenList 的 115 登录…")
            try:
                st = _update_openlist(data["uid"])
            except Exception as e:
                log("openlist 115 update failed:", _safe_err(e))
                send(f"⚠️ OpenList 更新失败：{_safe_err(e)}。115 登录可能没换上，可以再试一次 /115qr。")
                return
            # 复查列目录，失败时稍等再试一次
            code = None
            for i in range(3):
                try:
                    code = _verify_dav()
                    if code == 207:
                        break
                except Exception as e:
                    log("dav verify failed:", _safe_err(e)); code = None
                time.sleep(2 * (i + 1))
            if code == 207:
                send("✅ 115 登录已更新，目录检查正常。WebDAV 和 Flacbox 都走这一处，不用再改别的。")
                log("115 qr update verified, storage status:", st)
            else:
                send(f"⚠️ OpenList 已保存（存储状态：{st or '未知'}），但列目录没有成功"
                     f"{'' if code is None else '（状态 '+str(code)+'）'}。可以稍后再试，或再发送 /115qr。")
            return
    except Exception as e:
        log("/115qr failed:", _safe_err(e)); send(f"⚠️ /115qr 失败：{_safe_err(e)}")
    finally:
        _QR_LOCK.release()

def cmd_115qr(arg=""):
    arg = (arg or "").strip().lower()
    if arg in ("cancel", "取消"):
        if _QR_LOCK.locked():
            _QR_CANCEL.set()
            send("好，正在取消这次扫码。")
        else:
            send("现在没有进行中的扫码。")
        return
    if arg:
        send("用法：/115qr 开始扫码刷新 115 登录；/115qr cancel 取消。"); return
    if not _QR_LOCK.acquire(blocking=False):
        if _QR_CANCEL.is_set():
            send("正在取消上一次扫码，请稍等几秒再试。")
        else:
            send("已经有一次扫码在进行中。发送 /115qr cancel 可以取消。")
        return
    _QR_CANCEL.clear()
    try:
        threading.Thread(target=_do_115qr, daemon=True).start()
    except Exception:
        _QR_LOCK.release()
        raise

HELP = "可用命令：\n/status 监控检测结果\n/server 服务器资源状态\n/services 服务和容器运行状态\n/traffic 今日代理流量（按类型+域名）\n    /traffic yesterday 昨天\n    /traffic 7 近 7 天\n/backup 打包加密配置备份并发给你\n/dl <链接> 投递磁力或 ed2k 到 115 云下载（一条消息可多条，最多 20 条）\n/auto 自动入库开关（/auto on 或 /auto off）\n/115qr 扫码刷新 115 登录（/115qr cancel 取消）\n/help 显示本帮助"
COMMANDS = {"/status": cmd_status, "/server": cmd_server, "/services": cmd_services, "/traffic": cmd_traffic}
MENU = [{"command": "status", "description": "监控检测结果"},
        {"command": "server", "description": "服务器资源状态"},
        {"command": "services", "description": "服务和容器运行状态"},
        {"command": "traffic", "description": "代理流量统计（可加 yesterday 或 7）"},
        {"command": "backup", "description": "打包加密配置备份并发送"},
        {"command": "dl", "description": "投递磁力或 ed2k（一条可多条，最多 20 条）"},
        {"command": "auto", "description": "自动入库开关（on / off）"},
        {"command": "115qr", "description": "扫码刷新 115 登录"},
        {"command": "help", "description": "显示帮助"}]

def main():
    log("bot started")
    try:
        call("setMyCommands", timeout=15, commands=json.dumps(MENU, ensure_ascii=False))
    except Exception as e:
        log("setMyCommands failed:", e)
    offset, backoff = None, 5
    while True:
        try:
            p = {"timeout": 30}
            if offset: p["offset"] = offset
            updates = call("getUpdates", **p).get("result", [])
            backoff = 5
        except Exception as e:
            log(f"getUpdates failed: {e}; retry in {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 300); continue
        for u in updates:
            offset = u["update_id"] + 1
            if u.get("callback_query"):
                try:
                    handle_callback(u["callback_query"])
                except Exception as e:
                    log("callback failed:", e)
                continue
            m = u.get("message") or {}
            if m.get("chat", {}).get("id") != CHAT_ID:
                continue
            text = (m.get("text") or "").strip()
            parts = text.split()
            txt = parts[0].split("@")[0] if parts else ""
            arg = " ".join(parts[1:])
            if txt in COMMANDS:
                try:
                    send(COMMANDS[txt](arg) if txt == "/traffic" else COMMANDS[txt]())
                except Exception as e:
                    log(f"{txt} failed: {e}"); send(f"⚠️ {txt} 执行失败：{e}")
            elif txt == "/backup":
                start_backup()
            elif txt in ("/start", "/help"):
                send(HELP)
            elif txt == "/auto":
                try:
                    send(cmd_auto(arg))
                except Exception as e:
                    log("/auto failed:", e); send(f"⚠️ /auto 执行失败：{e}")
            elif txt == "/dl":
                try:
                    cmd_dl(arg)
                except Exception as e:
                    log("/dl failed:", e); send(f"⚠️ /dl 执行失败：{e}")
            elif txt == "/115qr":
                try:
                    cmd_115qr(arg)
                except Exception as e:
                    log("/115qr failed:", _safe_err(e)); send(f"⚠️ /115qr 执行失败：{_safe_err(e)}")
            elif extract_links(text):
                try:
                    submit_links(extract_links(text))
                except Exception as e:
                    log("dl auto failed:", e); send(f"⚠️ 投递失败：{e}")
            elif text in ("电影", "电视剧"):
                try:
                    handle_kind_text(text)
                except Exception as e:
                    log("kind reply failed:", e); send(f"⚠️ 设置类型失败：{e}")

if __name__ == "__main__":
    main()
