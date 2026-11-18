import json
import os

import pytest

from app.config import Settings
from app.fake_openlist import FakeOpenList
from app.tmdb import demo_fake_tmdb

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "..", "fixtures", "demo_tree.json")


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


@pytest.fixture
def settings():
    return Settings(rename_interval=2, move_wait=20, data_dir="/tmp/mr-test")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(clock):
    with open(FIXTURE, encoding="utf-8") as f:
        c = FakeOpenList(json.load(f))
    c.clock = clock
    c.move_busy_seconds = 15   # 115 需要约 15 秒处理完一次移动
    return c


@pytest.fixture
def tmdb():
    return demo_fake_tmdb()
