"""命令行入口。

默认路径只有一条命令：``discrate <文件夹>``。它会依次完成
扫描 → 出方案 → 确认 → 执行 → 报告，不需要用户理解任何配置项。
高级能力全部挂在可选参数上，不出现就是默认值。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__
from .decide import build_plan
from .execute import (
    JOURNAL_DIR,
    apply_plan,
    find_latest_journal,
    load_state,
    undo_journal,
)
from .lookup import LookupCache, MusicBrainzProvider, collect_suggestions
from .profile import DEFAULT_PROFILE, PROFILES, get_profile
from .report import render_plan, render_result, to_json, write_report
from .scan import scan_folder

__all__ = ["main"]

KNOWN_COMMANDS = {"tidy", "plan", "undo", "profiles", "version"}

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3


def _reconfigure_streams() -> None:
    """Windows 控制台默认 GBK，中文报告会直接 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _say(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))


def _ask(question: str) -> bool:
    try:
        answer = input(question).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        _say("")
        return False
    return answer in ("", "y", "yes", "是", "好")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discrate",
        description="把一堆命名混乱的音乐文件，整理成整齐、可回滚的音乐库。",
        epilog="直接运行 `discrate 你的音乐文件夹` 即可；源文件默认不会被改动。",
    )
    parser.add_argument("--version", action="version", version=f"discrate {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser, *, allow_exec: bool) -> None:
        p.add_argument("folder", help="要整理的文件夹")
        p.add_argument(
            "--profile",
            default=DEFAULT_PROFILE,
            choices=sorted(PROFILES),
            help=f"整理档位（默认 {DEFAULT_PROFILE}）："
            + "；".join(f"{k}={v.summary}" for k, v in PROFILES.items()),
        )
        p.add_argument("--output", metavar="DIR", help="输出目录（默认 <文件夹>/整理结果）")
        p.add_argument(
            "--mode",
            choices=("copy", "move"),
            help="覆盖档位的处理方式：copy 复制、move 移动（默认跟档位）",
        )
        p.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
        p.add_argument("-v", "--verbose", action="store_true", help="显示每条判定的理由")
        p.add_argument("--max-files", type=int, default=None, help="本次最多处理多少个文件")
        p.add_argument("--no-dedupe", action="store_true", help="不判定重复，全部按文件整理")
        p.add_argument("--no-tag-write", action="store_true", help="只整理文件，不补写标签")
        p.add_argument(
            "--lookup",
            action="store_true",
            help="联网查询元数据（默认关闭）。结果只作为建议展示，绝不自动写盘",
        )
        p.add_argument(
            "--lookup-limit",
            type=int,
            default=20,
            metavar="N",
            help="最多为多少个文件发起在线查询（默认 20，受 1 次/秒限速约束）",
        )
        if allow_exec:
            p.add_argument("-y", "--yes", action="store_true", help="跳过确认，直接执行")
            p.add_argument("--dry-run", action="store_true", help="只出方案，不执行")
            p.add_argument(
                "--no-report", action="store_true", help="不在输出目录写整理报告"
            )

    tidy = sub.add_parser("tidy", help="扫描并整理（默认命令）")
    add_common(tidy, allow_exec=True)

    plan = sub.add_parser("plan", help="只生成方案，不做任何修改")
    add_common(plan, allow_exec=False)

    undo = sub.add_parser("undo", help="撤销上一次整理")
    undo.add_argument("--target", metavar="DIR", help="整理时的输出目录")
    undo.add_argument("--journal", metavar="FILE", help="直接指定记录文件")
    undo.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    undo.add_argument("--json", action="store_true", help="输出 JSON")

    sub.add_parser("profiles", help="列出整理档位")
    sub.add_parser("version", help="显示版本")
    return parser


def _prepare_plan(args) -> tuple[object, object]:
    profile = get_profile(args.profile)
    if getattr(args, "mode", None):
        profile = replace(profile, mode=args.mode)
    root = Path(args.folder).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"这不是文件夹：{root}")
    if getattr(args, "output", None):
        target = Path(args.output).expanduser().resolve()
    else:
        target = root / "整理结果"
    scan = scan_folder(root, max_files=args.max_files, exclude=[target])

    hints = None
    if getattr(args, "lookup", False):
        # 只查「不是全部字段都来自标签」的文件 —— 标签齐全的不需要联网。
        hints = collect_suggestions(
            scan.tracks,
            MusicBrainzProvider(),
            LookupCache(target / JOURNAL_DIR / "lookup-cache.json"),
            limit=getattr(args, "lookup_limit", 20),
            should_lookup=lambda t: (
                t.error is None
                and bool(str(t.fields.get("title") or "").strip())
                and (
                    t.sources.get("title") != "tag"
                    or t.sources.get("artist") != "tag"
                )
            ),
        )

    plan = build_plan(
        scan,
        profile,
        target_root=target,
        previous=load_state(target),
        dedupe=not getattr(args, "no_dedupe", False),
        suggestions=hints,
    )
    return scan, plan


def _cmd_plan(args) -> int:
    scan, plan = _prepare_plan(args)
    if args.json:
        _say(json.dumps(to_json(plan), ensure_ascii=False, indent=2))
    else:
        _say(render_plan(plan, verbose=args.verbose))
        if scan.ignored:
            _say(f"  另有 {len(scan.ignored)} 个非音频文件被忽略。")
            _say("")
    return EXIT_OK


