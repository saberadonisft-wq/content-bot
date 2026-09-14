"""Split a jointly synthesized utterance only at observed, quiet word boundaries."""
from __future__ import annotations

import array
import math
import sys
import wave
from itertools import pairwise

from ..subtitle_alignment import _normalized_word
from .audio import audio_metadata
from .dubbed_speech import inspect_dubbed_speech
from .fit_plan import plan_source_fit
from .models import VoiceClip
from .repair_audio import validate_repair_asset
from .source_context import SourceContext
from .store import digest, generation_hash, normalized_text, read_json, write_json
from .sync_audit import audit_path, snapshot_binding
from .sync_candidate import neighbour_bounds, prepare_sync_candidate


def split_points(path, texts, words):
    """No proportional timing, no boundary through a word, at least 80 ms per side."""
    expected = [_normalized_word(text) for text in texts]
    if any(not text for text in expected) or any(word.get('confidence', 0) < .65 for word in words):
        raise ValueError('Mapping lời đọc của nhóm chưa đủ chắc chắn.')
    bounds, cursor = [], 0
    for text in expected:
        accumulated = ''
        first = cursor
        while cursor < len(words) and len(accumulated) < len(text):
            accumulated += _normalized_word(words[cursor]['text'])
            cursor += 1
        if accumulated != text or first == cursor:
            raise ValueError('ASR chưa tách được trọn từng vế của nhóm.')
        bounds.append((words[first]['start_ms'], words[cursor - 1]['end_ms']))
    if cursor != len(words):
        raise ValueError('WAV nhóm có thêm từ ngoài nội dung cần đọc.')
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError('WAV nhóm phải là PCM16 mono.')
        rate, frames = wav.getframerate(), wav.getnframes()
        cuts = [0]
        for (_, end), (start, _) in pairwise(bounds):
            if start - end < 160:
                raise ValueError('Giữa hai vế chưa có biên tách bảo vệ âm cuối/âm đầu.')
            cut = round((start + end) / 2 * rate / 1000)
            guard = max(1, round(.01 * rate))
            if cut - guard < 0 or cut + guard >= frames:
                raise ValueError('Điểm tách nằm ngoài WAV.')
            wav.setpos(cut - guard)
            samples = array.array('h', wav.readframes(2 * guard))
            if sys.byteorder != 'little':
                samples.byteswap()
            if len(samples) != 2 * guard or max(abs(sample) for sample in samples) >= 33:
                raise ValueError('Điểm tách còn tín hiệu âm thanh; giữ WAV riêng trước đó.')
            cuts.append(cut)
        cuts.append(frames)
    return cuts


