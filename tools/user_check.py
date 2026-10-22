"""模拟一个「零知识用户」走完核心任务。

这个脚本刻意**只用 README 里写给普通用户的那几条命令**，不碰任何高级参数
（除了模拟回车确认）。它要回答的问题是：

    「一个没读过 beets 文档、不知道什么是 albumartist 的人，
      能不能用这个工具把音乐文件夹整理好，并且能撤回？」

用法::

    python tools/user_check.py [--keep]

`--keep` 保留中间产物便于人工检查。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

# Windows 控制台默认不是 UTF-8，本脚本通篇中文，先把自己修好再说话。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import mediafile  # noqa: E402
from make_sample import build as build_sample  # noqa: E402

from discrate.cli import main as cli_main  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> None:
    RESULTS.append((ok, title, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {title}" + (f" —— {detail}" if detail else ""), flush=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeTTY:
    """让 CLI 以为自己在终端里，并自动按回车。"""

    def __init__(self) -> None:
        self._answers = iter([""] * 10)

    def __enter__(self):
        import builtins

        self._orig_isatty = sys.stdin.isatty if hasattr(sys.stdin, "isatty") else None
        self._orig_input = builtins.input
        try:
            sys.stdin.isatty = lambda: True  # type: ignore[method-assign]
        except (AttributeError, TypeError):
            pass
        builtins.input = lambda prompt="": next(self._answers)  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        import builtins

        builtins.input = self._orig_input  # type: ignore[assignment]
        try:
            sys.stdin.isatty = self._orig_isatty  # type: ignore[method-assign]
        except (AttributeError, TypeError):
            pass
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="普通用户可用性验证")
    parser.add_argument("--keep", action="store_true", help="保留中间产物")
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="discrate-usercheck-"))
    root = workdir / "我的音乐"
    print(f"工作目录：{workdir}", flush=True)

    print("\n[1] 用户手上有一个乱糟糟的音乐文件夹", flush=True)
    notes = build_sample(root)
    sources = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".mp3")
    before = {p: sha256(p) for p in sources}
    check(len(sources) == 12, "样本生成", f"{len(sources)} 个音频文件，{len(notes)} 类情况")

    print("\n[2] 用户先看一眼会发生什么（README 第 1 步：discrate plan）", flush=True)
    code = cli_main(["plan", str(root)])
    target = root / "整理结果"
    check(code == 0, "plan 正常退出")
    check(not target.exists(), "plan 不写任何文件", "输出目录未创建")

    print("\n[3] 用户决定执行，只按了回车（README 第 2 步：discrate 文件夹）", flush=True)
    with FakeTTY():
        code = cli_main([str(root)])
    check(code == 0, "交互确认后正常完成")

    print("\n[4] 检查结果对不对", flush=True)
    check(target.is_dir(), "生成了整理结果目录")

    expected = {
        "周杰伦/叶惠美/03 以父之名.mp3",
        "周杰伦/叶惠美/夜曲.mp3",
        "单曲/周杰伦/稻香.mp3",
        "单曲/未知歌手/晴天.mp3",
        "单曲/未知歌手/东风破.mp3",
        "AC_DC/Back_In_Black/01 Hells _Bells__.mp3",
    }
    produced = {str(p.relative_to(target)).replace("\\", "/") for p in target.rglob("*.mp3")}
    missing = expected - produced
    check(not missing, "关键文件都落到正确位置", f"共 {len(produced)} 个" + (f"，缺 {missing}" if missing else ""))

    check(
        not any("Non-Album" in p for p in produced),
        "没有出现 Non-Album 这种兜底目录",
        "上游默认会这样",
    )

    # 重复副本不应出现两份
    daoxiang = [p for p in produced if p.endswith("稻香.mp3")]
    check(len(daoxiang) == 1, "重复文件只保留一份", f"稻香：{daoxiang}")

    # 标签补全
    copy = target / "单曲" / "周杰伦" / "稻香.mp3"
    artist = mediafile.MediaFile(str(copy)).artist if copy.exists() else None
    check(artist == "周杰伦", "副本标签被补全", f"artist={artist!r}（原文件没有标签）")

    # 未知歌手不写进标签
    unknown = target / "单曲" / "未知歌手" / "晴天.mp3"
    unknown_artist = mediafile.MediaFile(str(unknown)).artist if unknown.exists() else "N/A"
    check(not unknown_artist, "占位符「未知歌手」不会被写进标签", f"artist={unknown_artist!r}")

    # 源文件完好
    changed = [p for p, digest in before.items() if not p.exists() or sha256(p) != digest]
    check(not changed, "源文件一个字节都没变", f"校验 {len(before)} 个文件")

    print("\n[5] 用户又跑了一次（增量场景）", flush=True)
    snapshot = {p: p.stat().st_size for p in target.rglob("*.mp3")}
    with FakeTTY():
        code = cli_main([str(root)])
    after = {p: p.stat().st_size for p in target.rglob("*.mp3")}
    check(code == 0, "第二次运行正常退出")
    check(after == snapshot, "没有产生重复副本", f"文件数 {len(snapshot)} → {len(after)}")
    check(
        not any("(2)" in p.name for p in after),
        "没有出现 (2) (3) 这类副本",
    )

    print("\n[6] 用户后悔了，撤回（README 第 3 步：discrate undo）", flush=True)
    with FakeTTY():
        code = cli_main(["undo", "--target", str(target)])
    check(code == 0, "undo 正常退出")
    leftovers = list(target.rglob("*.mp3"))
    check(not leftovers, "副本全部撤回", f"残留 {len(leftovers)} 个")
    still_there = all(p.exists() for p in before)
    check(still_there, "源文件仍在原处")

    print("\n[7] 撤回后还能重新整理", flush=True)
    code = cli_main(["plan", str(root)])
    check(code == 0, "重新 plan 正常")

    passed = sum(1 for ok, _t, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60, flush=True)
    print(f"普通用户核心任务验证：{passed}/{total} 项通过", flush=True)
    if passed != total:
        print("\n未通过项：", flush=True)
        for ok, title, detail in RESULTS:
            if not ok:
                print(f"  - {title} {detail}", flush=True)
    print("=" * 60, flush=True)

    if args.keep:
        print(f"\n产物保留在：{workdir}", flush=True)
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
