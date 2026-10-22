"""扫描与推断。"""

from __future__ import annotations

import pytest

from discrate.scan import parse_filename, parse_folder, scan_folder


class TestParseFilename:
    @pytest.mark.parametrize(
        "stem,expected",
        [
            ("01 - 晴天", {"track": 1, "title": "晴天"}),
            ("01. 晴天", {"track": 1, "title": "晴天"}),
            ("1_晴天", {"track": 1, "title": "晴天"}),
            ("03 以父之名", {"track": 3, "title": "以父之名"}),
        ],
    )
    def test_track_and_title(self, stem, expected):
        assert parse_filename(stem) == expected

    def test_artist_and_title(self):
        assert parse_filename("周杰伦 - 稻香") == {"artist": "周杰伦", "title": "稻香"}

    def test_dash_without_spaces_is_not_split(self):
        """AC-DC 这种连字符不能被当成「歌手 - 歌名」的分隔符。"""
        assert parse_filename("AC-DC") == {"title": "AC-DC"}

    def test_pure_digits_gives_track_only(self):
        """`01.mp3` 不能变成一首叫「01」的歌。"""
        assert parse_filename("01") == {"track": 1}
        assert parse_filename("007") == {"track": 7}

    def test_downloader_copy_suffix_stripped(self):
        assert parse_filename("晴天 (1)") == {"title": "晴天"}
        assert parse_filename("晴天 - 副本") == {"title": "晴天"}

    def test_plain_title(self):
        assert parse_filename("夜曲") == {"title": "夜曲"}

    def test_empty(self):
        assert parse_filename("   ") == {}


class TestParseFolder:
    def test_artist_and_album(self):
        assert parse_folder("周杰伦 - 叶惠美") == {"artist": "周杰伦", "album": "叶惠美"}

    def test_trailing_year(self):
        out = parse_folder("周杰伦 - 叶惠美 (2003)")
        assert out == {"artist": "周杰伦", "album": "叶惠美", "year": 2003}

    def test_bare_album_name(self):
        assert parse_folder("叶惠美") == {"album": "叶惠美"}

    @pytest.mark.parametrize(
        "name", ["下载", "Downloads", "音乐", "Music", "新建文件夹", "桌面", "整理结果"]
    )
    def test_generic_dirs_ignored(self, name):
        assert parse_folder(name) == {}

    def test_untrusted_single_file_dir(self):
        """目录里只有一个文件时，目录名更可能是描述性的，不能当专辑名。"""
        assert parse_folder("同名不同码率", trusted=False) == {}
        assert parse_folder("叶惠美", trusted=False) == {}

    def test_empty(self):
        assert parse_folder("") == {}


class TestScanFolder:
    def test_counts_and_ignores(self, sample_dir):
        result = scan_folder(sample_dir)
        assert len(result.tracks) == 8
        ignored_names = {item.path.name for item in result.ignored}
        assert "readme.txt" in ignored_names
        assert not any(t.path.suffix == ".txt" for t in result.tracks)

    def test_empty_file_reports_error(self, sample_dir):
        result = scan_folder(sample_dir)
        broken = [t for t in result.tracks if t.path.name == "空文件.mp3"]
        assert len(broken) == 1
        assert broken[0].error == "空文件（0 字节）"

    def test_root_level_files_get_no_folder_hints(self, sample_dir):
        result = scan_folder(sample_dir)
        track = next(t for t in result.tracks if t.path.name == "周杰伦 - 稻香.mp3")
        # 根目录名「音乐」是通用名，且根目录本身不参与专辑推断
        assert track.fields.get("album") in ("", None)

    def test_root_level_guard_survives_non_generic_name(self, tmp_path, make_track):
        """根目录名不是通用词、且目录里有多个文件时，也必须靠 at_root 挡住。

        单文件目录会被 ``trusted`` 规则一并挡住，那样测不出 at_root 本身，
        所以这里刻意放两个文件。
        """
        root = tmp_path / "我的收藏"
        root.mkdir()
        make_track("我的收藏/晴天.mp3", freq=300)
        make_track("我的收藏/夜曲.mp3", freq=320)
        result = scan_folder(root)
        assert len(result.tracks) == 2
        for track in result.tracks:
            assert track.fields.get("album") in ("", None)

    def test_nested_dir_with_non_generic_name_still_infers(self, tmp_path, make_track):
        """对照组：同样的名字出现在子目录里、且有两首歌，就应该推断。"""
        root = tmp_path / "我的收藏"
        root.mkdir()
        make_track("我的收藏/晴天.mp3", freq=300)
        make_track("我的收藏/叶惠美/东风破.mp3", freq=320)
        make_track("我的收藏/叶惠美/夜曲.mp3", freq=340)
        result = scan_folder(root)
        track = next(t for t in result.tracks if t.path.name == "东风破.mp3")
        assert track.fields.get("album") == "叶惠美"

    def test_single_file_dir_no_album(self, tmp_path, make_track):
        root = tmp_path / "下载"
        root.mkdir()
        make_track("下载/晴天.mp3", freq=300)
        result = scan_folder(root)
        assert result.tracks[0].fields.get("album") in ("", None)

    def test_multi_file_dir_album_inferred(self, sample_dir):
        result = scan_folder(sample_dir)
        track = next(t for t in result.tracks if t.path.name == "夜曲.mp3")
        assert track.fields.get("album") == "叶惠美"
        assert track.fields.get("artist") == "周杰伦"

    def test_hash_stable_and_distinct(self, sample_dir):
        result = scan_folder(sample_dir)
        by_name = {t.path.name: t for t in result.tracks}
        assert by_name["周杰伦 - 稻香.mp3"].sha256 == by_name["稻香 - 副本.mp3"].sha256
        assert by_name["周杰伦 - 稻香.mp3"].sha256 != by_name["02 - 晴天.mp3"].sha256

    def test_exclude_output_dir(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/a.mp3", freq=300)
        out = root / "整理结果"
        out.mkdir()
        make_track("音乐/整理结果/b.mp3", freq=320)
        result = scan_folder(root, exclude=[out])
        assert [t.path.name for t in result.tracks] == ["a.mp3"]

    def test_max_files(self, sample_dir):
        result = scan_folder(sample_dir, max_files=3)
        assert len(result.tracks) == 3
        assert any("上限" in item.reason for item in result.ignored)

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            scan_folder(tmp_path / "nope")

    def test_file_instead_of_dir_raises(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            scan_folder(f)

    def test_tag_source_recorded(self, sample_dir):
        result = scan_folder(sample_dir)
        tagged = next(t for t in result.tracks if t.path.name == "完整标签.mp3")
        assert tagged.sources["artist"] == "tag"
        inferred = next(t for t in result.tracks if t.path.name == "周杰伦 - 稻香.mp3")
        assert inferred.sources["artist"] == "filename"
