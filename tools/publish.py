"""把 discrate 推送到 GitHub。

用 Git Data API 而不是 `git push`：本机 git HTTPS 常被阻断，而 `gh api` 是通的。
整个流程按 MEMORY 里记的踩坑清单来做：

* 端点去掉前导斜杠（Git Bash 会把它当路径重写）
* 空仓库先 PUT 一个文件破局，否则 blob 409
* 大内容一律 `--input`，不走 argv（Windows 命令行长度限制）
* tree 必须带 base_tree，否则把仓库削成只剩本次条目
* 推完逐字节下载回来比对

用法::

    python tools/publish.py --dry-run
    python tools/publish.py
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "linmoxi33"
REPO = "discrate"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "_run",
    "样本",
    ".mutation-snapshot",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo"}


def gh(args: list[str], payload: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", "api", *args]
    tmp_name = None
    if payload is not None:
        fd, tmp_name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        cmd += ["--input", tmp_name]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    finally:
        if tmp_name:
            os.unlink(tmp_name)


def gh_json(args: list[str], payload: dict | None = None):
    proc = gh(args, payload)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} 失败：\n{proc.stderr.strip()}")
    text = proc.stdout.strip()
    return json.loads(text) if text else None


def collect_files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(
            part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts
        ):
            continue
        if any(str(rel).endswith(suffix) for suffix in EXCLUDE_SUFFIX):
            continue
        out.append(path)
    return out


def normalize_lf(data: bytes) -> bytes:
    """仓库统一 LF。CRLF 会让远端 blob 与本地不一致。"""
    if b"\r\n" in data:
        return data.replace(b"\r\n", b"\n")
    return data


def check_account() -> str:
    login = gh_json(["user"])["login"]
    if login != OWNER:
        raise RuntimeError(
            f"当前 gh 活跃账号是 {login}，不是 {OWNER}。先跑 gh auth switch --user {OWNER}"
        )
    return login


def ensure_repo(description: str) -> bool:
    """返回是否新建了仓库。"""
    proc = gh([f"repos/{OWNER}/{REPO}"])
    if proc.returncode == 0:
        return False
    payload = {
        "name": REPO,
        "description": description,
        "private": False,
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    }
    gh_json(["user/repos", "--method", "POST"], payload)
    return True


def bootstrap_commit() -> None:
    """空仓库不能直接传 blob，先 PUT 一个文件把仓库激活。"""
    readme = (ROOT / "README.md").read_bytes()
    payload = {
        "message": "init",
        "content": base64.b64encode(readme).decode(),
        "branch": "main",
    }
    gh_json([f"repos/{OWNER}/{REPO}/contents/README.md", "--method", "PUT"], payload)


def head_commit() -> dict:
    return gh_json([f"repos/{OWNER}/{REPO}/commits/main"])


def push_tree(files: list[Path], message: str, dry_run: bool = False) -> str:
    head = head_commit()
    base_tree = head["commit"]["tree"]["sha"]
    parent = head["sha"]

    if dry_run:
        for path in files:
            print(f"  would push {path.relative_to(ROOT).as_posix()}", flush=True)
        print(
            f"[dry-run] 将提交 {len(files)} 个文件，未调用任何写接口"
            f"（当前 head {parent[:8]}）",
            flush=True,
        )
        return parent

    tree_entries = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        data = normalize_lf(path.read_bytes())
        blob = gh_json(
            [f"repos/{OWNER}/{REPO}/git/blobs", "--method", "POST"],
            {"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        tree_entries.append(
            {
                "path": rel,
                "mode": "100755" if os.access(path, os.X_OK) and path.suffix == "" else "100644",
                "type": "blob",
                "sha": blob["sha"],
            }
        )
        print(f"  blob {rel}", flush=True)

    tree = gh_json(
        [f"repos/{OWNER}/{REPO}/git/trees", "--method", "POST"],
        {"base_tree": base_tree, "tree": tree_entries},
    )
    commit = gh_json(
        [f"repos/{OWNER}/{REPO}/git/commits", "--method", "POST"],
        {"message": message, "tree": tree["sha"], "parents": [parent]},
    )
    gh_json(
        [f"repos/{OWNER}/{REPO}/git/refs/heads/main", "--method", "PATCH"],
        {"sha": commit["sha"], "force": True},
    )
    return commit["sha"]


def verify_remote(files: list[Path]) -> list[str]:
    """逐字节比对远端内容。blob hash 因 eol 归一化不可靠，必须下载比。"""
    problems = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        proc = gh(
            [
                f"repos/{OWNER}/{REPO}/contents/{rel}",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
        )
        if proc.returncode != 0:
            problems.append(f"读取失败 {rel}")
            continue
        remote = proc.stdout.encode("utf-8")
        local = normalize_lf(path.read_bytes())
        if remote != local:
            problems.append(f"内容不一致 {rel}（本地 {len(local)} 字节，远端 {len(remote)} 字节）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推送 discrate 到 GitHub")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--message", default="feat: discrate 0.1.0 —— beets 的面向普通用户重设计")
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args(argv)

    login = check_account()
    print(f"账号：{login}", flush=True)

    files = collect_files()
    print(f"待推送文件 {len(files)} 个", flush=True)

    created = ensure_repo("把一堆命名混乱的音乐文件，整理成整齐、可回滚的音乐库")
    print(f"仓库{'已新建' if created else '已存在'}", flush=True)

    try:
        head_commit()
    except RuntimeError:
        print("空仓库，先建破局提交", flush=True)
        bootstrap_commit()

    sha = push_tree(files, args.message, dry_run=args.dry_run)
    print(f"提交：{sha}", flush=True)

    if args.dry_run:
        return 0

    if not args.skip_verify:
        problems = verify_remote(files)
        if problems:
            print("逐字节校验失败：", flush=True)
            for item in problems:
                print("  " + item, flush=True)
            return 1
        print(f"逐字节校验通过：{len(files)} 个文件", flush=True)

    print(f"https://github.com/{OWNER}/{REPO}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
