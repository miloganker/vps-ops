# 杭州：Gatus 与 Telegram 机器人

同一份 `gatus/status_bot.py` 部署在杭州阿里云 ECS 上，作为 @hangcity_bot。不设 `VPS_SITE`，或设为 `osaka` 时，脚本仍是大阪的行为（含 `/traffic` 和 sing-box 备份）。杭州必须在本机 `.env` 里写 `VPS_SITE=hangzhou`。

不要把 `.env`、证书、OpenList 的 `data.db` 或任何令牌提交到 Git。

## 放到哪

机器人读的是 `/opt/gatus/status_bot.py`，compose 挂的是 `/opt/gatus/config.yaml` 和 `/opt/gatus/.env`。仓库里的 `gatus/` 目录对应服务器上的 `/opt/gatus`，不是把整个仓库根目录直接当成 `/opt/gatus`。

```bash
git clone https://github.com/miloganker/vps-ops.git /opt/src/vps-ops
install -d -m 750 /opt/gatus
# config.yaml 不要直接用仓库里的那份：那是大阪模板。
rsync -a --exclude '.env' --exclude 'data/' --exclude 'config.yaml' \
  /opt/src/vps-ops/gatus/ /opt/gatus/
```

已经在跑的机器更新代码时用同一条 rsync。`.env`、`data/` 和改过探测路径的 `config.yaml` 都留在服务器上。

## `.env`

只在第一次从示例复制，之后保留服务器上的文件：

```bash
cp /opt/gatus/.env.example /opt/gatus/.env   # 仅第一次
chmod 600 /opt/gatus/.env
```

必填（值只写在这台机器的 `.env` 里）：

| 变量 | 作用 |
| --- | --- |
| `TELEGRAM_TOKEN` | @hangcity_bot 的令牌 |
| `TELEGRAM_CHAT_ID` | 只回应这个聊天 |
| `WEBDAV_BASIC` | `user:password` 的 base64。配置里已经写成 `Basic ${WEBDAV_BASIC}`，这里不要再加 `Basic ` 前缀 |
| `BACKUP_PASSWORD` | `/backup` 的 7z 密码 |
| `VPS_SITE` | `hangzhou` |

`CLASH_SECRET` 是大阪 sing-box 用的，杭州留空。不要加引号。`gatus-status-bot.service` 不写这些变量，脚本自己读 `/opt/gatus/.env`。

## Gatus 配置

```bash
cp /opt/gatus/config.hangzhou.yaml /opt/gatus/config.yaml
```

`docker-compose.yml` 不用改，它只挂 `./config.yaml`。杭州模板检查的是本机：

- `http://127.0.0.1:5244/ping`（OpenList，200 且正文 `pong`）
- `PROPFIND http://127.0.0.1:5244/dav/`（207）
- `PROPFIND http://127.0.0.1:5244/dav/guangdong/`（207）
- `GET http://127.0.0.1:5244/dav/guangdong/EXAMPLE_DIR/example-episode.mp4`（`Range: bytes=0-0`，忽略跳转，20 秒，期望 302）
- `http://127.0.0.1:8095/healthz`（mediahub，200）

没有 DuckDNS，也没有 sing-box。告警只用 `${TELEGRAM_TOKEN}` 和 `${TELEGRAM_CHAT_ID}`。

OpenList 和 mediahub 已经听在 `127.0.0.1`。Gatus 直接打这些地址，所以杭州 Caddy 上的自签公网证书跟监控无关。

把上面的 `EXAMPLE_DIR/example-episode.mp4` 换成库里一个稳定存在的视频，再启动。留着占位路径时，这项会一直失败并往 Telegram 发告警。

## 启动

Gatus：

```bash
cd /opt/gatus
# Docker Hub 慢时先经 docker.1ms.run 拉，再打回 compose 里的原标签
docker pull docker.1ms.run/twinproduction/gatus:latest
docker tag docker.1ms.run/twinproduction/gatus:latest twinproduction/gatus:latest
docker compose up -d
```

机器人用原来的单元，不要装、也不要启动 `traffic-collector`：

```bash
cp /opt/gatus/gatus-status-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gatus-status-bot.service
systemctl disable --now traffic-collector.service 2>/dev/null || true
```

杭州没有 sing-box，流量采集服务连不上，启动了只会报错。

## 命令

保留：`/status` `/server` `/services` `/backup` `/dl` `/auto` `/115qr` `/help`。

`/115qr`、`/dl`、`/auto` 仍走本机 `http://127.0.0.1:5244` 和 `http://127.0.0.1:8095`。

`/services` 只看 caddy、docker、containerd、ssh、gatus-status-bot，以及容器 gatus、openlist、mediahub。

`/traffic` 会回复：这台杭州主机没有代理流量采集，该命令只在大阪机器人上可用。它不读 `TRAFFIC_DB`。帮助文本里会标明不可用，Telegram 命令菜单里不列出。

`/backup` 不打包 `/etc/sing-box/*`、sing-box 的 systemd 单元和 `traffic-collector.service`。仍然打包：`/etc/caddy/*`（含证书，证书只在服务器和加密包里）、`/opt/gatus` 的 config、compose、`.env`、`*.py`、`gatus-status-bot.service`、OpenList 的 `data.db`（sqlite 在线备份）、`config.json`、`docker inspect`、整个 `/opt/mediahub`（跳过缓存）、ssh drop-in、`authorized_keys`、journald drop-in，以及 `/opt/gatus/handoff/` 里已有的说明。包内 README 按杭州来写恢复步骤（OpenList、Caddy、mediahub、Gatus、这个机器人）。

## 以后从 Git 更新

```bash
git -C /opt/src/vps-ops pull
rsync -a --exclude '.env' --exclude 'data/' --exclude 'config.yaml' \
  /opt/src/vps-ops/gatus/ /opt/gatus/
systemctl restart gatus-status-bot.service
```

`config.yaml` 被排除，是为了留下你改过的 302 路径。模板有新检查时，自己把 `config.hangzhou.yaml` 里的差异合并进 `config.yaml`，不要整文件覆盖。Gatus 配置改完后 `docker compose up -d` 会重建容器。`.env` 和 `data/` 一直留在服务器上。
