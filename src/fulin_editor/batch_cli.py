from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import process_video


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="女装直播素材文件夹逐条自适应剪辑")
    parser.add_argument("--input-dir", required=True, help="原视频文件夹")
    parser.add_argument("--output-dir", required=True, help="成片与报告输出文件夹")
    parser.add_argument("--cache-dir", default="data/cache", help="共享分析缓存")
    parser.add_argument("--database", default="data/fulin_editor.sqlite3", help="SQLite 数据库")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--whisper-model-path")
    parser.add_argument("--pose-model")
    parser.add_argument("--vision-fps", type=float, default=2.0)
    parser.add_argument("--product-type", choices=("auto", "single", "set", "bulky"), default="auto")
    parser.add_argument("--target-duration", type=float)
    parser.add_argument("--software-encoder", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not source_dir.is_dir():
        print(
            json.dumps(
                {"ok": False, "code": "missing_input_dir", "reason": str(source_dir), "data": {}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    iterator = source_dir.rglob("*") if args.recursive else source_dir.glob("*")
    videos = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES)
    completed: list[dict] = []
    failed: list[dict] = []
    skipped: list[str] = []
    for video in videos:
        relative_parent = video.parent.relative_to(source_dir) if args.recursive else Path()
        target_dir = output_dir / relative_parent
        output = target_dir / f"{video.stem}_成片.mp4"
        report = target_dir / f"{video.stem}_剪辑报告.json"
        if output.is_file() and report.is_file() and not args.overwrite:
            skipped.append(str(video))
            continue
        try:
            result = process_video(
                video,
                output,
                cache_dir=args.cache_dir,
                database=args.database,
                report=report,
                target_duration=args.target_duration,
                product_type=args.product_type,
                whisper_model=args.whisper_model,
                whisper_model_path=args.whisper_model_path,
                pose_model=args.pose_model,
                vision_sample_fps=args.vision_fps,
                prefer_hardware=not args.software_encoder,
            )
            completed.append(result.to_dict())
            print(
                json.dumps(
                    {
                        "ok": True,
                        "code": "item_completed",
                        "reason": "",
                        "data": result.to_dict(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            item = {"video": str(video), "reason": str(exc)}
            failed.append(item)
            print(
                json.dumps(
                    {"ok": False, "code": "item_failed", "reason": str(exc), "data": item},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            if args.fail_fast:
                break
    summary = {
        "ok": not failed,
        "code": "batch_completed" if not failed else "batch_completed_with_failures",
        "reason": "" if not failed else f"{len(failed)} 条处理失败",
        "data": {
            "found": len(videos),
            "completed": len(completed),
            "approved": sum(item["quality_passed"] for item in completed),
            "needs_review": sum(not item["quality_passed"] for item in completed),
            "skipped": len(skipped),
            "failed": failed,
        },
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
