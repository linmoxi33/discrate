"""测试夹具。

音频一律用 ffmpeg 现场生成，不用预置的二进制文件 —— 预置文件一旦缺失，
``skipif`` 会让一整片用例静默跳过并报绿。缺 ffmpeg 就直接失败。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import mediafile
import pytest


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "测试需要 ffmpeg 生成音频样本；未找到 ffmpeg，不能跳过，请先安装"
        )
    return exe


@pytest.fixture(scope="session")
def tones(tmp_path_factory, ffmpeg):
    """按 (频率, 时长) 缓存音频字节，避免每个用例都起一次 ffmpeg。"""
    root = tmp_path_factory.mktemp("tones")
    cache: dict[tuple[int, float], bytes] = {}

    def get(freq: int = 440, seconds: float = 0.4) -> bytes:
        key = (freq, seconds)
        if key not in cache:
            target = root / f"t{freq}_{int(seconds * 1000)}.mp3"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={freq}:sample_rate=8000:duration={seconds}",
                    "-ac",
                    "1",
                    "-q:a",
                    "9",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
            cache[key] = target.read_bytes()
        return cache[key]

    return get


@pytest.fixture
def make_track(tmp_path, tones):
    """在 tmp_path 里造一个音频文件，可选写入标签。"""

    def _make(
        name: str,
        *,
        freq: int = 440,
        seconds: float = 0.4,
        tags: dict | None = None,
    ) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tones(freq, seconds))
        if tags:
            mf = mediafile.MediaFile(str(path))
            for key, value in tags.items():
                setattr(mf, key, value)
            mf.save()
        return path

    return _make


@pytest.fixture
def sample_dir(tmp_path, make_track):
    """一个最小但覆盖多分支的「用户文件夹」。"""
    root = tmp_path / "音乐"
    root.mkdir()
    make_track("音乐/01.mp3", freq=300)
    make_track("音乐/02 - 晴天.mp3", freq=320)
    make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
    (root / "稻香 - 副本.mp3").write_bytes((root / "周杰伦 - 稻香.mp3").read_bytes())
    make_track(
        "音乐/完整标签.mp3",
        freq=360,
        tags={
            "title": "以父之名",
            "artist": "周杰伦",
            "albumartist": "周杰伦",
            "album": "叶惠美",
            "track": 3,
            "year": 2003,
        },
    )
    (root / "空文件.mp3").write_bytes(b"")
    (root / "readme.txt").write_text("说明", encoding="utf-8")
    make_track("音乐/周杰伦 - 叶惠美/夜曲.mp3", freq=420)
    make_track("音乐/周杰伦 - 叶惠美/03 以父之名.mp3", freq=440)
    return root
