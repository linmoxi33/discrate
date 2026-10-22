"""执行、记账、回滚。"""

from __future__ import annotations

import hashlib
import json

import mediafile
import pytest

from discrate.decide import build_plan
from discrate.execute import (
    apply_plan,
    find_latest_journal,
    load_state,
    state_path,
    undo_journal,
)
from discrate.profile import get_profile
from discrate.scan import scan_folder

SAFE = get_profile("safe")


def make_plan(root, profile=SAFE, target=None):
    """模拟 CLI 的真实行为：读取上一轮的登记表再出方案。"""
    target = target or root / "整理结果"
    scan = scan_folder(root, exclude=[target])
    return build_plan(
        scan, profile, target_root=target, previous=load_state(target)
    )


def sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestApply:
    def test_files_are_copied(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        assert result.ok
        assert len(result.done) == len(plan.writes)
        for decision in result.done:
            assert decision.target.exists()

    def test_source_files_untouched(self, sample_dir):
        before = {p: sha256(p) for p in sample_dir.rglob("*.mp3")}
        apply_plan(make_plan(sample_dir))
        for path, digest in before.items():
            assert path.exists()
            assert sha256(path) == digest

    def test_journal_records_session_and_done(self, sample_dir):
        result = apply_plan(make_plan(sample_dir))
        entries = [
            json.loads(line)
            for line in result.journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ops = [e["op"] for e in entries]
        assert ops[0] == "session"
        assert ops[-1] == "done"
        assert ops.count("copy") == len(result.done)
        session = entries[0]
        assert session["profile"] == "safe"
        assert session["mode"] == "copy"

    def test_tags_written_to_copy_only(self, sample_dir):
        plan = make_plan(sample_dir)
        apply_plan(plan)
        copy = plan.target_root / "单曲" / "周杰伦" / "稻香.mp3"
        assert copy.exists()
        assert mediafile.MediaFile(str(copy)).artist == "周杰伦"
        # 源文件标签不应被写入
        assert not (mediafile.MediaFile(str(sample_dir / "周杰伦 - 稻香.mp3")).artist)

    def test_no_tag_write_option(self, sample_dir):
        plan = make_plan(sample_dir)
        apply_plan(plan, write_meta=False)
        copy = plan.target_root / "单曲" / "周杰伦" / "稻香.mp3"
        assert not (mediafile.MediaFile(str(copy)).artist)

    def test_state_saved(self, sample_dir):
        plan = make_plan(sample_dir)
        apply_plan(plan)
        state = load_state(plan.target_root)
        assert "周杰伦 - 稻香.mp3" in state
        record = state["周杰伦 - 稻香.mp3"]
        assert record["target_size"] == (plan.target_root / "单曲" / "周杰伦" / "稻香.mp3").stat().st_size

    def test_target_occupied_between_plan_and_apply(self, sample_dir):
        plan = make_plan(sample_dir)
        first = plan.writes[0]
        first.target.parent.mkdir(parents=True, exist_ok=True)
        first.target.write_bytes(b"someone else got here first")
        result = apply_plan(plan)
        assert not result.ok
        assert any("目标已存在" in error for _d, error in result.failed)
        assert first.target.read_bytes() == b"someone else got here first"

    def test_journal_path_unique_per_run(self, sample_dir):
        plan = make_plan(sample_dir)
        first = apply_plan(plan)
        second = apply_plan(plan)
        assert first.journal_path != second.journal_path

    def test_find_latest_journal(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        assert find_latest_journal(plan.target_root) == result.journal_path

    def test_find_latest_journal_missing(self, tmp_path):
        assert find_latest_journal(tmp_path) is None


class TestUndo:
    def test_undo_removes_copies(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        targets = [d.target for d in result.done]
        undo = undo_journal(result.journal_path)
        # 音频副本 + 封面都要撤回
        assert undo.removed == len(targets) + result.covers
        assert not any(t.exists() for t in targets)

    def test_undo_restores_state(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        undo_journal(result.journal_path)
        assert load_state(plan.target_root) == {}

    def test_undo_keeps_modified_file(self, sample_dir):
        """用户事后改过的副本不该被删掉。"""
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        victim = result.done[0].target
        victim.write_bytes(b"user edited this")
        undo = undo_journal(result.journal_path)
        assert victim.exists()
        assert any(path == victim for path, _reason in undo.kept)

    def test_undo_move_mode_moves_back(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        src = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        profile = get_profile("inplace")
        plan = make_plan(root, profile=profile)
        result = apply_plan(plan)
        target = result.done[0].target
        assert not src.exists()
        assert target.exists()
        undo = undo_journal(result.journal_path)
        assert undo.moved_back == 1
        assert src.exists()

    def test_undo_missing_journal_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            undo_journal(tmp_path / "nope.jsonl")

    def test_undo_twice_is_safe(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        undo_journal(result.journal_path)
        second = undo_journal(result.journal_path)
        assert second.removed == 0
        assert second.moved_back == 0

    def test_undo_summary_text(self, sample_dir):
        plan = make_plan(sample_dir)
        result = apply_plan(plan)
        undo = undo_journal(result.journal_path)
        assert "删除副本" in undo.summary


class TestRoundTrip:
    def test_apply_undo_apply_is_stable(self, sample_dir):
        plan = make_plan(sample_dir)
        first = apply_plan(plan)
        undo_journal(first.journal_path)
        second_plan = make_plan(sample_dir)
        assert len(second_plan.writes) == len(plan.writes)
        second = apply_plan(second_plan)
        assert second.ok
        assert len(second.done) == len(first.done)

    def test_second_run_is_noop(self, sample_dir):
        plan = make_plan(sample_dir)
        apply_plan(plan)
        again = make_plan(sample_dir)
        assert again.writes == []
        assert again.by_action("skip")
        assert all("already-done" in d.reason_codes() for d in again.by_action("skip"))

    def test_state_file_location(self, sample_dir):
        plan = make_plan(sample_dir)
        apply_plan(plan)
        assert state_path(plan.target_root).exists()


class TestSourceIntegrity:
    def test_changed_source_is_skipped(self, tmp_path, make_track):
        """方案算完之后源文件被改了，不能按旧计划落盘。"""
        root = tmp_path / "音乐"
        root.mkdir()
        src = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        plan = make_plan(root)
        src.write_bytes(b"someone replaced this file")

        result = apply_plan(plan)
        assert not result.ok
        assert any("被改动过" in error for _d, error in result.failed)
        assert not (root / "整理结果" / "单曲" / "周杰伦" / "稻香.mp3").exists()

    def test_missing_source_is_skipped(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        src = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        plan = make_plan(root)
        src.unlink()

        result = apply_plan(plan)
        assert not result.ok
        assert any("读不到" in error for _d, error in result.failed)

    def test_unchanged_source_still_works(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        plan = make_plan(root)
        result = apply_plan(plan)
        assert result.ok

    def test_journal_records_source_changed(self, tmp_path, make_track):
        root = tmp_path / "音乐"
        root.mkdir()
        src = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        plan = make_plan(root)
        src.write_bytes(b"changed")
        result = apply_plan(plan)
        text = result.journal_path.read_text(encoding="utf-8")
        assert "source-changed" in text
