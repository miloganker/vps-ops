# VPS 对接说明书（给其他 AI / 运维助手）

- 快照时间：2026-11-17，时区 Asia/Shanghai（北京时间）
- 用途：说明这台私人 VPS 上实际在跑的功能、路径、约束和操作方式，方便另一套 AI 工具接手。本文不是给最终用户看的教程。
- 存放位置：服务器 `/opt/gatus/handoff/VPS对接说明书.md`；每次 `/backup` 会随加密包一起带走，旁边还有 `vps-migrate/restore.sh`。
- 本文不含任何密码、令牌、Cookie、密钥或代理分享链接。需要凭据时，到「密钥位置」一节列出的服务器文件里读取，且不要把值打印到聊天、日志或副本里。

主人使用简体中文沟通；未特别说明时，时间都按北京时间理解。

---

## 1. 必须遵守的硬规则

在改动服务器之前，先给出方案并获得主人同意。只读检查（看状态、日志、配置结构、端口）可以不先问。

1. **不要把密钥打到聊天或文件副本里。** 包括但不限于：`TELEGRAM_TOKEN`、`WEBDAV_BASIC`、`CLASH_SECRET`、`BACKUP_PASSWORD`、mediahub 的 `.env`、OpenList 管理员/站点令牌、115 Cookie / `qrcode_token`、SSH 私钥、sing-box UUID / Reality 密钥、JWT、rclone 密码、115 CDN 带签名的下载 URL。
2. **不要随便改或重启 sing-box。** 它占用 443，并做 TLS 伪装。
3. **不要随便重启 Gatus 容器。** 每次重启会多打一次 115 请求。
4. **不要升级 OpenList。** 现网是 v4.2.9。此版本在 115 存储上通过 WebDAV 重命名/移动有已知的异步备份拷贝问题；官方修复并经主人批准之前不要升级。重命名应走 OpenList 网页或 mediahub 的 API，不要用 WebDAV 批量重试（失败会额外消耗 115 请求次数）。
5. **不要修改 OpenList 用户权限**（包括 `admin` / `appletv` / `grok` / `mediahub` / `evil`）。
6. **不要在 VPS 上跑 BT / 做种 / 本机离线下载。** 磁力与 ed2k 一律交给 115 云下载。
7. **访问 115 不要超过约 4 次请求/秒。** OpenList 115 存储已设 `limit_rate = 4`、`page_size = 1500`。所有脚本、扫描、监控、手动 API 调用都要守这个上限。
8. **115 重命名若超时，不要立刻重试。** 请求往往已经成功，继续做后续步骤，最后再核对结果。
9. **备份只按需做。** 由主人给 Telegram 机器人 `@rokcket_bot` 发送 `/backup`。不要做定时备份，不要在服务器上留备份副本。
10. **不要在这台小机器上安装重量级或高风险软件**（例如需要常驻大模型、或可执行任意命令的机器人）。内存只有约 464 MiB。
11. 主机防火墙、SSH 配置收紧、自动安全更新、批量 `apt` 升级，都还没有做，需先提案再改。

---

## 2. 主机与访问

| 项 | 值 |
| --- | --- |
| 提供商 | Evoxt（KVM） |
| 主机名 | `jason-lrkk-01.evoxt.com` |
| 公网 IPv4 | `166.88.2.219` |
| 公网 IPv6 | `2400:8d60:8::b35a:58e8`（接口前缀 `/48`，看起来是固定地址） |
| 内网 | `eth1`：`10.7.10.128/8`（Evoxt 私网） |
| 系统 | Debian 13 (trixie)，内核 6.12.95+deb13-amd64，x86_64 |
| CPU / 内存 / 磁盘 | 1 vCPU（AMD EPYC-Turin）；内存约 464 MiB；根盘约 5G ext4（`/dev/vda2`，使用率大约六成）；swap 文件 `/swapfile` 512M，通常会用掉一部分 |
| SSH | `root@166.88.2.219`，仅公钥，禁止密码。配置在 `/etc/ssh/sshd_config.d/00-no-password.conf`（`PermitRootLogin prohibit-password`，`PasswordAuthentication no`）。`authorized_keys` 中有主人（Termius）和助手用的密钥。监听 `0.0.0.0:22` 与 `[::]:22` |
| 时区 | 按北京时间处理 |

IPv6 出网是通的，SSH / 443 / 8443 在本机也监听 IPv6。但域名 **没有 AAAA 记录**，日常访问只走 IPv4。Evoxt 的 IPv6 防火墙尚未开启；在面板里配好 IPv6 规则（至少放行 TCP 22/443/8443）之前，不要给域名加 AAAA，也不要把 IPv6 当正式入口宣传。

