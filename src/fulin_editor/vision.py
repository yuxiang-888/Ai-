from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

from .asr import source_fingerprint


VISION_ANALYZER_VERSION = 2


@dataclass(frozen=True)
class FrameObservation:
    time: float
    detected: bool
    center_x: float = 0.5
    center_y: float = 0.5
    bbox_width: float = 0.0
    bbox_height: float = 0.0
    face_visibility: float = 0.0
    torso_visibility: float = 0.0
    full_body: float = 0.0
    hand_detail: float = 0.0
    shoulder_depth: float = 0.0
    motion: float = 0.0
    sharpness: float = 0.0
    brightness: float = 0.0
    back_likelihood: float = 0.0


@dataclass(frozen=True)
class VisionAnalysis:
    source: str
    source_fingerprint: str
    sample_fps: float
    elapsed_seconds: float
    cached: bool
    observations: tuple[FrameObservation, ...]

    @property
    def detection_ratio(self) -> float:
        if not self.observations:
            return 0.0
        return sum(item.detected for item in self.observations) / len(self.observations)

    def between(self, start: float, end: float) -> list[FrameObservation]:
        return [item for item in self.observations if start <= item.time <= end]

    def interval_evidence(self, start: float, end: float, stage: str) -> dict[str, float]:
        frames = self.between(start, end)
        detected = [item for item in frames if item.detected]
        if not frames or not detected:
            return {
                "detected_ratio": 0.0,
                "stability": 0.0,
                "full_body": 0.0,
                "detail": 0.0,
                "back": 0.0,
                "front": 0.0,
                "turn_cycle": 0.0,
                "sharpness": 0.0,
                "center_x": 0.5,
                "center_y": 0.5,
                "scale": 1.0,
            }
        ratio = len(detected) / len(frames)
        stability = _clamp(1.0 - sum(item.motion for item in detected) / len(detected))
        full_body = sum(item.full_body for item in detected) / len(detected)
        hand_detail = sum(item.hand_detail for item in detected) / len(detected)
        back = sum(item.back_likelihood for item in detected) / len(detected)
        sharpness = sum(item.sharpness for item in detected) / len(detected)
        bbox_height = median(item.bbox_height for item in detected)
        center_x = median(item.center_x for item in detected)
        center_y = median(item.center_y for item in detected)
        depths = [item.shoulder_depth for item in detected]
        baseline = median(depths[: min(2, len(depths))])
        excursions = [abs(value - baseline) for value in depths]
        peak_index = max(range(len(excursions)), key=excursions.__getitem__)
        peak_excursion = excursions[peak_index]
        post_turn = excursions[peak_index + 1 :]
        returned = bool(post_turn) and min(post_turn) <= 0.10
        turn_cycle = _clamp((peak_excursion - 0.12) / 0.35) * (1.0 if returned else 0.25)
        detail = _clamp(hand_detail * 0.65 + min(1.0, bbox_height / 0.72) * 0.35)
        desired_height = 0.80 if stage in {"opening", "size", "back_return"} else 0.88
        scale = desired_height / max(0.25, bbox_height)
        if stage in {"opening", "size", "back_return"}:
            scale = min(1.08, max(0.86, scale))
        else:
            detail_cap = 1.04 if ratio < 0.75 else 1.10
            scale = min(detail_cap, max(0.96, scale))
        return {
            "detected_ratio": round(ratio, 4),
            "stability": round(stability, 4),
            "full_body": round(full_body, 4),
            "detail": round(detail, 4),
            "back": round(back, 4),
            "front": round(1.0 - back, 4),
            "turn_cycle": round(turn_cycle, 4),
            "sharpness": round(sharpness, 4),
            "center_x": round(center_x, 4),
            "center_y": round(center_y, 4),
            "scale": round(scale, 4),
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def _pose_model(explicit: str | Path | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    configured = os.getenv("FULIN_POSE_MODEL", "").strip()
    if configured:
        candidates.append(Path(configured))
    project_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            project_root / "models" / "pose_landmarker_lite.task",
            Path.home() / "Desktop" / "suchen-U盘完整版-内置浏览器" / "_internal" / "models" / "pose_landmarker_lite.task",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到 pose_landmarker_lite.task；请放到项目 models 目录或设置 FULIN_POSE_MODEL"
    )


def _visibility(landmarks: list[Any], indices: tuple[int, ...]) -> float:
    values = [float(getattr(landmarks[index], "visibility", 0.0) or 0.0) for index in indices]
    return sum(values) / len(values)


def _observation(
    timestamp: float,
    rgb: Any,
    result: Any,
    previous_center: tuple[float, float] | None,
    previous_height: float | None,
) -> FrameObservation:
    import cv2

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    brightness = _clamp(float(gray.mean()) / 255.0)
    sharpness = _clamp(float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 600.0)
    if not result.pose_landmarks:
        return FrameObservation(
            time=round(timestamp, 3), detected=False, sharpness=sharpness, brightness=brightness
        )
    landmarks = result.pose_landmarks[0]
    visible = [point for point in landmarks if float(getattr(point, "visibility", 0.0) or 0.0) >= 0.25]
    if len(visible) < 8:
        return FrameObservation(
            time=round(timestamp, 3), detected=False, sharpness=sharpness, brightness=brightness
        )
    xs = [float(point.x) for point in visible]
    ys = [float(point.y) for point in visible]
    left, right = max(0.0, min(xs)), min(1.0, max(xs))
    top, bottom = max(0.0, min(ys)), min(1.0, max(ys))
    width, height = right - left, bottom - top
    center = ((left + right) / 2.0, (top + bottom) / 2.0)
    face = _visibility(landmarks, tuple(range(0, 11)))
    torso = _visibility(landmarks, (11, 12, 23, 24))
    legs = _visibility(landmarks, (25, 26, 27, 28))
    full_body = _clamp(legs * 0.70 + min(1.0, height / 0.65) * 0.30)
    shoulder_y = (float(landmarks[11].y) + float(landmarks[12].y)) / 2.0
    hip_y = (float(landmarks[23].y) + float(landmarks[24].y)) / 2.0
    torso_left = min(float(landmarks[11].x), float(landmarks[12].x), float(landmarks[23].x), float(landmarks[24].x))
    torso_right = max(float(landmarks[11].x), float(landmarks[12].x), float(landmarks[23].x), float(landmarks[24].x))
    wrists = (landmarks[15], landmarks[16])
    hands_near_garment = sum(
        torso_left - 0.12 <= float(wrist.x) <= torso_right + 0.12
        and shoulder_y - 0.10 <= float(wrist.y) <= hip_y + 0.18
        for wrist in wrists
    ) / 2.0
    shoulder_depth = float(landmarks[11].z) - float(landmarks[12].z)
    motion = 0.0
    if previous_center is not None and previous_height is not None:
        center_motion = math.dist(center, previous_center) * 3.0
        scale_motion = abs(height - previous_height) * 2.0
        motion = _clamp(center_motion + scale_motion)
    back_likelihood = max(
        _clamp((torso - face) * 1.8 + (0.40 - face) * 0.8),
        _clamp((abs(shoulder_depth) - 0.18) * 1.6),
    )
    return FrameObservation(
        time=round(timestamp, 3),
        detected=True,
        center_x=round(center[0], 4),
        center_y=round(center[1], 4),
        bbox_width=round(width, 4),
        bbox_height=round(height, 4),
        face_visibility=round(face, 4),
        torso_visibility=round(torso, 4),
        full_body=round(full_body, 4),
        hand_detail=round(hands_near_garment, 4),
        shoulder_depth=round(shoulder_depth, 4),
        motion=round(motion, 4),
        sharpness=round(sharpness, 4),
        brightness=round(brightness, 4),
        back_likelihood=round(back_likelihood, 4),
    )


def _from_payload(payload: dict[str, Any], *, cached: bool) -> VisionAnalysis:
    return VisionAnalysis(
        source=str(payload["source"]),
        source_fingerprint=str(payload["source_fingerprint"]),
        sample_fps=float(payload["sample_fps"]),
        elapsed_seconds=0.0 if cached else float(payload.get("elapsed_seconds", 0.0)),
        cached=cached,
        observations=tuple(FrameObservation(**item) for item in payload["observations"]),
    )


def analyze_video(
    video: str | Path,
    cache_dir: str | Path,
    *,
    pose_model: str | Path | None = None,
    sample_fps: float = 2.0,
    force: bool = False,
) -> VisionAnalysis:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import vision

    source = Path(video).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    fingerprint = source_fingerprint(source)
    cache_root = Path(cache_dir).resolve() / "vision"
    cache_root.mkdir(parents=True, exist_ok=True)
    output = cache_root / (
        f"{source.stem}_{fingerprint}_v{VISION_ANALYZER_VERSION}_{sample_fps:.2f}fps.json"
    )
    if output.is_file() and not force:
        payload = json.loads(output.read_text(encoding="utf-8"))
        if payload.get("source_fingerprint") == fingerprint and payload.get("observations"):
            return _from_payload(payload, cached=True)

    model = _pose_model(pose_model)
    options = vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.IMAGE,
        num_poses=1,
        min_pose_detection_confidence=0.45,
        min_pose_presence_confidence=0.45,
        min_tracking_confidence=0.45,
    )
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV 无法打开视频：{source}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_step = max(1, round(source_fps / max(0.25, sample_fps)))
    observations: list[FrameObservation] = []
    previous_center: tuple[float, float] | None = None
    previous_height: float | None = None
    started = perf_counter()
    try:
        with vision.PoseLandmarker.create_from_options(options) as detector:
            frame_index = 0
            while True:
                ok, bgr = capture.read()
                if not ok:
                    break
                if frame_index % frame_step:
                    frame_index += 1
                    continue
                timestamp = frame_index / source_fps
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = detector.detect(image)
                item = _observation(timestamp, rgb, result, previous_center, previous_height)
                observations.append(item)
                if item.detected:
                    previous_center = (item.center_x, item.center_y)
                    previous_height = item.bbox_height
                frame_index += 1
    finally:
        capture.release()
    if not observations:
        raise RuntimeError("视频画面分析没有产生任何采样帧")
    elapsed = round(perf_counter() - started, 3)
    payload = {
        "source": str(source),
        "source_fingerprint": fingerprint,
        "analyzer_version": VISION_ANALYZER_VERSION,
        "pose_model": model.name,
        "sample_fps": sample_fps,
        "elapsed_seconds": elapsed,
        "observations": [asdict(item) for item in observations],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return _from_payload(payload, cached=False)
