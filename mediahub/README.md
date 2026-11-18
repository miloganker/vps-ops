# mediahub · 影视整理服务

> 以后其它媒体相关的小功能也放在这个项目里。现在包括：把磁力交给 115 云下载，以及“待整理 → 改名 → 入库”。

把 OpenList（115）里 `待整理/电影`、`待整理/电视剧` 的新资源按固定规则改名，然后一部一部移进正式片库。
单个 Docker 容器、一个目录、一个 `.env`，换服务器几分钟就能跑起来。

## 它会做什么

0. **投递云下载**：在网页粘贴磁力 / ed2k / http 链接（一次可多条；若选了电影或电视剧，会应用到这一批），或把链接发给 Telegram 的 `@rokcket_bot`（也可用 `/dl`，一条消息可多条）。每次最多处理 20 条，多出来的会明确告知未提交，不会悄悄丢掉。多条提交时会稍稍错开向 115 的请求；下载可同时盯梢，自动入库仍一次一条。链接名字（`dn=`）能判断类型就直接提交到对应的待整理目录。只有哈希的磁力（`magnet:?xt=urn:btih:…`）或名字看不准时不再先问：先下到 `待整理/未分类/<任务号>`（每个任务一个空文件夹），下完看 115 里真实的种子文件夹名和文件列表（`S01E01`、`第x集`、`全x集`、季文件夹 → 电视剧；只有一个正片 + 年份 → 电影），再用 TMDB 确认，然后一次一个地移到 `待整理/电影` 或 `待整理/电视剧`（间隔 `MOVE_WAIT`，遇 990009 等待重试，超时先核实不盲目重试）。通知里会写明判断理由。实在判断不了才发“电影 / 电视剧”按钮，你点了再移动。OpenList 4.2.9 的任务列表只给 `download <链接> to (<目录>)`，看不到种子名，所以要等文件落到 115 再判断。从不删除任何东西（移走后空的任务文件夹会留在 未分类 里）。下载完成后自动扫描并通知你，附上网页预览链接。是否继续自动改名入库见下面的“全自动入库”。同一条链接 115 已在队列里（错误 10008）时不会重复提交，只盯梢。离线下载只用 OpenList 的 `115 Cloud` 工具，不会用 SimpleHttp（避免下到服务器本机）。

   **全自动入库（第二阶段，`AUTO_ORGANIZE`，默认开）**：开着时所有链接都先下到自己的 `待整理/未分类/<任务号>`（名字看得出类型的也一样，这样能准确认出“这个任务的那一项”），下完移到 `待整理/电影|电视剧`，然后在整理任务队列里（和网页扫描/执行同一把锁，一次只跑一个，忙就排队）扫描，并只对这一项做安全检查；全部通过才用现有执行器改名（间隔 `RENAME_INTERVAL`）并一次一个移进 `电影` / `电视剧` 片库（间隔 `MOVE_WAIT`，超时不重试，之后只核对）。安全检查：分类高置信（你点按钮选的也算）；扫描里恰好找到这一项、类型一致；TMDB 高置信且和分类时对上的是同一部；没有任何提示/阻止项（只放过“UHD/4K 按 2160p 写入”“标签取自文件夹名”两种）；文件夹中文名和 TMDB 中文名（含大陆/港台别名、译名，模糊比对：忽略标点、2=二、：=:）对不上时，如果英文名 + 年份都对上且高置信，只记一条 ℹ️ 说明、不拦；年份不一致、匹配不够有把握、或这个中文名精确对应另一条 TMDB 结果，照样拦下；扫描没有片库列表错误；电影片库已有同名 → 跳过并报冲突；电视剧片库已有这部剧 → 新季直接放进已有剧集文件夹，已有同一季 → 跳过并报冲突。任何一条不通过就和以前一样：扫描 + Telegram 通知你去预览确认，并写明原因。服务重启打断的自动入库不会自动重来，标为“中断”请人工核对。网页首页可以开关（也可 `/auto on|off`），“最近整理任务”里能看到“自动入库”记录和明细。从不删除任何东西。
1. **扫描**（只读）：列出待整理目录，解析中文名/英文名/年份/季/集，用 TMDB（简体中文）确认片名和年份，生成预览。
2. **中文片名**：默认用 TMDB 简体中文标题；TMDB 简体标题为空（会退回英文名，例如《壮志凌云2：独行侠》361743）时，依次用 TMDB 大陆别名 → 新加坡译名/别名 → 文件夹里的中文名 → 港/台译名，预览里会注明来源。
3. **预览**：每一项显示 旧名 → 新名、置信度和提示。可以取消勾选，或修改片名 / 年份 / TMDB ID 后重新生成新名字。
   - 高置信度：默认勾选
   - 需确认：默认不勾选，确认后手动勾上
   - 低置信 / 未识别：不能执行，先修改
   - 片库已有同名：默认不勾选；勾上也只改名、不移动
