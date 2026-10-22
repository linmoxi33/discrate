"""结果展示。

面向普通用户的三条规则：

1. **先给结论，再给明细**。顶部一行数字回答「到底做了什么」。
2. **每条都能解释**。为什么改名、为什么算重复、为什么没处理。
3. **一定告诉用户怎么撤销**。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .decide import Decision, Plan
from .execute import ApplyResult

__all__ = ["display_width", "render_plan", "render_result", "short_path", "to_json"]

_WIDE = {"W", "F"}


def display_width(text: str) -> int:
    """终端显示宽度：中日韩全角字符占 2 列。"""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
    return total


def _truncate(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def short_path(path: Path | str, width: int) -> str:
    """把长路径压到给定宽度内，保留最有辨识度的尾部。

    普通用户需要看到「文件去哪了」，但一条 130 列的绝对路径会把整个
    版面撑破。这里从尾部往前保留尽可能多的层级。
    """
    text = str(path)
    if display_width(text) <= width:
        return text
    parts = Path(text).parts
    for count in range(2, len(parts) + 1):
        candidate = "…" + str(Path(*parts[-count:]))
        if display_width(candidate) <= width:
            return candidate
    return _truncate(text, width)


def _human_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _bar(width: int = 62, char: str = "─") -> str:
    return char * width


def _header(title: str, subtitle: str = "", width: int = 62) -> list[str]:
    lines = [f"  {title}"]
    if subtitle:
        lines.append(f"  {subtitle}")
    lines.append("  " + _bar(width))
    return lines


def _table(decisions: list[Decision], plan: Plan, verbose: bool, width: int) -> list[str]:
    if not decisions:
        return []
    lines = []
    col_no = 3
    col_src = max(18, int(width * 0.30))
    col_dst = max(24, width - col_no - col_src - 2)
    lines.append(
        "  "
        + _pad("#", col_no)
        + _pad("原文件", col_src)
        + "→ "
        + _pad("整理到", col_dst)
    )
    for index, decision in enumerate(decisions, start=1):
        src = str(decision.track.rel)
        if decision.target is not None:
            try:
                dst = str(decision.target.relative_to(plan.target_root))
            except ValueError:
                dst = str(decision.target)
        else:
            dst = "（未整理）"
        lines.append(
            "  "
            + _pad(str(index), col_no)
            + _pad(_truncate(src, col_src - 1), col_src)
            + "→ "
            + _pad(_truncate(dst, col_dst - 1), col_dst)
        )
        if verbose:
            lines.extend("      · " + reason.message for reason in decision.reasons)
    return lines


def _problem_list(title: str, decisions: list[Decision]) -> list[str]:
    if not decisions:
        return []
    lines = [f"  {title}"]
    for decision in decisions:
        lines.append(f"    {decision.track.rel}")
        lines.extend(f"      - {reason.message}" for reason in decision.reasons)
    return lines


def render_plan(plan: Plan, *, verbose: bool = False, width: int = 96) -> str:
    stats = plan.stats()
    mode_label = "复制（源文件不动）" if plan.profile.mode == "copy" else "原地移动"
    lines = _header(
        "整理方案",
        f"档位 {plan.profile.name} · {mode_label}",
        width=min(width, 78),
    )
    lines.append(f"  输出到 {short_path(plan.target_root, max(20, width - 10))}")

    lines.append(
        "  扫描到 {total} 个音频文件：整理 {writes}，重复 {dup}，"
        "无法处理 {dead}，建议复核 {review}".format(
            total=stats["total"],
            writes=stats["copy"] + stats["move"],
            dup=stats["duplicate"],
            dead=stats["review"] + stats["reject"],
            review=stats["needs_review"],
        )
    )
    lines.append(f"  预计输出 {_human_bytes(plan.bytes_out)}")
    lines.append("")

    writes = plan.writes
    if writes:
        lines.extend(_table(writes, plan, verbose, width))
        lines.append("")

    if plan.review:
        lines.extend(
            _problem_list(
                "建议复核（仍会整理，但信息不足，建议确认后再执行）", plan.review
            )
        )
        lines.append("")

    dead = plan.by_action("review")
    if dead:
        lines.extend(_problem_list("无法整理（没有可用的曲名信息）", dead))
        lines.append("")

    dups = plan.by_action("duplicate")
    if dups:
        lines.extend(_problem_list("判定为重复的（不复制）", dups))
        lines.append("")

    already = [d for d in plan.decisions if "already-done" in d.reason_codes()]
    if already:
        lines.append(f"  上次已整理过、本次跳过 {len(already)} 个。")
        lines.append("")

    rejects = [
        d
        for d in plan.by_action("reject") + plan.by_action("skip")
        if "already-done" not in d.reason_codes()
    ]
    if rejects:
        lines.extend(_problem_list("跳过或无法读取的", rejects))
        lines.append("")

    if not writes:
        lines.append("  没有需要整理的文件。")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_result(plan: Plan, result: ApplyResult) -> str:
    lines = _header("整理完成")
    lines.append(f"  输出目录 {short_path(plan.target_root, 60)}")
    lines.append(
        f"  整理成功 {len(result.done)} 个 · 失败 {len(result.failed)} 个 · 输出 {_human_bytes(plan.bytes_out)}"
    )
    if plan.profile.mode == "copy":
        lines.append("  源文件保持原样，未被修改。")
    lines.append("")

    if result.failed:
        lines.append("  失败清单")
        for decision, error in result.failed:
            lines.append(f"    {decision.track.rel}")
            lines.append(f"      - {error}")
        lines.append("")

    if plan.review:
        lines.append(
            f"  另有 {len(plan.review)} 个文件建议复核（已按推断整理，详见方案）。"
        )
        lines.append("")

    lines.append("  想撤回这次整理？运行：")
    lines.append(f"    discrate undo --target \"{plan.target_root}\"")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(plan: Plan, result: ApplyResult | None = None) -> dict:
    payload = {
        "version": 1,
        "source": str(plan.root),
        "target": str(plan.target_root),
        "profile": plan.profile.name,
        "mode": plan.profile.mode,
        "stats": plan.stats(),
        "bytes_out": plan.bytes_out,
        "decisions": [
            {
                "source": str(d.track.rel),
                "target": str(d.target) if d.target else None,
                "action": d.action,
                "confidence": d.confidence,
                "reasons": [{"code": r.code, "message": r.message} for r in d.reasons],
                "filled": d.filled,
                "sources": d.track.sources,
                "size": d.track.size,
                "sha256": d.track.sha256,
            }
            for d in plan.decisions
        ],
    }
    if result is not None:
        payload["applied"] = {
            "journal": str(result.journal_path),
            "done": len(result.done),
            "failed": [
                {"source": str(d.track.rel), "error": error}
                for d, error in result.failed
            ],
        }
    return payload
