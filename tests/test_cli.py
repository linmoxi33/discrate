"""命令行端到端。"""

from __future__ import annotations

import json

import pytest

from discrate.cli import EXIT_OK, EXIT_USAGE, main


class TestBasicCommands:
    def test_no_args_prints_help(self, capsys):
        assert main([]) == EXIT_USAGE
        out = capsys.readouterr().out
        assert "discrate" in out

    def test_version(self, capsys):
        assert main(["version"]) == EXIT_OK
        assert "discrate" in capsys.readouterr().out

    def test_profiles(self, capsys):
        assert main(["profiles"]) == EXIT_OK
        out = capsys.readouterr().out
        for name in ("safe", "library", "inplace"):
            assert name in out

    def test_missing_folder(self, tmp_path, capsys):
        code = main([str(tmp_path / "不存在")])
        assert code == EXIT_USAGE
        assert "不存在" in capsys.readouterr().out

    def test_path_is_file(self, tmp_path, capsys):
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        assert main([str(f)]) == EXIT_USAGE


class TestTidy:
    def test_plan_only_writes_nothing(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir)]) == EXIT_OK
        assert not (sample_dir / "整理结果").exists()
        assert "整理方案" in capsys.readouterr().out

    def test_dry_run_writes_nothing(self, sample_dir, capsys):
        assert main([str(sample_dir), "--dry-run"]) == EXIT_OK
        assert not (sample_dir / "整理结果").exists()
        assert "--dry-run" in capsys.readouterr().out

    def test_execute_with_yes(self, sample_dir, capsys):
        assert main([str(sample_dir), "--yes"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "整理完成" in out
        assert "discrate undo" in out
        assert (sample_dir / "整理结果" / "单曲" / "周杰伦" / "稻香.mp3").exists()

    def test_default_command_is_tidy(self, sample_dir, capsys):
        """不写子命令时，第一个参数就是文件夹。"""
        assert main([str(sample_dir), "--yes"]) == EXIT_OK
        assert "整理完成" in capsys.readouterr().out

    def test_non_tty_without_yes_refuses(self, sample_dir, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        code = main([str(sample_dir)])
        assert code == EXIT_USAGE
        assert "--yes" in capsys.readouterr().out
        assert not (sample_dir / "整理结果").exists()

    def test_json_output(self, sample_dir, capsys):
        assert main([str(sample_dir), "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["profile"] == "safe"
        assert payload["stats"]["total"] == 8
        assert isinstance(payload["decisions"], list)
        assert any(d["action"] == "duplicate" for d in payload["decisions"])

    def test_json_alone_does_not_execute(self, sample_dir, capsys):
        """--json 是只读语义，不因为加了它就把文件写了。"""
        assert main([str(sample_dir), "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "applied" not in payload
        assert not (sample_dir / "整理结果").exists()

    def test_json_with_yes_records_applied(self, sample_dir, capsys):
        assert main([str(sample_dir), "--json", "--yes"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"]["done"] > 0
        assert payload["applied"]["journal"]

    def test_verbose_shows_reasons(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir), "-v"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "从文件名/目录名补全" in out

    def test_max_files(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir), "--max-files", "2"]) == EXIT_OK
        assert "扫描到 2 个音频文件" in capsys.readouterr().out

    def test_profile_library_changes_paths(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir), "--profile", "library", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        targets = [d["target"] for d in payload["decisions"] if d["target"]]
        assert any("2003 - 叶惠美" in t for t in targets)

    def test_mode_move(self, tmp_path, make_track, capsys):
        root = tmp_path / "音乐"
        root.mkdir()
        src = make_track("音乐/周杰伦 - 稻香.mp3", freq=340)
        assert main([str(root), "--yes", "--mode", "move"]) == EXIT_OK
        capsys.readouterr()
        assert not src.exists()
        assert (root / "整理结果" / "单曲" / "周杰伦" / "稻香.mp3").exists()

    def test_no_dedupe(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir), "--no-dedupe", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert not any(d["action"] == "duplicate" for d in payload["decisions"])

    def test_custom_output(self, sample_dir, tmp_path, capsys):
        out_dir = tmp_path / "别处"
        assert main([str(sample_dir), "--output", str(out_dir), "--yes"]) == EXIT_OK
        capsys.readouterr()
        assert out_dir.is_dir()
        assert list(out_dir.rglob("*.mp3"))

    def test_second_run_reports_already_done(self, sample_dir, capsys):
        main([str(sample_dir), "--yes"])
        capsys.readouterr()
        assert main(["plan", str(sample_dir)]) == EXIT_OK
        assert "上次已整理过" in capsys.readouterr().out


class TestUndo:
    def test_undo_after_tidy(self, sample_dir, capsys):
        main([str(sample_dir), "--yes"])
        capsys.readouterr()
        target = sample_dir / "整理结果"
        assert main(["undo", "--target", str(target), "--yes"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "撤销完成" in out
        assert not list(target.rglob("*.mp3"))

    def test_undo_without_target(self, capsys):
        assert main(["undo"]) == EXIT_USAGE
        assert "--target" in capsys.readouterr().out

    def test_undo_missing_journal(self, tmp_path, capsys):
        (tmp_path / "空目录").mkdir()
        assert main(["undo", "--target", str(tmp_path / "空目录")]) == EXIT_USAGE

    def test_undo_json(self, sample_dir, capsys):
        main([str(sample_dir), "--yes"])
        capsys.readouterr()
        target = sample_dir / "整理结果"
        assert main(["undo", "--target", str(target), "--yes", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["removed"] > 0
        assert payload["kept"] == []


class TestUnknownProfile:
    def test_bad_profile_rejected_by_argparse(self, sample_dir):
        with pytest.raises(SystemExit):
            main([str(sample_dir), "--profile", "nope"])


class TestReportFile:
    def test_report_written_by_default(self, sample_dir, capsys):
        assert main([str(sample_dir), "--yes"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "整理报告" in out
        assert (sample_dir / "整理结果" / "整理报告.md").exists()
        assert (sample_dir / "整理结果" / "整理报告.json").exists()

    def test_no_report_flag(self, sample_dir, capsys):
        assert main([str(sample_dir), "--yes", "--no-report"]) == EXIT_OK
        capsys.readouterr()
        assert not (sample_dir / "整理结果" / "整理报告.md").exists()

    def test_no_report_when_nothing_written(self, tmp_path, capsys):
        empty = tmp_path / "空"
        empty.mkdir()
        assert main([str(empty), "--yes"]) == EXIT_OK
        capsys.readouterr()
        assert not (empty / "整理结果" / "整理报告.md").exists()


class TestLookupFlag:
    def test_lookup_off_by_default(self, sample_dir, capsys, monkeypatch):
        calls = []

        class Spy:
            def __init__(self):
                calls.append("init")

            def search(self, artist, title):
                calls.append((artist, title))
                return []

        monkeypatch.setattr("discrate.cli.MusicBrainzProvider", Spy)
        assert main(["plan", str(sample_dir)]) == EXIT_OK
        capsys.readouterr()
        assert calls == []

    def test_lookup_uses_provider(self, sample_dir, capsys, monkeypatch):
        from discrate.lookup import LookupHit

        class Fake:
            def __init__(self):
                self.calls = []

            def search(self, artist, title):
                self.calls.append((artist, title))
                return [LookupHit(artist="周杰伦", album="叶惠美", title="晴天", year=2003, score=0.9, source="fake")]

        fake = Fake()
        monkeypatch.setattr("discrate.cli.MusicBrainzProvider", lambda: fake)
        assert main(["plan", str(sample_dir), "--lookup", "--lookup-limit", "3"]) == EXIT_OK
        out = capsys.readouterr().out
        assert len(fake.calls) <= 3
        assert "在线匹配建议" in out

    def test_lookup_limit_zero(self, sample_dir, capsys, monkeypatch):
        class Fake:
            def __init__(self):
                self.calls = 0

            def search(self, artist, title):
                self.calls += 1
                return []

        fake = Fake()
        monkeypatch.setattr("discrate.cli.MusicBrainzProvider", lambda: fake)
        assert main(["plan", str(sample_dir), "--lookup", "--lookup-limit", "0"]) == EXIT_OK
        capsys.readouterr()
        assert fake.calls == 0

    def test_lookup_cache_written(self, sample_dir, capsys, monkeypatch):
        class Fake:
            def search(self, artist, title):
                return []

        monkeypatch.setattr("discrate.cli.MusicBrainzProvider", lambda: Fake())
        assert main(["plan", str(sample_dir), "--lookup", "--lookup-limit", "2"]) == EXIT_OK
        capsys.readouterr()
        cache = sample_dir / "整理结果" / ".discrate" / "lookup-cache.json"
        assert cache.exists()


class TestTreeInPlan:
    def test_structure_preview(self, sample_dir, capsys):
        assert main(["plan", str(sample_dir)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "整理后的结构" in out
        assert "置信" in out
