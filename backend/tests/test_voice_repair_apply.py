from copy import deepcopy

import pytest
from test_voice_sync_apply import candidate

from app.services.voiceover.repair_review import candidate_id
from app.services.voiceover.store import generation_hash, read_json, write_json
from app.services.voiceover.sync_apply import apply_sync_candidates
from app.services.voiceover.sync_audit import audit_path, snapshot_binding


def repaired_candidate(tmp_path):
    store, doc, identifier, record, args = candidate(tmp_path)
    clip = doc.clips[0]
    clip.spoken_text = 'Xin chào bạn thân mến.'
    # Bind this fixture's initial WAV to the initial wording before proposing a variant.
    meta = read_json(store.path('user', 'assets', clip.asset_id))
    meta['generation_hash'] = generation_hash(doc, clip, meta['device'])
    write_json(store.path('user', 'assets', clip.asset_id), meta)
    doc = store.save_document('user', doc)
    clip = doc.clips[0]
    args['revision'] = doc.revision
    text = 'Chào bạn thân mến.'
    context = {'id': clip.id, 'spoken_text': clip.spoken_text, 'display_text': args['subtitles']['segments'][0]['text'],
               'source_text': args['subtitles']['segments'][0]['source_text'], 'max_syllables': 4}
    review = {'candidate_id': candidate_id(context, text), 'reason': 'fixture',
              **dict.fromkeys(['meaning_preserved', 'polarity_preserved', 'quantities_preserved',
                               'addressing_preserved', 'tone_preserved', 'source_is_clear'], True)}
    staged = clip.model_copy(deep=True, update={'spoken_text': text})
    generated = {**meta, 'id': 'e' * 64, 'generation_hash': generation_hash(doc, staged, meta['device'])}
    store.path('user', 'assets', generated['id'], '.wav').write_bytes(
        store.path('user', 'assets', clip.asset_id, '.wav').read_bytes())
    write_json(store.path('user', 'assets', generated['id']), generated)
    record['rows'][0]['repair'] = {'clip_id': clip.id, 'mode': 'shorten', 'spoken_text': text,
        'input': context, 'candidate_id': review['candidate_id'], 'semantic_review': review,
        'original_asset_id': clip.asset_id, 'original_checksum': meta['checksum'], 'asset': generated}
    record['input_binding'] = snapshot_binding(doc, args['subtitles'])
    write_json(audit_path(store, 'user', identifier), record)
    return store, doc, identifier, record, args


def test_applied_spoken_variant_keeps_display_text_source_timing_and_original_wave(tmp_path):
    store, before, identifier, _record, args = repaired_candidate(tmp_path)
    subtitles = deepcopy(args['subtitles'])
    old_wav = store.path('user', 'assets', before.clips[0].asset_id, '.wav')
    old_bytes = old_wav.read_bytes()
    result = apply_sync_candidates(store, 'user', identifier, **args)
    clip = result.clips[0]
    assert clip.spoken_text == 'Chào bạn thân mến.'
    assert clip.source_text == before.clips[0].source_text
    assert (clip.start_ms, clip.end_ms) == (before.clips[0].start_ms, before.clips[0].end_ms)
    assert args['subtitles'] == subtitles and clip.rate == 1
    assert clip.sync.state == 'aligned' and clip.sync.alignment.original_asset_id == before.clips[0].asset_id
    assert old_wav.read_bytes() == old_bytes
    assert store.get_document('user', before.project_id).model_dump() == result.model_dump()
    restored = before.model_copy(deep=True, update={'revision': result.revision})
    restored = store.save_document('user', restored)
    assert restored.clips[0].asset_id == before.clips[0].asset_id
    assert restored.clips[0].spoken_text == before.clips[0].spoken_text


def test_missing_audio_can_be_filled_without_inventing_a_previous_wave(tmp_path):
    store, doc, identifier, record, args = repaired_candidate(tmp_path)
    clip = doc.clips[0]
    repair = record['rows'][0]['repair']
    repair.update(mode='retry_original', spoken_text=clip.spoken_text, original_asset_id=None, original_checksum=None)
    generated = read_json(store.path('user', 'assets', repair['asset']['id']))
    generated['generation_hash'] = generation_hash(doc, clip, generated['device'])
    repair['asset'] = generated
    write_json(store.path('user', 'assets', generated['id']), generated)
    clip.asset_id, clip.duration_ms, clip.status = None, 0, 'missing'
    doc = store.save_document('user', doc)
    args['revision'] = doc.revision
    record['input_binding'] = snapshot_binding(doc, args['subtitles'])
    write_json(audit_path(store, 'user', identifier), record)
    result = apply_sync_candidates(store, 'user', identifier, **args)
    assert result.clips[0].spoken_text == clip.spoken_text
    assert result.clips[0].asset_id and result.clips[0].sync.state == 'aligned'
    assert result.clips[0].sync.alignment.original_asset_id == generated['id']


@pytest.mark.parametrize('change', ['meaning', 'variant', 'source', 'locked', 'asset', 'original'])
def test_unapproved_or_changed_repair_never_updates_project(tmp_path, change):
    store, doc, identifier, record, args = repaired_candidate(tmp_path)
    repair = record['rows'][0]['repair']
    if change == 'meaning':
        repair['semantic_review']['meaning_preserved'] = False
    elif change == 'variant':
        repair['spoken_text'] = 'Lời đọc khác.'
    elif change == 'source':
        repair['input']['source_text'] += ' changed'
    elif change == 'locked':
        doc.clips[0].sync.text_locked = True
        doc = store.save_document('user', doc)
        args['revision'] = doc.revision
        record['input_binding'] = snapshot_binding(doc, args['subtitles'])
    elif change == 'asset':
        repair['asset']['checksum'] = '0' * 64
    else:
        repair['original_checksum'] = '0' * 64
    write_json(audit_path(store, 'user', identifier), record)
    project = store.path('user', 'projects', doc.project_id)
    before = project.read_bytes()
    with pytest.raises(ValueError):
        apply_sync_candidates(store, 'user', identifier, **args)
    assert project.read_bytes() == before
