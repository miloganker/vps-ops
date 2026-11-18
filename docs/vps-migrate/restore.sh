#!/usr/bin/env bash
# VPS 一键恢复 / 迁移脚本
# 用途：在一台全新的 Debian 13 服务器上，用 Telegram 机器人 /backup 生成的加密 7z 备份恢复整套服务
#       （sing-box、Caddy、Gatus、OpenList、mediahub、状态机器人、流量采集器）。
# 用法：  sudo bash restore.sh [选项] vps-backup-YYYYmmdd-HHMM.7z
# 选项：
#   --dry-run          只打印将要执行的操作，不改动系统（仍会解压到临时目录做内容校验，结束后删除）
#   --force            目标机已经在跑这些服务时也继续（用于中断后重跑）
#   --password-stdin   从标准输入第一行读取备份密码（默认在终端里交互输入，不回显）
#   --extracted DIR    使用已经解压好的备份目录（测试用，跳过 7z）
#   --timezone TZ      时区，默认 Asia/Singapore（与原服务器一致，UTC+8）
#   --swap-size SIZE   没有 swap 时创建的 /swapfile 大小，默认 512M；设为 0 不创建
#   --skip-ssh         不恢复 sshd 配置（只合并 authorized_keys）
#   --defer-bot        只 enable 状态机器人、先不启动（旧服务器的机器人还在跑时用，避免 Telegram 409 冲突）
#   -h, --help         显示帮助
# 密码永远不会出现在命令行参数、环境变量或 shell 历史里。
set -euo pipefail
umask 077

# ------------------------------------------------------------ 固定版本（与 2026-11-16 原服务器一致）
SINGBOX_VERSION="1.14.4"
SINGBOX_SHA256_AMD64="4e9b8809aed0e3ac78ad14fa3a0af533402425d736f0cc9dc75b1c03d381b614"  # 已核对：与原服务器二进制一致
declare -A APT_PIN=(
  [docker.io]="26.1.5+dfsg1-9+deb13u1"
  [docker-compose]="2.26.1-4"
  [docker-buildx]="0.13.1+ds1-3"
  [caddy]="2.6.2-12+deb13u1"
  [python3]="3.13.5-1"
  [7zip]="25.01+dfsg-1~deb13u2"
)
APT_EXTRA=(ca-certificates curl openssh-server)
OPENLIST_UID=1001
MEDIAHUB_UID=10001

# ------------------------------------------------------------ 参数
DRY=0; FORCE=0; PW_STDIN=0; EXTRACTED=""; TZ_NAME="Asia/Singapore"; SWAP_SIZE="512M"; SKIP_SSH=0; DEFER_BOT=0; ARCHIVE=""
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --password-stdin) PW_STDIN=1 ;;
    --extracted) EXTRACTED="${2:?--extracted 需要目录}"; shift ;;
    --timezone) TZ_NAME="${2:?}"; shift ;;
    --swap-size) SWAP_SIZE="${2:?}"; shift ;;
    --skip-ssh) SKIP_SSH=1 ;;
    --defer-bot) DEFER_BOT=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "未知选项：$1" >&2; usage; exit 2 ;;
    *) ARCHIVE="$1" ;;
  esac
  shift
done