4. **执行**（后台运行，有进度页；一次只能跑一个任务）：
   - 先改文件，再改文件夹；每个写操作之间隔 `RENAME_INTERVAL` 秒
   - 改名**超时不重试**（115 常见“超时但其实成功”），最后用 refresh 列表核对
   - 移动：一次只移一个文件夹，每次移动后等 `MOVE_WAIT` 秒（115 移动是异步的，重叠会报 990009）
   - 移动前先检查片库里有没有同名文件夹，有就跳过并提示——**从不合并文件、覆盖或删除**
   - 电视剧例外：片库里已经有 `剧名 (年份)`、但还没有这一季时，把新的 `Season NN` 直接移进片库里的剧集文件夹
     （不在待整理里新建剧名文件夹）。同一季片库里已有 → 跳过并提示，待整理里那份原样保留
   - 结果页显示每一项的状态和本次 115 请求次数
5. 可选：任务结束发 Telegram 通知（默认关闭）。

### 命名规则

| 类型 | 结果 |
| --- | --- |
| 电影文件夹 | `愤怒的公牛 (1980)` |
| 电影文件 | `愤怒的公牛 (1980) 2160p.DTS-HD.MA.5.1.mkv`、`七武士 (1954) 2160p.DV.HDR.FLAC.1.0.mkv` |
| 字幕 | 同名，保留语言后缀：`七武士 (1954) 2160p.DV.HDR.FLAC.1.0.chs.srt` |
| 剧集 | `绝命毒师 (2008)/Season 01/绝命毒师 (2008) S01E01 2160p.DDP.5.1.mkv` |

- 只保留原名里**真实出现**的标签：分辨率、帧率（60FPS）、DV / HDR / HDR10+ / SDR、Atmos / TrueHD / DTS-HD.MA / DTS / DDP / AC3 / FLAC / AAC 和声道数。
- 去掉片源（WEB-DL、BluRay、Remux、NF…）、视频编码（H.265、x264、HEVC…）、发布组和网站广告。
- 原名只有 `UHD`/`4K` 没写分辨率时写成 `2160p`，并在预览里提示。
- 广告文件（文件名带网址/“更多下载”，或小于 `AD_MAX_SIZE_MB` 的视频）：不改名、**不删除**、不算集数。
- 电影不新建文件夹（每部本来就在自己的文件夹里）；视频直接放在待整理根目录的会被标出来，需要手动处理。
- 剧集文件夹里是散放的剧集：先在原文件夹里逐集改名 → 新建 `剧名 (年份)` → 把原文件夹移进去 → 等 `MOVE_WAIT` → 把它改名为 `Season 01`。
  已经按季分好文件夹的（`Season 1`、`S02`、`第三季`…）：逐集改名，季文件夹改成 `Season NN`，再改剧名文件夹。
- 只有 `E01` 没有季号的按第 1 季处理。

## 部署 / 迁移到新服务器

前提：新服务器上已经有 Docker（含 compose 插件）和 OpenList。

```bash
# 1. 拿到代码（二选一）
# 从备份恢复（7z AES-256 加密，密码 = /opt/gatus/.env 里的 BACKUP_PASSWORD，和 /backup 命令同一套）：
#   7z x /backup/mediahub-YYYYmmdd-HHMM.7z -o/opt      （会提示输入密码；包含 .env）
# 或者：scp -r mediahub 新服务器:/opt/
cd /opt/mediahub

# 2. 填配置（旧服务器上的 .env 从 /backup 拷过来也可以）
cp .env.example .env
nano .env            # 至少填 APP_PASSWORD、OPENLIST_USER、OPENLIST_PASSWORD、TMDB_API_KEY
chmod 600 .env

# 3. 数据目录（容器里用 uid 10001 运行）
mkdir -p data && chown 10001:10001 data

# 4. 启动
docker compose up -d --build
curl -s http://127.0.0.1:8095/healthz      # 返回 {"ok":true} 就好了
```

然后在 Caddy 里加一个站点反代到 `127.0.0.1:8095`（示例，域名换成你的）：

```caddyfile
renamer.example.duckdns.org {
    reverse_proxy 127.0.0.1:8095
}
```

更新代码：把新代码覆盖到 `/opt/mediahub`（保留 `.env` 和 `data/`），再 `docker compose up -d --build`。
`data/` 里只有扫描预览、任务记录和会话密钥，丢了也没关系；真正需要备份的只有 `.env`。

