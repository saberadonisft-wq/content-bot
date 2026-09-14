import base64
import json
import threading

from app.services.voiceover import source_context as module
from app.services.voiceover.mix import ffmpeg
from app.services.voiceover.repair_budget import RepairBudget


def test_context_uses_bounded_source_video_and_reuses_input_bound_cache(tmp_path, monkeypatch):
    video = tmp_path / 'source.mp4'
    ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=8:d=1', '-f', 'lavfi',
            '-i', 'anullsrc=r=16000:cl=mono', '-t', '1', '-c:v', 'libx264', '-threads', '1',
            '-c:a', 'aac', str(video)], timeout=10)
    original = video.read_bytes()
    budget = RepairBudget(tmp_path / 'budget.json', input_binding='a' * 64, cluster_ids=['one'])

    class Service:
        def resolve_model(self):
            return 'fixture'

    calls = []

    def request(service, ledger, ids, prompt, schema, *, cancel, media_parts):
        calls.append(ids)
        receipt = ledger.reserve('gemini', ids, cancel=cancel)
        assert media_parts[0]['inline_data']['mime_type'] == 'video/mp4'
        content = base64.b64decode(media_parts[0]['inline_data']['data'])
        assert b'ftyp' in content[:32] and len(content) < 4 * 1024 * 1024
        ledger.settle(receipt, succeeded=True)
        return json.dumps({'clip_ids': ids, 'source_is_clear': True, 'same_speaking_turn': True,
            'fragmented_clauses': False, 'tail_pause_has_no_semantic_function': True,
            'no_scene_or_speaker_change_at_tail': True, 'confidence': .95, 'reason': 'fixture'})

    monkeypatch.setattr(module, 'request_repair_json', request)
    args = (Service(), budget, video, {'fingerprint': 'video-fixture', 'duration_ms': 1000},
            [{'id': 'one', 'source_start_ms': 100, 'source_end_ms': 300}])
    options = {'cache_dir': tmp_path / 'context', 'cancel': threading.Event()}
    result = module.inspect_source_context(*args, **options)
    assert result == module.inspect_source_context(*args, **options)
    assert len(calls) == 1 and budget.snapshot()['gemini_used'] == 1
    assert video.read_bytes() == original