TS="$(date +%Y%m%d-%H%M%S)"
PRE_DIR="/root/pre-restore-$TS"          # 覆盖前的原文件备份
WARN=(); DONE=()
c_red=$'\e[31m'; c_yel=$'\e[33m'; c_grn=$'\e[32m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_red=""; c_yel=""; c_grn=""; c_dim=""; c_off=""; }
step() { echo; echo "${c_grn}==> $*${c_off}"; }
info() { echo "    $*"; }
warn() { echo "${c_yel}[警告] $*${c_off}" >&2; WARN+=("$*"); }
die()  { echo "${c_red}[错误] $*${c_off}" >&2; exit 1; }
# run：执行命令；--dry-run 时只打印
run() {
  if (( DRY )); then echo "${c_dim}    + $*${c_off}"; else "$@"; fi
}
# 写文件（内容来自 stdin）
write_file() { # path mode owner
  local p="$1" m="$2" o="$3" tmp
  if (( DRY )); then cat >/dev/null; echo "${c_dim}    + 写入 $p ($m $o)${c_off}"; return; fi
  tmp="$(mktemp)"; cat >"$tmp"
  if [[ -f "$p" ]] && cmp -s "$tmp" "$p"; then rm -f "$tmp"; chmod "$m" "$p"; chown "$o" "$p"; return; fi
  backup_existing "$p"
  install -D -m "$m" -o "${o%%:*}" -g "${o##*:}" "$tmp" "$p"; rm -f "$tmp"
}
backup_existing() {
  local p="$1"
  if [[ -e "$p" ]]; then
    mkdir -p "$PRE_DIR$(dirname "$p")"; cp -a "$p" "$PRE_DIR$p"
  fi
}
# 安装一个备份里的文件到目标路径（内容相同则只修正权限）
put() { # src dst mode owner
  local s="$1" d="$2" m="$3" o="$4"
  [[ -f "$s" ]] || { warn "备份里没有 ${s#"$SRC"/}，跳过"; return 0; }
  if (( DRY )); then
    if [[ -f "$d" ]] && cmp -s "$s" "$d"; then echo "${c_dim}    = $d 内容相同，只校正权限${c_off}"
    else echo "${c_dim}    + install $m $o ${s#"$SRC"/} -> $d${c_off}"; fi
    return 0
  fi
  if [[ -f "$d" ]] && cmp -s "$s" "$d"; then chmod "$m" "$d"; chown "$o" "$d"; return; fi
  backup_existing "$d"
  install -D -m "$m" -o "${o%%:*}" -g "${o##*:}" "$s" "$d"
}

# ------------------------------------------------------------ 0. 前置检查
step "0. 前置检查"
if (( ! DRY )) && [[ $EUID -ne 0 ]]; then die "请用 root 运行"; fi
[[ -n "$ARCHIVE" || -n "$EXTRACTED" ]] || { usage; die "需要备份文件路径"; }
[[ -z "$ARCHIVE" || -f "$ARCHIVE" ]] || die "找不到备份文件：$ARCHIVE"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == debian && "${VERSION_ID:-}" == 13 ]] || warn "目标系统是 ${PRETTY_NAME:-未知}，脚本按 Debian 13 编写"
fi
ARCH="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
[[ "$ARCH" == amd64 ]] || warn "架构是 $ARCH；sing-box 校验值只对 amd64 有效"
(( DRY )) && info "dry-run 模式：只打印操作，不改动系统"

# 已经在跑的服务 → 拒绝（除非 --force）
running=()
for s in sing-box caddy gatus-status-bot traffic-collector; do
  systemctl is-active --quiet "$s" 2>/dev/null && running+=("$s")
done
if command -v docker >/dev/null 2>&1; then
  for c in gatus openlist mediahub; do
    docker ps -q --filter "name=^${c}$" 2>/dev/null | grep -q . && running+=("容器 $c")
  done
