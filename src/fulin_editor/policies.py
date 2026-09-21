from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal


ProductType = Literal["single", "set", "bulky"]
RequestedProductType = Literal["auto", "single", "set", "bulky"]


@dataclass(frozen=True)
class ProductPolicy:
    key: ProductType
    label: str
    minimum_duration: float
    target_duration: float
    maximum_duration: float
    size_minimum: float
    size_maximum: float
    detail_minimum: float
    detail_maximum: float
    back_minimum: float
    back_maximum: float
    required_content: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


POLICIES: dict[ProductType, ProductPolicy] = {
    "single": ProductPolicy(
        key="single",
        label="单品及小件",
        minimum_duration=15.0,
        target_duration=27.0,
        maximum_duration=30.0,
        size_minimum=5.0,
        size_maximum=8.0,
        detail_minimum=5.0,
        detail_maximum=10.0,
        back_minimum=5.0,
        back_maximum=15.0,
        required_content=("品牌", "版型", "尺码推荐", "面料细节", "工艺", "转身后背"),
    ),
    "set": ProductPolicy(
        key="set",
        label="上下套装及两件套",
        minimum_duration=30.0,
        target_duration=55.0,
        maximum_duration=60.0,
        size_minimum=6.0,
        size_maximum=12.0,
        detail_minimum=14.0,
        detail_maximum=34.0,
        back_minimum=8.0,
        back_maximum=20.0,
        required_content=("品牌", "上下装版型", "尺码推荐", "两件面料", "工艺", "转身后背"),
    ),
    "bulky": ProductPolicy(
        key="bulky",
        label="皮草 / 羽绒 / 派克 / 双面呢",
        minimum_duration=80.0,
        target_duration=115.0,
        maximum_duration=120.0,
        size_minimum=8.0,
        size_maximum=18.0,
        detail_minimum=48.0,
        detail_maximum=88.0,
        back_minimum=16.0,
        back_maximum=32.0,
        required_content=("品牌", "保暖版型", "尺码推荐", "面料成分", "工艺结构", "颜色", "转身后背"),
    ),
}

SET_RE = re.compile(r"套装|两件套|2\s*件套|二件套|上衣.{0,5}(?:裤|裙)|上下装")
BULKY_RE = re.compile(r"皮草|真毛|羽绒|派克|双面呢|羊绒大衣|毛呢大衣")


def resolve_policy(text: str, requested: RequestedProductType = "auto") -> tuple[ProductPolicy, tuple[str, ...]]:
    if requested != "auto":
        return POLICIES[requested], (f"人工指定品类={POLICIES[requested].label}",)
    bulky = BULKY_RE.findall(text)
    if bulky:
        return POLICIES["bulky"], tuple(f"大货关键词={item}" for item in dict.fromkeys(bulky))
    sets = SET_RE.findall(text)
    if sets:
        return POLICIES["set"], tuple(f"套装关键词={item}" for item in dict.fromkeys(sets))
    return POLICIES["single"], ("未命中套装或大货关键词，按单品及小件",)


def policy_catalog() -> list[dict]:
    return [POLICIES[key].to_dict() for key in ("single", "set", "bulky")]
