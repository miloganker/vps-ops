import pytest

from app import naming as N

# (原文件名, 中文名, 年份, 期望新文件名) —— 来自实际整理结果
MOVIES = [
    ("Raging.Bull.1980.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT.mkv", "愤怒的公牛", 1980,
     "愤怒的公牛 (1980) 2160p.DTS-HD.MA.5.1.mkv"),
    ("Seven.Samurai.1954.2160p.UHD.Blu-ray.Remux.DV.HDR.HEVC.FLAC.1.0-CiNEPHiLES.mkv", "七武士", 1954,
     "七武士 (1954) 2160p.DV.HDR.FLAC.1.0.mkv"),
    ("Dune Part Two 2024 V2 BluRay Remux UHD 2160p Atmos TrueHD7.1-DreamHD.mkv", "沙丘2", 2024,
     "沙丘2 (2024) 2160p.Atmos.TrueHD.7.1.mkv"),
    ("Echoes.Of.The.Rainbow.2010.BluRay.1080p.DTS.2Audio.x264-DreamHD.mkv", "岁月神偷", 2010,
     "岁月神偷 (2010) 1080p.DTS.mkv"),
    ("Lawrence.of.Arabia.1962.2160p.UHD.BluRay.Remux.HDR.HEVC.Atmos.SHD13.mkv", "阿拉伯的劳伦斯", 1962,
     "阿拉伯的劳伦斯 (1962) 2160p.HDR.Atmos.mkv"),
    ("Ran.1985.JAPANESE.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT.mkv", "乱", 1985,
     "乱 (1985) 2160p.DTS-HD.MA.5.1.mkv"),
    ("Eat.Drink.Man.Woman.1994.1080p.BluRay.Remux.AVC.FLAC.2.0-SONYHD.mkv", "饮食男女", 1994,
     "饮食男女 (1994) 1080p.FLAC.2.0.mkv"),
    ("2001 A Space Odyssey 1968 USA BluRay Remux UHD DoVi HDR10 2160p DTS-HDMA5.1-DreamHD.mkv", "2001太空漫游", 1968,
     "2001太空漫游 (1968) 2160p.DV.HDR.DTS-HD.MA.5.1.mkv"),
    ("In the Mood for Love 2000 UHD BluRay REMUX 2160p HEVC Atmos TrueHD7.1 3Audio-DreamHD.mkv", "花样年华", 2000,
     "花样年华 (2000) 2160p.Atmos.TrueHD.7.1.mkv"),
    ("八仙.All.Wishes.Come.True.2026.60FPS.2160p.WEB-DL.H265.10bit.DV.DTS.5.1-GPTHD.mp4", "八仙！", 2026,
     "八仙！ (2026) 60FPS.2160p.DV.DTS.5.1.mp4"),
    ("Yi Yi 2000 USA CC Bluray REMUX UHD SDR DTS-HDMA5.1-DreamHD.mkv", "一一", 2000,
     "一一 (2000) 2160p.SDR.DTS-HD.MA.5.1.mkv"),
]


@pytest.mark.parametrize("old,title,year,new", MOVIES)
def test_movie_names(old, title, year, new):
    tags = N.parse_tags(old)
    ext = N.split_ext(old)[1]
    assert N.movie_file_name(title, year, tags, ext) == new


@pytest.mark.parametrize("old,en,year", [
    ("Raging.Bull.1980.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT.mkv", "Raging Bull", 1980),
    ("Dune Part Two 2024 V2 BluRay Remux UHD 2160p Atmos TrueHD7.1-DreamHD.mkv", "Dune Part Two", 2024),
    ("2001 A Space Odyssey 1968 USA BluRay Remux UHD DoVi HDR10 2160p DTS-HDMA5.1-DreamHD.mkv", "2001 A Space Odyssey", 1968),
    ("Ran.1985.JAPANESE.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT", "Ran", 1985),
    ("Lawrence.of.Arabia.1962.2160p.UHD.BluRay.Remux.HDR.HEVC.Atmos.SHD13.mkv", "Lawrence of Arabia", 1962),
])
def test_parse_movie_title_year(old, en, year):
    p = N.parse_name(old)
    assert (p.en_title, p.year) == (en, year)


def test_parse_messy_chinese_folders():
    p = N.parse_name("【高清影视之家发布 www.SSDSSE.com】2001太空漫游[HDR+杜比视界双版本][简繁英字幕]."
                     "1968.USA.BluRay.Remux.UHD.DoVi.HDR10.2160p.DTS-HDMA5.1-DreamHD")
    assert (p.cn_title, p.year) == ("2001太空漫游", 1968)
    p = N.parse_name("【更多高清影视请访问 www.5BT0.com】八仙！[60帧率版本][杜比视界版本][国语音轨+中文字幕]."
                     "2026.60FPS.2160p.WEB-DL.H265.10bit.DV.DTS.5.1-GPTHD")
    assert (p.cn_title, p.year) == ("八仙！", 2026)
    p = N.parse_name("【高清影视之家发布 www.SSDSSE.com】岁月神偷[国粤多音轨].Echoes.Of.The.Rainbow.2010.BluRay.1080p.DTS.2Audio.x264-DreamHD")
    assert (p.cn_title, p.en_title, p.year) == ("岁月神偷", "Echoes Of The Rainbow", 2010)


