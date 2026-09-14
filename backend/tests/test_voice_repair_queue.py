from copy import deepcopy

from test_voice_sync_audit import setup

from app.services.voiceover.repair_queue import select_repairs


def test_only_actionable_failures_enter_repair_queue(tmp_path):
    _, doc, subtitles, media = setup(tmp_path)
    doc.clips[0].spoken_text = 'Tôi thật sự không thể trả cô số tiền này.'
    row = {'clip_id': 'one', 'completed': True,
           'source_evidence': {'transcript_complete': True, 'method': 'asr_observed', 'start_ms': 1100, 'end_ms': 1700},
           'proposal': {'state': 'blocked', 'blocked_reasons': ['duration_not_feasible']},
           'dubbed_audio': {'speech_verified': True, 'speech_evidence': {'start_ms': 80, 'end_ms': 1080}}}
    queue, skipped = select_repairs(doc, subtitles, {'rows': [row]}, media)
    assert not skipped and queue[0]['mode'] == 'shorten' and queue[0]['max_syllables'] == 6
    for change in ('confidence', 'locked', 'overlap', 'already_solved', 'missing_source'):
        voice, check = deepcopy(doc), deepcopy(row)
        if change == 'confidence':
            check['dubbed_audio'] = {'speech_verified': False, 'diagnostics': {
                'transcript_matches': True, 'minimum_confidence': .55}}
        elif change == 'locked':
            voice.clips[0].sync.text_locked = True
        elif change == 'overlap':
            voice.clips.append(voice.clips[0].model_copy(deep=True, update={'id': 'next',
                'source_cue_ids': ['next'], 'start_ms': 1500, 'end_ms': 2500}))
        elif change == 'already_solved':
            check['processed'] = {'state': 'ready_for_review'}
        else:
            check.pop('source_evidence')
        queue, skipped = select_repairs(voice, subtitles, {'rows': [check]}, media)
        assert not queue and 'one' in skipped
    row['dubbed_audio'] = {'speech_verified': False, 'diagnostics': {
        'transcript_matches': False, 'minimum_confidence': .91}}
    queue, _ = select_repairs(doc, subtitles, {'rows': [row]}, media)
    assert queue[0]['mode'] == 'retry_original' and queue[0]['spoken_text'] == doc.clips[0].spoken_text
