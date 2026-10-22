"""决策引擎。"""

from __future__ import annotations

from discrate.decide import UNKNOWN_ARTIST, build_plan
from discrate.profile import get_profile
from discrate.scan import scan_folder

SAFE = get_profile("safe")


def plan_for(root, target=None, previous=None, dedupe=True):
    scan = scan_folder(root, exclude=[target] if target else ())
    return build_plan(
        scan, SAFE, target_root=target, previous=previous, dedupe=dedupe
    )


def by_name(plan):
    return {d.track.path.name: d for d in plan.decisions}


class TestPaths:
    def test_full_tags_go_to_album_path(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["完整标签.mp3"]
        assert d.target.name == "03 以父之名.mp3"
        assert d.target.parent.name == "叶惠美"
        assert d.target.parent.parent.name == "周杰伦"

    def test_no_album_goes_to_single(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["周杰伦 - 稻香.mp3"]
        assert d.target.parent.name == "周杰伦"
        assert d.target.parent.parent.name == "单曲"

    def test_unknown_artist_placeholder(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["02 - 晴天.mp3"]
        assert UNKNOWN_ARTIST in str(d.target)
        assert d.confidence == "low"
        assert "unknown-artist" in d.reason_codes()

    def test_target_root_inside_source_not_rescanned(self, sample_dir):
        target = sample_dir / "整理结果"
        plan = plan_for(sample_dir, target=target)
        assert all(target not in d.track.path.parents for d in plan.decisions)


class TestDedupe:
    def test_identical_content_detected(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["稻香 - 副本.mp3"]
        assert d.action == "duplicate"
        assert "identical-content" in d.reason_codes()

    def test_same_song_keeps_larger(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/晴天.mp3", freq=320, seconds=0.3)
        make_track("音乐/02 - 晴天.mp3", freq=320, seconds=1.2)
        plan = plan_for(root)
        decided = {d.track.path.name: d for d in plan.decisions}
        # 1.2 秒那份体积更大，应该被保留；小的那份判重复。
        assert decided["02 - 晴天.mp3"].action == "copy"
        assert decided["晴天.mp3"].action == "duplicate"
        assert "same-song" in decided["晴天.mp3"].reason_codes()

    def test_cross_batch_dedupe(self, sample_dir):
        """上一轮已经整理过的文件，这一轮要能挡住同内容副本。"""
        target = sample_dir / "整理结果"
        first = plan_for(sample_dir, target=target)
        keep = next(d for d in first.decisions if d.track.path.name == "周杰伦 - 稻香.mp3")
        target.mkdir(parents=True, exist_ok=True)
        keep.target.parent.mkdir(parents=True, exist_ok=True)
        keep.target.write_bytes(keep.track.path.read_bytes())
        previous = {
            "周杰伦 - 稻香.mp3": {
                "sha256": keep.track.sha256,
                "target": str(keep.target),
                "key": list(keep.track.dedupe_key),
                "size": keep.track.size,
                "target_size": keep.target.stat().st_size,
            }
        }
        plan = plan_for(sample_dir, target=target, previous=previous)
        dup = next(d for d in plan.decisions if d.track.path.name == "稻香 - 副本.mp3")
        assert dup.action == "duplicate"

    def test_already_done_skipped(self, sample_dir):
        target = sample_dir / "整理结果"
        first = plan_for(sample_dir, target=target)
        done = next(d for d in first.decisions if d.track.path.name == "完整标签.mp3")
        done.target.parent.mkdir(parents=True, exist_ok=True)
        done.target.write_bytes(done.track.path.read_bytes())
        previous = {
            "完整标签.mp3": {
                "sha256": done.track.sha256,
                "target": str(done.target),
                "key": list(done.track.dedupe_key),
                "size": done.track.size,
            }
        }
        plan = plan_for(sample_dir, target=target, previous=previous)
        again = by_name(plan)["完整标签.mp3"]
        assert again.action == "skip"
        assert "already-done" in again.reason_codes()

    def test_skipped_file_is_not_dedupe_base(self, tmp_path, make_track):
        """被跳过的文件不能当去重基准，否则两份都不落盘，内容凭空消失。"""
        root = tmp_path / "音乐"
        root.mkdir()
        first = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        (root / "稻香 - 副本.mp3").write_bytes(first.read_bytes())
        target = root / "整理结果"
        occupied = target / "单曲" / "周杰伦" / "稻香.mp3"
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b"occupied")
        plan = plan_for(root, target=target)
        decided = {d.track.path.name: d for d in plan.decisions}
        assert decided["周杰伦 - 稻香.mp3"].action == "skip"
        assert decided["稻香 - 副本.mp3"].action != "duplicate"

    def test_stale_state_is_ignored(self, sample_dir):
        """登记表里有记录但目标文件已被删 —— 应该重新整理。"""
        target = sample_dir / "整理结果"
        previous = {
            "完整标签.mp3": {
                "sha256": "deadbeef",
                "target": str(target / "已删除.mp3"),
                "key": ["周杰伦", "以父之名"],
                "size": 1,
            }
        }
        plan = plan_for(sample_dir, target=target, previous=previous)
        again = by_name(plan)["完整标签.mp3"]
        assert again.action == "copy"

    def test_changed_content_is_reprocessed(self, sample_dir, make_track):
        target = sample_dir / "整理结果"
        previous = {
            "完整标签.mp3": {
                "sha256": "0000",
                "target": str(target / "x.mp3"),
                "key": ["周杰伦", "以父之名"],
                "size": 1,
            }
        }
        (target).mkdir(parents=True, exist_ok=True)
        (target / "x.mp3").write_bytes(b"placeholder")
        plan = plan_for(sample_dir, target=target, previous=previous)
        assert by_name(plan)["完整标签.mp3"].action == "copy"


class TestConflicts:
    def test_existing_target_is_skipped(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        target = root / "整理结果"
        existing = target / "单曲" / "周杰伦" / "稻香.mp3"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old")
        plan = plan_for(root, target=target)
        d = by_name(plan)["周杰伦 - 稻香.mp3"]
        assert d.action == "skip"
        assert "target-exists" in d.reason_codes()

    def test_name_collision_gets_numbered(self, tmp_path, make_track):
        """两个不同内容的文件渲染出同一个目标名时，后者加编号。

        默认档位会先做去重判定，同名不同内容几乎都会被判成「同一首歌」，
        所以这里关掉去重来验证撞名兜底逻辑本身。
        """
        root = tmp_path / "音乐"
        root.mkdir()
        tags = {"artist": "A", "album": "X", "albumartist": "A", "title": "Song", "track": 1}
        make_track("音乐/甲/01 - Song.mp3", freq=300, tags=tags)
        make_track("音乐/乙/01 - Song.mp3", freq=520, tags=tags)
        plan = plan_for(root, dedupe=False)
        names = sorted(d.target.name for d in plan.writes)
        assert len(names) == 2
        assert "01 Song.mp3" in names
        assert any("(2)" in n for n in names)
        assert any("name-collision" in d.reason_codes() for d in plan.writes)

    def test_no_dedupe_keeps_everything(self, sample_dir):
        plan = plan_for(sample_dir, dedupe=False)
        assert plan.by_action("duplicate") == []
        # 8 个音频里，01.mp3 没有曲名、空文件读不出来，其余 6 个都应整理
        assert len(plan.writes) == 6


class TestClassification:
    def test_missing_title_is_review(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["01.mp3"]
        assert d.action == "review"
        assert "no-title" in d.reason_codes()

    def test_unreadable_is_reject(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["空文件.mp3"]
        assert d.action == "reject"
        assert "unreadable" in d.reason_codes()

    def test_needs_review_excludes_review_and_reject(self, sample_dir):
        plan = plan_for(sample_dir)
        names = {d.track.path.name for d in plan.review}
        assert "01.mp3" not in names
        assert "空文件.mp3" not in names
        assert "02 - 晴天.mp3" in names

    def test_high_confidence_for_tagged(self, sample_dir):
        plan = plan_for(sample_dir)
        assert by_name(plan)["完整标签.mp3"].confidence == "high"

    def test_medium_confidence_for_filename_artist(self, sample_dir):
        plan = plan_for(sample_dir)
        assert by_name(plan)["周杰伦 - 稻香.mp3"].confidence == "medium"

    def test_decisions_sorted_by_source_path(self, sample_dir):
        plan = plan_for(sample_dir)
        keys = [str(d.track.rel).casefold() for d in plan.decisions]
        assert keys == sorted(keys)


class TestFilledFields:
    def test_inferred_fields_are_written(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["周杰伦 - 稻香.mp3"]
        assert d.filled.get("artist") == "周杰伦"
        assert d.filled.get("title") == "稻香"

    def test_tagged_fields_not_rewritten(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["完整标签.mp3"]
        assert d.filled == {}

    def test_unknown_artist_not_written(self, sample_dir):
        plan = plan_for(sample_dir)
        d = by_name(plan)["02 - 晴天.mp3"]
        assert d.filled.get("artist") != UNKNOWN_ARTIST


class TestStats:
    def test_stats_consistent(self, sample_dir):
        plan = plan_for(sample_dir)
        stats = plan.stats()
        assert stats["total"] == len(plan.decisions)
        assert stats["copy"] + stats["duplicate"] + stats["review"] + stats["reject"] + stats["skip"] == stats["total"]

    def test_bytes_out_matches_writes(self, sample_dir):
        plan = plan_for(sample_dir)
        assert plan.bytes_out == sum(d.track.size for d in plan.writes)
