"""Pick only unresolved, actionable clips after source and cheap audio checks."""
from __future__ import annotations

import math
from collections import Counter

from .store import normalized_text
from .sync_candidate import neighbour_bounds


def select_repairs(voice, subtitles: dict, audit: dict, media: dict) -> tuple[list[dict], dict[str, str]]:
    clips = {clip.id: clip for clip in voice.clips}
    sources = sorted(subtitles['segments'], key=lambda row: (row['start_ms'], row['id']))
    indexes = {row['id']: index for index, row in enumerate(sources)}
    references = Counter(cid for clip in voice.clips for cid in clip.source_cue_ids)
    queue, skipped = [], {}
    for row in audit['rows']:
        identifier = row['clip_id']
        clip = clips.get(identifier)
        reason = None
        if not clip or not row.get('completed'):
            reason = 'audit_incomplete'
        elif clip.sync.timing_locked or clip.sync.text_locked or clip.sync.timing_origin != 'automatic':
            reason = 'manual_or_locked'
        elif row.get('already_aligned') or row.get('processed', {}).get('state') == 'ready_for_review':
            reason = 'already_has_solution'
        elif (len(clip.source_cue_ids) != 1 or clip.source_cue_ids[0] not in indexes
              or references[clip.source_cue_ids[0]] != 1):
            reason = 'source_mapping_unresolved'
        elif (not row.get('source_evidence', {}).get('transcript_complete')
              or row['source_evidence'].get('method') != 'asr_observed'):
            reason = 'source_unverified'
        elif clip.status == 'stale' or 'audio_stale' in row.get('issues', []):
            reason = 'audio_stale'
        if reason:
            skipped[identifier] = reason
            continue
        source = row['source_evidence']
        previous, following = neighbour_bounds(voice, clip, source['start_ms'], media['duration_ms'])
        # Wrong neighbouring anchors require joint timing work, not shorter wording.
        if previous > source['start_ms'] or following < source['end_ms']:
            skipped[identifier] = 'neighbour_source_conflict'
            continue
        inspected = row.get('dubbed_audio', {})
        diagnostics = inspected.get('diagnostics', {})
        mode = None
        if not clip.asset_id:
            mode = 'retry_original'
        elif (inspected.get('speech_verified') and row.get('proposal', {}).get('state') == 'blocked'
                and set(row['proposal'].get('blocked_reasons', [])) == {'duration_not_feasible'}):
            mode = 'shorten'
        elif ('dubbed_speech_missing' in inspected.get('issues', [])
              or (diagnostics.get('transcript_matches') is False
                  and diagnostics.get('minimum_confidence', 0) >= .65)):
            mode = 'retry_original'
        if mode is None:
            skipped[identifier] = 'no_verified_repair_reason'
            continue
        index = indexes[clip.source_cue_ids[0]]
        cue = sources[index]
        item = {'id': identifier, 'mode': mode, 'spoken_text': clip.spoken_text,
                'source_text': cue.get('source_text') or cue.get('secondary_text') or '',
                'display_text': cue['text'], 'source_language': cue.get('source_language'),
                'source_start_ms': source['start_ms'], 'source_end_ms': source['end_ms'],
                'previous': sources[index - 1] if index else None,
                'next': sources[index + 1] if index + 1 < len(sources) else None}
        if mode == 'shorten':
            if not item['source_text'].strip():
                skipped[identifier] = 'source_text_missing'
                continue
            speech = inspected['speech_evidence']
            actual_ms = speech['end_ms'] - speech['start_ms']
            # Estimate from this voice's measured utterance, including pronunciation expansion.
            # The real waveform still decides; max_syllables cannot certify a fit.
            available_ms = source['end_ms'] - source['start_ms']
            count = len(normalized_text(clip.spoken_text, voice.pronunciation).split())
            item['max_syllables'] = min(len(clip.spoken_text.split()) - 1,
                                       math.floor(count * available_ms * 1.1 / actual_ms))
            if item['max_syllables'] < 1:
                skipped[identifier] = 'insufficient_speech_budget'
                continue
        queue.append(item)
    return queue, skipped
