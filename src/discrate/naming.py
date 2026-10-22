"""路径模板渲染与文件名安全化。

模板语法面向普通用户：``{albumartist}/{album}/{track:02d} {title}``。
相比上游的 ``$albumartist/$album%aunique{}/$track $title``，这里做了两个
刻意的取舍：

* **空值自动收缩**：某个字段没有值时，它在路径里占的那一段会被整段移除，
  而不是留下 ``//`` 或 ``00``。上游会渲染出 ``Non-Album.mp3`` 这种结果
  （实测：无标签文件全部撞到同一个名字）。
* **保留 ``$field`` 写法**：老 beets 用户可以直接写 ``$albumartist/$album``，
  这里会翻译过来。但上游的 ``%aunique{}`` / ``$if{}`` 等函数语法不支持，
  遇到会明确报错，而不是静默产生错路径。
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .bridge import safe_component

__all__ = [
    "RenderedPath",
    "TemplateError",
    "build_target",
    "normalize_key",
    "render_segments",
    "translate_beets_template",
]

_BEETS_FIELD = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
#: beets 的 2 位补零字段
_PADDED = {"track", "disc"}


class TemplateError(ValueError):
    """模板无法解析，或使用了不受支持的语法。"""


def translate_beets_template(fmt: str) -> str:
    """把 beets 的 ``$field`` 模板翻译成本项目的 ``{field}`` 模板。"""
    if "%" in fmt or "$if{" in fmt or "${" in fmt:
        raise TemplateError(
            "暂不支持 beets 的函数式模板（%aunique{} / $if{} / ${}）。"
            "请改用 {字段} 写法，例如 {albumartist}/{album}/{track:02d} {title}"
        )
    if "{" in fmt and "$" not in fmt:
        return fmt

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _PADDED:
            return f"{{{name}:02d}}"
        return f"{{{name}}}"

    return _BEETS_FIELD.sub(repl, fmt)


@dataclass(frozen=True)
class RenderedPath:
    """模板渲染结果。``segments`` 已剔除空段，但尚未做文件名合法化。"""

    segments: tuple[str, ...]
    dropped: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.segments


def _field_text(name: str, spec: str, values: dict[str, object]) -> str:
    value = values.get(name, "")
    if value is None or value == "" or value == 0:
        # 0 与空串都视为「没有这个信息」。轨号 0 对用户毫无意义，
        # 渲染成 "00" 只会制造噪音。
        return ""
    if spec:
        try:
            text = format(value, spec)
        except (ValueError, TypeError):
            text = str(value)
    else:
        text = str(value)
    # 字段值里的分隔符必须先转义，否则 `album="Back:In/Black"` 会在渲染后
    # 被当成路径层级，凭空多出两级目录（实测）。
    return text.replace("/", "_").replace("\\", "_")


def render_segments(fmt: str, values: dict[str, object]) -> RenderedPath:
    """按模板渲染出路径段列表。

    整段渲染为空（例如 ``{album}`` 缺失）时该段被丢弃；段内的空字段
    只留字面量，最终 strip 后若为空同样丢弃。
    """
    fmt = translate_beets_template(fmt)
    segments: list[str] = []
    dropped: list[str] = []

    for chunk in fmt.split("/"):
        if not chunk:
            continue
        pieces: list[str] = []
        try:
            parsed = list(string.Formatter().parse(chunk))
        except ValueError as exc:
            raise TemplateError(f"模板片段无法解析：{chunk!r}（{exc}）") from exc

        for literal, field_name, format_spec, _conv in parsed:
            if literal:
                pieces.append(literal)
            if field_name is None:
                continue
            if field_name == "":
                raise TemplateError(f"模板里出现了空字段：{chunk!r}")
            pieces.append(_field_text(field_name, format_spec or "", values))

        text = "".join(pieces).strip()
        if not text:
            dropped.append(chunk)
            continue
        segments.append(text)

    return RenderedPath(tuple(segments), tuple(dropped))


def build_target(
    base: Path, rendered: RenderedPath, suffix: str, max_component: int
) -> Path:
    """把渲染结果落成一个安全的绝对路径。"""
    if rendered.empty:
        raise TemplateError("模板渲染结果为空，无法生成目标路径")
    parts = [safe_component(s, max_component) for s in rendered.segments]
    *dirs, name = parts
    suffix = suffix if suffix.startswith(".") else "." + suffix
    return base.joinpath(*dirs, name + suffix.lower())


_PUNCT = re.compile(r"[\s\-_.,'\"·、，。！？（）()\[\]【】~]+")


def normalize_key(text: str) -> str:
    """归一化用于「是否同一首歌」的比较。

    全角转半角（NFKC）、大小写折叠、去掉空白与常见标点，
    使 ``周杰伦 - 稻香``、``周杰伦-稻香``、``周杰伦_稻香`` 视为同一个。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCT.sub("", folded)
