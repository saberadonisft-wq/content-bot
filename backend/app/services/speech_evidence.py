"""Provenance for speech timestamps; a sampling grid is not accuracy evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AlignmentMethod = Literal['asr_observed', 'ctc_aligned', 'energy_estimated', 'interpolated', 'manual']


class SpeechEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    method: AlignmentMethod
    audio_identity: str | None = Field(default=None, max_length=128)
    transcript_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    algorithm: str = Field(min_length=1, max_length=100)
    # True only describes transcript coverage, never an accuracy probability.
    transcript_complete: bool = False

    @model_validator(mode='after')
    def interval(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('Speech evidence requires a non-empty interval')
        return self


def transcript_hash(cue: dict) -> str:
    text = cue.get('source_text') or cue.get('secondary_text') or cue.get('text') or ''
    return hashlib.sha256(' '.join(str(text).split()).encode()).hexdigest()


def audio_identity(media: dict) -> str | None:
    values = {k: media.get(k) for k in ('audio_hash', 'fingerprint', 'duration_ms', 'source_start_ms')}
    if not (values['audio_hash'] or values['fingerprint']):
        return None
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def valid_speech_evidence(cue: dict, media: dict | None = None) -> SpeechEvidence | None:
    """Checks correspondence, not whether arbitrary imported provenance is trusted."""
    try:
        evidence = SpeechEvidence.model_validate(cue.get('speech_evidence'))
    except ValueError:
        return None
    if (evidence.transcript_sha256 != transcript_hash(cue)
            or (evidence.start_ms, evidence.end_ms) != (cue.get('speech_start_ms'), cue.get('speech_end_ms'))):
        return None
    if media is not None and (not evidence.audio_identity or evidence.audio_identity != audio_identity(media)
                              or evidence.end_ms > int(media['duration_ms'])):
        return None
    return evidence
