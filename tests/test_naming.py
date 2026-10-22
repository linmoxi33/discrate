"""模板渲染与文件名安全化。"""

from __future__ import annotations

from pathlib import Path

import pytest

from discrate.bridge import safe_component
from discrate.naming import (
    TemplateError,
    build_target,
    normalize_key,
    render_segments,
    translate_beets_template,
)


class TestRenderSegments:
    def test_full_fields(self):
        out = render_segments(
            "{albumartist}/{album}/{track:02d} {title}",
            {"albumartist": "周杰伦", "album": "叶惠美", "track": 3, "title": "以父之名"},
        )
        assert out.segments == ("周杰伦", "叶惠美", "03 以父之名")

    def test_empty_field_drops_whole_segment(self):
        """上游会把缺失字段渲染成空段，产生 `//` 或 `Non-Album.mp3`。"""
        out = render_segments(
            "{albumartist}/{album}/{track:02d} {title}",
            {"albumartist": "周杰伦", "album": "", "track": 0, "title": "晴天"},
        )
        assert out.segments == ("周杰伦", "晴天")
        assert out.dropped == ("{album}",)

    def test_zero_track_is_treated_as_missing(self):
        out = render_segments("{track:02d} {title}", {"track": 0, "title": "夜曲"})
        assert out.segments == ("夜曲",)

    def test_slash_inside_field_does_not_create_directories(self):
        """album='Back:In/Black' 曾凭空多出两级目录。"""
        out = render_segments(
            "{albumartist}/{album}/{title}",
            {"albumartist": "AC/DC", "album": "Back:In/Black", "title": "Hells Bells"},
        )
        assert out.segments == ("AC_DC", "Back:In_Black", "Hells Bells")

    def test_backslash_inside_field_is_escaped(self):
        out = render_segments("{title}", {"title": "a\\b"})
        assert out.segments == ("a_b",)

    def test_all_empty_gives_empty_result(self):
        out = render_segments("{albumartist}/{album}/{title}", {})
        assert out.empty is True

    def test_unknown_field_is_treated_as_empty(self):
        out = render_segments("{nope}/{title}", {"title": "x"})
        assert out.segments == ("x",)

    def test_literal_only_template(self):
        out = render_segments("单曲/{title}", {"title": "稻香"})
        assert out.segments == ("单曲", "稻香")

    def test_empty_placeholder_raises(self):
        with pytest.raises(TemplateError):
            render_segments("{}/x", {})


class TestBeetsSyntax:
    def test_dollar_fields_translate(self):
        assert (
            translate_beets_template("$albumartist/$album/$track $title")
            == "{albumartist}/{album}/{track:02d} {title}"
        )

    def test_dollar_track_padded(self):
        out = render_segments("$albumartist/$track $title", {"albumartist": "A", "track": 2, "title": "T"})
        assert out.segments == ("A", "02 T")

    def test_function_syntax_rejected(self):
        with pytest.raises(TemplateError) as exc:
            translate_beets_template("$albumartist/$album%aunique{}/$track $title")
        assert "aunique" in str(exc.value)

    def test_brace_syntax_passthrough(self):
        fmt = "{albumartist}/{title}"
        assert translate_beets_template(fmt) == fmt


class TestSafeComponent:
    @pytest.mark.parametrize("raw", ["CON", "nul", "PRN", "com1", "LPT9"])
    def test_windows_reserved_names(self, raw):
        """beets 的 sanitize_path 不处理保留名，直接落盘会失败。"""
        assert safe_component(raw).startswith("_")

    def test_reserved_with_extension(self):
        assert safe_component("CON.mp3").startswith("_")

    def test_illegal_chars_replaced(self):
        assert safe_component('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"

    def test_slash_replaced(self):
        assert safe_component("AC/DC") == "AC_DC"

    def test_trailing_dot_and_space_stripped(self):
        assert safe_component("name. ") == "name"
        assert safe_component("name...") == "name"

    def test_leading_space_stripped(self):
        assert safe_component("   name") == "name"

    def test_ascii_length_truncated(self):
        out = safe_component("a" * 300, max_len=120)
        assert len(out) == 120

    def test_chinese_char_limit(self):
        out = safe_component("长" * 300, max_len=120)
        assert len(out) <= 120

    def test_chinese_byte_limit(self):
        """115 个汉字 = 345 字节 > Linux 单段 255 字节上限，必须按字节再截一次。"""
        out = safe_component("长" * 115)
        assert len(out.encode("utf-8")) <= 200
        assert len(out) > 0

    def test_chinese_byte_limit_no_half_char(self):
        """按字节截断不能切出半个 UTF-8 字符。"""
        out = safe_component("长" * 115)
        assert out.encode("utf-8").decode("utf-8") == out

    def test_empty_becomes_underscore(self):
        assert safe_component("") == "_"
        assert safe_component("   ") == "_"

    def test_chinese_preserved(self):
        assert safe_component("周杰伦 - 稻香") == "周杰伦 - 稻香"


class TestNormalizeKey:
    def test_punctuation_and_space_ignored(self):
        assert normalize_key("周杰伦 - 稻香") == normalize_key("周杰伦-稻香")
        assert normalize_key("周杰伦_稻香") == normalize_key("周杰伦稻香")

    def test_case_folded(self):
        assert normalize_key("Hello World") == normalize_key("helloworld")

    def test_fullwidth_folded(self):
        assert normalize_key("ＡＢＣ") == normalize_key("abc")

    def test_empty(self):
        assert normalize_key("") == ""

    def test_different_titles_differ(self):
        assert normalize_key("稻香") != normalize_key("晴天")


class TestBuildTarget:
    def test_suffix_lowercased(self):
        rendered = render_segments("{title}", {"title": "Song"})
        out = build_target(Path("/base"), rendered, ".MP3", 120)
        assert str(out).endswith("Song.mp3")

    def test_empty_rendered_raises(self):
        rendered = render_segments("{album}", {})
        with pytest.raises(TemplateError):
            build_target(Path("/base"), rendered, ".mp3", 120)

    def test_nested_dirs(self):
        rendered = render_segments(
            "{albumartist}/{album}/{title}",
            {"albumartist": "A", "album": "B", "title": "C"},
        )
        out = build_target(Path("/base"), rendered, ".mp3", 120)
        assert out == Path("/base") / "A" / "B" / "C.mp3"
