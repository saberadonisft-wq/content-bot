from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ...schemas import SubtitleDocumentV2

MODEL_ID = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
MODEL_REVISION = "8b7e9cffb4b41918cb638b9f62f0a751184d14a6"
SDK_VERSION = "3.6.4"
V2_MODEL_ID = "pnnbao-ump/VieNeu-TTS-v2-Turbo"
V2_MODEL_REVISION = "afe400abff18c00b52b246bb4d21f02a86855eb7"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class VoiceProfile(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    preset: str | None = Field(default=None, max_length=100)
    reference_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model_id: Literal["pnnbao-ump/VieNeu-TTS-v3-Turbo", "pnnbao-ump/VieNeu-TTS-v2-Turbo"] = MODEL_ID
    model_revision: str = MODEL_REVISION
    denoise: bool = True

    @model_validator(mode="after")
    def source(self):
        expected = V2_MODEL_REVISION if self.model_id == V2_MODEL_ID else MODEL_REVISION
        if self.model_revision != expected:
            raise ValueError("Phiên bản không khớp với engine giọng đọc.")
        if bool(self.preset) == bool(self.reference_id):
            raise ValueError("Chọn giọng có sẵn hoặc mẫu giọng.")
        return self


class VoiceAlignment(Model):
    proof_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    original_asset_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    clip_signature: str = Field(max_length=64000)
    source_signature: str = Field(max_length=64000)
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    allowed_end_ms: int = Field(gt=0)
    speech_head_ms: float = Field(ge=0)
    speech_tail_ms: float = Field(gt=0)
    output_duration_ms: float = Field(gt=0)

    @model_validator(mode='after')
    def bounds(self):
        if (not self.source_start_ms < self.source_end_ms <= self.allowed_end_ms
                or not self.speech_head_ms < self.speech_tail_ms <= self.output_duration_ms):
            raise ValueError('Bằng chứng căn giọng có biên không hợp lệ.')
        return self


class VoiceSync(Model):
    # Legacy adjustments have unknown provenance and must not be overwritten.
    timing_origin: Literal["manual", "automatic", "legacy_unknown"] = "legacy_unknown"
    timing_locked: bool = True
    text_locked: bool = True
    state: Literal["unverified", "aligned", "needs_review"] = "unverified"
    issues: list[Literal["source_unverified", "missing_audio", "starts_early", "ends_late", "overlap"]] = Field(default_factory=list)
    alignment: VoiceAlignment | None = None


class VoiceClip(Model):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    source_cue_ids: list[str] = Field(default_factory=list, max_length=100)
    source_text: str = Field(default="", max_length=8000)
    spoken_text: str = Field(min_length=1, max_length=8000)
    start_ms: int = Field(ge=0, le=86_400_000, strict=True)
    end_ms: int = Field(gt=0, le=86_400_000, strict=True)
    offset_ms: int = Field(default=0, ge=-86_400_000, le=86_400_000, strict=True)
    rate: float = Field(default=1.08, ge=0.5, le=2)
    gain: float = Field(default=1, ge=0, le=2)
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    generation_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    duration_ms: int = Field(default=0, ge=0)
    status: Literal["missing", "ready", "stale", "overflow", "failed"] = "missing"
    error: str | None = Field(default=None, max_length=1000)
    sync: VoiceSync = Field(default_factory=VoiceSync)

    @model_validator(mode="after")
    def timing(self):
        if self.end_ms <= self.start_ms or self.start_ms + self.offset_ms < 0:
            raise ValueError("Khoảng giọng đọc không hợp lệ.")
        return self


class MixOptions(Model):
    enabled: bool = True
    muted: bool = False
    gain: float = Field(default=1, ge=0, le=2)
    original_gain: float = Field(default=0.25, ge=0, le=2)
    mode: Literal["voice", "mix", "duck"] = "duck"


class VoiceDocument(Model):
    _migrated_from_v1: bool = PrivateAttr(default=False)
    schema_version: Literal[2] = 2
    project_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    video_fingerprint: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=0, ge=0)
    profile: VoiceProfile
    clips: list[VoiceClip] = Field(default_factory=list, max_length=20000)
    mix: MixOptions = Field(default_factory=MixOptions)
    pronunciation: dict[str, str] = Field(default_factory=dict, max_length=500)

    @model_validator(mode="wrap")
    @classmethod
    def migrate_v1(cls, value, handler):
        migrated = isinstance(value, dict) and value.get("schema_version") == 1
        if migrated:
            value = {**value, "schema_version": 2, "clips": [
                {**clip, "sync": VoiceSync().model_dump()} for clip in value.get("clips", [])
            ]}
        result = handler(value)
        result._migrated_from_v1 = migrated
        return result

    @model_validator(mode="after")
    def unique(self):
        if len({c.id for c in self.clips}) != len(self.clips):
            raise ValueError("ID đoạn giọng bị trùng.")
        if any(
            not k or len(k) > 100 or len(v) > 200 for k, v in self.pronunciation.items()
        ):
            raise ValueError("Từ điển phát âm không hợp lệ.")
        return self


class StartJob(Model):
    project_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    clip_ids: list[str] | None = Field(default=None, max_length=20000)
    device: Literal["cpu", "cuda"] = "cpu"
    subtitle_document: SubtitleDocumentV2 | None = None


class PreviewRequest(Model):
    profile: VoiceProfile
    text: str = Field(min_length=1, max_length=3000)
    device: Literal["cpu", "cuda"] = "cpu"


class AudioSegment(Model):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class AudioExportRequest(Model):
    revision: int = Field(ge=0)
    duration_ms: int = Field(gt=0, le=86400000)
    format: Literal["wav", "flac", "mp3"] = "wav"
    trim_start_ms: int = Field(default=0, ge=0)
    trim_end_ms: int | None = Field(default=None, gt=0)
    video_speed: float = Field(default=1, ge=0.5, le=2)
    video_segments: list[AudioSegment] = Field(default_factory=list, max_length=20000)
