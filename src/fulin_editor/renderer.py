from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from .planner import EditPlan


@dataclass(frozen=True)
class FrameSpec:
    width: int = 1080
    height: int = 1440
    mode: str = "stage"
    full_body_scale: float = 0.96
    detail_scale: float = 1.12
    transition_seconds: float = 0.0
    # JianYing's Y control is a position value, not independent vertical scale.
    jianying_position_y_px: float = 500.0


@dataclass(frozen=True)
class RenderResult:
    output: Path
    encoder: str
    elapsed_seconds: float
    duration_seconds: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    jianying_position_y_px: float = 500.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output),
            "encoder": self.encoder,
            "elapsed_seconds": self.elapsed_seconds,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "jianying_position_y_px": self.jianying_position_y_px,
        }


def resolve_binary(name: str, explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path.resolve())
        raise FileNotFoundError(f"找不到 {name}: {path}")
    value = shutil.which(name)
    if not value:
        raise FileNotFoundError(f"PATH 中找不到 {name}")
    return value


def probe_video(video: str | Path, ffprobe: str | None = None) -> dict[str, Any]:
    ffprobe_bin = resolve_binary("ffprobe", ffprobe)
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of",
        "json",
        str(Path(video).resolve()),
    ]
    payload = json.loads(subprocess.check_output(command, text=True, encoding="utf-8"))
    video_stream = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
    audio_stream = next(
        (stream for stream in payload["streams"] if stream["codec_type"] == "audio"), None
    )
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1"
    numerator, denominator = rate.split("/", 1)
    fps = float(numerator) / max(1.0, float(denominator))
    return {
        "duration": float(payload["format"]["duration"]),
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "fps": fps,
        "video_codec": video_stream["codec_name"],
        "audio_codec": audio_stream["codec_name"] if audio_stream else None,
    }


def _stage_video_filters(index: int, stage: Any, frame: FrameSpec) -> list[str]:
    prefix = (
        f"[0:v]trim=start={{start}}:end={{end}},setpts=PTS-STARTPTS,"
        "setsar=1,split=2"
    )
    fallback_scale = frame.detail_scale if stage.name == "detail" else frame.full_body_scale
    scale = float(stage.scale) if float(getattr(stage, "visual_confidence", 0.0)) > 0 else fallback_scale
    scale = min(1.24, max(0.84, scale))
    center_x = min(0.85, max(0.15, float(getattr(stage, "center_x", 0.5))))
    center_y = min(0.85, max(0.15, float(getattr(stage, "center_y", 0.5))))
    x_expr = (
        f"max(min(0,W-w),min(max(0,W-w),(W-w)/2+(0.5-{center_x:.4f})*w))"
    )
    y_expr = (
        f"max(min(0,H-h),min(max(0,H-h),(H-h)/2+(0.5-{center_y:.4f})*h"
        f"-{frame.jianying_position_y_px:.3f}))"
    )
    return [
        prefix,
        (
            f"[bg{index}]scale={frame.width}:{frame.height}:force_original_aspect_ratio=increase,"
            f"crop={frame.width}:{frame.height},boxblur=20:2[blur{index}]"
        ),
        (
            f"[fg{index}]scale={frame.width}:{frame.height}:force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw*{scale:.4f}/2)*2:trunc(ih*{scale:.4f}/2)*2[fit{index}]"
        ),
        (
            f"[blur{index}][fit{index}]overlay=x='{x_expr}':y='{y_expr}',"
            "setsar=1,"
            f"fps=30,settb=AVTB,format=yuv420p[v{index}]"
        ),
    ]


