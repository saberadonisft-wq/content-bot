"""Find short adjacent clauses to ask about; proximity alone never approves grouping."""
from __future__ import annotations

from collections import Counter
from itertools import pairwise


def group_candidates(voice, subtitles, audit):
    sources = {row['id']: row for row in subtitles['segments']}
    rows = {row['clip_id']: row for row in audit['rows']}
    references = Counter(cid for clip in voice.clips for cid in clip.source_cue_ids)
    eligible = []
    for clip in sorted(voice.clips, key=lambda value: (value.start_ms, value.id)):
        row = rows.get(clip.id, {})
        evidence = row.get('source_evidence', {})
        if (clip.sync.timing_origin != 'automatic' or clip.sync.timing_locked or clip.sync.text_locked
                or clip.sync.state == 'aligned' or not clip.asset_id or clip.status == 'stale'
                or not row.get('completed') or evidence.get('method') != 'asr_observed'
                or not evidence.get('transcript_complete') or len(clip.source_cue_ids) != 1
                or references[clip.source_cue_ids[0]] != 1 or clip.source_cue_ids[0] not in sources
                or not 1 <= len(clip.spoken_text.split()) <= 8):
            eligible.append(None)
            continue
        cue = sources[clip.source_cue_ids[0]]
        if not (cue.get('source_text') or cue.get('secondary_text')):
            eligible.append(None)
            continue
        eligible.append({'id': clip.id, 'spoken_text': clip.spoken_text,
                         'source_text': cue.get('source_text') or cue.get('secondary_text'),
                         'source_start_ms': evidence['start_ms'], 'source_end_ms': evidence['end_ms']})
    groups = []
    used = set()
    for left, right in pairwise(eligible):
        if not left or not right or left['id'] in used or right['id'] in used:
            continue
        if (not left['spoken_text'].rstrip().endswith((',', ';', ':', '，', '；', '：'))
                or not 0 <= right['source_start_ms'] - left['source_end_ms'] <= 400
                or right['source_end_ms'] - left['source_start_ms'] > 10_000):
            continue
        groups.append([left, right])
        used.update((left['id'], right['id']))
    return groups
