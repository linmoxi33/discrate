"""决策引擎：把扫描结果变成一份**可以先看再执行**的方案。

这是本项目和「直接 ``beet import -q``」的分界线。上游的 quiet 模式是一次
闭眼执行：所有判定在内部完成，用户看不到任何一条决策的理由，也没有回滚。
这里把每个文件的判定摊开成 ``Decision``，并且每条判定都带 ``reasons``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .naming import TemplateError, build_target, render_segments
from .profile import Profile
from .scan import ScanResult, Track

__all__ = [
    "UNKNOWN_ARTIST",
    "Decision",
    "Plan",
    "Reason",
    "build_plan",
]

UNKNOWN_ARTIST = "未知歌手"

#: 会真正落盘的动作
WRITE_ACTIONS = frozenset({"copy", "move"})


@dataclass(frozen=True)
class Reason:
    code: str
    message: str


@dataclass
class Decision:
    track: Track
    action: str  # copy | move | skip | duplicate | review | reject
    target: Path | None = None
    confidence: str = "high"  # high | medium | low
    reasons: list[Reason] = field(default_factory=list)
    filled: dict[str, object] = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return self.action in WRITE_ACTIONS

    @property
    def needs_review(self) -> bool:
        # review / reject 已经由各自的清单负责展示，这里不重复计入。
        return self.confidence == "low" and self.action not in ("reject", "review")

    def reason_codes(self) -> list[str]:
        return [r.code for r in self.reasons]


@dataclass
class Plan:
    root: Path
    target_root: Path
    profile: Profile
    decisions: list[Decision]

    def by_action(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]

    @property
    def writes(self) -> list[Decision]:
        return [d for d in self.decisions if d.writes]

    @property
    def bytes_out(self) -> int:
        return sum(d.track.size for d in self.writes)

    @property
    def review(self) -> list[Decision]:
        return [d for d in self.decisions if d.needs_review]

    def stats(self) -> dict[str, int]:
        counts = {
            "total": len(self.decisions),
            "copy": 0,
            "move": 0,
            "skip": 0,
            "duplicate": 0,
            "review": 0,
            "reject": 0,
            "needs_review": len(self.review),
        }
        for d in self.decisions:
            counts[d.action] = counts.get(d.action, 0) + 1
        return counts


def _render_values(track: Track) -> dict[str, object]:
    values = dict(track.fields)
    # artist / albumartist 互相兜底，和上游 Item.__getitem__ 的语义保持一致，
    # 否则合辑或单曲会有一段路径空掉。
    if not values.get("artist") and values.get("albumartist"):
        values["artist"] = values["albumartist"]
    if not values.get("albumartist") and values.get("artist"):
        values["albumartist"] = values["artist"]
    if not values.get("artist"):
        values["artist"] = UNKNOWN_ARTIST
    if not values.get("albumartist"):
        values["albumartist"] = values["artist"]
    return values


def _confidence(track: Track) -> str:
    title_src = track.sources.get("title")
    artist_src = track.sources.get("artist") or track.sources.get("albumartist")
    if title_src == "tag" and artist_src == "tag":
        return "high"
    if title_src in ("tag", "filename") and artist_src in ("tag", "filename", "folder", "inferred"):
        if artist_src in ("tag", "filename"):
            return "medium"
        return "medium" if track.has_album else "low"
    return "low"


def _filled_fields(track: Track) -> dict[str, object]:
    """需要写回标签副本的字段：只写从推断得来、且原标签没有的。"""
    out: dict[str, object] = {}
    for key in ("artist", "albumartist", "album", "title", "track", "year"):
        if track.sources.get(key) in ("filename", "folder", "inferred"):
            value = track.fields.get(key)
            if value not in (None, "", 0):
                out[key] = value
    return out


def build_plan(
    scan: ScanResult,
    profile: Profile,
    target_root: Path | None = None,
    previous: dict[str, dict] | None = None,
    dedupe: bool = True,
) -> Plan:
    """生成整理方案。纯计算，不触碰文件系统（除读取已有目标判断冲突）。

    ``previous`` 是上一次整理的登记表（见 ``execute.load_state``）。
    命中且源文件内容未变、目标文件仍在的，直接跳过 —— 保证反复运行
    不会产出一堆 ``(2)`` ``(3)`` 副本。

    ``dedupe=False`` 时完全不做重复判定，每个文件都按普通文件整理。
    """
    root = scan.root
    target = Path(target_root) if target_root else root / "整理结果"
    previous = previous or {}

    decisions: list[Decision] = []
    # sha → (展示用的名字, 体积)；key → 同上。用元组而不是 Track，
    # 是因为「上次已整理」的记录也要参与判重，而它们没有 Track 对象。
    seen_sha: dict[str, tuple[str, int]] = {}
    seen_key: dict[tuple[str, str], tuple[str, int]] = {}
    taken_paths: dict[str, Track] = {}

    # 把上一轮的结果并入已知集合，避免同内容的文件在下一轮又被复制一份。
    # 目标文件的大小必须和登记时一致，否则说明它已经被用户换掉了，
    # 这条记录不能再当作「这份内容已经存在」的依据。
    if dedupe:
        for record in previous.values():
            recorded_target = str(record.get("target") or "")
            if not recorded_target:
                continue
            target_path = Path(recorded_target)
            if not target_path.exists():
                continue
            recorded_size = record.get("target_size")
            if recorded_size is None:
                # 没有目标大小的记录不可信（旧版本登记、或文件来历不明），
                # 宁可重新整理一次，也不要把「内容已存在」判错。
                continue
            try:
                if target_path.stat().st_size != int(recorded_size):
                    continue
            except (OSError, TypeError, ValueError):
                continue
            label = f"{target_path.name}（上次整理）"
            size = int(record.get("size") or 0)
            sha = str(record.get("sha256") or "")
            if sha:
                seen_sha.setdefault(sha, (label, size))
            stored_key = record.get("key")
            if (
                isinstance(stored_key, list)
                and len(stored_key) == 2
                and stored_key[1]
            ):
                seen_key.setdefault(
                    (str(stored_key[0]), str(stored_key[1])), (label, size)
                )

    # 先按体积从大到小排序，保证「保留最大那份」的判定与输入顺序无关。
    ordered = sorted(scan.tracks, key=lambda t: (-t.size, str(t.rel).casefold()))

    for track in ordered:
        if track.error:
            decisions.append(
                Decision(
                    track=track,
                    action="reject",
                    confidence="low",
                    reasons=[Reason("unreadable", track.error)],
                )
            )
            continue

        title = str(track.fields.get("title") or "").strip()

        # 上次已经整理过、内容没变、目标文件还在 —— 直接跳过。
        record = previous.get(str(track.rel))
        if record and record.get("sha256") == track.sha256:
            recorded_target = Path(str(record.get("target") or ""))
            if str(recorded_target) and recorded_target.exists():
                decisions.append(
                    Decision(
                        track=track,
                        action="skip",
                        target=recorded_target,
                        confidence="high",
                        reasons=[
                            Reason(
                                "already-done",
                                f"上次已整理到 {recorded_target.name}，本次跳过",
                            )
                        ],
                    )
                )
                continue

        if not title:
            decisions.append(
                Decision(
                    track=track,
                    action="review",
                    confidence="low",
                    reasons=[
                        Reason(
                            "no-title",
                            "文件里没有标题标签，文件名里也看不出曲名",
                        )
                    ],
                )
            )
            continue

        # 1) 内容完全相同
        if dedupe and track.sha256 and track.sha256 in seen_sha:
            label, _size = seen_sha[track.sha256]
            decisions.append(
                Decision(
                    track=track,
                    action="duplicate",
                    confidence="high",
                    reasons=[
                        Reason(
                            "identical-content",
                            f"与 {label} 内容完全相同（逐字节一致）",
                        )
                    ],
                )
            )
            continue

        # 2) 逻辑重复：同一首歌的不同码率/来源
        key = track.dedupe_key
        if dedupe and key[1] and key in seen_key:
            label, keeper_size = seen_key[key]
            decisions.append(
                Decision(
                    track=track,
                    action="duplicate",
                    confidence="high",
                    reasons=[
                        Reason(
                            "same-song",
                            f"与 {label} 是同一首歌，保留体积较大的那份"
                            f"（{keeper_size // 1024} KB vs {track.size // 1024} KB）",
                        )
                    ],
                )
            )
            continue

        # 3) 计算目标路径
        values = _render_values(track)
        template = profile.album_format if track.has_album else profile.single_format
        try:
            rendered = render_segments(template, values)
            dest = build_target(target, rendered, track.suffix, profile.max_component)
        except TemplateError as exc:
            decisions.append(
                Decision(
                    track=track,
                    action="review",
                    confidence="low",
                    reasons=[Reason("template-error", f"无法生成目标路径：{exc}")],
                )
            )
            continue

        reasons: list[Reason] = []
        action = profile.mode

        # 4) 目标名冲突
        conflict_key = str(dest).casefold()
        if conflict_key in taken_paths:
            other = taken_paths[conflict_key]
            dest = _dedupe_name(dest, taken_paths)
            conflict_key = str(dest).casefold()
            reasons.append(
                Reason(
                    "name-collision",
                    f"目标名与 {other.rel} 撞车，已自动加编号区分",
                )
            )
        elif dest.exists():
            action = "skip"
            reasons.append(
                Reason(
                    "target-exists",
                    f"目标位置已有同名文件（{dest.name}），本次跳过以免覆盖",
                )
            )

        # 5) 补全说明
        inferred = [
            f"{_FIELD_LABEL.get(k, k)}←{_SOURCE_LABEL[v]}"
            for k, v in track.sources.items()
            if v in ("filename", "folder", "inferred") and track.fields.get(k)
        ]
        if inferred:
            reasons.append(Reason("filled", "从文件名/目录名补全：" + "、".join(inferred)))
        if not track.fields.get("artist") and not track.fields.get("albumartist"):
            reasons.append(
                Reason("unknown-artist", f"找不到歌手信息，归入「{UNKNOWN_ARTIST}」")
            )

        confidence = _confidence(track)
        if confidence == "low":
            reasons.append(
                Reason("low-confidence", "信息不足，建议人工确认后再整理")
            )

        # 只有「确定会被写入」的文件才能成为去重基准。否则会出现
        # 「A 被判与 B 重复 → 跳过；B 因为目标已存在 → 也跳过」这种
        # 两头落空、文件凭空消失的情况。
        if action in WRITE_ACTIONS:
            label = str(track.rel)
            if track.sha256:
                seen_sha[track.sha256] = (label, track.size)
            if key[1]:
                seen_key[key] = (label, track.size)
        taken_paths[conflict_key] = track

        decisions.append(
            Decision(
                track=track,
                action=action,
                target=dest,
                confidence=confidence,
                reasons=reasons,
                filled=_filled_fields(track),
            )
        )

    # 输出顺序回到用户的直觉：按源路径排序，而不是按体积。
    decisions.sort(key=lambda d: str(d.track.rel).casefold())
    return Plan(root=root, target_root=target, profile=profile, decisions=decisions)


_FIELD_LABEL = {
    "artist": "歌手",
    "albumartist": "专辑歌手",
    "album": "专辑",
    "title": "曲名",
    "track": "轨号",
    "year": "年份",
    "genre": "风格",
}

_SOURCE_LABEL = {
    "filename": "文件名",
    "folder": "目录名",
    "inferred": "推断",
    "tag": "标签",
}


def _dedupe_name(dest: Path, taken: dict[str, Track]) -> Path:
    """为撞车的目标名生成 `名称 (2).mp3` 这样的可用名。"""
    stem, suffix = dest.stem, dest.suffix
    for index in range(2, 1000):
        candidate = dest.with_name(f"{stem} ({index}){suffix}")
        if str(candidate).casefold() not in taken and not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {dest.name} 找到可用的目标名")
