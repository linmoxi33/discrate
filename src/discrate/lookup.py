"""在线元数据**建议**。

v0.1 完全不联网，代价很直接：文件名只有 ``track01.mp3``、标签又全空的文件，
永远整理不出来。v0.2 补上查询能力，但守住三条：

1. **默认关闭**（``--lookup``）。普通用户不该为了整理本地文件去等网络。
2. **只产建议，不写盘**。匹配结果进报告和方案，标签写不写由用户决定。
   上游的 ``import -q`` 是把匹配直接落盘，猜错了也照写 —— 这里不做。
3. **只查信息不足的文件**，且结果落本地缓存，同一首歌第二次零请求。

网络实现只用标准库（``urllib``），不引入新依赖。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .naming import normalize_key

__all__ = [
    "LookupCache",
    "LookupHit",
    "MusicBrainzProvider",
    "NullProvider",
    "Provider",
    "cache_key",
    "collect_suggestions",
]

USER_AGENT = "discrate/0.2.0 ( https://github.com/linmoxi33/discrate )"
MIN_INTERVAL = 1.0  # MusicBrainz 要求每秒最多一次


@dataclass(frozen=True)
class LookupHit:
    artist: str = ""
    album: str = ""
    title: str = ""
    year: int = 0
    score: float = 0.0
    source: str = ""

    @property
    def label(self) -> str:
        bits = [b for b in (self.artist, self.album, self.title) if b]
        text = " / ".join(bits) or "（无信息）"
        if self.year:
            text += f"（{self.year}）"
        return text


class Provider(Protocol):
    name: str

    def search(self, artist: str, title: str) -> list[LookupHit]: ...


class NullProvider:
    """不联网的占位实现，用于测试和 ``--lookup`` 关闭时的默认路径。"""

    name = "none"

    def search(self, artist: str, title: str) -> list[LookupHit]:
        return []


class MusicBrainzProvider:
    name = "musicbrainz"

    def __init__(
        self,
        *,
        min_interval: float = MIN_INTERVAL,
        timeout: float = 8.0,
        limit: int = 3,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.limit = limit
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def search(self, artist: str, title: str) -> list[LookupHit]:
        if not title.strip():
            return []
        query = f'recording:"{title}"'
        if artist.strip():
            query += f' AND artist:"{artist}"'
        url = (
            "https://musicbrainz.org/ws/2/recording?query="
            + urllib.parse.quote(query)
            + f"&fmt=json&limit={self.limit}"
        )
        self._throttle()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # 网络问题的形态太多，统一降级为「没有建议」
            return []

        hits: list[LookupHit] = []
        for recording in payload.get("recordings", [])[: self.limit]:
            credits = recording.get("artist-credit") or []
            name = "".join(
                str(part.get("name", ""))
                for part in credits
                if isinstance(part, dict)
            )
            releases = recording.get("releases") or []
            album = ""
            year = 0
            if releases:
                album = str(releases[0].get("title", ""))
                date = str(releases[0].get("date", ""))
                if len(date) >= 4 and date[:4].isdigit():
                    year = int(date[:4])
            hits.append(
                LookupHit(
                    artist=name,
                    album=album,
                    title=str(recording.get("title", "")),
                    year=year,
                    score=float(recording.get("score", 0)) / 100.0,
                    source=self.name,
                )
            )
        return hits


def cache_key(artist: str, title: str) -> str:
    return f"{normalize_key(artist)}|{normalize_key(title)}"


class LookupCache:
    """把查询结果落盘，避免每次整理都重新联网。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, list[dict]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = payload.get("entries")
        if isinstance(entries, dict):
            self._data = {
                k: v for k, v in entries.items() if isinstance(v, list)
            }

    def get(self, key: str) -> list[LookupHit] | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        hits: list[LookupHit] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            hits.append(
                LookupHit(
                    artist=str(item.get("artist", "")),
                    album=str(item.get("album", "")),
                    title=str(item.get("title", "")),
                    year=int(item.get("year", 0) or 0),
                    score=float(item.get("score", 0.0) or 0.0),
                    source=str(item.get("source", "")),
                )
            )
        return hits

    def put(self, key: str, hits: list[LookupHit]) -> None:
        self._data[key] = [asdict(hit) for hit in hits]
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": self._data}
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            self._dirty = False
        except OSError:
            pass


def collect_suggestions(
    tracks,
    provider: Provider,
    cache: LookupCache | None = None,
    *,
    limit: int = 20,
    should_lookup=None,
) -> dict[str, list[LookupHit]]:
    """为信息不足的文件批量查询，返回 ``{源相对路径: [命中]}``。

    ``limit`` 是查询上限 —— 1 req/s 的限速下，几百首全查会让用户等到放弃。
    """
    out: dict[str, list[LookupHit]] = {}
    used = 0
    for track in tracks:
        if used >= limit:
            break
        title = str(track.fields.get("title") or "").strip()
        artist = str(track.fields.get("artist") or "").strip()
        if should_lookup is not None and not should_lookup(track):
            continue
        key = cache_key(artist, title)
        if cache is not None:
            cached = cache.get(key)
            if cached is not None:
                if cached:
                    out[str(track.rel)] = cached
                continue
        hits = provider.search(artist, title)
        used += 1
        if cache is not None:
            cache.put(key, hits)
        if hits:
            out[str(track.rel)] = hits
    if cache is not None:
        cache.save()
    return out
