from dataclasses import replace

from fulin_editor.planner import EditPlan, Stage, Sentence, evidence_failures, SIZE_RE
from fulin_editor.quality import _temporal_flash_kind
from fulin_editor.planner import build_plan, validate_plan
from fulin_editor.renderer import FrameSpec
from fulin_editor.vision import FrameObservation, VisionAnalysis
import pytest


class Vision:
    def interval_evidence(self, start, end, stage):
        return dict(full_body=.9, front=.9, stability=.9, detected_ratio=1)


def fixture_plan():
    return EditPlan((Stage('opening', '', 0, 1, '', .9),
                     Stage('size', '', 1, 5, '', .9),
                     Stage('detail', '', 5, 12, '', .9),
                     Stage('back_return', '', 12, 20, '', .9)), 20)


def test_contiguous_opening_does_not_split_speech():
    sentences = [Sentence(0, 5, '均码'), Sentence(5, 12, '纯棉面料'), Sentence(12, 20, '后背转回')]
    assert evidence_failures(fixture_plan(), sentences, Vision()) == []


def test_missing_fabric_and_color_are_reported():
    sentences = [Sentence(0, 5, '均码'), Sentence(5, 12, '版型很好'), Sentence(21, 24, '蓝色纯棉')]
    failures = evidence_failures(fixture_plan(), sentences, Vision())
    assert '缺少完整面料讲解' in failures
    assert any('蓝色' in f for f in failures)


def test_cut_inside_sentence_is_blocked():
    plan = fixture_plan()
    plan = replace(plan, stages=(*plan.stages[:2], replace(plan.stages[2], start=6), plan.stages[3]))
    assert any('切断' in f for f in evidence_failures(plan, [Sentence(0, 5, '均码'), Sentence(5, 12, '面料')], Vision()))


def test_generic_suitable_word_is_not_size():
    assert not SIZE_RE.search('适合通勤')
    assert SIZE_RE.search('均码')


def test_same_shot_gesture_is_not_whole_frame_flash():
    import numpy as np
    anchor = np.full((128, 96), 80, dtype=np.uint8)
    gesture = anchor.copy()
    gesture[20:110, 25:70] = 220
    assert _temporal_flash_kind(anchor, gesture, anchor, outgoing_difference=40, incoming_difference=40) == 'same_shot_pose_jump'
    flash = np.full_like(anchor, 250)
    assert _temporal_flash_kind(anchor, flash, anchor, outgoing_difference=170, incoming_difference=170) == 'whole_frame_flash'


def test_joint_search_keeps_ending_instead_of_late_detail():
    sentences = [Sentence(0, 6, '均码一百斤'), Sentence(6, 14, '纯棉面料'),
                 Sentence(14, 22, '转身后背转回正面'),
                 Sentence(30, 44, '纯棉面料材质成分弹力垂感面料')]
    plan = build_plan(sentences, vision=Vision())
    assert plan.stages[2].end <= 14
    assert plan.stages[-1].end == 22
    assert 15 <= plan.duration <= 30


def test_complete_long_size_speech_is_not_truncated():
    sentences = [Sentence(0, 10, '均码一百斤'), Sentence(10, 18, '纯棉面料'),
                 Sentence(18, 25, '转身后背转回正面')]
    plan = build_plan(sentences, vision=Vision())
    assert plan.stages[1].end == 10


def test_action_sequence_requires_retreat_before_back_and_front_after_back():
    def frame(t, height, back=0):
        return FrameObservation(time=t, detected=True, bbox_height=height,
                                center_x=.5, full_body=.9, back_likelihood=back)
    frames = (frame(3, .58), frame(4, .58), frame(6, .72), frame(6.5, .74),
              frame(9, .60), frame(9.5, .59), frame(12, .7), frame(12.5, .7),
              frame(15, .58, .6), frame(15.5, .58, .65),
              frame(17, .58, .05), frame(17.5, .58, .02))
    vision = VisionAnalysis('', '', 2, 0, False, frames)
    assert vision.action_sequence_evidence(4, 5, 10, 14, 18)['complete']
    assert not vision.action_sequence_evidence(4, 5, 8, 14, 18)['complete']
    assert not vision.action_sequence_evidence(4, 5, 10, 16, 18)['complete']


@pytest.mark.parametrize('bad_time', [float('nan'), float('inf'), -1])
def test_invalid_timestamps_rejected(bad_time):
    plan = fixture_plan()
    with pytest.raises(ValueError):
        validate_plan(replace(plan, stages=(replace(plan.stages[0], start=bad_time), *plan.stages[1:])))


def test_uncalibrated_y_does_not_move_or_stretch_subject():
    frame = FrameSpec()
    assert frame.jianying_position_y_px == 0
    assert frame.jianying_y_requested == 500
    assert frame.jianying_y_status == 'pending_calibration'


@pytest.mark.parametrize('metric,value,code', [
    ('longest_near_freeze_frames', 48, 'possible_freeze'),
    ('overexposed_frames', 5, 'overexposure'),
    ('decoded_frames', 200, 'decode_failure'),
])
def test_visual_risks_require_review(tmp_path, monkeypatch, metric, value, code):
    from fulin_editor import quality
    output = tmp_path / 'test.mp4'
    output.write_bytes(b'fixture')
    monkeypatch.setattr(quality, 'probe_video', lambda *a: dict(duration=20, fps=30, width=1080, height=1440, audio_codec='aac'))
    metrics = dict(decoded_frames=600, black_frames=0, single_frame_flashes=0,
                   longest_near_freeze_frames=0, overexposed_frames=0)
    metrics[metric] = value
    monkeypatch.setattr(quality, '_visual_integrity', lambda *a: metrics)
    report = quality.inspect_output(output, fixture_plan())
    assert not report.passed
    assert any(issue.code == code for issue in report.issues)
