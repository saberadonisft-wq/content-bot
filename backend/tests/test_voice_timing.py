import json
from pathlib import Path

import pytest

from app.services.voiceover.models import (
    VoiceAlignment,
    VoiceClip,
    VoiceDocument,
    VoiceProfile,
)
from app.services.voiceover.store import VoiceStore, write_json
from app.services.voiceover.timing import (
    clip_signature,
    file_interval,
    refresh_timing,
    window_issues,
)

FIXTURES = json.loads((Path(__file__).parents[2] / 'test-fixtures/voiceover-timing.json').read_text())


def document(clips):
    return VoiceDocument(project_id='a' * 20, video_fingerprint='video',
                         profile=VoiceProfile(id='default', name='Default', preset='Default'), clips=clips)


def clip(row):
    return VoiceClip(spoken_text='Test', asset_id='a' * 64, status='ready',
                     **{k: v for k, v in row.items() if k not in {'expected_end', 'issues'}})


@pytest.mark.parametrize('case', FIXTURES['cases'], ids=lambda row: row['id'])
def test_shared_timeline_fixtures(case):
    value = clip(case)
    assert file_interval(value)[1] == pytest.approx(case['expected_end'])
    assert window_issues(value) == case['issues']
    assert refresh_timing(document([value])).clips[0].sync.state != 'aligned'


def test_nested_overlaps_mark_all_participants():
    result = refresh_timing(document([clip(row) for row in FIXTURES['nested']]))
    assert all('overlap' in c.sync.issues for c in result.clips)


def test_shared_measured_alignment_binding_and_independent_display_window():
    from app.services.voiceover.timing import source_signature

    fixture = json.loads((Path(__file__).parents[2] / 'test-fixtures/voiceover-alignment.json').read_text(encoding='utf-8'))
    value = VoiceClip.model_validate(fixture['clip'])
    value.sync.alignment = VoiceAlignment.model_validate(fixture['alignment'])
    assert clip_signature(value) == fixture['alignment']['clip_signature']
    assert source_signature(fixture['cue']) == fixture['alignment']['source_signature']
    assert file_interval(value) == (fixture['file_start'], fixture['file_end'])
    assert window_issues(value) == []
    assert refresh_timing(document([value])).clips[0].sync.state == 'aligned'


def test_v1_read_is_non_mutating_and_first_write_creates_recovery_snapshot(tmp_path):
    store = VoiceStore(tmp_path)
    doc = document([VoiceClip(id='legacy', spoken_text='Custom wording', start_ms=1000,
                              end_ms=2000, offset_ms=157, rate=1.85)])
    payload = doc.model_dump()
    payload['schema_version'] = 1
    payload['revision'] = 540
    payload['clips'][0].pop('sync')
    path = store.path('user', 'projects', doc.project_id)
    write_json(path, payload)
    original = path.read_bytes()
    loaded = store.get_document('user', doc.project_id)
    assert path.read_bytes() == original
    assert loaded.schema_version == 2
    assert loaded.clips[0].sync.timing_locked and loaded.clips[0].sync.text_locked
    assert loaded.clips[0].sync.timing_origin == 'legacy_unknown'
    store.save_document('user', loaded)
    snapshots = list((path.parent / 'snapshots' / doc.project_id).glob('*.v1.json'))
    assert len(snapshots) == 1 and snapshots[0].read_bytes() == original
    result = store.get_document('user', doc.project_id)
    assert (result.clips[0].rate, result.clips[0].offset_ms, result.clips[0].spoken_text) == (1.85, 157, 'Custom wording')
    store.save_document('user', result)
    assert snapshots[0].read_bytes() == original
    assert json.loads(snapshots[0].read_bytes()) == payload
    old_client = {**payload, 'revision': result.revision}
    with pytest.raises(ValueError, match='nâng phiên bản'):
        store.save_document('user', VoiceDocument.model_validate(old_client))


def test_client_alignment_claims_are_not_accepted():
    value = clip(FIXTURES['cases'][3])
    value.sync.state = 'aligned'
    assert refresh_timing(document([value])).clips[0].sync.state == 'unverified'
