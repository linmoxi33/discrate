"""默认配置档。

上游 beets 的默认配置有 207 行、约 90 个键，其中 ``match.distance_weights``
一项就有 20 个子权重（``beets/config_default.yaml``）。这些键对专家有意义，
对「只想把下载文件夹理干净」的人是纯负担。

这里把它们收敛成三个具名档位。默认档位的每一个选择都以
**「不破坏用户已有文件」** 为第一约束。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEFAULT_PROFILE", "PROFILES", "Profile", "get_profile"]


@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    #: copy = 复制到目标目录，源文件不动；move = 移动
    mode: str
    #: 有专辑信息的文件走这个模板
    album_format: str
    #: 没有专辑信息的文件走这个模板
    single_format: str
    #: 重复文件的处理：report=只报告 / keep-largest=保留体积最大的那份
    dedupe: str
    #: 是否把补全的字段写进目标副本的标签
    write_tags: bool
    #: 单段文件名长度上限
    max_component: int = 120


PROFILES: dict[str, Profile] = {
    "safe": Profile(
        name="safe",
        summary="复制整理，源文件一个字节都不动（默认）",
        mode="copy",
        album_format="{albumartist}/{album}/{track:02d} {title}",
        single_format="单曲/{artist}/{title}",
        dedupe="keep-largest",
        write_tags=True,
    ),
    "library": Profile(
        name="library",
        summary="带年份与碟号的专业库结构，适合长期收藏",
        mode="copy",
        album_format="{albumartist}/{year} - {album}/{disc:02d}-{track:02d} {title}",
        single_format="单曲/{artist}/{year} - {title}",
        dedupe="keep-largest",
        write_tags=True,
    ),
    "inplace": Profile(
        name="inplace",
        summary="原地移动（不留副本，省空间；执行前请确认已备份）",
        mode="move",
        album_format="{albumartist}/{album}/{track:02d} {title}",
        single_format="单曲/{artist}/{title}",
        dedupe="keep-largest",
        write_tags=True,
    ),
}

DEFAULT_PROFILE = "safe"


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(
            f"未知档位 {name!r}，可选：{', '.join(sorted(PROFILES))}"
        ) from None
