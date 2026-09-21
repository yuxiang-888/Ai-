from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Protocol

from .asr import source_fingerprint, transcribe_video
from .planner import EditPlan, build_plan, load_sentences, validate_plan
from .policies import POLICIES, RequestedProductType
from .quality import inspect_output
from .renderer import FrameSpec, probe_video, render_plan
from .vision import analyze_video


AGENT_VERSION = "taobao-women-agent-v1"
RULE_VERSION = "taobao-women-v3-2026-09-21"


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    code: str
    reason: str
    data: Any
    evidence: tuple[dict[str, Any], ...] = ()
    elapsed_seconds: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        data = _bounded_summary(self.data)
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "data": data,
            "evidence": list(self.evidence),
            "elapsed_seconds": self.elapsed_seconds,
        }


def _bounded_summary(value: Any, *, depth: int = 0) -> Any:
    """Keep traces useful without storing raw frames, transcripts or local paths."""

    if depth > 3:
        return "[省略]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key.startswith("_") or key.endswith("_path") or key in {"source", "output"}:
                continue
            if key in {"observations", "sentences"} and isinstance(item, (list, tuple)):
                result[f"{key}_count"] = len(item)
                continue
            result[key] = _bounded_summary(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 20:
            return {"count": len(value), "sample": [_bounded_summary(item, depth=depth + 1) for item in value[:3]]}
        return [_bounded_summary(item, depth=depth + 1) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) or len(value) <= 500 else value[:500] + "…"
    return str(type(value).__name__)


@dataclass(frozen=True)
class AgentAttempt:
    number: int
    plan: dict[str, Any]
    output: str | None
    quality: dict[str, Any]
    revision_reason: str | None = None


@dataclass(frozen=True)
class AgentOutcome:
    status: str
    state: str
    source_fingerprint: str
    product_type: str
    output: str | None
    attempts: tuple[AgentAttempt, ...]
    tool_calls: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    total_seconds: float
    agent_version: str = AGENT_VERSION
    rule_version: str = RULE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "attempts": [asdict(item) for item in self.attempts],
            "tool_calls": list(self.tool_calls),
            "warnings": list(self.warnings),
        }


class EditingTools(Protocol):
    def probe_video(self, video: Path) -> ToolResult: ...

    def transcribe_speech(self, video: Path) -> ToolResult: ...

    def analyze_model_actions(self, video: Path) -> ToolResult: ...

    def retrieve_edit_rules(self, product_type: RequestedProductType) -> ToolResult: ...

    def build_edit_plan(
        self,
        transcript: ToolResult,
        vision: ToolResult,
        product_type: RequestedProductType,
        target_duration: float | None,
    ) -> ToolResult: ...

    def validate_plan(self, plan: EditPlan, media: dict[str, Any]) -> ToolResult: ...

    def render_video(self, video: Path, output: Path, plan: EditPlan, attempt: int) -> ToolResult: ...

    def inspect_output(self, output: Path, plan: EditPlan) -> ToolResult: ...

    def revise_plan(
        self,
        transcript: ToolResult,
        vision: ToolResult,
        previous: EditPlan,
        quality: ToolResult,
        product_type: RequestedProductType,
    ) -> ToolResult: ...


