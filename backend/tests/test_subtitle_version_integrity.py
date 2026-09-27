import json

import pytest
from test_subtitle_versions import document

from app.services.gemini_media import digest_json
from app.services.subtitle_versions import (
    SubtitleVersionMediaMismatch,
    SubtitleVersionStore,
)


@pytest.mark.parametrize('damage', ['text', 'hash', 'cue_count', 'schema', 'not_object'])
def test_corrupt_version_is_rejected_without_rewriting_it(tmp_path, damage):
    store = SubtitleVersionStore(tmp_path)
    record = store.save('a' * 12, document())
    path = store.directory('a' * 12) / f"{record['id']}.json"
    if damage == 'text':
        record['document']['segments'][0]['text'] = 'Modified'
    elif damage == 'hash':
        record['content_hash'] = 'b' * 64
    elif damage == 'cue_count':
        record['cue_count'] += 1
    elif damage == 'schema':
        record['document']['segments'][0]['end_ms'] = -1
    else:
        record = []
    path.write_text(json.dumps(record), encoding='utf-8')
    damaged = path.read_bytes()
    with pytest.raises(ValueError):
        store.load('a' * 12, path.stem)
    assert path.read_bytes() == damaged


def test_media_identity_is_part_of_version_deduplication(tmp_path):
    store = SubtitleVersionStore(tmp_path)
    first = store.save('a' * 12, document(), media_fingerprint='original')
    same = store.save('a' * 12, document(), media_fingerprint='original')
    replaced = store.save('a' * 12, document(), media_fingerprint='replacement')
    assert same['id'] == first['id'] != replaced['id']
    assert first['document']['segments'] == replaced['document']['segments']
    assert first['document']['media_fingerprint'] == 'original'
    assert replaced['document']['media_fingerprint'] == 'replacement'
    assert store.load('a' * 12, first['id'], media_fingerprint='original') == first
    with pytest.raises(SubtitleVersionMediaMismatch):
        store.load('a' * 12, first['id'], media_fingerprint='replacement')
    assert store.load('a' * 12, replaced['id'], media_fingerprint='replacement') == replaced


def test_media_identity_cannot_be_changed_without_invalidating_version(tmp_path):
    store = SubtitleVersionStore(tmp_path)
    record = store.save('a' * 12, document(), media_fingerprint='original')
    record['media_fingerprint'] = 'replacement'
    path = store.directory('a' * 12) / f"{record['id']}.json"
    path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='mã kiểm tra'):
        store.load('a' * 12, record['id'])


def test_legacy_document_is_verified_without_adding_new_defaults(tmp_path):
    store = SubtitleVersionStore(tmp_path)
    old_document = document()
    content_hash = digest_json(old_document)
    record = {'version': 1, 'id': content_hash[:32], 'video_id': 'a' * 12,
              'document': old_document, 'content_hash': content_hash, 'cue_count': 1}
    directory = store.directory('a' * 12)
    directory.mkdir()
    path = directory / f"{record['id']}.json"
    path.write_text(json.dumps(record), encoding='utf-8')
    original = path.read_bytes()
    loaded = store.load('a' * 12, record['id'], media_fingerprint='current')
    assert loaded == record and 'media_fingerprint' not in loaded
    assert path.read_bytes() == original


def test_damaged_version_does_not_hide_other_history_entries(tmp_path):
    store = SubtitleVersionStore(tmp_path)
    valid = store.save('a' * 12, document())
    damaged = store.save('a' * 12, document('Second'))
    path = store.directory('a' * 12) / f"{damaged['id']}.json"
    path.write_text('{incomplete json', encoding='utf-8')
    history = store.list('a' * 12)
    assert history['total'] == 2 and history['unreadable_count'] == 1
    assert [row['id'] for row in history['versions']] == [valid['id']]
    assert path.read_text(encoding='utf-8') == '{incomplete json'
