# Gatus 监控与 Telegram 机器人

这是大阪 VPS 上 `/opt/gatus` 的模板：Gatus 配置、`status_bot.py`（`/status` `/server` `/services` `/backup` `/dl` `/auto` `/115qr` 等）、流量采集器，以及对应的 systemd 单元。

密钥只放在服务器上的 `.env` 里，不要提交到 Git。

## 和线上文件的差异

公开仓库里做了两处模板化，部署前请对照：

- `status_bot.py` 的 `CHAT_ID` 改为读取 `TELEGRAM_CHAT_ID`。现网脚本里如果仍是写死的数字，更新前先把该值写进 `/opt/gatus/.env`。
- `config.yaml` 的 Telegram `id` 改为 `${TELEGRAM_CHAT_ID}`；「115 Token（视频取链）」的探测文件改成了占位路径，请改成你库里一个稳定存在的视频文件。

## 部署

```bash
# 同步到 /opt/gatus（保留已有 .env 和 data/）
cp .env.example .env
chmod 600 .env
# 填写 TELEGRAM_TOKEN、TELEGRAM_CHAT_ID、WEBDAV_BASIC、CLASH_SECRET、BACKUP_PASSWORD

cp gatus-status-bot.service traffic-collector.service /etc/systemd/system/
systemctl daemon-reload
docker compose up -d
systemctl enable --now gatus-status-bot.service
# traffic-collector 依赖本机 sing-box，只在大阪启用
```