def _filter_graph(
    plan: EditPlan, has_audio: bool, frame: FrameSpec
) -> tuple[str, str, str | None]:
    parts: list[str] = []
    for index, stage in enumerate(plan.stages):
        if frame.mode == "stage":
            stage_parts = _stage_video_filters(index, stage, frame)
            stage_parts[0] = stage_parts[0].format(start=f"{stage.start:.3f}", end=f"{stage.end:.3f}")
            stage_parts[0] += f"[bg{index}][fg{index}]"
            parts.extend(stage_parts)
        else:
            parts.append(
                f"[0:v]trim=start={stage.start:.3f}:end={stage.end:.3f},"
                f"setpts=PTS-STARTPTS,scale={frame.width}:{frame.height}:"
                f"force_original_aspect_ratio=increase,crop={frame.width}:{frame.height},"
                "setsar=1,"
                f"fps=30,settb=AVTB,format=yuv420p[v{index}]"
            )
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={stage.start:.3f}:end={stage.end:.3f},"
                f"asetpts=PTS-STARTPTS,aresample=async=1[a{index}]"
            )

    transition = frame.transition_seconds
    if transition <= 0.0:
        if has_audio:
            inputs = "".join(f"[v{index}][a{index}]" for index in range(len(plan.stages)))
            parts.append(
                f"{inputs}concat=n={len(plan.stages)}:v=1:a=1[vout][aout]"
            )
            return ";".join(parts), "[vout]", "[aout]"
        inputs = "".join(f"[v{index}]" for index in range(len(plan.stages)))
        parts.append(f"{inputs}concat=n={len(plan.stages)}:v=1:a=0[vout]")
        return ";".join(parts), "[vout]", None
    video_label = "v0"
    audio_label = "a0" if has_audio else None
    elapsed = plan.stages[0].duration
    for index in range(1, len(plan.stages)):
        video_out = "vout" if index == len(plan.stages) - 1 else f"vx{index}"
        offset = max(0.0, elapsed - transition)
        parts.append(
            f"[{video_label}][v{index}]xfade=transition=fade:duration={transition:.3f}:"
            f"offset={offset:.3f}[{video_out}]"
        )
        video_label = video_out
        if has_audio and audio_label:
            audio_out = "aout" if index == len(plan.stages) - 1 else f"ax{index}"
            parts.append(
                f"[{audio_label}][a{index}]acrossfade=d={transition:.3f}:c1=tri:c2=tri[{audio_out}]"
            )
            audio_label = audio_out
        elapsed += plan.stages[index].duration - transition
    return ";".join(parts), f"[{video_label}]", f"[{audio_label}]" if audio_label else None


def _run_render(
    *,
    ffmpeg: str,
    video: Path,
    output: Path,
    plan: EditPlan,
    encoder: str,
    has_audio: bool,
    frame: FrameSpec,
) -> tuple[int, str]:
    graph, video_map, audio_map = _filter_graph(plan, has_audio, frame)
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(video),
        "-filter_complex",
        graph,
        "-map",
        video_map,
    ]
    if audio_map:
        command.extend(["-map", audio_map])
    if encoder == "h264_amf":
        command.extend(["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "24"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"])
    if audio_map:
        command.extend(["-c:a", "aac", "-b:a", "160k"])
    command.extend(["-movflags", "+faststart", str(output)])
    completed = subprocess.run(command, text=True, capture_output=True, encoding="utf-8", errors="replace")
    return completed.returncode, completed.stderr


def render_plan(
    video: str | Path,
    output: str | Path,
    plan: EditPlan,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    prefer_hardware: bool = True,
    frame: FrameSpec | None = None,
) -> RenderResult:
    video_path = Path(video).resolve()
    output_path = Path(output).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(video_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = resolve_binary("ffmpeg", ffmpeg)
    source = probe_video(video_path, ffprobe)
    frame = frame or FrameSpec()
    if not -frame.height <= frame.jianying_position_y_px <= frame.height:
        raise ValueError("剪映位置Y必须位于画布高度范围内")
    encoders = ("h264_amf", "libx264") if prefer_hardware else ("libx264",)
    started = perf_counter()
    failures: list[str] = []
    chosen: str | None = None
    for encoder in encoders:
        code, stderr = _run_render(
            ffmpeg=ffmpeg_bin,
            video=video_path,
            output=output_path,
            plan=plan,
            encoder=encoder,
            has_audio=source["audio_codec"] is not None,
            frame=frame,
        )
        if code == 0 and output_path.is_file() and output_path.stat().st_size > 0:
            chosen = encoder
            break
        failures.append(f"{encoder}: {stderr[-1200:]}")
        output_path.unlink(missing_ok=True)
    if not chosen:
        raise RuntimeError("渲染失败\n" + "\n".join(failures))
    elapsed = round(perf_counter() - started, 3)
    rendered = probe_video(output_path, ffprobe)
    return RenderResult(
        output=output_path,
        encoder=chosen,
        elapsed_seconds=elapsed,
        duration_seconds=round(rendered["duration"], 3),
        width=rendered["width"],
        height=rendered["height"],
        fps=round(rendered["fps"], 3),
        video_codec=rendered["video_codec"],
        audio_codec=rendered["audio_codec"],
        jianying_position_y_px=frame.jianying_position_y_px,
    )
