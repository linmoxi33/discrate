"""生成一个「典型用户下载文件夹」样本，用来验证 discrate 的真实表现。

这个样本刻意做得难看：命名混乱、标签缺失、有重复副本、有坏文件、有非音频
杂物、有中文/日文/特殊字符、有超长标题。它对应的是普通人真实的音乐文件夹，
而不是整理好之后的样子。

用法::

    python examples/make_sample.py --out 样本文件夹
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import mediafile

SR = 44100


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("需要 ffmpeg 才能生成样本音频（本机未找到）")
    return exe


def make_tone(path: Path, seconds: float, freq: int = 440) -> Path:
    """生成一段确定性的音频。时长与频率决定内容，便于制造「同歌不同码率」。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq}:sample_rate={SR}:duration={seconds}",
        "-ac",
        "1",
        "-q:a",
        "9",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def make_cover(path: Path, color: str = "gray") -> Path:
    """生成一张纯色封面图，用来验证封面归位。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=200x200",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def tag(path: Path, **fields) -> Path:
    mf = mediafile.MediaFile(str(path))
    for key, value in fields.items():
        setattr(mf, key, value)
    mf.save()
    return path


def build(root: Path) -> list[str]:
    """返回生成的说明条目，供人工核对。"""
    root.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    # --- 无标签、命名混乱的散装文件 ---
    make_tone(root / "01.mp3", 1.0, 300)
    notes.append("01.mp3 —— 无标签、文件名只有数字，应进「需要你确认」")

    make_tone(root / "02 - 晴天.mp3", 1.2, 320)
    notes.append("02 - 晴天.mp3 —— 无标签，曲名可从文件名取出，歌手未知")

    daoxiang = make_tone(root / "周杰伦 - 稻香.mp3", 1.5, 340)
    notes.append("周杰伦 - 稻香.mp3 —— 无标签，歌手+曲名可从文件名取出")

    shutil.copy2(daoxiang, root / "稻香 - 副本.mp3")
    notes.append("稻香 - 副本.mp3 —— 与上一条逐字节相同，应判为重复")

    # --- 标签完整 ---
    full = make_tone(root / "完整标签.mp3", 2.0, 360)
    tag(
        full,
        title="以父之名",
        artist="周杰伦",
        albumartist="周杰伦",
        album="叶惠美",
        track=3,
        year=2003,
    )
    notes.append("完整标签.mp3 —— 标签齐全，应进 周杰伦/叶惠美/03 以父之名.mp3")

    # --- 特殊字符与超长 ---
    special = make_tone(root / "AC-DC - Back In Black.mp3", 1.8, 380)
    tag(special, artist="AC/DC", album="Back:In/Black", title='Hells "Bells"?', track=1)
    notes.append("AC-DC - Back In Black.mp3 —— 标签含 / : \" ?，落盘必须合法化")

    long_title = "这是一个特别特别长的歌曲标题" * 8
    longf = make_tone(root / "超长标题.mp3", 1.6, 400)
    tag(longf, artist="某位歌手", album="某张专辑", title=long_title, track=1)
    notes.append("超长标题.mp3 —— 标题 200+ 字符，文件名需要截断")

    # --- 坏文件与非音频 ---
    (root / "空文件.mp3").write_bytes(b"")
    notes.append("空文件.mp3 —— 0 字节，应判为无法读取")
    (root / "readme.txt").write_text("下载说明", encoding="utf-8")
    # 根目录混放一张封面：它不属于任何一个专辑，不该被贴到任何专辑上
    make_cover(root / "cover.jpg", "black")
    notes.append("根目录 cover.jpg —— 归属不明，不应被分给任何专辑")
    (root / ".DS_Store").write_bytes(b"\x00\x01")
    notes.append("readme.txt / .DS_Store —— 非音频，应被忽略")

    # --- 靠目录名推断 ---
    album_dir = root / "周杰伦 - 叶惠美"
    make_tone(album_dir / "夜曲.mp3", 1.4, 420)
    make_tone(album_dir / "03 以父之名.mp3", 1.4, 440)
    make_cover(album_dir / "cover.jpg", "dimgray")
    notes.append("周杰伦 - 叶惠美/ —— 目录名可推出歌手+专辑，文件名可推出轨号+曲名")

    bare_album = root / "叶惠美"
    make_tone(bare_album / "东风破.mp3", 1.3, 460)
    make_cover(bare_album / "folder.jpg", "silver")
    notes.append("叶惠美/东风破.mp3 —— 只有专辑名，歌手应落到「未知歌手」")

    # --- 同歌不同码率（体积不同）---
    alt = root / "同名不同码率"
    make_tone(alt / "晴天.mp3", 3.0, 320)
    notes.append("同名不同码率/晴天.mp3 —— 与 02 - 晴天.mp3 同歌但更大，应保留这份")

    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 discrate 验证用样本")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--force", action="store_true", help="目录非空时清空重建")
    args = parser.parse_args(argv)

    root = Path(args.out).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        if not args.force:
            print(f"目录非空：{root}（加 --force 清空重建）", file=sys.stderr)
            return 2
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    notes = build(root)
    print(f"样本已生成：{root}")
    for note in notes:
        print("  · " + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
