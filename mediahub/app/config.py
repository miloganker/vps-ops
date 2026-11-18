"""所有配置都来自环境变量（.env），没有任何写死在服务器上的东西。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _b(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    openlist_url: str = "http://openlist:5244"
    openlist_user: str = ""
    openlist_password: str = ""
    tmdb_api_key: str = ""
    staging_movies: str = "/guangdong/娱乐/影视/待整理/电影"
    staging_tv: str = "/guangdong/娱乐/影视/待整理/电视剧"
    # 名字看不出类型的链接先下到这里（每个任务一个子文件夹），下完看文件再移到电影/电视剧
    staging_unsorted: str = "/guangdong/娱乐/影视/待整理/未分类"
    library_movies: str = "/guangdong/娱乐/影视/电影"
    library_tv: str = "/guangdong/娱乐/影视/电视剧"
    rename_interval: float = 2.0
    move_wait: float = 20.0
    request_timeout: float = 30.0
    ad_max_size_mb: float = 5.0
    title_source: str = "tmdb"  # tmdb | folder
    move_retry_on_busy: bool = True
    app_password: str = ""
    session_secret: str = ""
    data_dir: str = "/data"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    fake_openlist: str = ""  # 仅测试/演示：指向一个 JSON 目录树，使用内存假客户端
    fake_tmdb: bool = False
    mediahub_internal_token: str = ""
    public_url: str = "https://media.sumax.duckdns.org:8443"
    download_poll_interval: float = 90.0
    download_refresh_wait: float = 30.0
    # 下完 + 分类 + 扫描之后，满足所有安全条件就自动改名并移进正式片库（可在网页/机器人里临时关掉）
    auto_organize: bool = True
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            openlist_url=e("OPENLIST_URL", "http://openlist:5244").rstrip("/"),
            openlist_user=e("OPENLIST_USER", ""),
            openlist_password=e("OPENLIST_PASSWORD", ""),
            tmdb_api_key=e("TMDB_API_KEY", ""),
            staging_movies=e("STAGING_MOVIES", cls.staging_movies).rstrip("/"),
            staging_tv=e("STAGING_TV", cls.staging_tv).rstrip("/"),
            staging_unsorted=e("STAGING_UNSORTED", cls.staging_unsorted).rstrip("/"),
            library_movies=e("LIBRARY_MOVIES", cls.library_movies).rstrip("/"),
            library_tv=e("LIBRARY_TV", cls.library_tv).rstrip("/"),
            rename_interval=_f("RENAME_INTERVAL", 2.0),
            move_wait=_f("MOVE_WAIT", 20.0),
            request_timeout=_f("REQUEST_TIMEOUT", 30.0),
            ad_max_size_mb=_f("AD_MAX_SIZE_MB", 5.0),
            title_source=e("TITLE_SOURCE", "tmdb").strip().lower() or "tmdb",
            move_retry_on_busy=_b("MOVE_RETRY_ON_BUSY", True),
            app_password=e("APP_PASSWORD", ""),
            session_secret=e("SESSION_SECRET", ""),
            data_dir=e("DATA_DIR", "/data"),
            telegram_token=e("TELEGRAM_TOKEN", ""),
            telegram_chat_id=e("TELEGRAM_CHAT_ID", ""),
            fake_openlist=e("FAKE_OPENLIST", ""),
            fake_tmdb=_b("FAKE_TMDB", False),
            mediahub_internal_token=e("MEDIAHUB_INTERNAL_TOKEN", ""),
            public_url=e("MEDIAHUB_PUBLIC_URL", "https://media.sumax.duckdns.org:8443").rstrip("/"),
            download_poll_interval=_f("DOWNLOAD_POLL_INTERVAL", 90.0),
            download_refresh_wait=_f("DOWNLOAD_REFRESH_WAIT", 30.0),
            auto_organize=_b("AUTO_ORGANIZE", True),
        )