另有 systemd 单元 `evoxt-sync.service` / `evoxt-sync.timer`，属提供商侧同步，不要改。

---

## 3. 端口、域名与防火墙

### 3.1 对外端口

| 端口 | 程序 | 说明 |
| --- | --- | --- |
| TCP 22 | sshd | SSH |
| TCP 443 | sing-box | 代理入口，TLS 伪装为微软站点 `www.microsoft.com`。不要占用或在前面抢 443 |
| TCP 8443 | Caddy | HTTPS 站点（OpenList、mediahub） |
| UDP 8443 | Caddy | HTTP/3（QUIC）。Evoxt IPv4 防火墙目前只放行了 TCP 22/443/8443，**未放行 UDP 8443**，因此外网 HTTP/3 可能不可用；站点应仍可通过 TCP（HTTP/2 或 HTTP/1.1）访问 |

**80 端口未在 VPS 上监听。** Caddy 把 `https_port` 设为 8443，并 `auto_https disable_redirects`。ACME 关闭了 TLS-ALPN 挑战（因为 443 被 sing-box 占用）。

### 3.2 仅本机环回

| 地址 | 程序 |
| --- | --- |
| `127.0.0.1:5244` | OpenList（Docker 发布，宿主机只绑环回） |
| `127.0.0.1:8095` | mediahub（uvicorn，Docker `network_mode: host`） |
| `127.0.0.1:8080` | Gatus |
| `127.0.0.1:2019` | Caddy admin |
| `127.0.0.1:9090` | sing-box clash API（密钥在 `/opt/gatus/.env` 的 `CLASH_SECRET`） |

OpenList 镜像还声明了 5245，但 **没有发布到宿主机**。

### 3.3 公网域名（均为 A 记录指向 `166.88.2.219`，无 AAAA）

| 域名 | 后端 | 用途 |
| --- | --- | --- |
| `https://sumax.duckdns.org:8443` | `127.0.0.1:5244` | 整个 OpenList（不只是 `/dav`）。`/` 与 `/@manage` 为网页；WebDAV 为 `https://sumax.duckdns.org:8443/dav` |
| `https://media.sumax.duckdns.org:8443` | `127.0.0.1:8095` | mediahub 网页 |

Caddy 配置：`/etc/caddy/Caddyfile`。当前只有上述两个站点。`music.sumax.duckdns.org` 已于 2026-11-17 删除。

### 3.4 防火墙

- **Evoxt 面板 IPv4 防火墙已开启**（与主机 nftables 相互独立，过滤发生在虚拟机之前）。放行：TCP 22 Anywhere、TCP 443 Anywhere、TCP 8443 Anywhere，以及默认的私网 TCP/UDP `10.0.0.0/8`。ICMP 未特意放行。
- **Evoxt IPv6 防火墙未开启。**
- **主机自身几乎没有入站过滤**：INPUT 策略 ACCEPT，未部署 fail2ban / 主机 nftables 限速。SSH 仍会对全世界开放，面板防火墙也不按连接频率限速。
- 不要把 SSH 限制成单个来源 IP（手机和助手的出口 IP 会变）。面板锁定时可用 Evoxt VNC 恢复。

---

## 4. 服务总览

全部应为 enabled + active。

| 组件 | 运行方式 | 路径 / 说明 |
| --- | --- | --- |
| sing-box | systemd `sing-box` | 配置在 `/etc/sing-box/`。软件包自带单元在 `/usr/lib/systemd/system/sing-box.service`。有历史备份 `config.json.bak-20261114-2118` |
| Caddy | systemd `caddy` | `/etc/caddy/Caddyfile` |
| OpenList | Docker 容器 `openlist`，镜像 `openlistteam/openlist:latest`，现为 **v4.2.9** | 数据 `/opt/openlist/data`，以 `docker run` 启动（无 compose），发布 `127.0.0.1:5244:5244` |
| mediahub | Docker 容器 `mediahub:latest`，`network_mode: host`，含健康检查 | 代码与组成在 `/opt/mediahub`，数据 `/opt/mediahub/data`，无 GitHub 副本 |
| Gatus | Docker 容器 `gatus`，镜像 `twinproduction/gatus:latest` | 配置 `/opt/gatus/config.yaml` 与 `docker-compose.yml` |
| @rokcket_bot | systemd `gatus-status-bot` | `/opt/gatus/status_bot.py`，只响应主人的 Telegram 聊天 |
| 流量统计 | systemd `traffic-collector` | `/opt/gatus/traffic_collector.py`，库在 `/opt/gatus/data_traffic/traffic.db`，自 2026-11-14 21:19 起按域名统计经 sing-box 的流量 |
| Docker / containerd / ssh | systemd | 常规 |

