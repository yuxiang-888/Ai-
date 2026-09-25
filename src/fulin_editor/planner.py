from __future__ import annotations

import json
import math
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
    r"均码|尺码|码数|体重|穿到|加\s*10\s*斤|\d{2,3}\s*斤|(?:^|[^A-Za-z])[SMLX]{1,3}(?:号|码|\d|[^A-Za-z]|$)",
    re.IGNORECASE,
)
DETAIL_RE = re.compile(
    r"品牌|面料|材质|弹力|微弹|颜色|色调|版型|衣型|裤型|裙型|扣子|拉链|口袋|里衬|底衬|工艺|成分|长度|厚度|光泽|垂感|纹理|线条|保暖|蓬松|走线"
)
BACK_RE = re.compile(r"后面|后背|背面|背后的|转身|转到后")
FABRIC_RE = re.compile(r"面料|材质|成分|纯棉|棉质|羊毛|羊绒|真丝|涤纶|聚酯|粘纤|氨纶|亚麻|牛仔|雪纺|针织|双面呢|羽绒|弹力|垂感")
COLOR_RE = re.compile(r"(?:黑|白|灰|红|蓝|绿|黄|紫|粉|米白|卡其|咖啡|藏青|酒红|杏|驼)色")
WASTE_RE = re.compile(r"找链接|等一下|稍等|上链接|点关注|扣个一")
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
    return len(DETAIL_RE.findall(text)) * 2.5 + len(FABRIC_RE.findall(text)) * 5.0


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
    ranked: bool = False,
    rank_limit: int = 64,
) -> tuple[int, int, float, dict[str, float]] | list[tuple[int, int, float, dict[str, float]]]:
    best: tuple[float, int, int, float, dict[str, float]] | None = None
    candidates = []
    for start_index, first in enumerate(sentences):
        if first.start + 0.05 < start_after:
            continue
        if stage == "size" and scorer(first.text) <= 0:
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
                - len(WASTE_RE.findall(text)) * 8.0
                - (len(selected) - len({re.sub(r'\W', '', s.text) for s in selected})) * 6.0
            )
            candidate = (score, start_index, end_index, duration, visual)
            candidates.append(candidate)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        raise ValueError(
            f"找不到满足时长 {minimum:.1f}～{maximum:.1f} 秒且位于 {start_after:.2f} 秒之后的候选片段"
        )
    if ranked:
        return [(c[1], c[2], c[0], c[4]) for c in sorted(candidates, key=lambda c: -c[0])[:rank_limit]]
    return best[1], best[2], best[0], best[4]


def _joint_windows(sentences, policy, target, vision):
    """Bounded search; do not let a late high-scoring detail consume the ending."""
    common = dict(sentences=sentences, start_after=0, vision=vision, ranked=True)
    sizes = _best_window(**common, scorer=_size_score, minimum=policy.size_minimum,
                         maximum=policy.size_maximum, target=6, required=SIZE_RE, stage="size")
    details = _best_window(**common, scorer=_detail_score, minimum=policy.detail_minimum,
                           maximum=policy.detail_maximum, target=8, required=FABRIC_RE, stage="detail",
                           rank_limit=256)
    backs = _best_window(**common, scorer=_back_score, minimum=policy.back_minimum,
                         maximum=policy.back_maximum, target=8, required=BACK_RE,
                         preferred=RETURN_RE, stage="back_return")
    best = None
    action_check = getattr(vision, "action_sequence_evidence", None)
    for size in sizes:
        for detail in details:
            if sentences[detail[0]].start < sentences[size[1]].end:
                continue
            for back in backs:
                if sentences[back[0]].start < sentences[detail[1]].end:
                    continue
                if policy.key == "single" and action_check is not None:
                    motion = action_check(
                        sentences[size[1]].end,
                        sentences[detail[0]].start, sentences[detail[1]].end,
                        sentences[back[0]].start, sentences[back[1]].end,
                    )
                    if not motion.get("complete", False):
                        continue
                duration = sum(sentences[c[1]].end - sentences[c[0]].start for c in (size, detail, back))
                if not policy.minimum_duration <= duration <= policy.maximum_duration:
                    continue
                score = sum(c[2] for c in (size, detail, back)) - abs(duration - target)
                if best is None or score > best[0]:
                    best = (score, (size, detail, back))
    if best is None:
        raise ValueError("候选片段无法同时满足尺码、面料、背面顺序和品类时长；需要人工复核")
    return best[1]


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
        evidence=(f"asr_sentences={start_index}:{end_index}",
                  f"source_interval={selected[0].start:.3f}:{selected[-1].end:.3f}") + tuple(
            f"{key}={float(value):.2f}"
            for key, value in visual.items()
            if key not in {"center_x", "center_y", "scale"}
        ),
    )


