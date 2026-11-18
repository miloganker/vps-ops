# Gatus 监控与 Telegram 机器人

`gatus/` 是 `/opt/gatus` 的模板，大阪和杭州用同一份 `status_bot.py`。站点由环境变量 `VPS_SITE` 决定：不设置或 `osaka` 时保持大阪现网行为；`hangzhou` 时给 @hangcity_bot 用。密钥只放在服务器上的 `.env` 里，不要提交到 Git。

杭州的部署步骤见 [docs/hangzhou-bot.md](../docs/hangzhou-bot.md)。

## 站点差异

| | 大阪（默认） | 杭州（`VPS_SITE=hangzhou`） |
| --- | --- | --- |
| Gatus 配置 | `config.yaml`（duckdns、证书、sing-box） | 把 `config.hangzhou.yaml` 复制为 `/opt/gatus/config.yaml`，只查 `127.0.0.1` |
| `/services` | 含 sing-box、traffic-collector | 只有 caddy、docker、containerd、ssh、gatus-status-bot |
| `/traffic` | 读本机流量库 | 回复本机没有采集，命令只在大阪机器人可用；菜单里不列出 |
| `/backup` | 含 `/etc/sing-box` 和 traffic-collector 单元 | 跳过这些路径；包内说明按 OpenList + Caddy + mediahub + Gatus + 机器人来写 |
| 不要启动 | — | `traffic-collector`（也没有 sing-box） |

两边的容器检查都是 gatus、openlist、mediahub。`/115qr`、`/dl`、`/auto` 都走本机 `http://127.0.0.1:5244` 和 `http://127.0.0.1:8095`。写成 `hangzhou` 以外的值会记一条日志并按大阪处理。

## 和线上文件的差异

公开仓库里做了模板化，部署前请对照：

- `status_bot.py` 的 `CHAT_ID` 改为读取 `TELEGRAM_CHAT_ID`。现网脚本里如果仍是写死的数字，更新前先把该值写进 `/opt/gatus/.env`。
- `config.yaml` 的 Telegram `id` 改为 `${TELEGRAM_CHAT_ID}`；「115 Token（视频取链）」的探测文件改成了占位路径，请改成你库里一个稳定存在的视频文件。杭州的占位路径在 `config.hangzhou.yaml`，同样要换掉。
- 新增 `VPS_SITE`。大阪不用改 `.env` 也会保持原行为。

## 大阪部署

```bash
# 同步到 /opt/gatus（保留已有 .env 和 data/）
cp .env.example .env
chmod 600 .env
# 填写 TELEGRAM_TOKEN、TELEGRAM_CHAT_ID、WEBDAV_BASIC、CLASH_SECRET、BACKUP_PASSWORD
# VPS_SITE 留空即可

cp gatus-status-bot.service traffic-collector.service /etc/systemd/system/
systemctl daemon-reload
docker compose up -d
systemctl enable --now gatus-status-bot.service
# traffic-collector 依赖本机 sing-box，只在大阪启用
```

## 杭州部署

不要在杭州启动 `traffic-collector`。把 `config.hangzhou.yaml` 复制为 `config.yaml`，`.env` 里写上 `VPS_SITE=hangzhou`，以及 `TELEGRAM_TOKEN`、`TELEGRAM_CHAT_ID`、`WEBDAV_BASIC`、`BACKUP_PASSWORD`。Hub 慢时用 `docker.1ms.run/twinproduction/gatus:latest` 再打回原标签。完整步骤、302 路径怎么换、更新时如何保住 `.env` 和 `data/`，见 [docs/hangzhou-bot.md](../docs/hangzhou-bot.md)。
