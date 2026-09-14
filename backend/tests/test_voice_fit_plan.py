import pytest
from test_voiceover import document

from app.services.voiceover.fit_plan import plan_source_fit
from app.services.voiceover.models import VoiceSync


def inputs():
    clip = document().clips[0]
    clip.duration_ms = 1800
    clip.asset_id = 'a' * 64
    clip.sync = VoiceSync(timing_origin='automatic', timing_locked=False)
    return clip, {'speech_verified': True, 'automatic_trim_allowed': True,
        'trim_start_ms': 220, 'trim_end_ms': 180, 'speech_evidence': {'start_ms': 300, 'end_ms': 1500}}


def test_places_spoken_onset_separately_from_file_and_keeps_utterance_rate():
    clip, audio = inputs()
    result = plan_source_fit(clip, 2000, 3500, video_duration_ms=5000, next_file_start_ms=3700, audio=audio)
    assert result['state'] == 'candidate' and result['suggested_rate'] == 1
    assert result['predicted_file_start_ms'] == 1920
    assert result['predicted_speech_start_ms'] == 2000
    assert result['predicted_speech_end_ms'] == 3200
    assert result['borrowed_ms'] == 0
    clip.offset_ms = result['suggested_offset_ms']
    assert plan_source_fit(clip, 2000, 3500, video_duration_ms=5000, next_file_start_ms=3700, audio=audio) == result


def test_borrow_and_tempo_are_joint_choice_and_next_clip_stays_fixed():
    clip, audio = inputs()
    result = plan_source_fit(clip, 2000, 3100, video_duration_ms=5000,
        next_file_start_ms=3500, audio=audio, verified_tail_ms=250)
    assert result['suggested_rate'] == 1 and result['borrowed_ms'] == 100
    constrained = plan_source_fit(clip, 2000, 3100, video_duration_ms=5000,
        next_file_start_ms=3270, audio=audio, verified_tail_ms=250)
    assert 1 < constrained['suggested_rate'] <= 1.15
    assert constrained['predicted_file_end_ms'] <= 3271


def test_weak_evidence_locks_impossible_duration_and_video_start_are_blocked():
    clip, audio = inputs()
    assert 'dubbed_transcript_unverified' in plan_source_fit(clip, 2000, 3500,
        video_duration_ms=5000)['blocked_reasons']
    clip.sync.timing_locked = True
    assert 'timing_locked' in plan_source_fit(clip, 2000, 3500,
        video_duration_ms=5000, audio=audio)['blocked_reasons']
    clip.sync.timing_locked = False
    assert 'duration_not_feasible' in plan_source_fit(clip, 2000, 2500,
        video_duration_ms=5000, audio=audio)['blocked_reasons']
    assert 'outside_video' in plan_source_fit(clip, 0, 1500,
        video_duration_ms=5000, audio=audio)['blocked_reasons']
    assert 'overlap' in plan_source_fit(clip, 2000, 3500,
        video_duration_ms=5000, previous_file_end_ms=2000, audio=audio)['blocked_reasons']
    with pytest.raises(ValueError):
        plan_source_fit(clip, 4000, 5500, video_duration_ms=5000, audio=audio)
