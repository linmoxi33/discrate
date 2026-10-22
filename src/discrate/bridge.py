"""beets 适配层。

这一层是 discrate 与上游 beets 的唯一接触面，目的有两个：

1. **隔离上游的全局状态**：beets 的 ``config`` 是模块级单例，会读取用户
   的 ``~/.config/beets/config.yaml``。discrate 不允许用户既有配置改变
   行为，因此先把 ``BEETSDIR`` 指到一个私有目录，再 import beets。
2. **只借用可靠的能力**：读标签（``Item.from_path``）、文件名合法化
   （``beets.util.sanitize_path``）。``Item.destination()`` 不使用 ——
   它对专辑型文件会选错路径模板，理由见 ``docs/UPSTREAM-NOTES.md``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 组件长度上限。
# Windows 单段上限是 255 个 UTF-16 字符，Linux/macOS 是 255 个**字节**。
# 一个中文字符 UTF-8 占 3 字节，所以「120 字符」在 Linux 上可能是 360 字节，
# 直接 ENAMETOOLONG。两个上限都要守，取更严格的那个。
MAX_COMPONENT = 120
MAX_COMPONENT_BYTES = 200

# Windows 保留设备名。beets 的 sanitize_path 不处理这些，
# 而它们会直接导致创建文件失败（`CON.mp3` 永远建不出来）。
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED |= {f"COM{i}" for i in range(1, 10)}
_RESERVED |= {f"LPT{i}" for i in range(1, 10)}


def _prepare_beets_env() -> Path:
    """在 import beets 之前把它的配置目录隔离到一个私有位置。"""
    anchor = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    base = (
        Path(anchor) / "discrate" / "beetsdir"
        if anchor
        else Path.home() / ".cache" / "discrate" / "beetsdir"
    )
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path.home() / ".discrate-beetsdir"
        base.mkdir(parents=True, exist_ok=True)
    os.environ["BEETSDIR"] = str(base)
    os.environ.setdefault("BEETS_NO_COLOR", "1")
    return base


BEETSDIR = _prepare_beets_env()

from beets.library import Item  # noqa: E402
from beets.library.exceptions import ReadError  # noqa: E402
from beets.util import sanitize_path  # noqa: E402

__all__ = [
    "BEETSDIR",
    "MAX_COMPONENT",
    "MAX_COMPONENT_BYTES",
    "RawTags",
    "ReadError",
    "read_tags",
    "safe_component",
    "write_tags",
]


@dataclass
class RawTags:
    """从音频文件里读到的原始标签。空字符串表示该字段不存在。"""

    artist: str = ""
    albumartist: str = ""
    album: str = ""
    title: str = ""
    track: int = 0
    disc: int = 0
    year: int = 0
    length: float = 0.0

    @property
    def empty(self) -> bool:
        return not any(
            [self.artist, self.albumartist, self.album, self.title, self.track]
        )


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe(item: Item, name: str, default: object = "") -> object:
    """读取单个字段。

    上游把 ``genre`` / ``composer`` 一类字段改成了多值字段（``genres``），
    直接属性访问会抛 ``KeyError``；这里统一走 ``item[name]`` 并兜底。
    """
    try:
        value = item[name]
    except (KeyError, AttributeError):
        return default
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    if value is None:
        return default
    return value


def read_tags(path: Path | str) -> tuple[RawTags | None, str | None]:
    """读取标签。返回 ``(tags, error)``，二者必有其一为 None。

    上游对非音频、空文件、缺失文件统一抛 ``ReadError``，这里归一成
    一句面向普通用户的中文原因。
    """
    p = Path(path)
    try:
        item = Item.from_path(str(p))
    except ReadError:
        if not p.exists():
            return None, "文件不存在"
        if p.stat().st_size == 0:
            return None, "空文件（0 字节）"
        return None, "不是可识别的音频文件，或文件已损坏"
    except Exception as exc:
        return None, f"读取失败：{type(exc).__name__}"

    return (
        RawTags(
            artist=_clean(_safe(item, "artist")),
            albumartist=_clean(_safe(item, "albumartist")),
            album=_clean(_safe(item, "album")),
            title=_clean(_safe(item, "title")),
            track=int(_safe(item, "track", 0) or 0),
            disc=int(_safe(item, "disc", 0) or 0),
            year=int(_safe(item, "year", 0) or 0),
            length=float(_safe(item, "length", 0.0) or 0.0),
        ),
        None,
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，不切断半个字符。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def safe_component(
    text: str,
    max_len: int = MAX_COMPONENT,
    max_bytes: int = MAX_COMPONENT_BYTES,
) -> str:
    """把任意字符串变成可安全落盘的单段文件名。

    在 beets 的 ``sanitize_path`` 之上补三件上游不做的事：

    * Windows 保留设备名（``CON`` / ``NUL`` / ``COM1`` …）会导致建文件失败；
    * 尾部空格与点在 Windows 上会被静默丢弃，造成实际路径与记录不一致；
    * 组件长度截断 —— **字符数与字节数都要守**，否则中文标题在 Linux 上
      会以 ENAMETOOLONG 直接失败（实测：115 个汉字 = 345 字节 > 255）。

    注意 ``sanitize_path`` 是给**路径片段**用的：它先按分隔符把字符串拆成
    多段再拼回去，所以 ``sanitize_path("AC/DC")`` 得到的是 ``AC\\DC``
    而不是 ``AC_DC``。传给它之前必须先把分隔符换掉。
    """
    raw = (text or "").strip()
    if not raw:
        return "_"
    raw = raw.replace("/", "_").replace("\\", "_")
    raw = raw.rstrip(". ")
    if not raw:
        return "_"
    # 默认替换表就是 beets 内置的 CHAR_REPLACE（含 : * ? " < > | 与控制字符）。
    cleaned = sanitize_path(raw)
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        return "_"
    stem = cleaned.split(".")[0].strip().upper()
    if stem in _RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    cleaned = _truncate_utf8(cleaned, max_bytes).rstrip(". ")
    return cleaned or "_"


def write_tags(path: Path | str, fields: dict[str, object]) -> tuple[bool, str | None]:
    """把字段写回音频文件。

    只在**目标副本**上调用；源文件永远不写。空值字段会被跳过，避免把
    已有标签清空 —— 上游 ``try_write()`` 会把整个 Item 写回，若先用
    ``Item()`` 造对象就会把未设置的字段写成空串（实测见上游笔记）。
    """
    try:
        item = Item.from_path(str(path))
        changed = False
        for key, value in fields.items():
            if value is None or value == "" or value == 0:
                continue
            if getattr(item, key, None) != value:
                setattr(item, key, value)
                changed = True
        if not changed:
            return True, None
        item.try_write()
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
