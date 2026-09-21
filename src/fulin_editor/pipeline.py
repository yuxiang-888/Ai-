from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from .asr import TranscriptionResult, transcribe_video
from .planner import build_plan, load_sentences
from .policies import RequestedProductType
from .quality import inspect_output
from .renderer import FrameSpec, render_plan
from .storage import save_job
from .vision import analyze_video


@dataclass(frozen=True)
class PipelineResult:
    job_id: str
    status: str
    output: Path
    report: Path
    transcript: Path
    total_seconds: float
    quality_passed: bool
    cached_asr: bool
    cached_vision: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "output": str(self.output),
                "report": str(self.report),
                "transcript": str(self.transcript),
            }
        )
        return payload


def process_video(
    video: str | Path,
    output: str | Path,
    *,
    transcript: str | Path | None = None,
    cache_dir: str | Path = "data/cache",
    database: str | Path = "data/fulin_editor.sqlite3",
    report: str | Path | None = None,
    target_duration: float | None = None,
    product_type: RequestedProductType = "auto",
    whisper_model: str = "small",
    whisper_model_path: str | Path | None = None,
    pose_model: str | Path | None = None,
    vision_sample_fps: float = 2.0,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    prefer_hardware: bool = True,
    transition_seconds: float = 0.0,
    progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    started = perf_counter()
    source = Path(video).resolve()
    output_path = Path(output).resolve()
    report_path = Path(report).resolve() if report else output_path.with_suffix(".json")
    cache_path = Path(cache_dir).resolve()

    notify = progress or (lambda _stage: None)
    notify("transcribing")
    if transcript:
        transcript_path = Path(transcript).resolve()
        transcription = TranscriptionResult(
            path=transcript_path,
            cached=True,
            elapsed_seconds=0.0,
            sentence_count=len(load_sentences(transcript_path)),
            language="zh",
            model="provided",
        )
    else:
        transcription = transcribe_video(
            source,
            cache_path,
            model=whisper_model,
            model_path=whisper_model_path,
        )
        transcript_path = transcription.path

    sentences = load_sentences(transcript_path)
    notify("analyzing")
    vision = analyze_video(
        source,
        cache_path,
        pose_model=pose_model,
        sample_fps=vision_sample_fps,
    )
    notify("planning")
    plan = build_plan(
        sentences,
        target_duration=target_duration,
        vision=vision,
        product_type=product_type,
    )
    notify("rendering")
    render = render_plan(
        source,
        output_path,
        plan,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        prefer_hardware=prefer_hardware,
        frame=FrameSpec(
            width=1080,
            height=1440,
            mode="stage",
            transition_seconds=transition_seconds,
            jianying_position_y_px=500.0,
        ),
    )
    notify("quality")
    quality = inspect_output(
        output_path,
        plan,
        expected_width=1080,
        expected_height=1440,
        ffprobe=ffprobe,
    )
    analysis = {
        "asr": {
            "cached": transcription.cached,
            "elapsed_seconds": transcription.elapsed_seconds,
            "sentence_count": transcription.sentence_count,
            "language": transcription.language,
            "model": transcription.model,
        },
        "vision": {
            "cached": vision.cached,
            "elapsed_seconds": vision.elapsed_seconds,
            "sample_fps": vision.sample_fps,
            "sample_count": len(vision.observations),
            "detection_ratio": round(vision.detection_ratio, 4),
        },
    }
    job_id, payload = save_job(
        database,
        source=source,
        transcript=transcript_path,
        plan=plan,
        render=render,
        quality=quality,
        analysis=analysis,
    )
    total_seconds = round(perf_counter() - started, 3)
    payload["pipeline_elapsed_seconds"] = total_seconds
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    result = PipelineResult(
        job_id=job_id,
        status=str(payload["status"]),
        output=output_path,
        report=report_path,
        transcript=transcript_path,
        total_seconds=total_seconds,
        quality_passed=quality.passed,
        cached_asr=transcription.cached,
        cached_vision=vision.cached,
    )
    notify("completed")
    return result
