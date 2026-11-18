# OpenList 部署说明

数据目录 `/opt/openlist/data`（含 `data.db`）只放在服务器上，不要提交到 Git。镜像建议钉在 `v4.2.9`，不要随便升级。目录属主需要是 `1001:1001`，并设置 `PUID/PGID=1001`。只监听 `127.0.0.1:5244`，由 Caddy 反代。

## 大阪（现网）

现网容器镜像是 `openlistteam/openlist:latest`，实际版本为 v4.2.9，数据卷 `/opt/openlist/data`，端口 `127.0.0.1:5244`。没有 compose 文件，是用 `docker run` 启动的。

## 杭州

国内拉取可用 `docker.1ms.run/openlistteam/openlist:v4.2.9`，再打回官方标签：

```bash
install -d -m 750 -o 1001 -g 1001 /opt/openlist/data
docker pull docker.1ms.run/openlistteam/openlist:v4.2.9
docker tag docker.1ms.run/openlistteam/openlist:v4.2.9 openlistteam/openlist:v4.2.9
docker run -d --name openlist --restart unless-stopped \
  -e TZ=Asia/Shanghai -e PUID=1001 -e PGID=1001 \
  -p 127.0.0.1:5244:5244 \
  -v /opt/openlist/data:/opt/openlist/data \
  openlistteam/openlist:v4.2.9
```

115 存储的登录信息在 `data.db` 里，不进 Git。刷新登录用机器人的 `/115qr`，不要把 cookie 或 `qrcode_token` 写进仓库。
