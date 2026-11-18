from app.executor import Executor
from app.scanner import Scanner

SM = "/guangdong/娱乐/影视/待整理/电影"
ST = "/guangdong/娱乐/影视/待整理/电视剧"
LM = "/guangdong/娱乐/影视/电影"
LT = "/guangdong/娱乐/影视/电视剧"


def scan(fake, tmdb, settings):
    return Scanner(fake, tmdb, settings, log=lambda m: None).scan()


def run(fake, settings, clock, items):
    ex = Executor(fake, settings, log=lambda m: None, sleep=clock.sleep)
    # 每次调用时记录当前时间，用于检查间隔
    orig = {}
    stamps = []
    for name in ("rename", "move", "mkdir", "list"):
        orig[name] = getattr(fake, name)

        def wrap(*a, _n=name, **k):
            stamps.append((clock.t, _n, a))
            return orig[_n](*a, **k)
        setattr(fake, name, wrap)
    res = ex.run(items)
    return res, stamps


def by_target(res):
    return {r["target"]: r for r in res["items"]}


def test_scan_is_read_only(fake, tmdb, settings):
    data = scan(fake, tmdb, settings)
    assert all(c[0] == "list" for c in fake.calls)
    assert all(c[2] is True for c in fake.calls)  # refresh
    names = {it["target_name"]: it for it in data["items"]}
    assert names["绝命毒师 (2008)"]["tv_ops"]["renames"][0]["new"] == "绝命毒师 (2008) S01E01 2160p.DDP.5.1.mkv"
    assert names["请回答1988 (2015)"]["library_conflict"]
    assert not names["请回答1988 (2015)"]["include"]
    assert not names["新建文件夹"]["executable"]
    # 广告文件不计入集数
    assert names["绝命毒师 (2008)"]["episode_count"] == 7
    assert names["请回答1988 (2015)"]["episode_count"] == 20