日志上限：`/etc/systemd/journald.conf.d/` 中 `SystemMaxUse=100M`，`SystemKeepFree=500M`，`MaxRetentionSec=1month`。

---

## 5. OpenList 与 115

### 5.1 存储

唯一云存储：

- 挂载路径：`/guangdong`
- 驱动：`115 Cloud`，状态应为 `work`
- `page_size = 1500`，`limit_rate = 4`
- `webdav_policy = 302_redirect`（刷新登录后必须保持不变）
- `root_folder_id = 0`
- 登录凭据在 OpenList 库的 addition 里（Cookie 和/或 `qrcode_token`）。刷新时用 TV 端二维码：`qrcode_source=tv`，写入 `qrcode_token` 并清空 cookie。

### 5.2 重要目录

| 路径 | 用途 |
| --- | --- |
| `/guangdong/音乐` | 音乐库。主人用手机 Flacbox 经 OpenList WebDAV 播放，**不要再在 VPS 上挂载音乐盘** |
| `/guangdong/娱乐/影视/待整理/电影` | 电影待整理（mediahub `STAGING_MOVIES`） |
| `/guangdong/娱乐/影视/待整理/电视剧` | 剧集待整理（`STAGING_TV`） |
| `/guangdong/娱乐/影视/待整理/未分类` | 仅哈希磁力等先下到这里再分类（`STAGING_UNSORTED`，设计上不删除） |
| `/guangdong/娱乐/影视/电影` | 电影正式库（`LIBRARY_MOVIES`） |
| `/guangdong/娱乐/影视/电视剧` | 剧集正式库（`LIBRARY_TV`） |

`/guangdong` 上有隐藏规则（广告、网址、`.url/.lnk`、sample 等，含子目录）。因此自动化账号需要能看见隐藏文件。

### 5.3 账号（不要改权限）

| 用户 | 角色/用途 |
| --- | --- |
| `admin` | 管理员。不要改权限，也不要在说明书或聊天里写密码 |
| `appletv` | 主人日常使用，基准路径 `/guangdong/娱乐/影视` |
| `grok` | 助手用的 API / WebDAV 账号（凭据在 `/opt/gatus/.env` 的 `WEBDAV_BASIC`），可见隐藏文件，基准路径 `/` |
| `mediahub` | 仅供 mediahub 服务调用 OpenList（用户名写在 `/opt/mediahub/.env`），基准路径 `/`。mediahub 代码路径里约定不调用删除接口 |
| `evil` | 基准路径 `/guangdong/娱乐/evil` |
| `guest` | 已禁用 |

WebDAV 根：`https://sumax.duckdns.org:8443/dav`。主人手机 Flacbox 建议打开「使用直接 URL」，以便跟随 302 从 115 CDN 拉流，而不是经 VPS 中转。

### 5.4 刷新 115 登录（`/115qr`）

在 Telegram 给 `@rokcket_bot` 发 `/115qr`。机器人向主人发送 TV 端二维码，长轮询确认后，**立即替换**现有 `/guangdong` 这套 115 存储的登录：写入 `qrcode_token`、`qrcode_source=tv`、清空 cookie，并尽量保持 `page_size` / `limit_rate` / `webdav_policy`。然后在本机对 WebDAV 做 PROPFIND 校验。

约束：不在 Telegram 里发送令牌或 Cookie；不自行调用 115 passport API；不改存储的页面大小与速率。取消：`/115qr cancel`。

自动检测 115 过期并推送二维码：**尚未做**，要等有真实鉴权失败样本再设计。

---

## 6. mediahub（影视整理）

自托管的影视整理服务，与助手进程相互独立。主人可以把磁力链发给 `@rokcket_bot`（`/dl`）或贴到网页 `https://media.sumax.duckdns.org:8443`，之后云下载、分类、扫描、自动入库和通知会自己进行。

