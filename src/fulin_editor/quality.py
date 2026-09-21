from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .planner import EditPlan
from .renderer import probe_video


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    severity: str = "block"


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    issues: tuple[QualityIssue, ...]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [asdict(issue) for issue in self.issues],
            "metrics": self.metrics,
        }


def _border_difference(left: Any, right: Any) -> float:
    """Measure scene/background change while ignoring the presenter's center area."""

    import cv2
    import numpy as np

    difference = cv2.absdiff(left, right)
    height, width = difference.shape[:2]
    mask = np.ones((height, width), dtype=bool)
    mask[int(height * 0.12) : int(height * 0.92), int(width * 0.18) : int(width * 0.82)] = False
    return float(difference[mask].mean())


def _temporal_flash_kind(
    anchor: Any,
    middle: Any,
    returned: Any,
    *,
    outgoing_difference: float,
    incoming_difference: float,
) -> str | None:
    """Separate a real whole-frame flash from an allowed same-shot pose jump."""

    import cv2
    import numpy as np

    return_difference = float(np.mean(cv2.absdiff(anchor, returned)))
    if not (
        outgoing_difference > 24.0
        and incoming_difference > 24.0
        and return_difference < 7.0
    ):
        return None
    border_out = _border_difference(anchor, middle)
    border_in = _border_difference(middle, returned)
    border_return = _border_difference(anchor, returned)
    if border_out > 14.0 and border_in > 14.0 and border_return < 5.0:
        return "whole_frame_flash"
    return "same_shot_pose_jump"


def _visual_integrity(video: Path) -> dict[str, Any]:
    import cv2
    import numpy as np

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"质量检查无法打开成片：{video}")
    decoded_frames = 0
    black_frames = 0
    flashes = 0
    ignored_pose_jumps = 0
    diff_total = 0.0
    diff_count = 0
    longest_freeze = 0
    current_freeze = 0
    previous_previous: Any | None = None
    previous: Any | None = None
    previous_diff: float | None = None
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (96, 128), interpolation=cv2.INTER_AREA)
            decoded_frames += 1
            black_frames += float(small.mean()) < 5.0
            if previous is not None:
                difference = float(np.mean(cv2.absdiff(previous, small)))
                diff_total += difference
                diff_count += 1
                if difference < 0.12:
                    current_freeze += 1
                    longest_freeze = max(longest_freeze, current_freeze)
                else:
                    current_freeze = 0
                if previous_previous is not None and previous_diff is not None:
                    kind = _temporal_flash_kind(
                        previous_previous,
                        previous,
                        small,
                        outgoing_difference=previous_diff,
                        incoming_difference=difference,
                    )
                    if kind == "whole_frame_flash":
                        flashes += 1
                    elif kind == "same_shot_pose_jump":
                        ignored_pose_jumps += 1
                previous_diff = difference
            previous_previous = previous
            previous = small
    finally:
        capture.release()
    if decoded_frames < 3:
        return {
            "decoded_frames": decoded_frames,
            "black_frames": black_frames,
            "single_frame_flashes": 0,
            "same_shot_pose_jumps_ignored": 0,
            "longest_near_freeze_frames": 0,
        }
    return {
        "decoded_frames": decoded_frames,
        "black_frames": black_frames,
        "single_frame_flashes": flashes,
        "same_shot_pose_jumps_ignored": ignored_pose_jumps,
        "longest_near_freeze_frames": longest_freeze,
        "mean_adjacent_frame_difference": round(diff_total / max(1, diff_count), 3),
    }


def inspect_output(
    output: str | Path,
    plan: EditPlan,
    *,
    expected_width: int = 1080,
    expected_height: int = 1440,
    ffprobe: str | None = None,
) -> QualityReport:
    path = Path(output).resolve()
    issues: list[QualityIssue] = []
    if not path.is_file() or path.stat().st_size == 0:
        return QualityReport(
            passed=False,
            issues=(QualityIssue("missing_output", "没有生成有效成片"),),
            metrics={},
        )
    media = probe_video(path, ffprobe)
    if media["duration"] > plan.maximum_duration + 0.08:
        issues.append(
            QualityIssue(
                "duration_over_limit",
                f"{plan.product_label}成片超过 {plan.maximum_duration:.0f} 秒上限",
            )
        )
    if media["duration"] < plan.minimum_duration - 0.08:
        issues.append(
            QualityIssue(
                "duration_under_limit",
                f"{plan.product_label}成片不足 {plan.minimum_duration:.0f} 秒",
            )
        )
    if (media["width"], media["height"]) != (expected_width, expected_height):
        issues.append(
            QualityIssue(
                "wrong_canvas",
                f"成片为 {media['width']}x{media['height']}，要求 {expected_width}x{expected_height}",
            )
        )
    if not 29.0 <= media["fps"] <= 31.0:
        issues.append(QualityIssue("wrong_fps", f"成片帧率为 {media['fps']:.2f}，要求30 FPS"))
    if media["audio_codec"] is None:
        issues.append(QualityIssue("missing_audio", "成片没有保留主播原声"))
    if plan.warnings:
        issues.extend(QualityIssue("plan_review", warning) for warning in plan.warnings)
    if not plan.stages or plan.stages[0].name != "opening":
        issues.append(QualityIssue("missing_hard_opening", "输出第一幕不是全身正面 HARD 开场"))
    elif plan.stages[0].duration > 1.05:
        issues.append(QualityIssue("opening_too_long", "全身正面开场超过 1 秒"))
    integrity = _visual_integrity(path)
    if integrity["decoded_frames"] < 3:
        issues.append(QualityIssue("decode_failure", "成片无法完整解码"))
    if integrity["black_frames"]:
        issues.append(
            QualityIssue("black_frame", f"检测到 {integrity['black_frames']} 个近黑帧")
        )
    if integrity["single_frame_flashes"]:
        issues.append(
            QualityIssue(
                "single_frame_flash",
                f"检测到 {integrity['single_frame_flashes']} 处疑似单帧闪现",
            )
        )
    if integrity["longest_near_freeze_frames"] >= 30:
        issues.append(
            QualityIssue(
                "possible_freeze",
                "检测到超过1秒的近似冻结画面",
                severity="warn",
            )
        )
    passed = not any(issue.severity == "block" for issue in issues)
    return QualityReport(
        passed=passed,
        issues=tuple(issues),
        metrics={"media": media, "visual_integrity": integrity},
    )
