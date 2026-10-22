"""封面归位。"""

from __future__ import annotations

import mediafile


def _tag(path, **fields):
    mf = mediafile.MediaFile(str(path))
    for key, value in fields.items():
        setattr(mf, key, value)
    mf.save()
    return path


class TestPickCover:
    def test_empty(self):
        from discrate.scan import pick_cover

        assert pick_cover([]) is None

    def test_single_named(self, make_cover):
        from discrate.scan import pick_cover

        cover = make_cover("cover.jpg")
        assert pick_cover([cover]) == cover

    def test_priority_cover_over_folder(self, make_cover):
        from discrate.scan import pick_cover

        folder = make_cover("folder.jpg")
        cover = make_cover("cover.jpg")
        assert pick_cover([folder, cover]) == cover

    def test_priority_folder_over_front(self, make_cover):
        from discrate.scan import pick_cover

        front = make_cover("front.jpg")
        folder = make_cover("folder.jpg")
        assert pick_cover([front, folder]) == folder

    def test_case_insensitive(self, make_cover):
        from discrate.scan import pick_cover

        cover = make_cover("COVER.JPG")
        assert pick_cover([cover]) == cover

    def test_single_unnamed_is_accepted(self, make_cover):
        """目录里只有一张图时，名字不标准也用得上。"""
        from discrate.scan import pick_cover

        only = make_cover("扫描0001.jpg")
        assert pick_cover([only]) == only

    def test_multiple_unnamed_is_refused(self, make_cover):
        """一整个目录十几张扫图时随便挑一张，比没有封面更糟。"""
        from discrate.scan import pick_cover

        a = make_cover("扫描0001.jpg")
        b = make_cover("扫描0002.jpg")
        assert pick_cover([a, b]) is None

    def test_named_wins_over_many_unnamed(self, make_cover):
        from discrate.scan import pick_cover

        a = make_cover("扫描0001.jpg")
        b = make_cover("扫描0002.jpg")
        cover = make_cover("cover.jpg")
        assert pick_cover([a, b, cover]) == cover


class TestScanImages:
    def test_images_collected_by_folder(self, sample_dir):
        from discrate.scan import scan_folder

        result = scan_folder(sample_dir)
        album_dir = sample_dir / "周杰伦 - 叶惠美"
        assert album_dir in result.images
        assert [p.name for p in result.images[album_dir]] == ["cover.jpg"]

    def test_images_not_counted_as_ignored(self, sample_dir):
        from discrate.scan import scan_folder

        result = scan_folder(sample_dir)
        names = {item.path.name for item in result.ignored}
        assert "cover.jpg" not in names


class TestCoversInPlan:
    def test_album_dir_gets_cover(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        scan = scan_folder(sample_dir, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        assert len(plan.covers) == 1
        album_dir, source = next(iter(plan.covers.items()))
        assert album_dir == target / "周杰伦" / "叶惠美"
        assert source.name == "cover.jpg"

    def test_root_cover_not_assigned(self, tmp_path, make_track, make_cover):
        """根目录的封面属于「哪个专辑」无从判断，不能瞎贴。"""
        from discrate.decide import build_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/a.mp3", freq=300, tags={"title": "A", "artist": "X", "album": "X1", "albumartist": "X"})
        make_track("音乐/b.mp3", freq=320, tags={"title": "B", "artist": "Y", "album": "Y1", "albumartist": "Y"})
        make_cover("音乐/cover.jpg")

        target = root / "整理结果"
        scan = scan_folder(root, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        assert plan.covers == {}

    def test_single_album_root_does_get_cover(self, tmp_path, make_track, make_cover):
        """对照组：根目录只有一张专辑时，封面归属是明确的。"""
        from discrate.decide import build_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        root = tmp_path / "音乐"
        root.mkdir()
        for index, name in enumerate(("A", "B"), start=1):
            make_track(
                f"音乐/{index} - {name}.mp3",
                freq=300 + index * 20,
                tags={"artist": "X", "album": "X1", "albumartist": "X"},
            )
        make_cover("音乐/cover.jpg")

        target = root / "整理结果"
        scan = scan_folder(root, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        assert len(plan.covers) == 1

    def test_no_album_no_cover(self, tmp_path, make_track, make_cover):
        """单曲路径里混着不同专辑的歌，塞封面会误导。"""
        from discrate.decide import build_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/晴天.mp3", freq=300)
        make_cover("音乐/cover.jpg")

        target = root / "整理结果"
        scan = scan_folder(root, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        assert plan.covers == {}


class TestApplyCovers:
    def test_cover_copied(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.execute import apply_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        scan = scan_folder(sample_dir, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        result = apply_plan(plan)
        assert result.covers == 1
        assert (target / "周杰伦" / "叶惠美" / "cover.jpg").exists()

    def test_cover_in_journal(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.execute import apply_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        scan = scan_folder(sample_dir, exclude=[target])
        result = apply_plan(build_plan(scan, get_profile("safe"), target_root=target))
        ops = [line for line in result.journal_path.read_text(encoding="utf-8").splitlines() if '"cover"' in line]
        assert len(ops) == 1

    def test_existing_cover_not_overwritten(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.execute import apply_plan
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        album = target / "周杰伦" / "叶惠美"
        album.mkdir(parents=True)
        (album / "cover.jpg").write_bytes(b"user own cover")

        scan = scan_folder(sample_dir, exclude=[target])
        result = apply_plan(build_plan(scan, get_profile("safe"), target_root=target))
        assert (album / "cover.jpg").read_bytes() == b"user own cover"
        assert result.covers == 0

    def test_undo_removes_cover(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.execute import apply_plan, undo_journal
        from discrate.profile import get_profile
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        scan = scan_folder(sample_dir, exclude=[target])
        result = apply_plan(build_plan(scan, get_profile("safe"), target_root=target))
        cover = target / "周杰伦" / "叶惠美" / "cover.jpg"
        assert cover.exists()
        undo_journal(result.journal_path)
        assert not cover.exists()

    def test_cover_count_in_report(self, sample_dir):
        from discrate.decide import build_plan
        from discrate.execute import apply_plan
        from discrate.profile import get_profile
        from discrate.report import render_result
        from discrate.scan import scan_folder

        target = sample_dir / "整理结果"
        scan = scan_folder(sample_dir, exclude=[target])
        plan = build_plan(scan, get_profile("safe"), target_root=target)
        result = apply_plan(plan)
        assert "封面" in render_result(plan, result)