手动备份（与现有习惯一致：7z、AES-256、加密文件名，密码走 stdin 不出现在命令行）：

```bash
mkdir -p /backup && chmod 700 /backup
PW=$(grep '^BACKUP_PASSWORD=' /opt/gatus/.env | cut -d= -f2-)
cd /opt && printf '%s\n%s\n' "$PW" "$PW" | 7z a -t7z -p -mhe=on -m0=lzma2 -mx=5 -md=1m -mmt=1 -bd /backup/mediahub-$(date +%Y%m%d-%H%M).7z mediahub
chmod 600 /backup/mediahub-*.7z; unset PW
```

### 网络说明

默认 `network_mode: host`：服务只监听 `127.0.0.1:8095`，并且能直接访问宿主机上的 OpenList（`http://127.0.0.1:5244`）。
如果 Caddy 也跑在 Docker 的 bridge 网络里，看 `docker-compose.yml` 底部注释改成 bridge + 端口映射。

## 配置项（.env）

见 `.env.example`，每一项都有中文注释。常用的：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_PASSWORD` | – | 网页登录密码（服务端用哈希做常量时间比较；会话是签名 Cookie，7 天有效） |
| `OPENLIST_URL` / `OPENLIST_USER` / `OPENLIST_PASSWORD` | – | OpenList 地址和账号（建议用能看到隐藏文件的账号） |
| `TMDB_API_KEY` | – | v3 API Key 或 v4 读取令牌（`eyJ` 开头）都支持 |
| `STAGING_MOVIES` / `STAGING_TV` | `…/待整理/电影`、`…/待整理/电视剧` | 待整理目录 |
| `LIBRARY_MOVIES` / `LIBRARY_TV` | `…/影视/电影`、`…/影视/电视剧` | 正式片库 |
| `RENAME_INTERVAL` | 2 | 写操作间隔（秒） |
| `MOVE_WAIT` | 20 | 每次移动后等待（秒） |
| `TITLE_SOURCE` | tmdb | 中文名用 TMDB 的（tmdb）还是文件夹里的（folder） |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | 空 | 都填了才发通知 |
| `MEDIAHUB_INTERNAL_TOKEN` | 空 | 本机机器人调用内部 API 的令牌 |
| `MEDIAHUB_PUBLIC_URL` | `https://media.sumax.duckdns.org:8443` | 通知里的预览链接 |
| `DOWNLOAD_POLL_INTERVAL` | 90 | 盯梢 115 离线任务的间隔（秒）；0 表示关闭后台轮询 |

## 115 请求次数

每次调用 OpenList 的 `/api/fs/*` 都算一次（登录不算）。
- 扫描：每个待整理根目录 1 次 + 每个条目文件夹 1 次 + 每个季文件夹 1 次 + 每个片库 1 次。
- 执行：每个改名/新建/移动 1 次 + 核对（每类：待整理 1 次、片库 1 次，最后再各 1 次）；只有出现超时的条目才会多列一次它的内部。
- 预览页底部会显示预计次数，结果页显示实际次数。

## 本地开发 / 测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

# 不连 OpenList 的演示模式（内存里的假目录树，真实 TMDB 或假 TMDB）
DATA_DIR=/tmp/mr APP_PASSWORD=demo COOKIE_SECURE=false \
FAKE_OPENLIST=fixtures/demo_tree.json FAKE_TMDB=1 RENAME_INTERVAL=0.3 MOVE_WAIT=1 \
uvicorn app.main:app --host 127.0.0.1 --port 8095
```

## 目录结构

```
app/
  config.py        配置（全部来自环境变量）
  naming.py        文件名解析与新名字生成
  tmdb.py          TMDB 查询和打分（v3/v4 都支持）
  openlist.py      OpenList API 客户端（list/rename/mkdir/move + 115 云下载，从不调用 remove）
  fake_openlist.py 测试和演示用的假 OpenList
  downloads.py     磁力投递、分类、115 云下载盯梢（与整理任务分开）
  release.py       下完后按真实文件判断电影 / 电视剧（只读 list）
  scanner.py       扫描并生成预览/操作计划
  executor.py      执行顺序、超时不重试、一次一个移动、同名跳过、最终核对
  jobs.py          后台任务（一次一个）和 JSON 状态
  notify.py        Telegram 通知（可选）
  main.py          网页（FastAPI）
  templates/ static/
tests/             pytest：命名规则、TMDB 打分、执行顺序、网页流程、云下载投递
fixtures/demo_tree.json  基于真实文件名的演示目录树
```
