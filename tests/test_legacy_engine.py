from __future__ import annotations

import unittest

from fulin_editor.legacy_engine import normalize_legacy_report


REQUIRED_ROLES = (
    "opening_full_front",
    "origin_size_intro",
    "move_to_center_product_detail",
    "back_full_body_show",
    "return_origin_front_finish",
)


class LegacyReportEvidenceTests(unittest.TestCase):
    def test_contiguous_fallback_cannot_be_approved(self) -> None:
        raw = {
            "制作模式": "硬剪辑兜底",
            "Agent决策": {
                "engine": "contiguous_hard_cut",
                "stage_coverage": {},
                "content_coverage": {},
            },
            "商品类型": {"识别": "single", "名称": "单品"},
            "成片时长标准": {"最短秒": 3, "最长秒": 30, "目标秒": 25},
            "成片总时长": 30,
            "成片片段": [{"开始": 10, "结束": 40, "时长": 30}],
            "质量门": {"通过": True, "驳回项": 0, "动作引擎": "contiguous_hard_cut"},
        }

        normalized = normalize_legacy_report(raw, "auto", 25)

        self.assertEqual(normalized["status"], "needs_review")
        self.assertFalse(normalized["quality"]["passed"])
        self.assertTrue(normalized["quality"]["technical_passed"])
        self.assertTrue(normalized["quality"]["fallback"])

    def test_real_stage_and_content_evidence_can_be_approved(self) -> None:
        raw = {
            "制作模式": "智能动作剪辑",
            "Agent决策": {
                "engine": "mediapipe_asr_timeline",
                "stage_coverage": {
                    role: {"covered": True, "start": index, "end": index + 1}
                    for index, role in enumerate(REQUIRED_ROLES)
                },
                "content_coverage": {
                    "content_size": {"covered": True, "source": "asr"},
                    "content_fabric": {"covered": True, "source": "asr"},
                },
            },
            "商品类型": {"识别": "single", "名称": "单品"},
            "成片时长标准": {"最短秒": 3, "最长秒": 30, "目标秒": 25},
            "成片总时长": 26,
            "成片片段": [{"开始": 0, "结束": 26, "时长": 26}],
            "质量门": {"通过": True, "驳回项": 0, "动作引擎": "mediapipe"},
        }

        normalized = normalize_legacy_report(raw, "auto", 25)

        self.assertEqual(normalized["status"], "approved")
        self.assertTrue(normalized["quality"]["passed"])


if __name__ == "__main__":
    unittest.main()
