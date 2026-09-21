from __future__ import annotations

from dataclasses import replace

from fulin_editor.agent_runtime import EditingAgent, ToolResult
from fulin_editor.planner import EditPlan, Stage


def _plan(target: float = 27.0) -> EditPlan:
    return EditPlan(
        stages=(
            Stage("opening", "首秒全身正面", 0.0, 1.0, "开场", 0.9),
            Stage("size", "尺码介绍", 1.0, 6.0, "尺码", 0.9),
            Stage("detail", "商品与细节", 6.0, 16.0, "面料", 0.9),
            Stage("back_return", "背面展示与转回", 16.0, 27.0, "背面转回", 0.9),
        ),
        target_duration=target,
    )


class FakeTools:
    def __init__(self, qualities: list[ToolResult]) -> None:
        self.qualities = qualities
        self.render_attempts: list[int] = []
        self.revisions = 0
        self.plan = _plan()

    def probe_video(self, _video):
        return ToolResult(True, "probe_video", "", {"duration": 90.0, "fingerprint": "abc"})

    def transcribe_speech(self, _video):
        return ToolResult(True, "transcribe_speech", "", {"sentences": [{"id": 0}]})

    def analyze_model_actions(self, _video):
        return ToolResult(True, "analyze_model_actions", "", {"observations": []})

    def retrieve_edit_rules(self, _product_type):
        return ToolResult(True, "retrieve_edit_rules", "", {"rule_version": "test"})

    def build_edit_plan(self, _transcript, _vision, _product_type, _target_duration):
        return ToolResult(True, "build_edit_plan", "", {"plan": self.plan.to_dict(), "_native": self.plan})

    def validate_plan(self, plan, _media):
        return ToolResult(True, "validate_plan", "", {"valid": True, "duration": plan.duration})

    def render_video(self, _video, output, _plan, attempt):
        self.render_attempts.append(attempt)
        return ToolResult(True, "render_video", "", {"output": str(output)})

    def inspect_output(self, _output, _plan):
        return self.qualities.pop(0)

    def revise_plan(self, _transcript, _vision, previous, _quality, _product_type):
        self.revisions += 1
        revised = replace(previous, target_duration=29.0)
        return ToolResult(
            True,
            "revise_plan",
            "",
            {"plan": revised.to_dict(), "_native": revised, "revision_reason": "duration_under_limit"},
        )


def test_agent_passes_after_quality_feedback_revision(tmp_path):
    failed = ToolResult(
        False,
        "quality_failed",
        "时长不足",
        {"passed": False, "issues": [{"code": "duration_under_limit"}]},
    )
    passed = ToolResult(True, "inspect_output", "", {"passed": True, "issues": []})
    tools = FakeTools([failed, passed])

    outcome = EditingAgent(tools, max_retries=2).run(
        tmp_path / "source.mp4", tmp_path / "output.mp4"
    )

    assert outcome.status == "approved"
    assert outcome.state == "PASSED"
    assert len(outcome.attempts) == 2
    assert tools.revisions == 1
    assert tools.render_attempts == [0, 1]
    assert outcome.attempts[1].revision_reason == "duration_under_limit"


def test_agent_stops_when_analysis_tool_fails(tmp_path):
    tools = FakeTools([])
    tools.transcribe_speech = lambda _video: ToolResult(
        False, "transcribe_speech_failed", "没有识别到口播", {}
    )

    outcome = EditingAgent(tools).run(tmp_path / "source.mp4", tmp_path / "output.mp4")

    assert outcome.status == "needs_review"
    assert outcome.state == "NEEDS_REVIEW"
    assert outcome.attempts == ()
    assert "没有识别到口播" in outcome.warnings
