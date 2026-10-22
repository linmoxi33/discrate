"""扫描：发现音频文件、读标签、在标签缺失时从文件名/目录名做保守推断。

推断的每一处都记录来源（``tag`` / ``filename`` / ``folder``），这是结果
可解释性的基础 —— 用户需要知道「这个歌手名是文件自带的，还是你猜的」。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import RawTags, read_tags
from .naming import normalize_key

__all__ = [
    "AUDIO_SUFFIXES",
    "COVER_NAMES",
    "IMAGE_SUFFIXES",
    "IgnoredItem",
    "ScanResult",
    "Track",
    "parse_filename",
    "pick_cover",
    "scan_folder",
]

AUDIO_SUFFIXES = {
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
    ".mpc",
    ".dsf",
    ".dff",
}

#: 常见的封面文件名（小写、无扩展名）
COVER_NAMES = ("cover", "folder", "front", "albumart", "albumartsmall", "artwork")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

#: 这些目录名不含歌手/专辑信息，不能拿来推断。
_GENERIC_DIRS = {
    "下载",
    "downloads",
    "download",
    "音乐",
    "music",
    "musics",
    "我的音乐",
    "新建文件夹",
    "new folder",
    "桌面",
    "desktop",
    "歌曲",
    "songs",
    "mp3",
    "audio",
    "音频",
    "临时",
    "temp",
    "tmp",
    "整理结果",
    "unknown artist",
    "未知艺术家",
}

_JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

#: `01 - 标题` / `01. 标题` / `01 标题` / `1_标题`
_RE_TRACK_TITLE = re.compile(r"^(?P<track>\d{1,3})\s*[-._、]\s*(?P<title>.+)$")
#: `01 标题`（无分隔符，纯空格）
_RE_TRACK_SPACE = re.compile(r"^(?P<track>\d{1,3})\s+(?P<title>[^\d].*)$")
#: `歌手 - 标题`（要求分隔符两侧有空格，避免拆开 AC-DC）
_RE_ARTIST_TITLE = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<title>.+)$")
#: `歌手 - 专辑`（目录名）
_RE_ARTIST_ALBUM = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$")
#: 目录名尾部的 `(2003)` / `[2003]`
_RE_TRAILING_YEAR = re.compile(r"[\(\[]\s*(?P<year>(?:19|20)\d{2})\s*[\)\]]\s*$")


@dataclass
class Track:
    path: Path
    rel: Path
    size: int
    sha256: str
    tags: RawTags | None
    error: str | None
    fields: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def has_album(self) -> bool:
        return bool(self.fields.get("album"))

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """判断「逻辑上是不是同一首歌」的键。"""
        return (
            normalize_key(str(self.fields.get("albumartist") or self.fields.get("artist") or "")),
            normalize_key(str(self.fields.get("title") or "")),
        )


@dataclass
class IgnoredItem:
    path: Path
    reason: str


@dataclass
class ScanResult:
    root: Path
    tracks: list[Track]
    ignored: list[IgnoredItem]
    #: 源目录 → 该目录下的图片文件（用来找专辑封面）
    images: dict[Path, list[Path]] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.tracks)


def pick_cover(images: list[Path]) -> Path | None:
    """从一堆图片里选一张当封面。

    只认标准名（cover/folder/front…）；非标准名时，只有「目录里仅此一张图」
    才敢用 —— 一整个目录十几张扫图时随便挑一张，比没有封面更糟。
    """
    if not images:
        return None

    def rank(path: Path) -> int:
        stem = path.stem.casefold()
        try:
            return COVER_NAMES.index(stem)
        except ValueError:
            return len(COVER_NAMES)

    named = [p for p in images if p.stem.casefold() in COVER_NAMES]
    if named:
        return min(named, key=rank)
    if len(images) == 1:
        return images[0]
    return None


def _hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def parse_filename(stem: str) -> dict[str, object]:
    """从文件名主干推断字段。返回的键可能包含 track / title / artist。"""
    text = stem.strip()
    if not text:
        return {}

    # 先剥掉 `(1)` / ` - 副本` 这类下载器留下的痕迹
    text = re.sub(r"\s*[\(\[]\d+[\)\]]\s*$", "", text)
    text = re.sub(r"\s*-\s*副本\s*$", "", text)
    text = re.sub(r"\s*\(\d+\)\s*$", "", text)

    # 纯数字（如 `01.mp3`）只说明轨号，不能当曲名 —— 否则会得到一个
    # 叫「01」的曲目，用户根本认不出那是哪首歌。
    if text.isdigit():
        return {"track": int(text)}

    out: dict[str, object] = {}
    for pattern in (_RE_TRACK_TITLE, _RE_TRACK_SPACE):
        m = pattern.match(text)
        if m:
            out["track"] = int(m.group("track"))
            out["title"] = m.group("title").strip()
            return out

    m = _RE_ARTIST_TITLE.match(text)
    if m:
        out["artist"] = m.group("artist").strip()
        out["title"] = m.group("title").strip()
        return out

    out["title"] = text
    return out


def parse_folder(name: str, *, trusted: bool = True) -> dict[str, object]:
    """从目录名推断字段。

    ``trusted=False`` 表示这个目录里只有一个音频文件 —— 此时目录名更可能
    是「下载」「临时」「同名不同码率」这类描述性名字，不是专辑名，一律
    不推断。宁可走单曲路径，也不要把文件塞进一个不存在的专辑。
    """
    text = (name or "").strip()
    if not text or text.casefold() in _GENERIC_DIRS:
        return {}
    if not trusted:
        return {}

    out: dict[str, object] = {}
    year_match = _RE_TRAILING_YEAR.search(text)
    if year_match:
        out["year"] = int(year_match.group("year"))
        text = text[: year_match.start()].strip()

    m = _RE_ARTIST_ALBUM.match(text)
    if m:
        out["artist"] = m.group("artist").strip()
        out["album"] = m.group("album").strip()
    else:
        # 单个名字当作专辑名更常见（如 `叶惠美`），不当歌手 —— 宁可少补，
        # 也不要把专辑名错填成歌手。
        out["album"] = text
    return out


def _merge_fields(tags: RawTags | None, filename_hints: dict, folder_hints: dict) -> tuple[dict, dict, list[str]]:
    """按「标签 > 文件名 > 目录名」的优先级合成字段，并记录来源。"""
    fields: dict[str, object] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []

    tag_values = {}
    if tags is not None:
        tag_values = {
            "artist": tags.artist,
            "albumartist": tags.albumartist,
            "album": tags.album,
            "title": tags.title,
            "track": tags.track,
            "disc": tags.disc,
            "year": tags.year,
        }

    for key in ("artist", "albumartist", "album", "title", "track", "disc", "year"):
        value = tag_values.get(key)
        if value not in (None, "", 0):
            fields[key] = value
            sources[key] = "tag"
            continue
        for source_name, hints in (("filename", filename_hints), ("folder", folder_hints)):
            hint = hints.get(key)
            if hint not in (None, "", 0):
                fields[key] = hint
                sources[key] = source_name
                break

    # albumartist 缺失时用 artist 顶上 —— 否则路径模板的第一段会空掉。
    if not fields.get("albumartist") and fields.get("artist"):
        fields["albumartist"] = fields["artist"]
        sources["albumartist"] = "inferred"

    if fields.get("album") and not fields.get("albumartist"):
        warnings.append("有专辑名但找不到歌手，专辑路径可能不理想")

    return fields, sources, warnings


def scan_folder(
    root: Path | str,
    *,
    compute_hash: bool = True,
    max_files: int | None = None,
    exclude: list[Path] | tuple[Path, ...] = (),
) -> ScanResult:
    """扫描目录，返回可判定的 Track 列表。只读，不写任何东西。

    ``exclude`` 用来排除输出目录 —— 默认输出目录就在源目录里面
    （``<源>/整理结果``），不排除的话第二次运行会把上次的产物当成新素材
    再整理一遍，越整理越乱。
    """
    base = Path(root).expanduser().resolve()
    if not base.exists():
        raise FileNotFoundError(f"目录不存在：{base}")
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录：{base}")

    excluded = {os.path.normcase(str(Path(p).expanduser().resolve())) for p in exclude}

    tracks: list[Track] = []
    ignored: list[IgnoredItem] = []

    # 第一遍：只收集候选文件，同时统计每个目录里有几个音频。
    # 目录里有几首歌决定了目录名能不能当专辑名（见 parse_folder）。
    candidates: list[Path] = []
    images: dict[Path, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and os.path.normcase(str(Path(dirpath) / d)) not in excluded
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name.startswith(".") or name.casefold() in _JUNK_NAMES:
                continue
            suffix = path.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                # 图片不是待整理的文件，但可能是这张专辑的封面。
                images.setdefault(path.parent, []).append(path)
                continue
            if suffix not in AUDIO_SUFFIXES:
                ignored.append(IgnoredItem(path, "非音频文件"))
                continue
            candidates.append(path)

    dir_counts: dict[Path, int] = {}
    for path in candidates:
        dir_counts[path.parent] = dir_counts.get(path.parent, 0) + 1

    # 第二遍：读取标签并推断字段。
    for path in candidates:
        if max_files is not None and len(tracks) >= max_files:
            ignored.append(IgnoredItem(path, "超过本次处理上限"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            ignored.append(IgnoredItem(path, f"无法读取文件信息：{exc}"))
            continue

        tags, error = read_tags(path)
        # 根目录本身的名字（比如「下载」「我的音乐」）不能当专辑名，
        # 否则根目录下所有散装文件都会被塞进一个以文件夹命名的假专辑。
        at_root = os.path.normcase(str(path.parent)) == os.path.normcase(str(base))
        sibling_count = dir_counts.get(path.parent, 1)
        folder_hints = (
            {}
            if at_root
            else parse_folder(path.parent.name, trusted=sibling_count > 1)
        )
        file_hints = parse_filename(path.stem)
        fields, sources, warnings = _merge_fields(tags, file_hints, folder_hints)

        sha = ""
        if compute_hash and error is None:
            try:
                sha = _hash_file(path)
            except OSError as exc:
                warnings.append(f"内容指纹计算失败：{exc}")

        tracks.append(
            Track(
                path=path,
                rel=path.relative_to(base),
                size=size,
                sha256=sha,
                tags=tags,
                error=error,
                fields=fields,
                sources=sources,
                warnings=warnings,
            )
        )

    return ScanResult(root=base, tracks=tracks, ignored=ignored, images=images)
