"""在线元数据建议。"""

from __future__ import annotations

import json

from discrate.decide import build_plan
from discrate.lookup import (
    LookupCache,
    LookupHit,
    MusicBrainzProvider,
    NullProvider,
    cache_key,
    collect_suggestions,
)
from discrate.profile import get_profile
from discrate.report import render_plan, to_json
from discrate.scan import scan_folder

SAFE = get_profile("safe")


class FakeProvider:
    name = "fake"

    def __init__(self, table: dict[tuple[str, str], list[LookupHit]]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def search(self, artist: str, title: str) -> list[LookupHit]:
        self.calls.append((artist, title))
        return self.table.get((artist, title), [])


def hit(artist="周杰伦", album="叶惠美", title="晴天", year=2003, score=0.95):
    return LookupHit(
        artist=artist, album=album, title=title, year=year, score=score, source="fake"
    )


class TestCacheKey:
    def test_normalizes(self):
        assert cache_key("周杰伦", "晴天") == cache_key("周杰伦 ", "晴天")

    def test_case_and_punctuation(self):
        assert cache_key("Jay", "Qing-Tian") == cache_key("jay", "qingtian")

    def test_differs(self):
        assert cache_key("A", "B") != cache_key("A", "C")


class TestLookupCache:
    def test_miss_on_empty(self, tmp_path):
        cache = LookupCache(tmp_path / "c.json")
        assert cache.get("x") is None

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "c.json"
        cache = LookupCache(path)
        cache.put("k", [hit()])
        cache.save()

        again = LookupCache(path)
        hits = again.get("k")
        assert hits is not None
        assert hits[0].title == "晴天"
        assert hits[0].year == 2003

    def test_empty_result_is_cached(self, tmp_path):
        """查过没结果也要记住，否则每次都会重新发请求。"""
        path = tmp_path / "c.json"
        cache = LookupCache(path)
        cache.put("k", [])
        cache.save()
        assert LookupCache(path).get("k") == []

    def test_corrupt_file_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json", encoding="utf-8")
        assert LookupCache(path).get("k") is None

    def test_wrong_shape_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"entries": []}), encoding="utf-8")
        assert LookupCache(path).get("k") is None

    def test_no_save_when_clean(self, tmp_path):
        path = tmp_path / "c.json"
        LookupCache(path).save()
        assert not path.exists()


