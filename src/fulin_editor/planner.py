from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .policies import ProductType, RequestedProductType, resolve_policy


@dataclass(frozen=True)
class Sentence:
    start: float
    end: float
    text: str
    confidence: float = 1.0


@dataclass(frozen=True)
class Stage:
    name: str
    label: str
    start: float
    end: float
    text: str
    confidence: float
    center_x: float = 0.5
    center_y: float = 0.5
    scale: float = 1.0
    visual_confidence: float = 0.0
    evidence: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["duration"] = self.duration
        return value


@dataclass(frozen=True)
class EditPlan:
    stages: tuple[Stage, ...]
    target_duration: float
    product_type: ProductType = "single"
    product_label: str = "单品及小件"
    minimum_duration: float = 15.0
    maximum_duration: float = 30.0
    classification_evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return round(sum(stage.duration for stage in self.stages), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_duration": self.target_duration,
            "actual_duration": self.duration,
            "product_type": self.product_type,
            "product_label": self.product_label,
            "minimum_duration": self.minimum_duration,
            "maximum_duration": self.maximum_duration,
            "classification_evidence": list(self.classification_evidence),
            "stages": [stage.to_dict() for stage in self.stages],
            "warnings": list(self.warnings),
        }


SIZE_RE = re.compile(
    r"尺码|码数|体重|穿到|适合|加\s*10\s*斤|\d{2,3}\s*斤|(?:^|[^A-Za-z])[SMLX]{1,3}(?:号|码|\d|[^A-Za-z]|$)",
    re.IGNORECASE,
)
DETAIL_RE = re.compile(
    r"品牌|面料|材质|弹力|微弹|颜色|色调|版型|衣型|裤型|裙型|扣子|拉链|口袋|里衬|底衬|工艺|成分|长度|厚度|光泽|垂感|纹理|线条|保暖|蓬松|走线"
)
BACK_RE = re.compile(r"后面|后背|背面|背后的|转身|转到后")
RETURN_RE = re.compile(r"转回来|转回|回正|正面|转过来|一圈过来|回到原位|收尾")


def load_sentences(path: str | Path) -> list[Sentence]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    raw_sentences = payload.get("sentences", payload)
    if not isinstance(raw_sentences, list):
        raise ValueError("转写文件缺少 sentences 数组")
    sentences: list[Sentence] = []
    for item in raw_sentences:
        start = round(float(item["start"]), 3)
        end = round(float(item["end"]), 3)
        text = str(item.get("text", "")).strip()
        confidence = float(item.get("confidence", 1.0))
        if end > start and text:
            sentences.append(Sentence(start=start, end=end, text=text, confidence=confidence))
    if not sentences:
        raise ValueError("转写文件没有可用句子")
    sentences.sort(key=lambda sentence: sentence.start)
    return sentences


def _size_score(text: str) -> float:
    score = len(SIZE_RE.findall(text)) * 3.0
    if re.search(r"\d{2,3}\s*斤", text):
        score += 5.0
    if re.search(r"[SMLX]{1,3}", text, re.IGNORECASE):
        score += 3.0
    return score


def _detail_score(text: str) -> float:
    return len(DETAIL_RE.findall(text)) * 2.5


def _back_score(text: str) -> float:
    return len(BACK_RE.findall(text)) * 5.0 + len(RETURN_RE.findall(text)) * 3.0


def _window_text(sentences: Iterable[Sentence]) -> str:
    return "".join(sentence.text for sentence in sentences)


