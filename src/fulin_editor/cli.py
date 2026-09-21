from __future__ import annotations

import argparse
import json
import sys

from .pipeline import process_video


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="女装直播三阶段自动剪辑引擎")
    parser.add_argument("--video", required=True, help="原视频绝对路径")
    parser.add_argument("--transcript", help="可选：已有转写 JSON；不提供时自动识别口播")
    parser.add_argument("--output", required=True, help="成片输出路径")
    parser.add_argument("--database", default="data/fulin_editor.sqlite3", help="SQLite 数据库路径")
    parser.add_argument("--report", help="剪辑报告 JSON；默认与成片同名")
    parser.add_argument("--product-type", choices=("auto", "single", "set", "bulky"), default="auto", help="品类；auto 根据口播识别")
    parser.add_argument("--target-duration", type=float, help="可选目标时长；默认使用品类策略")
    parser.add_argument("--cache-dir", default="data/cache", help="ASR与视觉分析缓存目录")
    parser.add_argument("--whisper-model", default="small", help="faster-whisper 模型名称")
    parser.add_argument("--whisper-model-path", help="本地 faster-whisper 模型目录")
    parser.add_argument("--pose-model", help="pose_landmarker_lite.task 路径")
    parser.add_argument("--vision-fps", type=float, default=2.0, help="人物动作分析采样帧率")
    parser.add_argument("--ffmpeg", help="ffmpeg 路径")
    parser.add_argument("--ffprobe", help="ffprobe 路径")
    parser.add_argument("--software-encoder", action="store_true", help="禁用 AMD 硬件编码")
    parser.add_argument("--transition", type=float, default=0.0, help="视觉转场秒数；业务标准默认直接切换")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = process_video(
            args.video,
            args.output,
            transcript=args.transcript,
            cache_dir=args.cache_dir,
            database=args.database,
            report=args.report,
            target_duration=args.target_duration,
            product_type=args.product_type,
            whisper_model=args.whisper_model,
            whisper_model_path=args.whisper_model_path,
            pose_model=args.pose_model,
            vision_sample_fps=args.vision_fps,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            prefer_hardware=not args.software_encoder,
            transition_seconds=args.transition,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "code": "quality_passed" if result.quality_passed else "needs_review",
                    "reason": "",
                    "data": result.to_dict(),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "code": "edit_failed", "reason": str(exc), "data": {}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