- 代码：`/opt/mediahub`（Docker Compose + `network_mode: host`）
- 网页密码、OpenList 密码、TMDB、Telegram、内部令牌均在 `/opt/mediahub/.env`（权限 600）
- 对外 URL：`MEDIAHUB_PUBLIC_URL=https://media.sumax.duckdns.org:8443`
- 监听：`127.0.0.1:8095`
- 内部接口令牌：`MEDIAHUB_INTERNAL_TOKEN`（`@rokcket_bot` 的 `/dl`、`/auto` 用它调用 `http://127.0.0.1:8095`）
- 标题来源：`TITLE_SOURCE=tmdb`，入库使用 **TMDB 中文标题**
- 新一季放入已有剧集目录；正式库里已有同名文件夹则跳过并通知
- 自动入库：网页开关，或 Telegram `/auto on` / `/auto off`
- 匹配规则：若英文名或中文名、年份、TMDB ID 都对得上，即使中文标题措辞不同，也视为同一部作品，仍按 TMDB 中文标题重命名入库。只有这几项对不上、年份不一致，或 TMDB 匹配不够有把握时，才停下来请主人确认
- 只有哈希的磁力：先让 115 解析出种子名，再按名称 + TMDB 判断电影/剧集并放入对应「待整理」；判断不了再问主人
- 分类和 TMDB 都有把握时，自动跑完全流程（下载、整理、扫描、改名、移入正式库）；否则停在预览/确认。每一步应发 Telegram 通知
- 对 115 的调用间隔受 `RENAME_INTERVAL` 等参数约束，须保持在限速以内
- 主人有时也用 Filmix（经 WebDAV 连 OpenList）做人工重命名整理

重启 mediahub（需先获同意）：`cd /opt/mediahub && docker compose up -d --build`。数据目录归属 `10001:10001`。

---

## 7. Telegram 机器人 `@rokcket_bot`

由 `gatus-status-bot.service` 运行 `/opt/gatus/status_bot.py`，只响应主人的那一个聊天。令牌在 `/opt/gatus/.env` 的 `TELEGRAM_TOKEN`，不要复制到别处。这是通知与命令通道，不是和某个外部助手「对话」的聊天室。

| 命令 | 作用 |
| --- | --- |
| `/status` | Gatus 最新检测结果 |
| `/server` | 服务器资源 |
| `/services` | 服务与容器状态 |
| `/traffic` | 今日经 sing-box 的代理流量（按类型+域名）。`/traffic yesterday`、`/traffic 7` 为昨天或近 7 天。只统计经过本机代理的流量，不能区分具体 App |
| `/backup` | 打包加密配置并发到该聊天，服务器不留副本 |
| `/dl <链接>` | 投递磁力或 ed2k 到 115 云下载（一条消息可多条，最多 20 条） |
| `/auto`、`/auto on`、`/auto off` | 查看或开关 mediahub 自动入库 |
| `/115qr`、`/115qr cancel` | 扫码刷新 115 登录 |
| `/help` | 帮助 |

Gatus 告警也走同一个机器人。

Gatus 检测项（见 `/opt/gatus/config.yaml`）：

- OpenList 网站：每 1 分钟 `GET https://sumax.duckdns.org:8443/ping`，期望 200 且正文 `pong`
- WebDAV 认证与目录：每 1 小时对 `/dav/` 做 PROPFIND，期望 207
- 115 网盘挂载：每 1 小时对 `/dav/guangdong/` 做 PROPFIND，期望 207
- Caddy 证书：每 1 小时检查证书剩余有效期 > 336 小时
- sing-box 443：每 1 分钟 TCP 连通
- DuckDNS 解析：每 10 分钟查 `sumax.duckdns.org` 的 A 记录是否为 `166.88.2.219`
- 115 Token（视频取链）：每 4 小时对库中一个固定剧集文件做 `Range: bytes=0-0` 的 GET（不跟随重定向，超时 20s），期望 302。失败 1 次即告警

---

## 8. 备份（`/backup`）

脚本已于 2026-11-17 09:56 更新，**不再打包 Navidrome**。内容包括：

- `/etc/sing-box/*`、`/etc/caddy/*`
- `/opt/gatus/config.yaml`、`docker-compose.yml`、`.env`、`/opt/gatus/*.py`
- systemd：`gatus-status-bot`、`traffic-collector`、若存在的 `sing-box.service` 及其 drop-in
- `/root/.ssh/authorized_keys`
- OpenList：`/opt/openlist/data/config.json`、`data.db`（优先 sqlite 在线备份）、可能的 yml、以及 `docker inspect openlist`
- `/etc/ssh/sshd_config.d/*.conf`、`/etc/systemd/journald.conf.d/*.conf`
- 整个 `/opt/mediahub`（含 `.env`，跳过缓存/venv/.git）
- `/opt/gatus/handoff/`：本说明书 `VPS对接说明书.md`，以及迁移脚本 `vps-migrate/restore.sh` 与其 `README.md`
- `README.txt` 恢复说明