class LocalEditingTools:
    """Deterministic tools used by the Agent; no model success is fabricated."""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        whisper_model: str = "small",
        whisper_model_path: str | Path | None = None,
        pose_model: str | Path | None = None,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        vision_sample_fps: float = 2.0,
    ) -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.whisper_model = whisper_model
        self.whisper_model_path = whisper_model_path
        self.pose_model = pose_model
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.vision_sample_fps = vision_sample_fps

    @staticmethod
    def _call(code: str, operation: Callable[[], Any]) -> ToolResult:
        started = perf_counter()
        try:
            data = operation()
            return ToolResult(True, code, "", data, elapsed_seconds=round(perf_counter() - started, 3))
        except Exception as exc:
            return ToolResult(
                False,
                f"{code}_failed",
                f"{type(exc).__name__}: {exc}",
                {},
                elapsed_seconds=round(perf_counter() - started, 3),
            )

    def probe_video(self, video: Path) -> ToolResult:
        def operation() -> dict[str, Any]:
            media = probe_video(video, self.ffprobe)
            return {**media, "fingerprint": source_fingerprint(video)}

        return self._call("probe_video", operation)

    def transcribe_speech(self, video: Path) -> ToolResult:
        def operation() -> dict[str, Any]:
            result = transcribe_video(
                video,
                self.cache_dir,
                model=self.whisper_model,
                model_path=self.whisper_model_path,
            )
            sentences = load_sentences(result.path)
            return {
                "transcript_path": str(result.path),
                "sentences": [
                    {"id": index, **asdict(sentence)} for index, sentence in enumerate(sentences)
                ],
                "cached": result.cached,
                "model": result.model,
            }

        return self._call("transcribe_speech", operation)

    def analyze_model_actions(self, video: Path) -> ToolResult:
        def operation() -> dict[str, Any]:
            analysis = analyze_video(
                video,
                self.cache_dir,
                pose_model=self.pose_model,
                sample_fps=self.vision_sample_fps,
            )
            observations = [asdict(item) for item in analysis.observations]
            return {
                "detection_ratio": round(analysis.detection_ratio, 4),
                "sample_fps": analysis.sample_fps,
                "sample_count": len(observations),
                "cached": analysis.cached,
                "observations": observations,
                "_native": analysis,
            }

        return self._call("analyze_model_actions", operation)

    def retrieve_edit_rules(self, product_type: RequestedProductType) -> ToolResult:
        selected = product_type if product_type != "auto" else "single"
        policy = POLICIES[selected]
        return ToolResult(
            True,
            "retrieve_edit_rules",
            "",
            {"requested_product_type": product_type, "policy": policy.to_dict(), "rule_version": RULE_VERSION},
        )

    def build_edit_plan(
        self,
        transcript: ToolResult,
        vision: ToolResult,
        product_type: RequestedProductType,
        target_duration: float | None,
    ) -> ToolResult:
        def operation() -> dict[str, Any]:
            sentences = load_sentences(transcript.data["transcript_path"])
            plan = build_plan(
                sentences,
                target_duration=target_duration,
                vision=vision.data["_native"],
                product_type=product_type,
            )
            return {"plan": plan.to_dict(), "_native": plan}

        return self._call("build_edit_plan", operation)

    def validate_plan(self, plan: EditPlan, media: dict[str, Any]) -> ToolResult:
        def operation() -> dict[str, Any]:
            validate_plan(plan)
            duration = float(media["duration"])
            for stage in plan.stages:
                if stage.start < 0 or stage.end > duration + 0.05:
                    raise ValueError(f"阶段 {stage.name} 超出原片时长")
            return {"valid": True, "stage_count": len(plan.stages), "duration": plan.duration}

        return self._call("validate_plan", operation)

    def render_video(self, video: Path, output: Path, plan: EditPlan, attempt: int) -> ToolResult:
        def operation() -> dict[str, Any]:
            result = render_plan(
                video,
                output,
                plan,
                ffmpeg=self.ffmpeg,
                ffprobe=self.ffprobe,
                prefer_hardware=attempt == 0,
                frame=FrameSpec(width=1080, height=1440, mode="stage", transition_seconds=0.0),
            )
            return result.to_dict()

        return self._call("render_video", operation)

    def inspect_output(self, output: Path, plan: EditPlan) -> ToolResult:
        def operation() -> dict[str, Any]:
            report = inspect_output(output, plan, ffprobe=self.ffprobe)
            return report.to_dict()

        result = self._call("inspect_output", operation)
        if result.ok and not result.data.get("passed"):
            return ToolResult(
                False,
                "quality_failed",
                "成片没有通过全部质量门",
                result.data,
                elapsed_seconds=result.elapsed_seconds,
            )
        return result

    def revise_plan(
        self,
        transcript: ToolResult,
        vision: ToolResult,
        previous: EditPlan,
        quality: ToolResult,
        product_type: RequestedProductType,
    ) -> ToolResult:
        issues = quality.data.get("issues", []) if isinstance(quality.data, dict) else []
        codes = {str(item.get("code")) for item in issues if isinstance(item, dict)}
        retriable = {"duration_under_limit", "duration_over_limit"}
        if not codes or not codes.issubset(retriable):
            return ToolResult(
                False,
                "no_safe_revision",
                "当前失败涉及内容或画面证据，没有安全替代片段，必须人工复核",
                {"issue_codes": sorted(codes)},
            )
        delta = 3.0 if "duration_under_limit" in codes else -3.0
        new_target = min(
            previous.maximum_duration,
            max(previous.minimum_duration, previous.target_duration + delta),
        )
        if abs(new_target - previous.target_duration) < 0.01:
            return ToolResult(False, "revision_exhausted", "时长已达到品类边界", {"issue_codes": sorted(codes)})
        revised = self.build_edit_plan(transcript, vision, product_type, new_target)
        if not revised.ok:
            return revised
        return ToolResult(
            True,
            "revise_plan",
            "",
            {**revised.data, "revision_reason": ",".join(sorted(codes)), "target_duration": new_target},
            elapsed_seconds=revised.elapsed_seconds,
        )


