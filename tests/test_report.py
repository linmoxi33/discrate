"""结果展示。"""

from __future__ import annotations

import json

from discrate.decide import build_plan
from discrate.execute import apply_plan
from discrate.profile import get_profile
from discrate.report import display_width, render_plan, render_result, to_json
from discrate.scan import scan_folder

SAFE = get_profile("safe")


def plan_for(root):
    target = root / "整理结果"
    scan = scan_folder(root, exclude=[target])
    return build_plan(scan, SAFE, target_root=target)


class TestDisplayWidth:
    def test_ascii(self):
        assert display_width("abc") == 3

    def test_chinese_counts_double(self):
        assert display_width("周杰伦") == 6

    def test_mixed(self):
        assert display_width("A周B") == 4

    def test_empty(self):
        assert display_width("") == 0


class TestRenderPlan:
    def test_contains_summary(self, sample_dir):
        text = render_plan(plan_for(sample_dir))
        assert "整理方案" in text
        assert "扫描到 8 个音频文件" in text

    def test_lists_review_and_duplicates(self, sample_dir):
        text = render_plan(plan_for(sample_dir))
        assert "建议复核" in text
        assert "判定为重复" in text
        assert "无法整理" in text

    def test_mentions_source_untouched(self, sample_dir):
        text = render_plan(plan_for(sample_dir))
        assert "源文件不动" in text

    def test_verbose_adds_reasons(self, sample_dir):
        plain = render_plan(plan_for(sample_dir))
        verbose = render_plan(plan_for(sample_dir), verbose=True)
        assert len(verbose) > len(plain)
        assert "从文件名/目录名补全" in verbose

    def test_no_writes_message(self, tmp_path):
        root = tmp_path / "空"
        root.mkdir()
        text = render_plan(plan_for(root))
        assert "没有需要整理的文件" in text

    def test_lines_within_width(self, sample_dir):
        width = 96
        for line in render_plan(plan_for(sample_dir), width=width).splitlines():
            assert display_width(line) <= width + 4


class TestRenderResult:
    def test_contains_undo_hint(self, sample_dir):
        plan = plan_for(sample_dir)
        result = apply_plan(plan)
        text = render_result(plan, result)
        assert "整理完成" in text
        assert "discrate undo" in text

    def test_reports_failures(self, sample_dir):
        plan = plan_for(sample_dir)
        first = plan.writes[0]
        first.target.parent.mkdir(parents=True, exist_ok=True)
        first.target.write_bytes(b"occupied")
        result = apply_plan(plan)
        text = render_result(plan, result)
        assert "失败清单" in text


class TestJson:
    def test_structure(self, sample_dir):
        plan = plan_for(sample_dir)
        payload = to_json(plan)
        assert set(payload) >= {"version", "source", "target", "profile", "stats", "decisions"}
        assert payload["version"] == 1
        assert all(
            set(d) >= {"source", "target", "action", "confidence", "reasons", "sources"}
            for d in payload["decisions"]
        )

    def test_serializable(self, sample_dir):
        plan = plan_for(sample_dir)
        result = apply_plan(plan)
        text = json.dumps(to_json(plan, result), ensure_ascii=False)
        assert "applied" in json.loads(text)

    def test_applied_counts(self, sample_dir):
        plan = plan_for(sample_dir)
        result = apply_plan(plan)
        payload = to_json(plan, result)
        assert payload["applied"]["done"] == len(result.done)
        assert payload["applied"]["failed"] == []