def _fmt_span(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}秒"
    return f"{total // 60}分{total % 60:02d}秒"


def _cmd_tidy(args) -> int:
    scan, plan = _prepare_plan(args)
    if args.json:
        # --json 本身是只读语义：给机器可读的方案。要执行必须同时给 --yes，
        # 不能因为加了 --json 就顺带把文件写了。
        result = None
        if args.yes and not args.dry_run and plan.writes:
            result = apply_plan(plan, write_meta=not args.no_tag_write)
        _say(json.dumps(to_json(plan, result), ensure_ascii=False, indent=2))
        return EXIT_OK if (result is None or result.ok) else EXIT_PARTIAL

    _say(render_plan(plan, verbose=args.verbose))
    if scan.ignored:
        _say(f"  另有 {len(scan.ignored)} 个非音频文件被忽略。")
        _say("")

    if not plan.writes:
        return EXIT_OK

    if args.dry_run:
        _say("  （--dry-run：以上只是方案，没有修改任何文件）")
        _say("")
        return EXIT_OK

    if not args.yes:
        if not sys.stdin.isatty():
            _say("  当前不是交互终端，请加 --yes 明确同意执行。")
            return EXIT_USAGE
        mode_text = "复制" if plan.profile.mode == "copy" else "移动"
        if not _ask(f"  即将{mode_text} {len(plan.writes)} 个文件，继续？[Y/n] "):
            _say("  已取消，没有修改任何文件。")
            return EXIT_CANCELLED

    total = len(plan.writes)
    show_progress = sys.stdout.isatty()
    started = time.monotonic()
    state = {"last": 0, "ts": 0.0}

    def progress(done: int, _total: int, decision) -> None:
        if not show_progress:
            return
        now = time.monotonic()
        # 每 2% 或每 0.15 秒刷一次，避免几百首时疯狂重绘。
        if done != _total and done - state["last"] < max(1, _total // 50):
            return
        if done != _total and now - state["ts"] < 0.15:
            return
        state["last"] = done
        state["ts"] = now
        elapsed = now - started
        rate = done / elapsed if elapsed > 0 else 0
        remain = (_total - done) / rate if rate > 0 else 0
        name = decision.track.path.name
        line = (
            f"\r  [{done}/{_total}] {done * 100 // _total}%"
            f" · 已用 {_fmt_span(elapsed)} · 剩约 {_fmt_span(remain)}"
            f" · {name[:26]:<26}"
        )
        sys.stdout.write(line)
        sys.stdout.flush()

    result = apply_plan(plan, progress=progress, write_meta=not args.no_tag_write)
    if show_progress and total:
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()

    report_md = None
    if result.done and not args.no_report:
        report_md, _json_path = write_report(plan, result)

    _say(render_result(plan, result, report_path=report_md))
    return EXIT_OK if result.ok else EXIT_PARTIAL


def _cmd_undo(args) -> int:
    if args.journal:
        journal = Path(args.journal).expanduser()
    elif args.target:
        journal = find_latest_journal(Path(args.target).expanduser())
        if journal is None:
            _say(f"在 {args.target} 下没找到整理记录。")
            return EXIT_USAGE
    else:
        _say("请用 --target 指定整理时的输出目录，或用 --journal 指定记录文件。")
        return EXIT_USAGE

    if not args.yes:
        if not sys.stdin.isatty():
            _say("当前不是交互终端，请加 --yes 确认撤销。")
            return EXIT_USAGE
        if not _ask(f"  将撤销 {journal.name} 记录的操作，继续？[Y/n] "):
            _say("  已取消。")
            return EXIT_CANCELLED

    result = undo_journal(journal)
    if args.json:
        _say(
            json.dumps(
                {
                    "journal": str(journal),
                    "removed": result.removed,
                    "moved_back": result.moved_back,
                    "kept": [{"path": str(p), "reason": r} for p, r in result.kept],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    _say("")
    _say(f"  撤销完成：{result.summary}")
    if result.kept:
        _say("  以下文件没有动：")
        for path, reason in result.kept:
            _say(f"    {path}")
            _say(f"      - {reason}")
    _say("")
    return EXIT_OK


def _cmd_profiles(_args) -> int:
    _say("")
    _say("  可用档位：")
    for name, profile in PROFILES.items():
        mark = "（默认）" if name == DEFAULT_PROFILE else ""
        _say(f"    {name:<8}{mark} {profile.summary}")
        _say(f"             专辑：{profile.album_format}")
        _say(f"             单曲：{profile.single_format}")
    _say("")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _reconfigure_streams()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in KNOWN_COMMANDS and not raw[0].startswith("-"):
        raw.insert(0, "tidy")

    parser = _build_parser()
    if not raw:
        parser.print_help()
        return EXIT_USAGE
    args = parser.parse_args(raw)

    if args.command in (None, "version"):
        _say(f"discrate {__version__}")
        return EXIT_OK
    if args.command == "profiles":
        return _cmd_profiles(args)
    if args.command == "undo":
        return _cmd_undo(args)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        return _cmd_tidy(args)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # 路径写错是普通用户最常见的失误，给一句话而不是 traceback。
        _say(f"  {exc}")
        return EXIT_USAGE
    except PermissionError as exc:
        _say(f"  没有权限访问：{exc.filename or exc}")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
