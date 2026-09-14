import asyncio
import wave
from pathlib import Path

import pytest
from fastapi import HTTPException
from test_voice_sync_audit import setup

from app.api import subtitles as api
from app.services.voiceover import sync_candidate as module
from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.audio_cache import AudioCache
from app.services.voiceover.store import write_json
from app.services.voiceover.sync_audit import audit_path


def test_processed_candidate_uses_actual_wav_and_measured_speech_not_prediction(tmp_path, monkeypatch):
    store, doc, _, media = setup(tmp_path)
    clip = doc.clips[0]
    original = store.path('user', 'assets', clip.asset_id, '.wav')
    before, document_before = original.read_bytes(), doc.model_dump()
    proposal = {'state': 'candidate', 'dubbed_speech_verified': True, 'trim_start_ms': 50,
        'trim_end_ms': 50, 'suggested_rate': 1.1, 'source_start_ms': 1100, 'allowed_end_ms': 1700}
    observed = {'issues': [], 'speech_verified': True, 'speech_evidence': {'start_ms': 75, 'end_ms': 290}}

    def inspect(path, text, **kwargs):
        assert path != original and text == clip.spoken_text
        assert audio_metadata(path)['checksum'] == kwargs['checksum']
        with wave.open(str(path)) as wav:
            assert wav.getnframes() > 0
        return observed

    monkeypatch.setattr(module, 'inspect_dubbed_speech', inspect)
    kwargs = {'source': original, 'checksum': audio_metadata(original)['checksum'],
              'video_duration_ms': media['duration_ms'], 'cache_dir': tmp_path / 'candidates', 'model_dir': tmp_path}
    result = module.prepare_sync_candidate(doc, clip, proposal, **kwargs)
    assert result['state'] == 'ready_for_review'
    assert result['offset_ms'] == 25  # 1100 - observed 75 - cue start 1000
    assert result['speech_start_ms'] == 1100 and result['speech_end_ms'] == 1315
    assert result['playback_rate'] == 1 and not result['applied']
    assert result['file_end_ms'] == 1025 + result['audio']['duration_ms']
    assert doc.model_dump() == document_before and original.read_bytes() == before
    late = module.prepare_sync_candidate(doc, clip, {**proposal, 'allowed_end_ms': 1200}, **kwargs)
    assert late['state'] == 'needs_review' and 'processed_speech_ends_late' in late['issues']
    doc.clips.append(clip.model_copy(deep=True, update={'id': 'next', 'offset_ms': 300}))
    overlap = module.prepare_sync_candidate(doc, clip, proposal, **kwargs)
    assert overlap['state'] == 'needs_review' and 'overlap' in overlap['issues']
    doc.clips.pop()
    # Reinspection, rather than a shorter calculated duration, can reject an output.
    observed.update(speech_verified=False, issues=['dubbed_transcript_unverified'])
    failed = module.prepare_sync_candidate(doc, clip, proposal, **kwargs)
    assert failed['state'] == 'needs_review' and 'offset_ms' not in failed


def test_neighbour_positions_use_offsets_and_equal_cue_starts(tmp_path):
    _, doc, _, _ = setup(tmp_path)
    clip = doc.clips[0]
    other = clip.model_copy(deep=True, update={'id': 'other', 'offset_ms': 300})
    doc.clips.append(other)
    assert module.neighbour_bounds(doc, clip, 1100, 5000) == (0, 1300)
    other.start_ms, other.end_ms, other.offset_ms = 2000, 3000, -1000
    previous, following = module.neighbour_bounds(doc, clip, 1100, 5000)
    assert previous > 1100 and following == 5000


def test_audio_download_is_bound_to_owner_row_and_checksum(application_services, tmp_path):
    store, doc, _, _ = setup(tmp_path)
    application_services.voiceover_manager.store = store
    source = store.path('user', 'assets', doc.clips[0].asset_id, '.wav')
    identifier, key = 'd' * 32, 'e' * 64
    output = store.owner_root('user') / 'sync-candidates' / 'audio' / f'{key}.wav'
    output.parent.mkdir(parents=True)
    output.write_bytes(source.read_bytes())
    write_json(audit_path(store, 'user', identifier), {'rows': [{'clip_id': 'one', 'completed': True,
        'processed': {'audio': {'id': key, 'checksum': audio_metadata(output)['checksum']}}}]})
    response = api.get_voice_sync_candidate_audio(identifier, 'one', {'sub': 'user'}, services=application_services)
    assert Path(response.path) == output
    cache = AudioCache(output.parent, max_bytes=1)
    with pytest.raises(ValueError, match='đang đầy'):
        cache.trim()
    asyncio.run(response.background())
    for owner, clip_id in [('other-user', 'one'), ('user', 'missing')]:
        with pytest.raises(HTTPException) as error:
            api.get_voice_sync_candidate_audio(identifier, clip_id, {'sub': owner}, services=application_services)
        assert error.value.status_code == 404
    output.write_bytes(b'corrupt')
    with pytest.raises(HTTPException):
        api.get_voice_sync_candidate_audio(identifier, 'one', {'sub': 'user'}, services=application_services)
