# VPS 一键恢复 / 迁移脚本（restore.sh）

这个脚本用 Telegram 机器人 `/backup` 生成的加密 7z 备份，把整套服务恢复到**一台全新的 Debian 13（amd64）服务器**上：
sing-box（443）、Caddy（8443，自动证书）、Gatus、OpenList、mediahub、状态机器人和流量采集器。
架构跟现在一样：sing-box、Caddy、机器人、采集器跑在宿主机上，Gatus、OpenList、mediahub 跑在 Docker 里。

## 用法

```bash
# 1. 把脚本和备份传到新服务器
scp restore.sh vps-backup-20261116-0206.7z root@新IP:/root/

# 2. 先演练一遍：只打印要做的事，不改系统（会解压到临时目录检查内容，结束后删除）
ssh root@新IP
bash /root/restore.sh --dry-run /root/vps-backup-20261116-0206.7z

# 3. 正式恢复（旧服务器的机器人还在跑时加 --defer-bot）
bash /root/restore.sh --defer-bot /root/vps-backup-20261116-0206.7z
```

密码：默认在终端里提示输入，不回显。用 `--password-stdin` 可以从标准输入读取，例如 `bash restore.sh --password-stdin x.7z < 密码文件`。
密码不会出现在命令行参数、环境变量或 shell 历史里。只在内存里通过管道交给 7z。

| 选项 | 作用 |
|---|---|
| `--dry-run` | 只打印操作，不改系统 |
| `--force` | 机器上已经在跑这些服务时也继续（中断后重跑用）。不加的话脚本会拒绝执行 |
| `--password-stdin` | 从 stdin 读取密码 |
| `--extracted DIR` | 用已经解压好的目录（测试用） |
| `--timezone TZ` | 默认 `Asia/Singapore`（跟原服务器一样，UTC+8） |
| `--swap-size 512M` | 没有 swap 时创建 /swapfile；设为 `0` 不创建 |
| `--skip-ssh` | 不恢复 sshd 配置，只合并公钥 |
| `--defer-bot` | 状态机器人只 enable、先不启动，避免和旧机器同时拉取消息导致 Telegram 409 |

脚本可以重复运行：内容相同的文件不会重新写入，已有的 swap、已安装的 sing-box、已存在的 openlist 容器都会跳过。

## 脚本做了什么

0. **前置检查**：必须是 root；不是 Debian 13 或 amd64 时给出警告；sing-box、caddy、机器人、采集器或容器已经在运行时拒绝执行，加 `--force` 才继续。
1. **解压并检查**：解压到 `/root/restore-XXXXXX`（权限 700，退出时自动删除）。按机器人 `build_backup` 的清单检查必需文件，缺任何一个就停止。只检查 `.env` 里的**键名**，不打印值。`authorized_keys` 里没有公钥时停止，防止把自己锁在外面。
2. **装软件**：从 Debian 官方源安装固定版本：docker.io 26.1.5、docker-compose 2.26.1、docker-buildx、caddy 2.6.2、python3 3.13.5 和 7zip。软件源里已经没有某个原版本时，改装当前版本并给出警告。sing-box 1.14.4 从 GitHub release 下载 .deb，**校验 SHA256** 后再安装。
3. **系统设置**：设置时区，写入 BBR 的 sysctl 配置，创建 512M swap 并写入 fstab，恢复 journald 的大小限制。
4. **SSH**：把备份里的公钥合并进目标机已有的 `authorized_keys`（去重，不删除已有公钥），再恢复 `00-no-password.conf`。先跑 `sshd -t`，通过了才 reload。这样当前连接不会断；检查失败就还原。恢复后**只能用密钥登录**。
5. **恢复文件**，同时设置权限：
   - sing-box 配置 600
   - `/opt/gatus` 700，`.env` 600
   - `/opt/openlist/data` 归 1001:1001，权限 750
   - `/opt/mediahub` 700，`.env` 600，`data/` 归 10001:10001
   - 被覆盖的原文件先备份到 `/root/pre-restore-时间戳/`
6. **启动服务**：
   - 先跑 `sing-box check` 和 `caddy validate`，通过了才启动 sing-box 和 caddy
   - Gatus 用 `docker compose up -d` 启动
   - OpenList 按备份里的 `openlist-inspect.json` 重新 `docker run`，端口、挂载、环境变量和重启策略跟原来一样
   - mediahub 用 `compose up -d --build` 启动
   - 启用流量采集器和状态机器人
7. **健康检查**：
   - systemd 服务和容器状态
   - 端口 443、8443、9090
   - OpenList `/ping`、Gatus `/health`、mediahub `/healthz`
8. **汇总**：显示本机公网 IP 和 sumax.duckdns.org 现在的解析结果，再列出要手动做的事。

## 恢复后要手动做的事

1. **停掉旧服务器的机器人**：`systemctl stop gatus-status-bot`。然后在新机器上运行 `systemctl start gatus-status-bot`（用了 `--defer-bot` 时）。
2. **DuckDNS**：把 sumax 的 IP 改成新 IP。
3. **证书**：DNS 生效后，Caddy 会自动重新申请 sumax 和 media.sumax 的证书，需要 80 端口能访问，几分钟内完成。
4. **sing-box 客户端**：客户端里填的是 IP 的话，要改成新 IP。UUID 和 Reality 密钥不变。
5. **OpenList / 115**：换了 IP 后，115 可能要求重新登录。打开 `https://sumax.duckdns.org:8443` 检查存储状态，需要的话重新扫码或更新 Cookie。
6. Gatus 监控历史和流量统计目前不在备份里，新机器会从头开始。

## 回滚

被覆盖的原文件都在 `/root/pre-restore-时间戳/`，目录结构跟原路径一样，可以直接拷回去。
在全新机器上一般不需要回滚。真出问题时，最简单的办法是重装系统再跑一遍。

## 预计耗时

总共大约 **10–20 分钟**：
- apt 安装：3–6 分钟
- 拉取镜像并构建 mediahub：3–8 分钟
- 其余步骤：1 分钟以内

另外还要等 DuckDNS 生效和证书签发，通常 5 分钟左右。

## 备份里缺的东西（建议加到 /backup）

| 项目 | 影响 | 脚本现在的处理 |
|---|---|---|
| `/etc/sysctl.d/99-bbr.conf` | BBR | 按原服务器的内容直接写入 |
| `gatus.db` 和 `traffic.db` | 历史数据 | 从头开始。建议用 sqlite `.backup` 做一致性快照后加入备份 |
| 镜像 digest（openlist、gatus 用的是 `:latest`） | 新机器可能拉到更新的版本 | 先用 latest。建议备份时记下 `RepoDigests` |
| 时区、swap 大小、sing-box 版本 | 小事 | 写死在脚本的默认值里 |

另外，机器人备份里 README 写的是 "apt install sing-box"，这是错的：sing-box 不在 Debian 源里，要用 GitHub 的 .deb。