def run_group_audio(store, *, owner, identifier, media, voice, subtitles, clip_ids, model_dir,
                    dubbed_whisper_model, repair, progress=None, **unused):
    generated = repair['attempt']
    members = generated['member_ids']
    context = SourceContext.model_validate(generated['context']['decision'])
    if (clip_ids != members or context.clip_ids != members or not context.source_is_clear
            or not context.same_speaking_turn or not context.fragmented_clauses or context.confidence < .9):
        raise ValueError('Chưa xác minh các vế thuộc cùng một lượt nói cần ghép.')
    originals = {clip.id: clip for clip in voice.clips}
    joined = ' '.join(originals[member].spoken_text for member in members)
    if generated['spoken_text'] != joined:
        raise ValueError('Không sửa nội dung khi ghép các vế.')
    prototype = VoiceClip.model_validate({**originals[members[0]].model_dump(), 'spoken_text': joined})
    source = store.path(owner, 'assets', generated['asset']['id'], '.wav')
    metadata = read_json(store.path(owner, 'assets', generated['asset']['id']))
    if (audio_metadata(source)['checksum'] != metadata['checksum']
            or metadata['checksum'] != generated['asset']['checksum']
            or generation_hash(voice, prototype, metadata['device']) != metadata['generation_hash']):
        raise ValueError('WAV nhóm không khớp nội dung/profile.')
    for member in members:
        clip = originals[member]
        if clip.sync.text_locked or clip.sync.timing_locked or clip.sync.timing_origin != 'automatic':
            raise ValueError('Một vế đã được chỉnh tay hoặc khóa.')
    inspected = inspect_dubbed_speech(source, normalized_text(joined, voice.pronunciation),
        checksum=metadata['checksum'], model_dir=model_dir, whisper_model=dubbed_whisper_model,
        cache_dir=store.owner_root(owner) / 'dubbed-speech')
    if not inspected.get('speech_verified'):
        raise ValueError('WAV nhóm chưa đạt đối chiếu lời đọc.')
    cuts = split_points(source, [normalized_text(originals[member].spoken_text, voice.pronunciation)
                                for member in members], inspected['words'])
    rows, clips, inspections = [], [], []
    input_binding = snapshot_binding(voice, subtitles)
    record = {'id': identifier, 'project_id': voice.project_id, 'voice_revision': voice.revision,
              'source_fingerprint': media['fingerprint'], 'source_document': subtitles,
              'input_binding': input_binding, 'state': 'running', 'rows': rows,
              'automatic_apply': False, 'algorithm': 'group-speech-v1'}
    output_path = audit_path(store, owner, identifier)
    with wave.open(str(source), 'rb') as wav:
        for index, member in enumerate(members):
            clip = originals[member]
            first, last = cuts[index:index + 2]
            key = digest({'group_checksum': metadata['checksum'], 'first': first, 'last': last,
                          'generation_hash': generation_hash(voice, clip, metadata['device'])})
            path = store.path(owner, 'assets', key, '.wav')
            partial = path.with_suffix('.part')
            try:
                with wave.open(str(partial), 'wb') as part:
                    part.setparams(wav.getparams())
                    wav.setpos(first)
                    part.writeframes(wav.readframes(last - first))
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
            meta = {**audio_metadata(path), 'id': key, 'device': metadata['device'],
                    'generation_hash': generation_hash(voice, clip, metadata['device']),
                    'group_asset_id': metadata['id'], 'group_samples': [first, last]}
            write_json(store.path(owner, 'assets', key), meta)
            original_meta = read_json(store.path(owner, 'assets', clip.asset_id))
            attempt = {'clip_id': member, 'mode': 'retry_original', 'spoken_text': clip.spoken_text,
                'original_asset_id': clip.asset_id, 'original_checksum': original_meta['checksum'], 'asset': meta,
                'group_members': members, 'group_context': generated['context']}
            proposed, _, path = validate_repair_asset(store, owner, voice, clip, subtitles, attempt)
            observation = inspect_dubbed_speech(path, normalized_text(clip.spoken_text, voice.pronunciation),
                checksum=meta['checksum'], model_dir=model_dir, whisper_model=dubbed_whisper_model,
                cache_dir=store.owner_root(owner) / 'dubbed-speech')
            if not observation.get('speech_verified'):
                raise ValueError('Một vế sau tách chưa đạt đối chiếu lời đọc.')
            row = {'clip_id': member, 'completed': False, 'issues': [], 'repair': attempt,
                   'source_evidence': repair['source_rows'][index]['source_evidence'], 'dubbed_audio': observation}
            rows.append(row)
            clips.append(proposed)
            inspections.append(observation)
            write_json(output_path, record)
    # All members are re-anchored to their own measured source onset. External clips stay fixed.
    staged = voice.model_copy(deep=True)
    staged.clips = [clip for clip in staged.clips if clip.id not in members]
    previous_output_end = 0
    for index, (clip, row, observation) in enumerate(zip(clips, rows, inspections, strict=True)):
        evidence = row['source_evidence']
        previous, following = neighbour_bounds(staged, clip, evidence['start_ms'], media['duration_ms'])
        previous = max(previous, previous_output_end)
        if index + 1 < len(rows):
            next_observed = inspections[index + 1]
            head = next_observed['speech_evidence']['start_ms'] - next_observed['trim_start_ms']
            following = min(following, math.floor(rows[index + 1]['source_evidence']['start_ms'] - head))
        row['proposal'] = plan_source_fit(clip, evidence['start_ms'], evidence['end_ms'],
            video_duration_ms=media['duration_ms'], previous_file_end_ms=previous,
            next_file_start_ms=following, audio=observation)
        if row['proposal']['state'] != 'candidate':
            raise ValueError('Nhóm sau tách chưa vừa các vùng nói nguồn cố định.')
        row['processed'] = prepare_sync_candidate(staged, clip, row['proposal'],
            source=store.path(owner, 'assets', clip.asset_id, '.wav'), checksum=row['repair']['asset']['checksum'],
            video_duration_ms=media['duration_ms'], cache_dir=store.owner_root(owner) / 'sync-candidates',
            model_dir=model_dir, whisper_model=dubbed_whisper_model)
        if row['processed']['state'] != 'ready_for_review' or row['processed']['file_start_ms'] < previous_output_end:
            raise ValueError('Một vế sau căn còn chồng hoặc chưa đạt; giữ cả nhóm trước đó.')
        previous_output_end = row['processed']['file_end_ms']
        row['completed'] = True
        write_json(output_path, record)
    record['state'] = 'succeeded'
    write_json(output_path, record)
    return {'sync_audit_id': identifier, 'project_id': voice.project_id, 'rows': len(rows)}