打包为 AES-256 的 7z（加密文件名），密码为 `/opt/gatus/.env` 里的 `BACKUP_PASSWORD`，发送到 Telegram 后删除服务器临时文件。

**未备份：** Gatus 历史库、流量统计库、OpenList 日志/临时目录、Evoxt 面板防火墙规则（不在系统盘上）。

截至本说明书快照，最近一次成功发出备份是 **2026-11-14 21:34**。之后的变更（mediahub、`/115qr`、卸掉 Navidrome、Caddy 站点调整等）要重新发一次 `/backup` 才会进入新的压缩包。

---

## 9. 密钥与敏感文件位置（只列位置，不写值）

| 名称 | 位置 |
| --- | --- |
| `TELEGRAM_TOKEN`、`WEBDAV_BASIC`、`CLASH_SECRET`、`BACKUP_PASSWORD` | `/opt/gatus/.env` |
| mediahub 网页密码、OpenList 密码、TMDB、Telegram、`MEDIAHUB_INTERNAL_TOKEN` 等 | `/opt/mediahub/.env` |
| OpenList 管理/站点令牌、115 cookie / `qrcode_token` | OpenList 数据目录 `/opt/openlist/data/`（尤其是 `data.db`、`config.json`） |
| sing-box 密钥与分享链接 | `/etc/sing-box/` |
| SSH 主机密钥与 `authorized_keys` | `/etc/ssh/`、`/root/.ssh/` |

不要把这些文件拷到聊天、网盘公开目录或 Git。

---

## 10. 主人侧常用客户端

- iPhone，代理客户端为 Shadowrocket（小火箭），规则模式，连接到本机 sing-box。
- 影视日常账号走 OpenList；整理可用 mediahub 网页或 Filmix。
- 音乐：Flacbox + OpenList WebDAV（`/guangdong/音乐`），不要再部署 Navidrome 或 VPS 音乐挂载。

---

## 11. 已拆除、不要恢复的组件

2026-11-17 已彻底卸载 **Navidrome**：Docker、`/opt/navidrome`、rclone 挂载 `navidrome-music.service`、`/mnt/navidrome-music`、Caddy 站点 `music.sumax.duckdns.org:8443`、系统用户/组，以及备份中的 Navidrome 打包。115 上的音乐文件未删除，只是不再经 VPS 挂载播放。

---

## 12. 常见操作提示

- **先读后改。** 变更前说明将改什么、影响哪些端口/配置/内存磁盘，等主人同意。
- **查服务：** `systemctl is-active sing-box caddy docker ssh gatus-status-bot traffic-collector`；`docker ps`。
- **从外部测 HTTPS：** 用解析到 `166.88.2.219` 的方式访问 `:8443`，避免被本地代理把 `*.duckdns.org` 劫持到错误地址。
- **115 登录失效：** 请主人向 `@rokcket_bot` 发 `/115qr` 扫码，不要在聊天里要 Cookie。
- **磁盘很紧（约 5G）。** 不要拉大镜像、不要把媒体文件落到 VPS 本地，注意 Docker 日志与 journal 大小。
- **内存很紧。** 不要再堆常驻服务；改动前评估 RSS。
- 主机上可能有未应用的 Debian 安全更新；不要自行批量升级或重启关键服务。

---

## 13. 明确未做 / 需主人批准才做的事

- 主机 nftables / SSH 连接频率限制、收紧 SSH（如关闭 X11、缩短 `LoginGraceTime`、降低 `MaxAuthTries`）
- `unattended-upgrades`（即使做，也须关闭自动重启）
- 应用积压的 apt 安全更新
- 将 OpenList 管理后台从公网收掉、只对外保留 WebDAV
- 给网站套 Cloudflare
- 启用 IPv6 公网入口（须先开 Evoxt IPv6 防火墙并添加 AAAA）
- 自动探测 115 登录过期并推送二维码
- 把这台机器当大模型或可执行任意命令的 bot 宿主

---

## 14. 给接手助手的工作方式

1. 把本文当权威清单，但仍以服务器现网为准；本文是 2026-11-17 的快照。
2. 能只读解决的，先只读。
3. 需要改配置、装软件、重启服务、改防火墙或改 DNS 时，先说明方案，得到主人同意再执行。
4. 输出里不要出现密钥或带签名的 115 直链。
5. 与 115 / OpenList 交互时统计并控制请求频率。
6. 主人与 mediahub、`@rokcket_bot` 可以直接完成的下载和入库，不必由助手代劳，除非主人要求你介入。