TV_FOLDER = ("【高清剧集网 www.BTHDTV.com】绝命毒师 第一季[全7集][简繁英字幕]."
             "Breaking.Bad.S01.2160p.NF.WEB-DL.DDP.5.1.H.265-BlackTV")


def test_parse_tv_folder():
    p = N.parse_name(TV_FOLDER)
    assert p.cn_title == "绝命毒师"
    assert p.en_title == "Breaking Bad"
    assert p.season == 1
    assert p.year is None
    p = N.parse_name("【高清剧集网 www.BTHDTV.com】请回答1988[全20集][简繁英字幕].Reply.1988.2015.Amazon.WEB-DL.1080p.H264.DDP-Xiaomi")
    assert (p.cn_title, p.en_title, p.year) == ("请回答1988", "Reply 1988", 2015)


def test_breaking_bad_episode():
    old = "Breaking.Bad.S01E01.Pilot.2160p.NF.WEB-DL.DDP.5.1.H.265-BlackTV.mkv"
    p = N.parse_name(old)
    assert (p.en_title, p.season, p.episode) == ("Breaking Bad", 1, 1)
    assert N.episode_file_name("绝命毒师", 2008, p.season, p.episode, N.parse_tags(old), "mkv") == \
        "绝命毒师 (2008) S01E01 2160p.DDP.5.1.mkv"


def test_reply_1988_episode_without_season():
    old = "Reply.1988.E01.2015.Amazon.WEB-DL.1080p.H264.DDP-Xiaomi.mkv"
    p = N.parse_name(old)
    assert (p.en_title, p.year, p.season, p.episode) == ("Reply 1988", 2015, None, 1)
    assert N.episode_file_name("请回答1988", 2015, p.season or 1, p.episode, N.parse_tags(old), "mkv") == \
        "请回答1988 (2015) S01E01 1080p.DDP.mkv"


@pytest.mark.parametrize("name,ep", [
    ("Show.S02E10.1080p.WEB-DL.AAC2.0.H.264.mkv", (2, 10)),
    ("Show.S01E01E02.1080p.mkv", (1, 1)),
    ("某剧.第05集.1080p.mkv", (None, 5)),
    ("Show.EP12.2160p.mkv", (None, 12)),
])
def test_episode_variants(name, ep):
    p = N.parse_name(name)
    assert (p.season, p.episode) == ep


def test_tags_only_present():
    assert N.parse_tags("Movie.2019.1080p.WEB-DL.H264-GRP.mkv").format() == "1080p"
    assert N.parse_tags("Movie.2019.720p.BluRay.x264.AC3-GRP.mkv").format() == "720p.AC3"
    assert N.parse_tags("Movie.2019.2160p.WEB-DL.DDP5.1.Atmos.HDR10+.mkv").format() == "2160p.HDR10+.Atmos.DDP.5.1"
    assert N.parse_tags("Movie.2019.1080p.AAC2.0.mkv").format() == "1080p.AAC.2.0"
    # H.265 不能被当成声道，DD 不带声道时不认（避免误伤组名）
    assert N.parse_tags("Movie.2019.1080p.H.265-DD.mkv").format() == "1080p"
    assert N.parse_tags("Movie.2019.1080p.DD+5.1.mkv").format() == "1080p.DDP.5.1"


def test_ads():
    mb = 1024 * 1024
    assert N.is_ad("【更多高清剧集下载请访问 www.BTHDTV.com】【更多剧集打包下载请访问 www.BTHDTV.com】.mkv", 636976)
    assert N.is_ad("random.mkv", 2 * mb)                       # 很小的视频
    assert not N.is_ad("Breaking.Bad.S01E01.mkv", 5_000 * mb)
    assert not N.is_ad("Movie.chs.srt", 90_000)                # 字幕本来就小，不能按大小判断
    assert N.is_ad("【更多无水印高清电影请访问 www.BBQDDQ.com】.png", 1_300_000)


@pytest.mark.parametrize("name,suffix", [
    ("Movie.2010.chs.srt", ".chs"),
    ("Movie.2010.zh-cn.forced.ass", ".zh-cn.forced"),
    ("Movie.2010.eng.sup", ".eng"),
    ("Lawrence.of.Arabia.1962.2160p.UHD.BluRay.X265-IAMABLE.srt", ""),
])
def test_subtitle_suffix(name, suffix):
    assert N.subtitle_suffix(name) == suffix


def test_season_folder_names():
    assert N.is_season_folder_name("Season 1") == 1
    assert N.is_season_folder_name("S02") == 2
    assert N.is_season_folder_name("第三季") == 3
    assert N.is_season_folder_name("第十二季") == 12
    assert N.is_season_folder_name("Extras") is None
    assert N.season_folder(1) == "Season 01"


def test_illegal_chars():
    assert N.folder_name("星球大战：新希望/特别版", 1977) == "星球大战：新希望 特别版 (1977)"
    assert N.folder_name('A: B?', 2000) == "A B (2000)"


def test_multi_season_folder_title():
    assert N.parse_name("Breaking.Bad.S01-S03.2160p.NF.WEB-DL.DDP.5.1-GRP").en_title == "Breaking Bad"
