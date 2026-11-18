#!/usr/bin/env python3
"""sing-box 按域名流量采集器（只用标准库）。

每 POLL 秒读取 clash_api 的 GET /connections，按连接 id 记住上次看到的累计
upload/download，把增量按 (北京日期, 可注册域名) 累加到 sqlite。

已知局限（已接受）：
- 连接在两次轮询之间关闭时，最后一段增量拿不到；在两次轮询之间开始并结束的短连接
  完全看不到。为保证总量准确，用全局 uploadTotal/downloadTotal 的增量减去已归属的
  增量，差值记到特殊域名 "(未归属)"。
- sing-box 重启后连接 id 与全局计数都会归零：检测到全局计数变小即重置基线，
  重启前最后一个轮询周期的流量会丢失。
- 只统计经过本代理的流量，从启用统计之后开始。
"""
import json, os, sqlite3, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

POLL = 15
KEEP_DAYS = 60
DB = "/opt/gatus/data_traffic/traffic.db"
API = "http://127.0.0.1:9090/connections"
TZ = timezone(timedelta(hours=8))
UNATTRIBUTED = "(未归属)"
TWO_PART = {"co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go", "gr", "ad", "ed", "lg", "mil", "nic", "ltd", "plc", "sch", "info", "biz", "idv", "nom"}

def log(*a):
    print(*a, file=sys.stderr, flush=True)

def load_secret(p="/opt/gatus/.env"):
    for line in open(p):
        if line.startswith("CLASH_SECRET="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("CLASH_SECRET missing in .env")

def reg_domain(host):
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return "(未知)"
    if h.startswith("[") or ":" in h or h.replace(".", "").isdigit():
        return h.strip("[]")  # IP 地址原样保留
    p = h.split(".")
    if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in TWO_PART:
        return ".".join(p[-3:])
    return ".".join(p[-2:])

def open_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS traffic (day TEXT, domain TEXT, up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, PRIMARY KEY(day, domain))")
    db.commit()
    return db

def prune(db):
    cut = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    n = db.execute("DELETE FROM traffic WHERE day < ?", (cut,)).rowcount
    db.commit()
    if n:
        log(f"pruned {n} rows older than {cut}")

def fetch(secret):
    req = urllib.request.Request(API, headers={"Authorization": f"Bearer {secret}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def main():
    secret = load_secret()
    db = open_db(); prune(db)
    seen = {}            # id -> (up, down)
    gprev = None         # (uploadTotal, downloadTotal)
    backoff, last_prune, fails = POLL, time.time(), 0
    log("collector started")
    while True:
        try:
            d = fetch(secret)
        except Exception as e:
            fails += 1
            if fails == 1 or fails % 20 == 0:
                log(f"fetch failed ({fails}x): {e}; retry in {backoff}s")
            time.sleep(backoff); backoff = min(backoff * 2, 300)
            seen, gprev = {}, None  # 可能是 sing-box 重启，重置基线
            continue
        if fails:
            log(f"fetch recovered after {fails} failures"); fails = 0
        backoff = POLL
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        gu, gd = d.get("uploadTotal", 0), d.get("downloadTotal", 0)
        acc, cur, au, ad = {}, {}, 0, 0
        for c in d.get("connections") or []:
            cid = c.get("id"); u, dn = c.get("upload", 0), c.get("download", 0)
            pu, pd = seen.get(cid, (0, 0))
            du, dd = max(u - pu, 0), max(dn - pd, 0)
            cur[cid] = (u, dn)
            if gprev is None and cid not in seen:
                continue  # 首次轮询（启动/重启后）：只建立基线，不计历史
            if du or dd:
                m = c.get("metadata") or {}
                dom = reg_domain(m.get("host") or m.get("destinationIP"))
                x = acc.setdefault(dom, [0, 0]); x[0] += du; x[1] += dd
                au += du; ad += dd
        if gprev is not None and gu >= gprev[0] and gd >= gprev[1]:
            ru, rd = (gu - gprev[0]) - au, (gd - gprev[1]) - ad
            if ru > 0 or rd > 0:
                x = acc.setdefault(UNATTRIBUTED, [0, 0]); x[0] += max(ru, 0); x[1] += max(rd, 0)
        elif gprev is not None:
            log("global counters went down (sing-box restart?), baseline reset")
            acc = {}
        seen, gprev = cur, (gu, gd)
        if acc:
            try:
                db.executemany(
                    "INSERT INTO traffic(day,domain,up,down) VALUES(?,?,?,?) "
                    "ON CONFLICT(day,domain) DO UPDATE SET up=up+excluded.up, down=down+excluded.down",
                    [(day, k, v[0], v[1]) for k, v in acc.items()])
                db.commit()
            except Exception as e:
                log(f"db write failed: {e}")
        if time.time() - last_prune > 86400:
            prune(db); last_prune = time.time()
        time.sleep(POLL)

if __name__ == "__main__":
    main()