def _best_window(
    sentences: list[Sentence],
    *,
    scorer: Callable[[str], float],
    start_after: float,
    minimum: float,
    maximum: float,
    target: float,
    required: re.Pattern[str] | None = None,
    preferred: re.Pattern[str] | None = None,
    stage: str,
    vision: Any | None = None,
) -> tuple[int, int, float, dict[str, float]]:
    best: tuple[float, int, int, float, dict[str, float]] | None = None
    for start_index, first in enumerate(sentences):
        if first.start + 0.05 < start_after:
            continue
        if stage != "back_return" and scorer(first.text) <= 0:
            continue
        for end_index in range(start_index, len(sentences)):
            selected = sentences[start_index : end_index + 1]
            duration = selected[-1].end - selected[0].start
            if duration > maximum + 0.001:
                break
            if duration < minimum:
                continue
            text = _window_text(selected)
            semantic = sum(scorer(sentence.text) for sentence in selected)
            visual = (
                vision.interval_evidence(selected[0].start, selected[-1].end, stage)
                if vision is not None
                else {}
            )
            has_required_text = required is None or bool(required.search(text))
            visual_substitute = stage == "back_return" and (
                visual.get("back", 0.0) >= 0.32 or visual.get("turn_cycle", 0.0) >= 0.42
            )
            if not has_required_text and not visual_substitute:
                continue
            if semantic <= 0 and not visual_substitute:
                continue
            preferred_bonus = 4.0 if preferred and preferred.search(text) else 0.0
            gap = sum(
                max(0.0, selected[index + 1].start - selected[index].end)
                for index in range(len(selected) - 1)
            )
            visual_score = 0.0
            if stage == "size":
                visual_score = (
                    visual.get("full_body", 0.0) * 4.0
                    + visual.get("stability", 0.0) * 2.0
                    + visual.get("sharpness", 0.0)
                )
            elif stage == "detail":
                visual_score = (
                    visual.get("detail", 0.0) * 5.0
                    + visual.get("sharpness", 0.0) * 2.0
                    + visual.get("stability", 0.0)
                )
            else:
                visual_score = (
                    visual.get("back", 0.0) * 5.0
                    + visual.get("turn_cycle", 0.0) * 8.0
                    + visual.get("stability", 0.0)
                )
            speech_confidence = sum(sentence.confidence for sentence in selected) / len(selected)
            score = (
                semantic
                + preferred_bonus
                + visual_score
                + speech_confidence
                - abs(duration - target) * 0.9
                - gap * 0.35
            )
            candidate = (score, start_index, end_index, duration, visual)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        raise ValueError(
            f"找不到满足时长 {minimum:.1f}～{maximum:.1f} 秒且位于 {start_after:.2f} 秒之后的候选片段"
        )
    return best[1], best[2], best[0], best[4]


def _make_stage(
    name: str,
    label: str,
    sentences: list[Sentence],
    start_index: int,
    end_index: int,
    score: float,
    visual: dict[str, float],
) -> Stage:
    selected = sentences[start_index : end_index + 1]
    confidence = max(0.35, min(0.99, 0.55 + score / 50.0))
    return Stage(
        name=name,
        label=label,
        start=selected[0].start,
        end=selected[-1].end,
        text=_window_text(selected),
        confidence=round(confidence, 3),
        center_x=float(visual.get("center_x", 0.5)),
        center_y=float(visual.get("center_y", 0.5)),
        scale=float(visual.get("scale", 1.0)),
        visual_confidence=round(float(visual.get("detected_ratio", 0.0)), 3),
        evidence=tuple(
            f"{key}={float(value):.2f}"
            for key, value in visual.items()
            if key not in {"center_x", "center_y", "scale"}
        ),
    )