class EditingAgent:
    def __init__(self, tools: EditingTools, *, max_retries: int = 2) -> None:
        if not 0 <= max_retries <= 2:
            raise ValueError("自动重剪次数必须在 0—2 之间")
        self.tools = tools
        self.max_retries = max_retries

    def run(
        self,
        video: str | Path,
        output: str | Path,
        *,
        product_type: RequestedProductType = "auto",
        target_duration: float | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> AgentOutcome:
        source = Path(video).resolve()
        destination = Path(output).resolve()
        started = perf_counter()
        calls: list[dict[str, Any]] = []
        attempts: list[AgentAttempt] = []
        warnings: list[str] = []
        notify = progress or (lambda _state, _payload: None)

        def call(name: str, state: str, operation: Callable[[], ToolResult]) -> ToolResult:
            notify(state, {"tool": name})
            result = operation()
            calls.append({"tool": name, "state": state, **result.public_dict()})
            return result

        notify("RECEIVED", {"agent_version": AGENT_VERSION, "rule_version": RULE_VERSION})
        media = call("probe_video", "ANALYZING", lambda: self.tools.probe_video(source))
        transcript = call("transcribe_speech", "ANALYZING", lambda: self.tools.transcribe_speech(source))
        vision = call("analyze_model_actions", "ANALYZING", lambda: self.tools.analyze_model_actions(source))
        rules = call("retrieve_edit_rules", "PLANNING", lambda: self.tools.retrieve_edit_rules(product_type))
        required = (media, transcript, vision, rules)
        failed = next((item for item in required if not item.ok), None)
        if failed:
            warnings.append(failed.reason)
            return self._finish("needs_review", "NEEDS_REVIEW", media, product_type, None, attempts, calls, warnings, started)

        plan_result = call(
            "build_edit_plan",
            "PLANNING",
            lambda: self.tools.build_edit_plan(transcript, vision, product_type, target_duration),
        )
        if not plan_result.ok:
            warnings.append(plan_result.reason)
            return self._finish("needs_review", "NEEDS_REVIEW", media, product_type, None, attempts, calls, warnings, started)

        plan: EditPlan = plan_result.data["_native"]
        final_output: Path | None = None
        revision_reason: str | None = None
        for attempt in range(self.max_retries + 1):
            validation = call(
                "validate_plan",
                "VALIDATING",
                lambda: self.tools.validate_plan(plan, media.data),
            )
            if not validation.ok:
                warnings.append(validation.reason)
                break
            attempt_output = destination if attempt == 0 else destination.with_name(
                f"{destination.stem}_retry{attempt}{destination.suffix}"
            )
            rendered = call(
                "render_video",
                "RENDERING",
                lambda attempt=attempt, attempt_output=attempt_output: self.tools.render_video(
                    source, attempt_output, plan, attempt
                ),
            )
            if not rendered.ok:
                warnings.append(rendered.reason)
                if attempt >= self.max_retries:
                    break
                revision_reason = "render_failure_software_retry"
                continue
            final_output = attempt_output
            quality = call(
                "inspect_output",
                "EVALUATING",
                lambda attempt_output=attempt_output: self.tools.inspect_output(attempt_output, plan),
            )
            attempts.append(
                AgentAttempt(
                    number=attempt,
                    plan=plan.to_dict(),
                    output=str(attempt_output),
                    quality=quality.data if isinstance(quality.data, dict) else {},
                    revision_reason=revision_reason,
                )
            )
            if quality.ok and quality.data.get("passed"):
                actual_type = str(plan.product_type)
                return self._finish("approved", "PASSED", media, actual_type, final_output, attempts, calls, warnings, started)
            if attempt >= self.max_retries:
                warnings.append(quality.reason or "自动重剪次数已用完")
                break
            revised = call(
                "revise_plan",
                "REVISING",
                lambda: self.tools.revise_plan(transcript, vision, plan, quality, product_type),
            )
            if not revised.ok:
                warnings.append(revised.reason)
                break
            plan = revised.data["_native"]
            revision_reason = str(revised.data.get("revision_reason") or "quality_feedback")

        actual_type = str(getattr(plan, "product_type", product_type))
        return self._finish("needs_review", "NEEDS_REVIEW", media, actual_type, final_output, attempts, calls, warnings, started)

    @staticmethod
    def _finish(
        status: str,
        state: str,
        media: ToolResult,
        product_type: str,
        output: Path | None,
        attempts: list[AgentAttempt],
        calls: list[dict[str, Any]],
        warnings: list[str],
        started: float,
    ) -> AgentOutcome:
        fingerprint = ""
        if isinstance(media.data, dict):
            fingerprint = str(media.data.get("fingerprint") or "")
        return AgentOutcome(
            status=status,
            state=state,
            source_fingerprint=fingerprint,
            product_type=product_type,
            output=str(output) if output else None,
            attempts=tuple(attempts),
            tool_calls=tuple(calls),
            warnings=tuple(dict.fromkeys(item for item in warnings if item)),
            total_seconds=round(perf_counter() - started, 3),
        )


def save_outcome(outcome: AgentOutcome, path: str | Path) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target
