from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal


ProductType = Literal["auto", "single", "set", "bulky"]

LEGACY_PROFILES: dict[str, tuple[float, float, float]] = {
    "auto": (3.0, 120.0, 25.0),
    "single": (3.0, 30.0, 25.0),
    "set": (3.0, 60.0, 50.0),
    "bulky": (3.0, 120.0, 120.0),
}


@dataclass(frozen=True)
class LegacyEditResult:
    output: Path | None
    report: Path
    status: Literal["approved", "needs_review", "failed"]
    product_type: str
    target_duration: float
    engine_job_id: str | None
    payload: dict
    error: str | None


class SuchenEngineError(RuntimeError):
    pass


class SuchenEngineClient:
    """Server-side adapter for the original SOCHEN 1.2.3 HTTP API.

    This submits work and mirrors results; it does not reproduce or replace the
    original program's editing algorithm.
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = None
            raise SuchenEngineError(str(detail or f"SOCHEN 请求失败（{exc.code}）")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SuchenEngineError(f"SOCHEN 原剪辑引擎未连接：{exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SuchenEngineError("SOCHEN 原剪辑引擎返回了无效数据") from exc
        if not isinstance(data, dict):
            raise SuchenEngineError("SOCHEN 原剪辑引擎返回格式不正确")
        return data

    def health(self) -> dict:
        return self.request_json("GET", "/api/status")

    def action_template(self) -> dict:
        payload = self.request_json("GET", "/api/action-template")
        template = payload.get("template")
        return template if isinstance(template, dict) else {}

    def save_action_template(self, template: dict) -> dict:
        return self.request_json("POST", "/api/action-template", template)

    def run_current(
        self,
        source: Path,
        output_dir: Path,
        product_type: ProductType,
        target_duration: float | None,
        progress: Callable[[str], None],
        timeout_seconds: float = 7200.0,
    ) -> tuple[dict, dict]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self.health()
            if not status.get("running"):
                break
            if time.monotonic() >= deadline:
                raise SuchenEngineError("SOCHEN 原剪辑引擎长时间忙碌，任务等待超时")
            progress(_stage_from_legacy(status))
            time.sleep(1.2)

        output_dir.mkdir(parents=True, exist_ok=True)
        folder_query = urllib.parse.quote(str(source.parent), safe="")
        media = self.request_json("GET", f"/api/media?folder={folder_query}")
        if media.get("error"):
            raise SuchenEngineError(str(media["error"]))
        selected_output = self.request_json("POST", "/api/output", {"output": str(output_dir)})
        if selected_output.get("error"):
            raise SuchenEngineError(str(selected_output["error"]))

        minimum, maximum, default_target = LEGACY_PROFILES[product_type]
        chosen_target = default_target if target_duration is None else target_duration
        chosen_target = min(maximum, max(minimum, float(chosen_target)))
        template = self.action_template()
        modes = template.get("modes") if isinstance(template.get("modes"), dict) else {}
        request_payload: dict[str, object] = {
            "name": source.name,
            "folder": str(source.parent),
            "output": str(output_dir),
            "template": "main",
            "product_type": product_type,
            "min_sec": str(minimum),
            "max_sec": str(maximum),
            "target_sec": str(chosen_target),
            "model": "small",
            "quality": "1",
            "pending_only": "0",
            "cam": "0",
        }
        for key in (
            "opening_max_sec",
            "approach_preroll_sec",
            "back_min_duration_sec",
            "return_stable_sec",
        ):
            if key in template:
                request_payload[key] = str(template[key])
        for role, mode in modes.items():
            request_payload[f"action_{role}"] = mode

        started = self.request_json("POST", "/api/start-current", request_payload)
        if started.get("error"):
            raise SuchenEngineError(str(started["error"]))
        progress("analyzing")

        last_status: dict = started
        while time.monotonic() < deadline:
            time.sleep(1.2)
            last_status = self.health()
            progress(_stage_from_legacy(last_status))
            if not last_status.get("running"):
                timelines = self.request_json("GET", "/api/timelines")
                return last_status, timelines
        raise SuchenEngineError("SOCHEN 原剪辑引擎处理超时")


def _stage_from_legacy(status: dict) -> str:
    phase = str(status.get("phase") or "").lower()
    message = str(status.get("phase_message") or "")
    if "口播" in message or "转写" in message or "asr" in message.lower():
        return "transcribing"
    if phase in {"plan", "planning"} or "规划" in message:
        return "planning"
    if phase in {"render", "rendering"} or "渲染" in message or "编码" in message:
        return "rendering"
    if phase in {"quality", "qa"} or "质量" in message or "质检" in message:
        return "quality"
    return "analyzing"


def _latest_legacy_run(database: Path, source_name: str, output_dir: Path) -> dict | None:
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM edit_runs WHERE source_name = ? ORDER BY id DESC LIMIT 8",
            (source_name,),
        ).fetchall()
    finally:
        connection.close()
    wanted = str(output_dir.resolve()).casefold()
    for row in rows:
        candidate = dict(row)
        workspace = str(candidate.get("output_workspace") or "").casefold()
        if not workspace or workspace == wanted:
            return candidate
    return dict(rows[0]) if rows else None


def _find_output(output_dir: Path, source: Path, status: dict, timelines: dict) -> Path | None:
    names: list[str] = []
    for item in status.get("results") or []:
        if isinstance(item, dict) and item.get("name") == source.name and item.get("file"):
            names.append(str(item["file"]))
    timeline_map = timelines.get("timelines") if isinstance(timelines, dict) else None
    timeline = timeline_map.get(source.name) if isinstance(timeline_map, dict) else None
    if isinstance(timeline, dict) and timeline.get("file"):
        names.append(str(timeline["file"]))
    for name in names:
        path = Path(name)
        candidates = [path] if path.is_absolute() else [output_dir / path.name]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    matches = [
        item
        for item in output_dir.glob(f"*{source.stem}*.mp4")
        if item.is_file() and item.stat().st_size > 0
    ]
    return max(matches, key=lambda item: item.stat().st_mtime).resolve() if matches else None


def _load_legacy_report(run: dict | None) -> dict:
    if not run:
        return {}
    report_path = Path(str(run.get("report_path") or ""))
    if report_path.is_file():
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        payload = json.loads(str(run.get("report_json") or "{}"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def normalize_legacy_report(raw: dict, requested_type: ProductType, fallback_target: float) -> dict:
    agent = raw.get("Agent决策") if isinstance(raw.get("Agent决策"), dict) else {}
    product = raw.get("商品类型") if isinstance(raw.get("商品类型"), dict) else {}
    duration_rule = raw.get("成片时长标准") if isinstance(raw.get("成片时长标准"), dict) else {}
    qa = raw.get("质量门") if isinstance(raw.get("质量门"), dict) else {}
    actual_type = str(product.get("识别") or requested_type)
    if actual_type not in {"single", "set", "bulky"}:
        actual_type = "single" if requested_type == "auto" else requested_type
    clips = raw.get("成片片段") if isinstance(raw.get("成片片段"), list) else []
    stages: list[dict] = []
    output_cursor = 0.0
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            continue
        start = float(clip.get("开始") or 0.0)
        end = float(clip.get("结束") or start)
        duration = max(0.0, float(clip.get("时长") or end - start))
        label = str(clip.get("阶段") or f"片段 {index + 1}")
        stages.append(
            {
                "name": f"suchen_{index + 1}",
                "label": label,
                "start": start,
                "end": end,
                "output_start": output_cursor,
                "output_end": output_cursor + duration,
                "duration": duration,
                "text": str(clip.get("备注") or label),
                "confidence": 1.0,
                "evidence": [{"type": "suchen-original", "detail": "SOCHEN 1.2.3 原剪辑时间轴"}],
            }
        )
        output_cursor += duration
    issues = []
    for item in qa.get("问题") or []:
        message = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        issues.append({"code": "suchen_quality", "message": message, "severity": "block"})
    total = float(raw.get("成片总时长") or output_cursor or fallback_target)
    technical_passed = bool(qa.get("通过", False)) and int(qa.get("驳回项", 0) or 0) == 0
    engine = str(agent.get("engine") or qa.get("动作引擎") or "unknown")
    fallback = engine == "contiguous_hard_cut" or "兜底" in str(raw.get("制作模式") or "")
    required_roles = (
        "opening_full_front",
        "origin_size_intro",
        "move_to_center_product_detail",
        "back_full_body_show",
        "return_origin_front_finish",
    )
    stage_checks = qa.get("阶段检查") if isinstance(qa.get("阶段检查"), dict) else {}
    stage_coverage = (
        agent.get("stage_coverage") if isinstance(agent.get("stage_coverage"), dict) else {}
    )

    def stage_proved(role: str) -> bool:
        check = stage_checks.get(role)
        if isinstance(check, dict) and check.get("passed") is True:
            return True
        evidence = stage_coverage.get(role)
        return bool(
            isinstance(evidence, dict)
            and evidence.get("covered") is True
            and evidence.get("start") is not None
            and evidence.get("end") is not None
        )

    missing_roles = [role for role in required_roles if not stage_proved(role)]
    content_coverage = (
        agent.get("content_coverage") if isinstance(agent.get("content_coverage"), dict) else {}
    )

    def content_proved(role: str) -> bool:
        evidence = content_coverage.get(role)
        return bool(
            isinstance(evidence, dict)
            and evidence.get("covered") is True
            and evidence.get("source") != "missing_source"
        )

    missing_content = [
        role for role in ("content_size", "content_fabric") if not content_proved(role)
    ]
    passed = technical_passed and not fallback and not missing_roles and not missing_content
    if fallback:
        issues.append({
            "code": "fallback_requires_review",
            "message": "连续硬剪兜底没有完整的 MediaPipe/ASR 五阶段证据，只能待人工复核",
            "severity": "review",
        })
    if missing_roles:
        issues.append({
            "code": "missing_stage_evidence",
            "message": "缺少固定叙事阶段证据：" + "、".join(missing_roles),
            "severity": "review",
        })
    if missing_content:
        issues.append({
            "code": "missing_content_evidence",
            "message": "缺少真实口播保留证据：" + "、".join(missing_content),
            "severity": "review",
        })
    if total > 120.04:
        passed = False
        issues.append(
            {
                "code": "duration_over_120_seconds",
                "message": "成片超过120秒，不符合公司最长时长标准",
                "severity": "block",
            }
        )
    target = float(duration_rule.get("目标秒") or fallback_target)
    return {
        "status": "approved" if passed else "needs_review",
        "engine": "suchen-1.2.3-original",
        "editing_method_preserved": True,
        "plan": {
            "duration": total,
            "actual_duration": total,
            "target_duration": target,
            "product_type": actual_type,
            "product_label": str(product.get("名称") or actual_type),
            "minimum_duration": float(duration_rule.get("最短秒") or 3.0),
            "maximum_duration": float(duration_rule.get("最长秒") or LEGACY_PROFILES[actual_type][1]),
            "classification_evidence": list(product.get("证据") or []),
            "stages": stages,
            "warnings": list(agent.get("warnings") or []),
        },
        "quality": {
            "passed": passed,
            "issues": issues,
            "technical_passed": technical_passed,
            "fallback": fallback,
            "missing_stage_evidence": missing_roles,
            "missing_content_evidence": missing_content,
        },
        "legacy_report": raw,
    }


def run_suchen_edit(
    source: Path,
    output_dir: Path,
    legacy_root: Path,
    product_type: ProductType,
    target_duration: float | None,
    progress: Callable[[str], None],
) -> LegacyEditResult:
    base_url = os.getenv("FULIN_SUCHEN_ENGINE_URL", "http://127.0.0.1:5000")
    client = SuchenEngineClient(base_url)
    status, timelines = client.run_current(
        source, output_dir, product_type, target_duration, progress
    )
    run = _latest_legacy_run(
        legacy_root / "便携数据" / "runtime" / "memory" / "suchen_memory.sqlite3",
        source.name,
        output_dir,
    )
    raw_report = _load_legacy_report(run)
    _, _, default_target = LEGACY_PROFILES[product_type]
    normalized = normalize_legacy_report(raw_report, product_type, target_duration or default_target)
    output = _find_output(output_dir, source, status, timelines)
    result_item = next(
        (item for item in status.get("results") or [] if isinstance(item, dict) and item.get("name") == source.name),
        {},
    )
    passed = bool(normalized["quality"]["passed"])
    if output is None:
        final_status: Literal["approved", "needs_review", "failed"] = "failed"
    elif passed and result_item.get("status", "成功") == "成功":
        final_status = "approved"
    else:
        final_status = "needs_review"
    normalized["status"] = final_status
    report_path = output_dir / f"{source.stem}_suchen_report.json"
    report_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    actual_type = str((normalized.get("plan") or {}).get("product_type") or product_type)
    chosen_target = float((normalized.get("plan") or {}).get("target_duration") or default_target)
    error = None if final_status != "failed" else str(result_item.get("error") or "SOCHEN 未生成可用成片")
    return LegacyEditResult(
        output=output,
        report=report_path,
        status=final_status,
        product_type=actual_type,
        target_duration=chosen_target,
        engine_job_id=str(run.get("run_uuid")) if run and run.get("run_uuid") else None,
        payload={"engine": "suchen-1.2.3-original", "editing_method_preserved": True, "legacy_status": status},
        error=error,
    )
