"""定向变异测试。

目的不是「覆盖率好看」，而是回答一个问题：**把某一行逻辑改坏，测试会不会红**。
每个变异体都会跑一次全量测试，记录失败用例集合，然后按 sha256 逐字节恢复源码。

⚠️ 运行期间不要提交、不要推送、不要做任何写操作 —— 变异器会原地改源码。

进程被强杀时 ``finally`` 不会执行，缺陷会留在源码里。所以这里每次运行前
先把所有被变异的文件快照到 ``.mutation-snapshot/``，**下次启动时先检查快照
并自动恢复**。也可以手动 ``python tools/mutate.py --restore``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "discrate"
SNAPSHOT = ROOT / ".mutation-snapshot"
MANIFEST = SNAPSHOT / "manifest.json"


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    old: str
    new: str
    note: str


MUTATIONS: list[Mutation] = [
    Mutation(
        "no-slash-escape",
        "naming.py",
        'return text.replace("/", "_").replace("\\\\", "_")',
        "return text",
        "字段值里的分隔符不转义",
    ),
    Mutation(
        "keep-empty-segments",
        "naming.py",
        "        if not text:\n            dropped.append(chunk)\n            continue",
        "        if not text:\n            dropped.append(chunk)\n            text = '_'",
        "空字段不再整段丢弃",
    ),
    Mutation(
        "no-pure-digit-track",
        "scan.py",
        "    if text.isdigit():\n        return {\"track\": int(text)}",
        "    if False:\n        return {\"track\": int(text)}",
        "纯数字文件名被当成曲名",
    ),
    Mutation(
        "no-root-guard",
        "scan.py",
        "        at_root = os.path.normcase(str(path.parent)) == os.path.normcase(str(base))",
        "        at_root = False",
        "根目录名被当成专辑名",
    ),
    Mutation(
        "no-trust-check",
        "scan.py",
        "    if not trusted:\n        return {}",
        "    if False:\n        return {}",
        "单文件目录也拿目录名当专辑",
    ),
    Mutation(
        "dedupe-registers-skipped",
        "decide.py",
        "        if action in WRITE_ACTIONS:",
        "        if True:",
        "跳过/未写入的文件也成了去重基准",
    ),
    Mutation(
        "target-size-check-inverted",
        "decide.py",
        "                if target_path.stat().st_size != int(recorded_size):\n                    continue",
        "                if target_path.stat().st_size == int(recorded_size):\n                    continue",
        "目标文件大小比对反向，被替换的旧记录仍参与判重",
    ),
    Mutation(
        "needs-review-includes-review",
        "decide.py",
        '        return self.confidence == "low" and self.action not in ("reject", "review")',
        '        return self.confidence == "low" and self.action != "reject"',
        "无法整理的文件重复出现在建议复核里",
    ),
    Mutation(
        "no-reserved-names",
        "bridge.py",
        "    if stem in _RESERVED:\n        cleaned = \"_\" + cleaned",
        "    if False:\n        cleaned = \"_\" + cleaned",
        "Windows 保留名不处理",
    ),
    Mutation(
        "no-truncation",
        "bridge.py",
        "    if len(cleaned) > max_len:\n        cleaned = cleaned[:max_len].rstrip(\". \")",
        "    if False:\n        cleaned = cleaned[:max_len].rstrip(\". \")",
        "文件名不按字符截断",
    ),
    Mutation(
        "no-byte-truncation",
        "bridge.py",
        "    cleaned = _truncate_utf8(cleaned, max_bytes).rstrip(\". \")",
        "    cleaned = cleaned.rstrip(\". \")",
        "文件名不按字节截断（Linux 上 ENAMETOOLONG）",
    ),
    Mutation(
        "no-hash-after-tag",
        "execute.py",
        "                    after = _sha256(dst) if ok else \"\"",
        "                    after = \"\"",
        "不记录写标签后的哈希",
    ),
    Mutation(
        "no-state-save",
        "execute.py",
        "        save_state(target_root, records)",
        "        pass",
        "不登记已整理，二次运行重复整理",
    ),
    Mutation(
        "undo-ignores-modified",
        "execute.py",
        "            if expected and _sha256(dst) != expected:",
        "            if False:",
        "撤销时误删用户改过的文件",
    ),
    Mutation(
        "no-exclude-output",
        "scan.py",
        "            and os.path.normcase(str(Path(dirpath) / d)) not in excluded",
        "            and True",
        "输出目录被当成素材二次扫描",
    ),
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_raw(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_raw(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def take_snapshot(files: list[Path]) -> None:
    """把将被变异的文件原样存一份，作为被强杀后的恢复来源。"""
    if SNAPSHOT.exists():
        shutil.rmtree(SNAPSHOT)
    SNAPSHOT.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for path in files:
        data = path.read_bytes()
        (SNAPSHOT / path.name).write_bytes(data)
        manifest[path.name] = hashlib.sha256(data).hexdigest()
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def restore_from_snapshot() -> int:
    """用快照覆盖被改坏的源码，返回实际恢复的文件数。"""
    if not MANIFEST.is_file():
        return 0
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    restored = 0
    for name, digest in manifest.items():
        target = SRC / name
        backup = SNAPSHOT / name
        if not backup.is_file():
            continue
        if not target.exists() or sha256_file(target) != digest:
            shutil.copy2(backup, target)
            restored += 1
    return restored


def drop_snapshot() -> None:
    if SNAPSHOT.exists():
        shutil.rmtree(SNAPSHOT, ignore_errors=True)


def run_tests() -> tuple[int, set[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no", "-rf", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    failed = {
        line.split(" ", 1)[1].split(" - ")[0].strip()
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED ")
    }
    return proc.returncode, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定向变异测试")
    parser.add_argument("--only", action="append", help="只跑指定变异")
    parser.add_argument(
        "--restore", action="store_true", help="只从上次快照恢复源码，不跑变异"
    )
    args = parser.parse_args(argv)

    restored = restore_from_snapshot()
    if restored:
        print(f"检测到上次中断的残留，已恢复 {restored} 个源文件", flush=True)
    if args.restore:
        drop_snapshot()
        print("源码已恢复，快照已清理", flush=True)
        return 0

    selected = [m for m in MUTATIONS if not args.only or m.name in args.only]
    print(f"变异体 {len(selected)} 个", flush=True)

    code, baseline_failed = run_tests()
    if code != 0:
        print("基线测试未通过，先修测试再跑变异：", flush=True)
        for name in sorted(baseline_failed):
            print("  " + name, flush=True)
        return 2
    print("基线：全绿", flush=True)

    results: list[tuple[Mutation, bool, set[str], str]] = []
    take_snapshot(sorted({SRC / m.file for m in selected}))
    try:
        for index, mutation in enumerate(selected, start=1):
            path = SRC / mutation.file
            original = read_raw(path)
            digest = sha256_file(path)
            if mutation.old not in original:
                results.append((mutation, False, set(), "模式未命中"))
                print(
                    f"[{index}/{len(selected)}] {mutation.name}: 模式未命中，跳过",
                    flush=True,
                )
                continue

            mutated = original.replace(mutation.old, mutation.new, 1)
            write_raw(path, mutated)
            error: BaseException | None = None
            code, failed = 1, set()
            try:
                code, failed = run_tests()
            except BaseException as exc:
                error = exc
            finally:
                write_raw(path, original)
                restored_ok = sha256_file(path) == digest

            if not restored_ok:
                print(
                    f"!! 恢复失败：{mutation.file} 哈希不一致，立即中止", flush=True
                )
                return 3
            if error is not None:
                raise error

            killed = bool(failed) or code != 0
            results.append((mutation, killed, failed, ""))
            mark = "杀死" if killed else "存活"
            detail = (
                f"{len(failed)} 个用例失败"
                if failed
                else ("收集/运行错误" if code != 0 else "无失败")
            )
            print(
                f"[{index}/{len(selected)}] {mutation.name}: {mark}（{detail}）",
                flush=True,
            )
    finally:
        drop_snapshot()

    survived = [r for r in results if not r[1] and not r[3]]
    print("", flush=True)
    print(
        f"总计 {len(results)}，杀死 {len(results) - len(survived)}，存活 {len(survived)}",
        flush=True,
    )
    for mutation, _killed, _failed, _note in survived:
        print(f"  存活：{mutation.name} —— {mutation.note}", flush=True)
    return 0 if not survived else 1


if __name__ == "__main__":
    raise SystemExit(main())
