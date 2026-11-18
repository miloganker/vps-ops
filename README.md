# vps-ops

大阪 Evoxt 与杭州阿里云上自用服务的代码和部署模板，方便两边同步功能更新。

## 目录

- `mediahub/`：影视整理服务源码（网页、自动入库、Docker）
- `gatus/`：Gatus 监控配置模板、Telegram 机器人脚本、systemd 单元
- `caddy/`：大阪和杭州的 Caddy 配置模板
- `openlist/`：OpenList / 115 的部署说明
- `ssh/`：SSH 禁止密码登录的配置模板
- `docs/`：对接说明和迁移恢复脚本

## 不要提交的内容

`.env`、OpenList 的 `data.db`、SSH 密钥、证书私钥、备份压缩包、sing-box 配置。仓库是公开的，只放代码和说明。

同步到服务器时，保留各机已有的 `.env` 和数据目录，只更新代码和模板。杭州 mediahub 构建如需国内镜像，可在本机 Dockerfile 里临时改 `FROM` 和 pip 源，不必把镜像地址写回仓库的默认文件。