class TestCollectSuggestions:
    def test_basic(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        make_track("t/晴天.mp3", freq=300)
        scan = scan_folder(tmp_path / "t")
        provider = FakeProvider({("", "晴天"): [hit()]})
        out = collect_suggestions(scan.tracks, provider)
        assert len(out) == 1
        assert next(iter(out.values()))[0].title == "晴天"

    def test_limit_respected(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        for index in range(5):
            make_track(f"t/{index}.mp3", freq=300 + index * 10, tags={"title": f"T{index}"})
        scan = scan_folder(tmp_path / "t")
        provider = FakeProvider({})
        collect_suggestions(scan.tracks, provider, limit=2)
        assert len(provider.calls) == 2

    def test_should_lookup_filters(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        make_track("t/a.mp3", freq=300, tags={"title": "A", "artist": "X"})
        make_track("t/b.mp3", freq=320, tags={"title": "B", "artist": "Y"})
        scan = scan_folder(tmp_path / "t")
        provider = FakeProvider({})
        collect_suggestions(
            scan.tracks,
            provider,
            should_lookup=lambda t: t.fields.get("title") == "A",
        )
        assert provider.calls == [("X", "A")]

    def test_cache_prevents_second_request(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        make_track("t/晴天.mp3", freq=300)
        scan = scan_folder(tmp_path / "t")
        cache = LookupCache(tmp_path / "cache.json")
        provider = FakeProvider({("", "晴天"): [hit()]})

        collect_suggestions(scan.tracks, provider, cache)
        assert len(provider.calls) == 1

        provider2 = FakeProvider({})
        out = collect_suggestions(scan.tracks, provider2, cache)
        assert provider2.calls == []
        assert len(out) == 1

    def test_cached_empty_skips_request(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        make_track("t/晴天.mp3", freq=300)
        scan = scan_folder(tmp_path / "t")
        cache = LookupCache(tmp_path / "cache.json")
        collect_suggestions(scan.tracks, FakeProvider({}), cache)

        provider = FakeProvider({})
        out = collect_suggestions(scan.tracks, provider, cache)
        assert provider.calls == []
        assert out == {}

    def test_null_provider(self, tmp_path, make_track):
        from discrate.scan import scan_folder

        make_track("t/晴天.mp3", freq=300)
        scan = scan_folder(tmp_path / "t")
        assert collect_suggestions(scan.tracks, NullProvider()) == {}


class TestLookupHit:
    def test_label_full(self):
        assert hit().label == "周杰伦 / 叶惠美 / 晴天（2003）"

    def test_label_partial(self):
        assert LookupHit(title="晴天").label == "晴天"

    def test_label_empty(self):
        assert LookupHit().label == "（无信息）"

    def test_label_without_year(self):
        assert LookupHit(artist="A", title="T").label == "A / T"


class TestMusicBrainzProvider:
    def test_empty_title_makes_no_request(self):
        provider = MusicBrainzProvider(min_interval=0.0)
        assert provider.search("某歌手", "   ") == []


class TestPlanIntegration:
    def test_suggestions_injected(self, sample_dir):
        scan = scan_folder(sample_dir, exclude=[sample_dir / "整理结果"])
        rel = next(str(t.rel) for t in scan.tracks if t.path.name == "02 - 晴天.mp3")
        plan = build_plan(
            scan, SAFE, target_root=sample_dir / "整理结果", suggestions={rel: [hit()]}
        )
        decision = next(d for d in plan.decisions if d.track.path.name == "02 - 晴天.mp3")
        assert decision.suggestions[0].title == "晴天"

    def test_no_suggestions_by_default(self, sample_dir):
        scan = scan_folder(sample_dir, exclude=[sample_dir / "整理结果"])
        plan = build_plan(scan, SAFE, target_root=sample_dir / "整理结果")
        assert all(d.suggestions == [] for d in plan.decisions)

    def test_render_shows_suggestions(self, sample_dir):
        scan = scan_folder(sample_dir, exclude=[sample_dir / "整理结果"])
        rel = next(str(t.rel) for t in scan.tracks if t.path.name == "02 - 晴天.mp3")
        plan = build_plan(
            scan, SAFE, target_root=sample_dir / "整理结果", suggestions={rel: [hit()]}
        )
        text = render_plan(plan)
        assert "在线匹配建议" in text
        assert "晴天" in text
        assert "不会自动写进标签" in text

    def test_suggestions_do_not_change_action(self, sample_dir):
        """建议绝不影响落盘决策。"""
        scan = scan_folder(sample_dir, exclude=[sample_dir / "整理结果"])
        before = build_plan(scan, SAFE, target_root=sample_dir / "整理结果")
        rel = str(before.decisions[0].track.rel)
        after = build_plan(
            scan, SAFE, target_root=sample_dir / "整理结果", suggestions={rel: [hit()]}
        )
        assert [d.action for d in before.decisions] == [d.action for d in after.decisions]
        assert [d.target for d in before.decisions] == [d.target for d in after.decisions]

    def test_json_contains_suggestions(self, sample_dir):
        scan = scan_folder(sample_dir, exclude=[sample_dir / "整理结果"])
        rel = next(str(t.rel) for t in scan.tracks if t.path.name == "02 - 晴天.mp3")
        plan = build_plan(
            scan, SAFE, target_root=sample_dir / "整理结果", suggestions={rel: [hit()]}
        )
        payload = to_json(plan)
        found = [d for d in payload["decisions"] if d["suggestions"]]
        assert len(found) == 1
        assert found[0]["suggestions"][0]["score"] == 0.95