def test_full_run(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    fake.calls.clear()
    before = fake.requests_115
    items = data["items"]
    res, stamps = run(fake, settings, clock, items)
    r = by_target(res)
    assert r["岁月神偷 (2010)"]["status"] == "done"
    assert r["沙丘2 (2024)"]["status"] == "done"
    assert r["七武士 (1954)"]["status"] == "done"
    assert r["绝命毒师 (2008)"]["status"] == "done"
    assert "愤怒的公牛 (1980)" not in r      # 片库同名，默认不勾选
    assert "新建文件夹" not in r             # 未识别，不执行

    assert fake.names(LM + "/沙丘2 (2024)") == [
        "【更多无水印高清电影请访问 www.HDBTHD.com】.MKV",   # 广告原样保留，不删
        "沙丘2 (2024) 2160p.Atmos.TrueHD.7.1.mkv"]
    assert fake.names(LM + "/七武士 (1954)") == [
        "七武士 (1954) 2160p.DV.HDR.FLAC.1.0.chs.srt",
        "七武士 (1954) 2160p.DV.HDR.FLAC.1.0.eng.ass",
        "七武士 (1954) 2160p.DV.HDR.FLAC.1.0.mkv"]
    bb = fake.names(LT + "/绝命毒师 (2008)/Season 01")
    assert "绝命毒师 (2008) S01E07 2160p.DDP.5.1.mkv" in bb and len(bb) == 9
    assert fake.names(LT + "/绝命毒师 (2008)") == ["Season 01"]
    assert res["requests_115"] == fake.requests_115 - before

    # 每次移动只有一个文件夹，且相邻移动之间至少隔 MOVE_WAIT
    moves = [s for s in stamps if s[1] == "move"]
    assert all(len(s[2][2]) == 1 for s in moves)
    for a, b in zip(moves, moves[1:]):
        assert b[0] - a[0] >= settings.move_wait
    # 写操作之间至少隔 RENAME_INTERVAL
    writes = [s for s in stamps if s[1] in ("rename", "mkdir", "move")]
    for a, b in zip(writes, writes[1:]):
        assert b[0] - a[0] >= settings.rename_interval
    assert not hasattr(fake, "removed")


def test_tv_flat_sequence(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    items = [it for it in data["items"] if it["target_name"] == "绝命毒师 (2008)"]
    res, stamps = run(fake, settings, clock, items)
    ops = [(s[1], s[2]) for s in stamps if s[1] != "list"]
    kinds = [o[0] for o in ops]
    # 7 集改名 → 建剧名文件夹 → 原文件夹移进去 → 季文件夹改名 → 移进片库
    assert kinds == ["rename"] * 7 + ["mkdir", "move", "rename", "move"]
    assert ops[7][1] == (ST + "/绝命毒师 (2008)",)
    assert ops[8][1][1] == ST + "/绝命毒师 (2008)"
    assert ops[9][1][1] == "Season 01"
    t_move = next(s[0] for s in stamps if s[1] == "move")
    t_season = [s[0] for s in stamps if s[1] == "rename"][-1]
    assert t_season - t_move >= settings.move_wait   # 等 115 处理完移动再动里面的东西
    assert by_target(res)["绝命毒师 (2008)"]["status"] == "done"


def test_timeout_not_retried_but_verified(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    target = "沙丘2 (2024) 2160p.Atmos.TrueHD.7.1.mkv"
    fake.faults[("rename", target)] = "timeout"       # 超时但其实成功
    items = [it for it in data["items"] if it["target_name"] == "沙丘2 (2024)"]
    res, stamps = run(fake, settings, clock, items)
    renames = [s for s in stamps if s[1] == "rename" and s[2][1] == target]
    assert len(renames) == 1                           # 没有重试
    r = by_target(res)["沙丘2 (2024)"]
    assert r["status"] == "done"
    assert any("核对确认已生效" in n for n in r["notes"])


def test_timeout_really_failed_is_not_moved(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    target = "岁月神偷 (2010) 1080p.DTS.mkv"
    fake.faults[("rename", target)] = "timeout_fail"
    items = [it for it in data["items"] if it["target_name"] == "岁月神偷 (2010)"]
    res, stamps = run(fake, settings, clock, items)
    assert len([s for s in stamps if s[1] == "rename" and s[2][1] == target]) == 1
    r = by_target(res)["岁月神偷 (2010)"]
    assert r["status"] == "failed"
    assert not [s for s in stamps if s[1] == "move"]
    assert "岁月神偷 (2010)" in fake.names(SM)          # 留在待整理


def test_conflict_skips_move(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    # 扫描后片库里出现了同名文件夹
    fake.mkdir(LM + "/沙丘2 (2024)")
    items = [it for it in data["items"] if it["target_name"] == "沙丘2 (2024)"]
    res, stamps = run(fake, settings, clock, items)
    r = by_target(res)["沙丘2 (2024)"]
    assert r["status"] == "conflict"
    assert not [s for s in stamps if s[1] == "move"]
    assert fake.names(LM + "/沙丘2 (2024)") == []        # 片库里的没被动过
    assert "沙丘2 (2024)" in fake.names(SM)


def test_busy_move_retried_once_after_wait(fake, tmdb, settings, clock):
    fake.move_busy_seconds = 30                          # 比 MOVE_WAIT 还慢
    data = scan(fake, tmdb, settings)
    items = [it for it in data["items"] if it["target_name"] in ("沙丘2 (2024)", "岁月神偷 (2010)")]
    res, stamps = run(fake, settings, clock, items)
    moves = [s for s in stamps if s[1] == "move"]
    assert len(moves) == 3                               # 第二个移动被拒绝一次，等待后再试一次
    assert all(r["status"] == "done" for r in res["items"])


def test_busy_without_retry(fake, tmdb, settings, clock):
    fake.move_busy_seconds = 30
    settings.move_retry_on_busy = False
    data = scan(fake, tmdb, settings)
    items = [it for it in data["items"] if it["target_name"] in ("沙丘2 (2024)", "岁月神偷 (2010)")]
    res, stamps = run(fake, settings, clock, items)
    assert len([s for s in stamps if s[1] == "move"]) == 2
    st = sorted(r["status"] for r in res["items"])
    assert st == ["done", "move_failed"]


def test_seasons_layout(clock, tmdb, settings):
    from app.fake_openlist import FakeOpenList
    show = "Breaking.Bad.Complete.2160p.NF.WEB-DL.DDP.5.1-GRP"
    big = 5 * 1024 ** 3
    fake = FakeOpenList({
        ST: [{"name": show, "is_dir": True}],
        f"{ST}/{show}": [{"name": "Season 1", "is_dir": True}, {"name": "S02", "is_dir": True}],
        f"{ST}/{show}/Season 1": [{"name": "Breaking.Bad.S01E01.2160p.DDP.5.1.mkv", "size": big},
                                  {"name": "Breaking.Bad.S01E01.chs.srt", "size": 50000}],
        f"{ST}/{show}/S02": [{"name": "Breaking.Bad.S02E01.2160p.DDP.5.1.mkv", "size": big}],
        LT: [], LM: [], SM: [],
    })
    fake.clock = clock
    data = Scanner(fake, tmdb, settings, log=lambda m: None).scan()
    it = data["items"][0]
    assert it["tv_ops"]["layout"] == "seasons"
    assert it["include"]
    res, stamps = run(fake, settings, clock, data["items"])
    assert res["items"][0]["status"] == "done"
    assert fake.names(LT + "/绝命毒师 (2008)") == ["Season 01", "Season 02"]
    assert fake.names(LT + "/绝命毒师 (2008)/Season 01") == [
        "绝命毒师 (2008) S01E01 2160p.DDP.5.1.chs.srt", "绝命毒师 (2008) S01E01 2160p.DDP.5.1.mkv"]
    assert not [s for s in stamps if s[1] == "mkdir"]
    assert len([s for s in stamps if s[1] == "move"]) == 1


def test_never_calls_remove(fake, tmdb, settings, clock):
    data = scan(fake, tmdb, settings)
    for it in data["items"]:
        if it["executable"]:
            it["include"] = True     # 连有冲突的也勾上
    res, _ = run(fake, settings, clock, data["items"])
    assert by_target(res)["请回答1988 (2015)"]["status"] == "conflict"
    assert by_target(res)["愤怒的公牛 (1980)"]["status"] == "conflict"
    assert all(c[0] in ("list", "rename", "mkdir", "move") for c in fake.calls)


# ------------------------------------------------------------ 新季并入片库已有剧集

BIG = 5 * 1024 ** 3
BB_S2_FLAT = "Breaking.Bad.S02.2160p.NF.WEB-DL.DDP.5.1.H.265-BlackTV"


def merge_tree(lib_seasons, staging):
    from app.fake_openlist import FakeOpenList
    tree = {SM: [], LM: [], ST: [], LT: [{"name": "绝命毒师 (2008)", "is_dir": True}],
            f"{LT}/绝命毒师 (2008)": [{"name": n, "is_dir": True} for n in lib_seasons]}
    for d, items in staging.items():
        tree[ST].append({"name": d, "is_dir": True})
        tree[f"{ST}/{d}"] = items
    return FakeOpenList(tree)


def flat_s2():
    return {BB_S2_FLAT: [
        {"name": "Breaking.Bad.S02E01.Seven.Thirty-Seven.2160p.NF.WEB-DL.DDP.5.1.H.265-BlackTV.mkv", "size": BIG},
        {"name": "Breaking.Bad.S02E02.Grilled.2160p.NF.WEB-DL.DDP.5.1.H.265-BlackTV.mkv", "size": BIG},
        {"name": "【更多高清剧集下载请访问 www.BTHDTV.com】.mkv", "size": 636976},
    ]}


def test_merge_flat_new_season(clock, tmdb, settings):
    fake = merge_tree(["Season 01"], flat_s2())
    fake.clock = clock
    data = scan(fake, tmdb, settings)
    it = data["items"][0]
    assert it["merge"] == {"show": "绝命毒师 (2008)", "seasons": [2], "skip": []}
    assert it["include"] and not it.get("library_conflict")
    res, stamps = run(fake, settings, clock, data["items"])
    ops = [(s[1], s[2]) for s in stamps if s[1] != "list"]
    # 2 集改名 → 原文件夹改名为 Season 02 → 一次移动直接进片库里的剧集文件夹；不新建任何文件夹
    assert [o[0] for o in ops] == ["rename", "rename", "rename", "move"]
    assert ops[2][1] == (f"{ST}/{BB_S2_FLAT}", "Season 02")
    assert ops[3][1] == (ST, f"{LT}/绝命毒师 (2008)", ["Season 02"])
    assert fake.names(f"{LT}/绝命毒师 (2008)") == ["Season 01", "Season 02"]
    assert "绝命毒师 (2008) S02E02 2160p.DDP.5.1.mkv" in fake.names(f"{LT}/绝命毒师 (2008)/Season 02")
    assert fake.names(ST) == []                          # 待整理里没有留下空的剧名文件夹
    r = res["items"][0]
    assert r["status"] == "merged"
    assert res["requests_115"] == len(stamps)
    # 预计次数和实际一致（扫描后的执行阶段）
    from app.scanner import estimate_requests
    assert estimate_requests(it) + 2 == res["requests_115"]   # +2：核对待整理目录、列一次片库


def test_merge_same_season_exists_is_conflict(clock, tmdb, settings):
    fake = merge_tree(["Season 01", "Season 02"], flat_s2())
    fake.clock = clock
    data = scan(fake, tmdb, settings)
    it = data["items"][0]
    assert it["merge"]["seasons"] == [] and it["library_conflict"] and not it["include"]
    it["include"] = True                                 # 用户强行勾选：只改名，不移动
    res, stamps = run(fake, settings, clock, data["items"])
    assert not [s for s in stamps if s[1] == "move"]
    assert res["items"][0]["status"] == "conflict"
    assert fake.names(f"{LT}/绝命毒师 (2008)/Season 02") == []   # 片库里的没被动过
    assert not [s for s in stamps if s[1] == "mkdir"]              # 也没在待整理里建剧名文件夹


def test_merge_seasons_layout_partial(clock, tmdb, settings):
    show = "Breaking.Bad.S01-S03.2160p.NF.WEB-DL.DDP.5.1-GRP"
    staging = {show: [{"name": "Season 1", "is_dir": True}, {"name": "S02", "is_dir": True},
                      {"name": "Season 3", "is_dir": True}]}
    fake = merge_tree(["Season 01"], staging)
    fake.dirs[f"{ST}/{show}/Season 1"] = {}
    for sn, folder in ((1, "Season 1"), (2, "S02"), (3, "Season 3")):
        from app.openlist import Entry
        n = f"Breaking.Bad.S0{sn}E01.2160p.DDP.5.1.mkv"
        fake.dirs[f"{ST}/{show}/{folder}"] = {n: Entry(n, False, BIG)}
    fake.clock = clock
    data = scan(fake, tmdb, settings)
    it = data["items"][0]
    assert it["merge"]["seasons"] == [2, 3] and it["merge"]["skip"] == [1]
    res, stamps = run(fake, settings, clock, data["items"])
    moves = [s[2] for s in stamps if s[1] == "move"]
    assert moves == [(f"{ST}/{show}", f"{LT}/绝命毒师 (2008)", ["Season 02"]),
                     (f"{ST}/{show}", f"{LT}/绝命毒师 (2008)", ["Season 03"])]
    assert fake.names(f"{LT}/绝命毒师 (2008)") == ["Season 01", "Season 02", "Season 03"]
    assert fake.names(f"{LT}/绝命毒师 (2008)/Season 01") == []   # 片库原有的第一季没被动
    # 片库已有的第 1 季：待整理里那份原样保留（不改名、不移动、不删除）
    assert fake.names(f"{ST}/{show}") == ["Season 1"]
    assert fake.names(f"{ST}/{show}/Season 1") == ["Breaking.Bad.S01E01.2160p.DDP.5.1.mkv"]
    r = res["items"][0]
    assert r["status"] == "partial"
    assert any("Season 01 片库里已有" in n for n in r["notes"])
    assert not [s for s in stamps if s[1] == "mkdir"]
    t = [s[0] for s in stamps if s[1] == "move"]
    assert t[1] - t[0] >= settings.move_wait


def test_merge_season_appears_before_move(clock, tmdb, settings):
    fake = merge_tree(["Season 01"], flat_s2())
    fake.clock = clock
    data = scan(fake, tmdb, settings)
    fake.mkdir(f"{LT}/绝命毒师 (2008)/Season 02")         # 扫描后有人手动放进了第二季
    res, stamps = run(fake, settings, clock, data["items"])
    assert not [s for s in stamps if s[1] == "move"]
    assert res["items"][0]["status"] == "conflict"
    assert "Season 02" in fake.names(ST)                  # 改好名留在待整理


def test_demo_reply1988_same_season_conflict(fake, tmdb, settings):
    data = scan(fake, tmdb, settings)
    it = next(i for i in data["items"] if i["target_name"] == "请回答1988 (2015)")
    assert it["library_conflict"] and it["merge"]["seasons"] == []
