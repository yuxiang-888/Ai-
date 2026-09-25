from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def copy_tree(source: Path, target: Path, *, excludes: set[str] = frozenset()) -> None:
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*excludes),
        copy_function=shutil.copy2,
    )


def snapshot_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the private owner-only portable package.")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--python-runtime", type=Path, default=Path(r"D:\tools\python"))
    parser.add_argument("--raw-videos", type=Path, default=Path.home() / "Desktop" / "未剪辑视频")
    args = parser.parse_args()
    root = args.root.resolve()
    destination = args.destination.resolve()
    if destination == root or root in destination.parents:
        raise SystemExit("输出目录不能放在原程序目录内。")
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit("输出目录必须为空，防止覆盖旧备份。")
    destination.mkdir(parents=True, exist_ok=True)

    copy_tree(root, destination, excludes={".git", "__pycache__", ".pytest_cache", "*.pyc"})
    runtime_target = destination / "运行环境" / "python"
    copy_tree(args.python_runtime.resolve(), runtime_target, excludes={"ikuuu_vpn", "__pycache__", "*.pyc"})
    if not args.raw_videos.is_dir():
        raise SystemExit("找不到原视频目录：" + str(args.raw_videos))
    copy_tree(args.raw_videos.resolve(), destination / "便携数据" / "原视频")

    database_entries: list[dict[str, object]] = []
    for source in sorted((root / "便携数据").rglob("*.sqlite3")):
        relative = source.relative_to(root)
        target = destination / relative
        snapshot_sqlite(source, target)
        database_entries.append({"path": str(relative), "bytes": target.stat().st_size, "sha256": sha256(target)})

    required = {
        "agent_source": destination / "AI智能剪辑网站" / "src" / "fulin_editor" / "agent_runtime.py",
        "legacy_engine": destination / "suchen-server.exe",
        "accounts_database": destination / "便携数据" / "unified" / "fulin_editor.sqlite3",
        "whisper_model": destination / "_internal" / "models" / "faster-whisper-small",
        "pose_model": destination / "AI智能剪辑网站" / "models" / "pose_landmarker_lite.task",
        "ffmpeg": destination / "_internal" / "bin" / "ffmpeg.exe",
        "python": runtime_target / "python.exe",
        "session_secret": destination / "便携数据" / "unified" / ".suchen-session-secret",
        "private_config": destination / "suchen_llm.json",
        "tunnel_token": destination / "Agent工具" / "国内连接令牌.txt",
        "raw_videos": destination / "便携数据" / "原视频",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit("便携包缺少必要项：" + "、".join(missing))
    file_count = sum(1 for path in destination.rglob("*") if path.is_file())
    total_bytes = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
    manifest = {
        "package": "淘宝女装AI剪辑Agent-负责人完整便携包",
        "owner_only": True,
        "contains_private_company_data": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(root),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "required": {name: True for name in required},
        "databases": database_entries,
        "warning": "包含原片、成片、账号库、会话密钥和公网令牌，只限负责人保管，不得发给客户或上传GitHub。",
    }
    (destination / "负责人便携包验收.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (destination / "重要-仅限负责人.txt").write_text(
        "本包含公司数据、用户账号库、视频、私有配置、会话密钥和cpolar令牌。\n"
        "不要发给客户，不要上传GitHub。客户只使用公网网址。\n"
        "新电脑首次运行：右键“安装到新电脑.ps1”，选择用PowerShell运行。\n"
        "两台电脑的SQLite数据不会自动同步，同一时间只选一台作为生产主机。\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
