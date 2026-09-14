"""Apply server-measured candidates atomically, keeping the original WAV and revision."""
from __future__ import annotations

import hashlib
import shutil
import uuid
import wave
from contextlib import ExitStack

from .audio import audio_metadata
from .audio_cache import AudioCache
from .models import VoiceAlignment
from .store import VoiceStore, digest, generation_hash, read_json, write_json
from .sync_audit import audit_path, snapshot_binding
from .timing import clip_signature, file_interval, refresh_timing, source_signature


def apply_sync_candidates(store: VoiceStore, owner: str, identifier: str, *, project_id: str,
                          revision: int, subtitles: dict, clip_ids: list[str], media: dict):
    if not clip_ids or len(clip_ids) > 20000 or len(set(clip_ids)) != len(clip_ids):
        raise ValueError('Danh sách áp dụng không hợp lệ.')
    with store.lock, ExitStack() as leases:
        path = audit_path(store, owner, identifier)
        record = read_json(path)
        voice = store.get_document(owner, project_id)
        if (voice.revision != revision or voice.video_fingerprint != media['fingerprint']
                or record['project_id'] != project_id or record['source_fingerprint'] != media['fingerprint']):
            raise ValueError('Dự án hoặc video đã thay đổi. Lưu và kiểm tra lại trước khi áp dụng.')
        binding = snapshot_binding(voice, subtitles)
        if record.get('applied_binding') == binding and record.get('applied_clip_ids') == clip_ids:
            return voice
        if record['state'] != 'succeeded' or record['input_binding'] != binding:
            raise ValueError('Đầu vào đã thay đổi sau lần kiểm tra. Không áp kết quả cũ.')
        rows = {row['clip_id']: row for row in record['rows']}
        clips = {clip.id: clip for clip in voice.clips}
        sources = {cue['id']: cue for cue in subtitles['segments']}
        plans = []
        for clip_id in clip_ids:
            clip, row = clips.get(clip_id), rows.get(clip_id)
            if not clip or not row or not row.get('completed'):
                raise ValueError('Đoạn chưa được kiểm tra xong.')
            processed = row.get('processed', {})
            if (processed.get('state') != 'ready_for_review' or not processed.get('output_speech_verified')
                    or processed.get('issues') or not row.get('source_evidence')
                    or row['source_evidence'].get('method') != 'asr_observed'
                    or not row['source_evidence'].get('transcript_complete')
                    or not processed.get('inspection', {}).get('speech_verified')
                    or clip.sync.timing_locked or clip.sync.timing_origin != 'automatic'):
                raise ValueError('Đoạn chưa đạt kiểm tra hoặc mốc đã khóa.')
            prepared = processed['audio']
            allowed_extra = row['proposal']['allowed_end_ms'] - row['source_evidence']['end_ms']
            if allowed_extra > 0:
                pause = row.get('pause_evidence', {})
                decision = pause.get('context', {}).get('decision', {})
                if (allowed_extra > min(250, pause.get('allowed_ms', 0))
                        or pause.get('reason') != 'source_pause_verified'
                        or not decision.get('source_is_clear') or decision.get('confidence', 0) < .9
                        or not decision.get('tail_pause_has_no_semantic_function')
                        or not decision.get('no_scene_or_speaker_change_at_tail')):
                    raise ValueError('Khoảng nghỉ mượn chưa đủ bằng chứng hoặc vượt giới hạn.')
            if row.get('repair', {}).get('group_members'):
                members = row['repair']['group_members']
                if not set(members) <= set(clip_ids):
                    raise ValueError('Phải áp đủ các vế đã kiểm tra của cùng nhóm.')
            original_id = clip.sync.alignment.original_asset_id if clip.sync.alignment else clip.asset_id
            if row.get('repair'):
                from .repair_audio import validate_repair_asset
                proposed, original, source_wav = validate_repair_asset(store, owner, voice, clip, subtitles, row['repair'])
                # Keep the displayed cue and its timing; bind only the approved spoken variant.
                clip.spoken_text, clip.generation_hash = proposed.spoken_text, proposed.generation_hash
                original_id = original_id or proposed.asset_id
            else:
                original = read_json(store.path(owner, 'assets', clip.asset_id))
                source_wav = store.path(owner, 'assets', clip.asset_id, '.wav')
            if (audio_metadata(source_wav)['checksum'] != prepared['specification']['source_checksum']
                    or generation_hash(voice, clip, original['device']) != original['generation_hash']):
                raise ValueError('WAV gốc hoặc lời đọc đã thay đổi.')
            key = prepared['id']
            if len(key) != 64 or any(char not in '0123456789abcdef' for char in key):
                raise ValueError('ID audio xử lý không hợp lệ.')
            candidate = store.owner_root(owner) / 'sync-candidates' / 'audio' / f'{key}.wav'
            leases.enter_context(AudioCache(candidate.parent).pin(key))
            measured = audio_metadata(candidate)
            if measured['checksum'] != prepared['checksum']:
                raise ValueError('WAV sau xử lý không còn khớp kết quả kiểm tra.')
            with wave.open(str(candidate), 'rb') as wav:
                exact_duration = wav.getnframes() * 1000 / wav.getframerate()
            if not prepared.get('output_measured') or abs(exact_duration - prepared['duration_ms']) > .000001:
                raise ValueError('Thời lượng WAV không khớp kết quả đã đo.')
            asset_key = digest({'processed': key, 'generation_hash': original['generation_hash']})
            clip.asset_id, clip.duration_ms, clip.rate = asset_key, measured['duration_ms'], 1.0
            clip.offset_ms = processed['offset_ms']
            clip.status, clip.error = 'ready', None
            proof = VoiceAlignment(proof_id='0' * 64, original_asset_id=original_id,
                clip_signature=clip_signature(clip), source_signature=source_signature(sources[clip.source_cue_ids[0]]),
                source_start_ms=row['source_evidence']['start_ms'], source_end_ms=row['source_evidence']['end_ms'],
                allowed_end_ms=row['proposal']['allowed_end_ms'],
                speech_head_ms=processed['inspection']['speech_evidence']['start_ms'],
                speech_tail_ms=processed['inspection']['speech_evidence']['end_ms'],
                output_duration_ms=exact_duration)
            proof_binding = store.alignment_binding(voice, clip)
            proof.proof_id = digest({'alignment': proof.model_dump(exclude={'proof_id'}), 'binding': proof_binding})
            clip.sync.alignment = proof
            meta = {**original, **measured, 'id': asset_key, 'original_asset_id': original_id,
                    'processing': prepared['specification']}
            plans.append((candidate, meta, proof, proof_binding))
        # Validate the combined result, not only each candidate against old neighbours.
        refresh_timing(voice)
        for clip_id in clip_ids:
            clip = clips[clip_id]
            start, end = file_interval(clip)
            if clip.sync.issues or start < 0 or end > media['duration_ms']:
                raise ValueError('Các phương án kết hợp gây chồng hoặc vượt vùng được phép.')
        # Snapshot before any project write. Audio is immutable and remains recoverable.
        project_path = store.path(owner, 'projects', project_id)
        raw = project_path.read_bytes()
        snapshot = project_path.parent / 'snapshots' / project_id / f'{hashlib.sha256(raw).hexdigest()}.before-sync.json'
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            with snapshot.open('xb') as stream:
                stream.write(raw)
        for candidate, meta, proof, proof_binding in plans:
            target = store.path(owner, 'assets', meta['id'], '.wav')
            temporary = target.with_suffix(f'.{uuid.uuid4().hex}.part')
            try:
                shutil.copyfile(candidate, temporary)
                if audio_metadata(temporary)['checksum'] != meta['checksum']:
                    raise ValueError('Audio thay đổi trong lúc áp dụng.')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            write_json(store.path(owner, 'assets', meta['id']), meta)
            write_json(store.owner_root(owner) / 'sync-fits' / f'{proof.proof_id}.json',
                {'binding': proof_binding, 'alignment': proof.model_dump(), 'audit_id': identifier})
        saved = store.save_document(owner, voice)
        record.update(applied_revision=saved.revision, applied_clip_ids=clip_ids,
                      applied_proofs={clip.id: clip.sync.alignment.proof_id for clip in saved.clips if clip.id in clip_ids},
                      applied_binding=snapshot_binding(saved, subtitles))
        write_json(path, record)
        return saved
