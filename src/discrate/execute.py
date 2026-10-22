"""执行、记账、回滚。

普通用户对这类工具最大的恐惧是「它把我的文件搞乱了还还原不回去」。
所以这里的每一条写操作都先落 journal 再动手，``undo`` 能按逆序撤回。
撤销时只删除**内容哈希仍然匹配**的文件 —— 如果用户事后自己改过它，
宁可留着也不误删。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import write_tags
from .decide import Decision, Plan

__all__ = [
    "JOURNAL_DIR",
    "ApplyResult",
    "UndoResult",
    "apply_plan",
    "find_latest_journal",
    "undo_journal",
]

JOURNAL_DIR = ".discrate"
JOURNAL_VERSION = 1
STATE_FILE = "state.json"


def state_path(target_root: Path | str) -> Path:
    return Path(target_root) / JOURNAL_DIR / STATE_FILE


def load_state(target_root: Path | str) -> dict[str, dict]:
    """读取「已经整理过」的登记表。

    copy 模式不改动源文件，所以源目录里那些文件下次扫描还会出现。
    没有这张表，用户每运行一次就会得到一整套 ``(2)`` ``(3)`` 副本。
    """
    path = state_path(target_root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    done = payload.get("done")
    return done if isinstance(done, dict) else {}


def save_state(target_root: Path | str, records: dict[str, dict]) -> None:
    path = state_path(target_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": JOURNAL_VERSION, "done": records}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def forget_state(target_root: Path | str, sources: list[str]) -> None:
    records = load_state(target_root)
    changed = False
    for source in sources:
        if records.pop(source, None) is not None:
            changed = True
    if changed:
        save_state(target_root, records)


@dataclass
class ApplyResult:
    journal_path: Path
    done: list[Decision] = field(default_factory=list)
    failed: list[tuple[Decision, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class UndoResult:
    removed: int = 0
    moved_back: int = 0
    kept: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = []
        if self.removed:
            parts.append(f"删除副本 {self.removed} 个")
        if self.moved_back:
            parts.append(f"移回原位 {self.moved_back} 个")
        if self.kept:
            parts.append(f"保留未动 {len(self.kept)} 个")
        return "，".join(parts) or "没有需要撤销的操作"


def _journal_path(target_root: Path) -> Path:
    folder = target_root / JOURNAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"journal-{stamp}.jsonl"
    index = 2
    while path.exists():
        path = folder / f"journal-{stamp}-{index}.jsonl"
        index += 1
    return path


def _append(handle, payload: dict) -> None:
    payload = {"v": JOURNAL_VERSION, "ts": time.time(), **payload}
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    handle.flush()


def apply_plan(
    plan: Plan,
    *,
    progress=None,
    write_meta: bool | None = None,
) -> ApplyResult:
    """执行方案。``progress`` 是可选回调 ``(done, total, decision)``。"""
    target_root = plan.target_root
    target_root.mkdir(parents=True, exist_ok=True)
    journal = _journal_path(target_root)
    writes = plan.writes
    result = ApplyResult(journal_path=journal)
    do_write_tags = plan.profile.write_tags if write_meta is None else write_meta

    with journal.open("w", encoding="utf-8", newline="\n") as handle:
        _append(
            handle,
            {
                "op": "session",
                "source": str(plan.root),
                "target": str(target_root),
                "profile": plan.profile.name,
                "mode": plan.profile.mode,
                "planned": len(writes),
            },
        )

        for index, decision in enumerate(writes, start=1):
            src = decision.track.path
            dst = decision.target
            assert dst is not None  # writes 一定有 target

            try:
                if dst.exists():
                    # plan 与 apply 之间目标被占用：宁可跳过也不覆盖
                    result.failed.append((decision, f"目标已存在，跳过：{dst}"))
                    _append(handle, {"op": "skip", "src": str(src), "dst": str(dst), "reason": "target-exists"})
                    continue

                # 先记账再动手，崩溃后仍可回滚
                _append(
                    handle,
                    {
                        "op": decision.action,
                        "src": str(src),
                        "dst": str(dst),
                        "sha256": decision.track.sha256,
                        "size": decision.track.size,
                    },
                )

                dst.parent.mkdir(parents=True, exist_ok=True)
                if decision.action == "move":
                    shutil.move(str(src), str(dst))
                else:
                    shutil.copy2(str(src), str(dst))

                if do_write_tags and decision.filled:
                    ok, err = write_tags(dst, decision.filled)
                    # 补写标签会改变文件字节，因此必须记下写完之后的新哈希，
                    # 否则 undo 时按复制前的哈希校验会认为「文件被改动过」，
                    # 从而拒绝删除自己刚生成的副本（实测踩到）。
                    after = _sha256(dst) if ok else ""
                    _append(
                        handle,
                        {
                            "op": "tag",
                            "path": str(dst),
                            "fields": decision.filled,
                            "ok": ok,
                            "error": err,
                            "sha256_after": after,
                        },
                    )
                    if not ok:
                        decision.reasons.append(_tag_warning(err))

                result.done.append(decision)
            except Exception as exc:
                result.failed.append((decision, f"{type(exc).__name__}: {exc}"))
                _append(
                    handle,
                    {
                        "op": "error",
                        "src": str(src),
                        "dst": str(dst),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            finally:
                if progress is not None:
                    progress(index, len(writes), decision)

        _append(
            handle,
            {
                "op": "done",
                "applied": len(result.done),
                "failed": len(result.failed),
            },
        )

    if result.done:
        records = load_state(target_root)
        stamp = time.time()
        for decision in result.done:
            target = Path(str(decision.target))
            try:
                target_size = target.stat().st_size
            except OSError:
                target_size = None
            records[str(decision.track.rel)] = {
                "sha256": decision.track.sha256,
                "target": str(decision.target),
                # 记下判定「同一首歌」用的键，下一轮才能跨批次去重。
                "key": list(decision.track.dedupe_key),
                "size": decision.track.size,
                # 写标签会改变字节数，所以记的是目标文件最终大小。
                # 下一轮用它确认「这个文件还是我当初生成的那个」。
                "target_size": target_size,
                "ts": stamp,
            }
        save_state(target_root, records)

    return result


def _tag_warning(err: str | None):
    from .decide import Reason

    return Reason("tag-write-failed", f"标签写入失败（文件已复制）：{err}")


def find_latest_journal(target_root: Path | str) -> Path | None:
    folder = Path(target_root) / JOURNAL_DIR
    if not folder.is_dir():
        return None
    candidates = sorted(folder.glob("journal-*.jsonl"))
    return candidates[-1] if candidates else None


def _read_journal(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def undo_journal(journal: Path | str) -> UndoResult:
    """按逆序撤销一次 apply。"""
    path = Path(journal)
    if not path.is_file():
        raise FileNotFoundError(f"找不到记录文件：{path}")

    entries = _read_journal(path)
    result = UndoResult()

    # 先算出每个目标文件的「最终期望哈希」。复制时记的是源文件哈希，
    # 若之后补写过标签，则以写标签后的哈希为准。
    final_sha: dict[str, str] = {}
    for entry in entries:
        if entry.get("op") in ("copy", "move"):
            final_sha[entry["dst"]] = entry.get("sha256") or ""
        elif entry.get("op") == "tag" and entry.get("sha256_after"):
            final_sha[entry["path"]] = entry["sha256_after"]

    for entry in reversed(entries):
        op = entry.get("op")
        if op not in ("copy", "move"):
            continue
        src = Path(entry["src"])
        dst = Path(entry["dst"])
        expected = final_sha.get(entry["dst"], "")

        if not dst.exists():
            continue

        if op == "copy":
            if expected and _sha256(dst) != expected:
                result.kept.append((dst, "内容已被改动，保留"))
                continue
            try:
                dst.unlink()
                result.removed += 1
            except OSError as exc:
                result.kept.append((dst, f"删除失败：{exc}"))
        else:  # move
            if src.exists():
                result.kept.append((dst, f"原位置已被占用（{src.name}），未移回"))
                continue
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                result.moved_back += 1
            except OSError as exc:
                result.kept.append((dst, f"移回失败：{exc}"))

    # 撤销之后这些源文件应该重新变成「未整理」，否则用户撤销完再跑一次
    # 会发现什么都没发生。
    source_root = ""
    for entry in entries:
        if entry.get("op") == "session":
            source_root = entry.get("source") or ""
            break
    if source_root:
        rels: list[str] = []
        for entry in entries:
            if entry.get("op") not in ("copy", "move"):
                continue
            try:
                rels.append(str(Path(entry["src"]).relative_to(source_root)))
            except ValueError:
                continue
        forget_state(Path(entries[0].get("target") or Path(source_root)), rels)

    return result


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