def _split_single_action_stages(
    sentences: list[Sentence], detail: Stage, back: Stage,
    action: dict[str, Any], vision: Any,
) -> tuple[Stage, Stage, Stage, Stage, Stage]:
    """Keep short evidence-backed speech around each action, not the whole close-up."""
    detail_lines = [s for s in sentences if detail.start <= s.start and s.end <= detail.end]
    back_lines = [s for s in sentences if back.start <= s.start and s.end <= back.end]
    approach_time = float(action["approach_time"])
    retreat_time = float(action["retreat_time"])
    back_time = float(action["back_time"])
    front_time = float(action["front_time"])
    approach_end = next((s.end for s in detail_lines if s.end >= approach_time + .4), None)
    retreat_start = next((s.start for s in detail_lines if s.start <= retreat_time < s.end), None)
    if approach_end is None or retreat_start is None or approach_end >= retreat_start:
        raise ValueError("无法在完整口播边界拆分走近和退回动作；需要人工复核")
    pitch_options = []
    for i, first in enumerate(detail_lines):
        if first.start < approach_end or first.start >= retreat_start:
            continue
        for last in detail_lines[i:]:
            if last.end > retreat_start:
                break
            duration = last.end - first.start
            if duration > 5.0:
                break
            text = _window_text(s for s in detail_lines if first.start <= s.start and s.end <= last.end)
            if 3.0 <= duration <= 5.0 and FABRIC_RE.search(text):
                coverage = vision.interval_evidence(first.start, last.end, "detail").get(
                    "detected_ratio", 0.0
                )
                pitch_options.append((len(FABRIC_RE.findall(text)) * 5 + len(DETAIL_RE.findall(text)) * 2
                                      + coverage * 20 - abs(duration - 4), first.start, last.end))
    if not pitch_options:
        raise ValueError("找不到 3–5 秒且包含完整面料口播的近景卖点；需要人工复核")
    _, pitch_start, pitch_end = max(pitch_options)
    back_split = next((s.end for s in reversed(back_lines) if s.end >= back_time and
                       s.end <= front_time and s.end > back.start), None)
    if back_split is None:
        raise ValueError("背面与转正无法在完整口播边界拆分；需要人工复核")
    # The actual front must remain in the second piece, not just at its cut point.
    if front_time >= back.end or back.end - back_split < .5:
        raise ValueError("缺少原地转回正面的完整收尾；需要人工复核")
    spans = (
        ("approach", "走近镜头", detail.start, approach_end),
        ("pitch", "近景卖点", pitch_start, pitch_end),
        ("retreat", "退回原位", retreat_start, detail.end),
        ("back", "转身展示背面", back.start, back_split),
        ("return_front", "原地转回正面", back_split, back.end),
    )
    result = []
    for name, label, start, end in spans:
        if end <= start:
            raise ValueError(f"{label}没有有效时长；需要人工复核")
        selected = [s for s in sentences if start <= s.start and s.end <= end]
        visual = vision.interval_evidence(start, end, name)
        result.append(Stage(name, label, start, end, _window_text(selected), detail.confidence,
                            visual.get("center_x", .5), visual.get("center_y", .5),
                            visual.get("scale", 1.0), visual.get("detected_ratio", 0.0),
                            (f"source_interval={start:.3f}:{end:.3f}",)))
    return tuple(result)


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
    joint_size, joint_detail, joint_back = _joint_windows(sentences, policy, effective_target, vision)
    size_i, size_j, size_score, size_visual = joint_size
    full_size_stage = _make_stage(
        "size", "尺码介绍", sentences, size_i, size_j, size_score, size_visual
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

    detail_i, detail_j, detail_score, detail_visual = joint_detail
    detail_stage = _make_stage(
        "detail", "商品与细节", sentences, detail_i, detail_j, detail_score, detail_visual
    )

    back_i, back_j, back_score, back_visual = joint_back
    back_stage = _make_stage(
        "back_return", "背面展示与转回", sentences, back_i, back_j, back_score, back_visual
    )

    action_check = getattr(vision, "action_sequence_evidence", None)
    action = (action_check(size_stage.end, detail_stage.start, detail_stage.end,
                           back_stage.start, back_stage.end) if action_check else {})
    if action.get("complete"):
        detail_stage = Stage(**{**asdict(detail_stage), "evidence": detail_stage.evidence + (
            f"approach_time={action['approach_time']:.3f}",
            f"retreat_time={action['retreat_time']:.3f}",
        )})
        back_stage = Stage(**{**asdict(back_stage), "evidence": back_stage.evidence + (
            f"back_time={action['back_time']:.3f}",
            f"front_time={action['front_time']:.3f}",
        )})
    if not action.get("complete") and not RETURN_RE.search(back_stage.text) and back_visual.get("turn_cycle", 0.0) < 0.42:
        warnings.append("背面阶段未从口播或姿态中确认完整转回，必须人工复核")
    if vision is None:
        warnings.append("未执行人物姿态分析，构图与背面动作必须人工复核")
    elif min(size_stage.visual_confidence, back_stage.visual_confidence) < 0.55:
        warnings.append("部分阶段人物检测覆盖率不足，必须人工复核构图")
    if vision is not None and detail_stage.visual_confidence < 0.5:
        warnings.append("走近及商品细节阶段人物检测中断，必须人工复核动作连续性")
    if size_visual.get("full_body", 0.0) < 0.62:
        warnings.append("首秒没有高置信全身画面，主图必须人工复核")
    stages: tuple[Stage, ...] = (opening_stage, size_stage, detail_stage, back_stage)
    if policy.key == "single" and action.get("complete") and vision is not None:
        stages = (opening_stage, size_stage) + _split_single_action_stages(
            sentences, detail_stage, back_stage, action, vision
        )
    plan = EditPlan(
        stages=stages,
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
    for stage in plan.stages:
        if not all(math.isfinite(t) for t in (stage.start, stage.end)) or stage.start < 0 or stage.end <= stage.start:
            raise ValueError("阶段时间戳必须有限、非负且结束晚于开始")
    expected = ("opening", "size", "detail", "back_return")
    expanded = ("opening", "size", "approach", "pitch", "retreat", "back", "return_front")
    actual = tuple(stage.name for stage in plan.stages)
    if actual not in (expected, expanded):
        raise ValueError(f"阶段顺序错误：{actual}")
    opening, size = plan.stages[:2]
    if actual == expanded and not 3.0 <= plan.stages[3].duration <= 5.0:
        raise ValueError("近景卖点讲解必须控制在 3–5 秒")
    if not 0.5 <= opening.duration <= 1.05:
        raise ValueError(f"首秒全身阶段时长不合格：{opening.duration:.2f} 秒")
    if opening.duration + size.duration <= 0:
        raise ValueError("尺码阶段没有有效时长")
    if not plan.minimum_duration <= plan.duration <= plan.maximum_duration:
        raise ValueError(
            f"{plan.product_label} 成片时长 {plan.duration:.2f} 秒，不在 "
            f"{plan.minimum_duration:.0f}～{plan.maximum_duration:.0f} 秒内"
        )
    for left, right in zip(plan.stages, plan.stages[1:]):
        if right.start < left.end - 0.001:
            raise ValueError(f"源时间轴重叠：{left.name} 与 {right.name}")


def evidence_failures(plan: EditPlan, sentences: list[Sentence], vision: Any) -> list[str]:
    """Check source evidence before render; contiguous stages are one speech interval."""
    failures = list(plan.warnings)
    intervals: list[list[float]] = []
    for stage in plan.stages:
        if intervals and abs(intervals[-1][1] - stage.start) < 0.001:
            intervals[-1][1] = stage.end
        else:
            intervals.append([stage.start, stage.end])
    kept = [s for s in sentences if any(a <= s.start + .001 and b >= s.end - .001 for a, b in intervals)]
    for a, b in intervals:
        if any(s.start + .001 < boundary < s.end - .001 for s in sentences for boundary in (a, b)):
            failures.append(f"口播句子被切断：{a:.3f}–{b:.3f}")
    text = _window_text(kept)
    if not SIZE_RE.search(text):
        failures.append("缺少完整尺码或均码讲解")
    if not FABRIC_RE.search(text):
        failures.append("缺少完整面料讲解")
    missing = set(COLOR_RE.findall(_window_text(sentences))) - set(COLOR_RE.findall(text))
    if missing:
        failures.append("未覆盖原片口播颜色：" + "、".join(sorted(missing)))
    if vision is None:
        failures.append("缺少人物动作证据")
    else:
        opening = plan.stages[0]
        evidence = vision.interval_evidence(opening.start, opening.end, "opening")
        if any(evidence.get(key, 0) < limit for key, limit in (("full_body", .62), ("front", .65), ("stability", .6), ("detected_ratio", .55))):
            failures.append("实际首秒未确认稳定全身正面")
        action_check = getattr(vision, "action_sequence_evidence", None)
        if plan.product_type == "single" and action_check is not None:
            stages = {stage.name: stage for stage in plan.stages}
            if "approach" in stages:
                size, approach, retreat, back, front = (
                    stages[key] for key in ("size", "approach", "retreat", "back", "return_front")
                )
                action = action_check(size.end, approach.start, retreat.end, back.start, front.end)
                if action.get("complete") and not (
                    approach.start <= action["approach_time"] <= approach.end
                    and retreat.start <= action["retreat_time"] <= retreat.end
                    and back.start <= action["back_time"] <= back.end
                    and front.start <= action["front_time"] <= front.end
                ):
                    action = {"complete": False}
            else:
                size, detail, back = plan.stages[1:]
                action = action_check(size.end, detail.start, detail.end, back.start, back.end)
            if not action.get("complete", False):
                failures.append("未确认走近、退回原位、背面、转回正面的完整顺序")
    return list(dict.fromkeys(failures))