fi
if (( ${#running[@]} )); then
  if (( FORCE )); then warn "这台机器已经在运行：${running[*]}（--force，继续）"
  else die "这台机器已经在运行：${running[*]}。如果确实要覆盖（例如中断后重跑），加 --force"; fi
fi

# ------------------------------------------------------------ 1. 解压备份
step "1. 解压并校验备份"
WORK=""
# shellcheck disable=SC2329  # 通过 trap 调用
cleanup() { if [[ -n "$WORK" && -d "$WORK" ]]; then rm -rf "$WORK"; fi; return 0; }
trap cleanup EXIT
if [[ -n "$EXTRACTED" ]]; then
  SRC="$(cd "$EXTRACTED" && pwd)"; info "使用已解压目录：$SRC"
else
  if ! command -v 7z >/dev/null 2>&1; then
    if (( DRY )); then die "dry-run 需要先有 7z 才能校验备份：apt-get install -y 7zip"; fi
    info "安装 7zip"; DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq 7zip >/dev/null
  fi
  if (( PW_STDIN )); then IFS= read -r BPW || true
  else
    [[ -t 0 ]] || die "标准输入不是终端；请加 --password-stdin 从管道传入密码"
    IFS= read -rs -p "备份密码（不回显）：" BPW; echo
  fi
  [[ -n "${BPW:-}" ]] || die "密码为空"
  WORK="$(mktemp -d /root/restore-XXXXXX)"; chmod 700 "$WORK"
  # 密码只通过管道给 7z（printf 是 shell 内建命令，不会出现在进程列表里）
  if ! printf '%s\n' "$BPW" | 7z x -y -bd -o"$WORK" "$ARCHIVE" >"$WORK/.7z.log" 2>&1; then
    tail -3 "$WORK/.7z.log" | grep -v -i password >&2 || true
    die "解压失败（密码错误或文件损坏）"
  fi
  unset BPW; rm -f "$WORK/.7z.log"
  SRC="$WORK"
fi

# 必需文件（与 status_bot.py 的 build_backup 清单对应）
REQUIRED=(
  etc/sing-box/config.json
  etc/caddy/Caddyfile
  etc/systemd/system/gatus-status-bot.service
  etc/systemd/system/traffic-collector.service
  opt/gatus/.env opt/gatus/config.yaml opt/gatus/docker-compose.yml
  opt/gatus/status_bot.py opt/gatus/traffic_collector.py
  opt/openlist/data/config.json opt/openlist/data/data.db
  openlist-inspect.json
  root/.ssh/authorized_keys
  opt/mediahub/docker-compose.yml opt/mediahub/Dockerfile opt/mediahub/.env
  opt/mediahub/requirements.txt opt/mediahub/app/main.py
)
RECOMMENDED=(
  "etc/ssh/sshd_config.d/*.conf|SSH 只允许密钥登录的配置"
  "etc/systemd/journald.conf.d/*.conf|journald 日志上限"
  "etc/sysctl.d/*.conf|BBR 等内核参数（备份里没有时按原服务器写入 BBR）"
  "opt/gatus/data/gatus.db|Gatus 监控历史（没有就从头开始）"
  "opt/gatus/data_traffic/traffic.db|流量统计历史（没有就从头开始）"
)
missing=0
for f in "${REQUIRED[@]}"; do
  if [[ ! -s "$SRC/$f" ]]; then echo "${c_red}    缺少：$f${c_off}"; missing=1; fi
done
for r in "${RECOMMENDED[@]}"; do
  pat="${r%%|*}"; desc="${r#*|}"
  # shellcheck disable=SC2086
  if ! compgen -G "$SRC/"$pat >/dev/null; then warn "备份里没有 $pat（$desc）"; fi
done
(( missing )) && die "备份缺少恢复所需的文件，停止"
# .env 里需要的键（只检查键名，不读值）
need_keys() { local f="$1"; shift; for k in "$@"; do grep -q "^$k=." "$f" || warn "${f#"$SRC"/} 里没有 $k"; done; }
need_keys "$SRC/opt/gatus/.env" TELEGRAM_TOKEN BACKUP_PASSWORD CLASH_SECRET WEBDAV_BASIC
need_keys "$SRC/opt/mediahub/.env" APP_PASSWORD OPENLIST_USER OPENLIST_PASSWORD TMDB_API_KEY
NKEYS=$(grep -c -E '^(ssh-|ecdsa-|sk-)' "$SRC/root/.ssh/authorized_keys" || true)
info "备份内容校验通过：authorized_keys 里有 $NKEYS 把公钥"
(( NKEYS > 0 )) || die "备份里的 authorized_keys 没有公钥；恢复只允许密钥登录的 SSH 配置会把你锁在外面"

# ------------------------------------------------------------ 2. 安装软件
step "2. 安装软件（Debian 官方源 + sing-box 官方 release，版本与原服务器一致）"
run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
pkgs=()
for p in "${!APT_PIN[@]}"; do
  v="${APT_PIN[$p]}"
  if apt-cache madison "$p" 2>/dev/null | awk -F'|' '{gsub(/ /,"",$2); print $2}' | grep -qxF "$v"; then
    pkgs+=("$p=$v")
  else
    pkgs+=("$p"); warn "$p 的原版本 $v 已不在软件源里，改装当前版本（Debian 稳定版更新通常只是安全修复）"
  fi
done
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${pkgs[@]}" "${APT_EXTRA[@]}"
cur_sb="$(dpkg-query -W -f='${Version}' sing-box 2>/dev/null || true)"
if [[ "$cur_sb" == "$SINGBOX_VERSION" ]]; then
  info "sing-box $SINGBOX_VERSION 已安装"
else
  deb="/root/sing-box_${SINGBOX_VERSION}_${ARCH}.deb"
  run curl -fsSL --retry 3 -o "$deb" "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box_${SINGBOX_VERSION}_${ARCH}.deb"
  if (( ! DRY )); then
    if [[ "$ARCH" == amd64 ]]; then
      echo "$SINGBOX_SHA256_AMD64  $deb" | sha256sum -c --quiet - || die "sing-box 安装包校验失败"
    else warn "非 amd64，未校验 sing-box 安装包"; fi
  fi
  run dpkg -i "$deb"
  run rm -f "$deb"
fi

# ------------------------------------------------------------ 3. 系统设置
step "3. 系统设置（时区、BBR、swap、journald）"
run timedatectl set-timezone "$TZ_NAME"
if compgen -G "$SRC/etc/sysctl.d/*.conf" >/dev/null; then
  for f in "$SRC"/etc/sysctl.d/*.conf; do put "$f" "/etc/sysctl.d/$(basename "$f")" 644 root:root; done
else
  printf 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' | write_file /etc/sysctl.d/99-bbr.conf 644 root:root
fi
run sysctl --system -q
if [[ "$SWAP_SIZE" != 0 ]] && ! swapon --show --noheadings 2>/dev/null | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    run fallocate -l "$SWAP_SIZE" /swapfile
    run chmod 600 /swapfile
    run mkswap -q /swapfile
  fi
  run swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab 2>/dev/null || { if (( DRY )); then info "+ 在 /etc/fstab 加入 /swapfile"; else echo '/swapfile none swap sw 0 0' >>/etc/fstab; fi; }
else
  info "已有 swap 或 --swap-size 0，跳过"
fi
for f in "$SRC"/etc/systemd/journald.conf.d/*.conf; do
  if [[ -f "$f" ]]; then put "$f" "/etc/systemd/journald.conf.d/$(basename "$f")" 644 root:root; fi
done
run systemctl restart systemd-journald

# ------------------------------------------------------------ 4. SSH（先合并公钥，再校验配置，最后 reload，不断开当前会话）
step "4. SSH"
if (( DRY )); then info "+ 合并 authorized_keys（保留目标机已有公钥，加入备份里的 $NKEYS 把）"
else
  install -d -m 700 -o root -g root /root/.ssh
  touch /root/.ssh/authorized_keys; backup_existing /root/.ssh/authorized_keys
  tmpk="$(mktemp)"
  cat /root/.ssh/authorized_keys "$SRC/root/.ssh/authorized_keys" | awk 'NF && !seen[$0]++' >"$tmpk"
  install -m 600 -o root -g root "$tmpk" /root/.ssh/authorized_keys; rm -f "$tmpk"
fi
if (( SKIP_SSH )); then info "--skip-ssh：不改 sshd 配置"
elif compgen -G "$SRC/etc/ssh/sshd_config.d/*.conf" >/dev/null; then
  for f in "$SRC"/etc/ssh/sshd_config.d/*.conf; do put "$f" "/etc/ssh/sshd_config.d/$(basename "$f")" 644 root:root; done
  if (( ! DRY )); then
    if sshd -t; then
      systemctl reload ssh || systemctl reload sshd
      info "sshd 配置已校验并 reload（当前会话不受影响）"
    else
      for f in "$SRC"/etc/ssh/sshd_config.d/*.conf; do
        b="/etc/ssh/sshd_config.d/$(basename "$f")"
        if [[ -e "$PRE_DIR$b" ]]; then cp -a "$PRE_DIR$b" "$b"; else rm -f "$b"; fi
      done
      warn "sshd -t 校验失败，已撤回 sshd 配置，SSH 保持原样"
    fi
  else info "+ sshd -t && systemctl reload ssh"; fi
  warn "恢复后 SSH 只允许密钥登录（root 禁止密码）。断开当前会话前，请新开一个窗口确认能用密钥登录"
fi

# ------------------------------------------------------------ 5. 恢复配置和数据
step "5. 恢复配置文件和数据"
run install -d -m 755 -o root -g root /etc/sing-box /etc/caddy
for f in "$SRC"/etc/sing-box/*; do if [[ -f "$f" ]]; then put "$f" "/etc/sing-box/$(basename "$f")" 600 root:root; fi; done
for f in "$SRC"/etc/caddy/*; do if [[ -f "$f" ]]; then put "$f" "/etc/caddy/$(basename "$f")" 644 root:root; fi; done
for f in "$SRC"/etc/systemd/system/*.service; do if [[ -f "$f" ]]; then put "$f" "/etc/systemd/system/$(basename "$f")" 644 root:root; fi; done
if [[ -d "$SRC/etc/systemd/system/sing-box.service.d" ]]; then
  for f in "$SRC"/etc/systemd/system/sing-box.service.d/*; do put "$f" "/etc/systemd/system/sing-box.service.d/$(basename "$f")" 644 root:root; done
fi
# /opt/gatus
run install -d -m 700 -o root -g root /opt/gatus /opt/gatus/data
run install -d -m 755 -o root -g root /opt/gatus/data_traffic
put "$SRC/opt/gatus/.env" /opt/gatus/.env 600 root:root
put "$SRC/opt/gatus/config.yaml" /opt/gatus/config.yaml 644 root:root
put "$SRC/opt/gatus/docker-compose.yml" /opt/gatus/docker-compose.yml 644 root:root
for f in "$SRC"/opt/gatus/*.py; do put "$f" "/opt/gatus/$(basename "$f")" 700 root:root; done
if [[ -f "$SRC/opt/gatus/data/gatus.db" ]]; then put "$SRC/opt/gatus/data/gatus.db" /opt/gatus/data/gatus.db 644 root:root; fi
if [[ -f "$SRC/opt/gatus/data_traffic/traffic.db" ]]; then put "$SRC/opt/gatus/data_traffic/traffic.db" /opt/gatus/data_traffic/traffic.db 644 root:root; fi
# /opt/openlist
run install -d -m 755 -o root -g root /opt/openlist
run install -d -m 750 -o "$OPENLIST_UID" -g "$OPENLIST_UID" /opt/openlist/data
for f in "$SRC"/opt/openlist/*.yml "$SRC"/opt/openlist/*.yaml; do if [[ -f "$f" ]]; then put "$f" "/opt/openlist/$(basename "$f")" 644 root:root; fi; done
put "$SRC/opt/openlist/data/config.json" /opt/openlist/data/config.json 644 "$OPENLIST_UID:$OPENLIST_UID"
put "$SRC/opt/openlist/data/data.db" /opt/openlist/data/data.db 644 "$OPENLIST_UID:$OPENLIST_UID"
# /opt/mediahub（整个目录；.env 600；data 归 10001）
run install -d -m 700 -o root -g root /opt/mediahub
if (( DRY )); then info "+ 复制 opt/mediahub/ -> /opt/mediahub/（$(find "$SRC/opt/mediahub" -type f | wc -l) 个文件）"
else
  while IFS= read -r -d '' f; do
    rel="${f#"$SRC"/opt/mediahub/}"
    case "$rel" in
      data/*) put "$f" "/opt/mediahub/$rel" 600 "$MEDIAHUB_UID:$MEDIAHUB_UID" ;;
      *)      put "$f" "/opt/mediahub/$rel" 644 root:root ;;
    esac
  done < <(find "$SRC/opt/mediahub" -type f -print0)
  chmod 600 /opt/mediahub/.env
fi
run install -d -m 700 -o "$MEDIAHUB_UID" -g "$MEDIAHUB_UID" /opt/mediahub/data
if (( ! DRY )) && [[ -d /opt/mediahub/data ]]; then chown -R "$MEDIAHUB_UID:$MEDIAHUB_UID" /opt/mediahub/data; fi

# ------------------------------------------------------------ 6. 启动服务
step "6. 校验配置并启动服务"
run systemctl daemon-reload
run systemctl enable --now docker
if (( ! DRY )); then sing-box check -C /etc/sing-box >/dev/null || die "sing-box 配置校验失败"; else info "+ sing-box check -C /etc/sing-box"; fi
run systemctl enable sing-box
run systemctl restart sing-box
if (( ! DRY )); then caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || die "Caddyfile 校验失败"; else info "+ caddy validate"; fi
run systemctl enable caddy
run systemctl restart caddy
# Gatus
run docker compose -f /opt/gatus/docker-compose.yml up -d
# OpenList：按备份里的 docker inspect 重建（原来是 docker run 启动的，没有 compose 文件）
OL_ARGS=()
mapfile -t OL_ARGS < <(python3 - "$SRC/openlist-inspect.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))[0]
cfg, hc = c["Config"], c["HostConfig"]
out = ["--name", c["Name"].lstrip("/"), "--restart", hc.get("RestartPolicy", {}).get("Name") or "unless-stopped"]
for port, binds in (hc.get("PortBindings") or {}).items():
    for b in binds or []:
        hip = b.get("HostIp") or ""
        out += ["-p", (hip + ":" if hip else "") + f"{b.get('HostPort')}:{port.split('/')[0]}"]
for v in hc.get("Binds") or []:
    out += ["-v", v]
for e in cfg.get("Env") or []:
    if not e.startswith("PATH="):
        out += ["-e", e]
if hc.get("Memory"):
    out += ["--memory", str(hc["Memory"])]
out.append(cfg["Image"])
print("\n".join(out))
PY
)
if docker ps -a -q --filter name=^openlist$ 2>/dev/null | grep -q .; then
  info "openlist 容器已存在：docker start openlist"
  run docker start openlist
else
  run docker run -d "${OL_ARGS[@]}"
fi
# mediahub
run docker compose -f /opt/mediahub/docker-compose.yml --project-directory /opt/mediahub up -d --build
# 主机上的两个 Python 服务
run systemctl enable --now traffic-collector
if (( DEFER_BOT )); then
  run systemctl enable gatus-status-bot
  warn "状态机器人已 enable 但没有启动；停掉旧服务器的机器人后执行：systemctl start gatus-status-bot"
else
  run systemctl enable --now gatus-status-bot
fi

# ------------------------------------------------------------ 7. 健康检查
step "7. 健康检查"
if (( DRY )); then
  info "+ 检查 systemd 服务、容器状态、本机端口（443/8443/5244/8080/8095/9090）和 HTTP 健康接口"
else
  sleep 8
  ok() { echo "    ${c_grn}✔${c_off} $*"; DONE+=("$*"); }
  bad() { echo "    ${c_red}✘${c_off} $*"; WARN+=("$*"); }
  for s in docker sing-box caddy gatus-status-bot traffic-collector ssh; do
    if [[ "$s" == gatus-status-bot ]] && (( DEFER_BOT )); then continue; fi
    if systemctl is-active --quiet "$s"; then ok "服务 $s 运行中"; else bad "服务 $s 未运行（journalctl -u $s -n 50）"; fi
  done
  for c in gatus openlist mediahub; do
    st="$(docker ps --filter "name=^${c}$" --format '{{.Status}}' 2>/dev/null || true)"
    if [[ "$st" == Up* ]]; then ok "容器 $c：$st"; else bad "容器 $c 未运行（docker logs $c）"; fi
  done
  chk_http() { # 名称 URL
    local code; code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$2" || true)"
    if [[ "$code" =~ ^(200|204|301|302|303|401)$ ]]; then ok "$1 HTTP $code"; else bad "$1 无响应（HTTP $code）"; fi
  }
  chk_http "OpenList 127.0.0.1:5244/ping" http://127.0.0.1:5244/ping
  chk_http "Gatus 127.0.0.1:8080/health" http://127.0.0.1:8080/health
  chk_http "mediahub 127.0.0.1:8095/healthz" http://127.0.0.1:8095/healthz
  for p in 443 8443 9090; do
    if ss -ltn "( sport = :$p )" | grep -q LISTEN; then ok "端口 $p 在监听"; else bad "端口 $p 没有在监听"; fi
  done
fi

# ------------------------------------------------------------ 8. 总结
step "8. 完成"
MYIP="$(curl -4 -s -m 8 https://api.ipify.org 2>/dev/null || echo 未知)"
DNSIP="$(getent ahostsv4 sumax.duckdns.org 2>/dev/null | awk 'NR==1{print $1}' || true)"
echo "    本机公网 IP：$MYIP    sumax.duckdns.org 当前解析：${DNSIP:-未知}"
cat <<EOF

    接下来需要你手动做的事：
    1. 先停掉旧服务器上的机器人（systemctl stop gatus-status-bot），否则两台同时拉取 Telegram 消息会冲突（409）。
    2. 到 duckdns.org 把 sumax 的 IP 改成 ${MYIP}（media.sumax.duckdns.org 等子域名会跟着变）。
    3. DNS 生效、80 端口可以访问后，Caddy 会自动为 sumax 和 media.sumax 重新申请证书（几分钟内），不用手动操作。
    4. sing-box 客户端如果填的是 IP 而不是域名，要改成新 IP；Reality 的密钥和 UUID 不变。
    5. OpenList：换了 IP，115 可能要求重新登录或者触发风控。打开 https://sumax.duckdns.org:8443 检查 115 存储状态，需要的话重新扫码或者更新 Cookie。
    6. Gatus 监控历史和流量统计如果不在备份里，会从头开始。
    7. 覆盖前的原文件保存在 $PRE_DIR（如果有）。
EOF
if (( ${#WARN[@]} )); then
  echo; echo "${c_yel}    需要留意（${#WARN[@]} 条）：${c_off}"
  for w in "${WARN[@]}"; do echo "    - $w"; done
fi
(( DRY )) && echo && echo "    （dry-run：以上操作都没有执行）"
exit 0