def build_plan(
    sentences: list[Sentence],
    target_duration: float | None = None,
    *,
    vision: Any | None = None,
    product_type: RequestedProductType = "auto",
) -> EditPlan:
    all_text = _window_text(sentences)
    policy, classification_evidence = resolve_policy(all_text, product_type)
    effective_target = float(target_duration or policy.target_duration)
    effective_target = min(policy.maximum_duration, max(policy.minimum_duration, effective_target))
    warnings: list[str] = []
    missing_size = False
    try:
        size_i, size_j, size_score, size_visual = _best_window(
            sentences,
            scorer=_size_score,
            start_after=0.0,
            minimum=policy.size_minimum,
            maximum=policy.size_maximum,
            target=(policy.size_minimum + policy.size_maximum) / 2.0,
            required=SIZE_RE,
            stage="size",
            vision=vision,
        )
    except ValueError:
        missing_size = True
        size_i, size_j, size_score, size_visual = _best_window(
            sentences,
            scorer=lambda _text: 1.0,
            start_after=0.0,
            minimum=policy.size_minimum,
            maximum=policy.size_maximum,
            target=policy.size_minimum,
            stage="size",
            vision=vision,
        )
        warnings.append("没有识别到可靠尺码口播，已用原位全身开场替代；不得自动发布")
    full_size_stage = _make_stage(
        "size", "尺码介绍" if not missing_size else "原位全身起势", sentences, size_i, size_j, size_score, size_visual
    )
    opening_duration = min(1.0, max(0.5, full_size_stage.duration / 3.0))
    opening_stage = Stage(
        name="opening",
        label="首秒全身正面",
        start=full_size_stage.start,
        end=round(full_size_stage.start + opening_duration, 3),
        text=full_size_stage.text,
        confidence=full_size_stage.confidence,
        center_x=full_size_stage.center_x,
        center_y=full_size_stage.center_y,
        scale=full_size_stage.scale,
        visual_confidence=full_size_stage.visual_confidence,
        evidence=full_size_stage.evidence,
    )
    size_stage = Stage(
        name="size",
        label=full_size_stage.label,
        start=opening_stage.end,
        end=full_size_stage.end,
        text=full_size_stage.text,
        confidence=full_size_stage.confidence,
        center_x=full_size_stage.center_x,
        center_y=full_size_stage.center_y,
        scale=full_size_stage.scale,
        visual_confidence=full_size_stage.visual_confidence,
        evidence=full_size_stage.evidence,
    )

    detail_i, detail_j, detail_score, detail_visual = _best_window(
        sentences,
        scorer=_detail_score,
        start_after=full_size_stage.end - 0.001,
        minimum=policy.detail_minimum,
        maximum=policy.detail_maximum,
        target=min(policy.detail_maximum, max(policy.detail_minimum, effective_target * 0.58)),
        required=DETAIL_RE,
        stage="detail",
        vision=vision,
    )
    detail_stage = _make_stage(
        "detail", "商品与细节", sentences, detail_i, detail_j, detail_score, detail_visual
    )

    remaining_capacity = policy.maximum_duration - full_size_stage.duration - detail_stage.duration
    remaining_max = min(policy.back_maximum, remaining_capacity)
    if remaining_max < policy.back_minimum:
        raise ValueError(
            f"{policy.label} 的尺码与细节片段占用过长，无法保留完整背面转回动作"
        )
    remaining_target = max(
        policy.back_minimum,
        min(remaining_max, effective_target - full_size_stage.duration - detail_stage.duration),
    )
    back_i, back_j, back_score, back_visual = _best_window(
        sentences,
        scorer=_back_score,
        start_after=detail_stage.end - 0.001,
        minimum=policy.back_minimum,
        maximum=remaining_max,
        target=remaining_target,
        required=BACK_RE,
        preferred=RETURN_RE,
        stage="back_return",
        vision=vision,
    )
    back_stage = _make_stage(
        "back_return", "背面展示与转回", sentences, back_i, back_j, back_score, back_visual
    )

    if not RETURN_RE.search(back_stage.text) and back_visual.get("turn_cycle", 0.0) < 0.42:
        warnings.append("背面阶段未从口播或姿态中确认完整转回，必须人工复核")
    if vision is None:
        warnings.append("未执行人物姿态分析，构图与背面动作必须人工复核")
    elif min(size_stage.visual_confidence, back_stage.visual_confidence) < 0.55:
        warnings.append("部分阶段人物检测覆盖率不足，必须人工复核构图")
    if size_visual.get("full_body", 0.0) < 0.62:
        warnings.append("首秒没有高置信全身画面，主图必须人工复核")
    plan = EditPlan(
        stages=(opening_stage, size_stage, detail_stage, back_stage),
        target_duration=effective_target,
        product_type=policy.key,
        product_label=policy.label,
        minimum_duration=policy.minimum_duration,
        maximum_duration=policy.maximum_duration,
        classification_evidence=classification_evidence,
        warnings=tuple(warnings),
    )
    validate_plan(plan)
    return plan


def validate_plan(plan: EditPlan) -> None:
    expected = ("opening", "size", "detail", "back_return")
    actual = tuple(stage.name for stage in plan.stages)
    if actual != expected:
        raise ValueError(f"阶段顺序错误：{actual}")
    opening, size, _detail, _back = plan.stages
    if not 0.5 <= opening.duration <= 1.05:
        raise ValueError(f"首秒全身阶段时长不合格：{opening.duration:.2f} 秒")
    if plan.product_type == "single" and not 5.0 <= opening.duration + size.duration <= 8.0:
        raise ValueError(f"尺码阶段总时长不合格：{opening.duration + size.duration:.2f} 秒")
    if not plan.minimum_duration <= plan.duration <= plan.maximum_duration:
        raise ValueError(
            f"{plan.product_label} 成片时长 {plan.duration:.2f} 秒，不在 "
            f"{plan.minimum_duration:.0f}～{plan.maximum_duration:.0f} 秒内"
        )
    for left, right in zip(plan.stages, plan.stages[1:]):
        if right.start < left.end - 0.001:
            raise ValueError(f"源时间轴重叠：{left.name} 与 {right.name}")
