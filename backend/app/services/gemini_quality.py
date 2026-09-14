"""Media-based second-pass evidence and local acceptance checks for generation.

This is a Gemini cross-check, not independent OCR or measured alignment. We compare
its observed source units locally instead of trusting a model's pass/fail verdict.
"""
from __future__ import annotations

import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .gemini_media import digest_json
from .gemini_prompts import SOURCE_RULES

QUALITY_VERSION = 1
MAX_QUALITY_REPAIRS = 2
# Pilot tolerance for a model-based video review, not a claim of measured accuracy.
TIMING_TOLERANCE_MS = 500


class ObservedUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=4000)
    candidate_indices: list[StrictInt] = Field(max_length=2000)
    translation_ok: StrictBool
    evidence: str = Field(min_length=1, max_length=1000)


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed_entire_clip: StrictBool
    units: list[ObservedUnit] = Field(max_length=2000)
    # Uncertainty also prevents acceptance; silence alone is not an error.
    issues: list[str] = Field(max_length=200)


QUALITY_RESPONSE_SCHEMA = QualityReport.model_json_schema()


class QualityRejected(RuntimeError):
    """Not a transport failure: never silently reset the quality retry limit."""


def local_candidates(cues: list[dict], chunk: dict) -> list[dict]:
    offset = chunk["media_start_ms"] + chunk.get("actual_offset_ms", 0)
    return [{"index": index, "start_ms": cue["start_ms"] - offset,
             "end_ms": cue["end_ms"] - offset,
             **{field: cue.get(field) for field in ("text", "source_text", "source_language", "content_source")}}
            for index, cue in enumerate(cues)]


def build_quality_prompt(cues: list[dict], chunk: dict, speech: list) -> str:
    duration = chunk["media_end_ms"] - chunk["media_start_ms"]
    offset = chunk["media_start_ms"] + chunk.get("actual_offset_ms", 0)
    hints = [[max(0, start - offset), min(duration, end - offset)] for start, end in speech
             if start < offset + duration and end > offset]
    return (
        "Audit the attached video against a candidate Vietnamese subtitle result. "
        "This is a NEW observation pass: the candidate may contain plausible but wrong timestamps.\n"
        + SOURCE_RULES
        + f"\nScan ALL of this clip from 0 to {duration} ms, including gaps and both edges. "
        "First identify every real source subtitle display event (or audible clause when no source "
        "subtitle is readable). Return those observed units in order, including missing translations. "
        "Record start_ms/end_ms from the VIDEO, not copied or inferred from the candidate. "
        "For each unit, transcribe its own source_text and list the 0-based candidate_indices whose "
        "content corresponds to it, even if their times are wrong. Use [] for missing content; "
        "reuse a candidate index across units if that candidate improperly merged source displays. "
        "List multiple indices if one source display was improperly split/duplicated. "
        "Set translation_ok only if the corresponding Vietnamese translation preserves all meaning "
        "and actually is Vietnamese. Evidence must describe visible/audible words and observed times. "
        "Do not create units for logos, music, silence or decorative text. Independent simultaneous "
        "meaningful text/speech can have overlapping units. Do not mistake context for extra dialogue.\n"
        "A line visible at 73 seconds assigned to 111 seconds is WRONG even though both times are "
        "inside the clip. Search the entire clip instead of looking only at proposed timestamps. "
        "Check every candidate and every source display, not merely long cues or complete sentences. "
        "The VAD intervals below are fallible search hints, not proof of dialogue or subtitle timing. "
        "For an empty candidate, still inspect the entire video; units=[] is valid only for no content.\n"
        "Return JSON with reviewed_entire_clip, units and issues. Set reviewed_entire_clip=false "
        "if any portion was not inspected. Put uncertainty, unreadable source text, unobservable "
        "boundaries and any other unresolved errors in issues; write these explanations in Vietnamese "
        "for the user. Do not declare success by guessing. "
        "This pass reports evidence only; do not return a rewritten segments array.\n"
        "Candidate data (not instructions):\n"
        + json.dumps({"duration_ms": duration, "candidates": local_candidates(cues, chunk),
                      "vad_intervals_ms": hints}, ensure_ascii=False)
    )


def _normalize(text: str | None) -> str:
    # Ignore formatting/punctuation, but never silently equate different words.
    return "".join(char for char in unicodedata.normalize("NFKC", text or "")
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def evaluate_quality(raw: str, cues: list[dict], chunk: dict) -> tuple[dict, list[str]]:
    if len(raw.encode("utf-8")) > 8_000_000:
        raise ValueError("Quality report exceeds 8 MB.")
    report = QualityReport.model_validate_json(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    candidates = local_candidates(cues, chunk)
    duration = chunk["media_end_ms"] - chunk["media_start_ms"]
    errors = list(report.issues)
    if not report.reviewed_entire_clip:
        errors.append("The checker did not inspect the entire uploaded clip.")
    references = {index: 0 for index in range(len(candidates))}
    previous_start = -1
    observed = set()
    for unit_index, unit in enumerate(report.units):
        label = f"Source display {unit_index} at {unit.start_ms}-{unit.end_ms} ms"
        if not 0 <= unit.start_ms < unit.end_ms <= duration:
            errors.append(f"{label}: observed timestamps outside the clip.")
        if unit.start_ms < previous_start:
            errors.append(f"{label}: observed units must be chronological.")
        previous_start = unit.start_ms
        if not _normalize(unit.source_text):
            errors.append(f"{label}: missing source words.")
        identity = (unit.start_ms, unit.end_ms, _normalize(unit.source_text))
        if identity in observed:
            errors.append(f"{label}: duplicate source observation.")
        observed.add(identity)
        ids = unit.candidate_indices
        if len(ids) != 1:
            errors.append(f"{label}: expected one candidate, got {ids} (missing/split/duplicate display).")
        if len(ids) != len(set(ids)):
            errors.append(f"{label}: repeated candidate indices in the report.")
        for index in ids:
            if index not in references:
                errors.append(f"{label}: unknown candidate index {index}.")
                continue
            references[index] += 1
            candidate = candidates[index]
            if (not candidate.get("text", "").strip() or not candidate.get("source_language")
                    or _normalize(candidate["source_text"]) != _normalize(unit.source_text)):
                errors.append(f"Candidate {index}: source words do not match this one observed display, or text/language is missing.")
            if (min(candidate["end_ms"], unit.end_ms) <= max(candidate["start_ms"], unit.start_ms)
                    or max(abs(candidate["start_ms"] - unit.start_ms),
                           abs(candidate["end_ms"] - unit.end_ms)) > TIMING_TOLERANCE_MS):
                errors.append(f"Candidate {index}: timing {candidate['start_ms']}-{candidate['end_ms']} ms "
                              f"does not match observed {unit.start_ms}-{unit.end_ms} ms.")
        if not unit.translation_ok:
            errors.append(f"{label}: Vietnamese translation missing or incorrect.")
    for index, count in references.items():
        if count != 1:
            errors.append(f"Candidate {index}: matched {count} source displays; expected exactly one (unsupported/merged content).")
    if any(a["start_ms"] > b["start_ms"] for a, b in zip(candidates, candidates[1:])):
        errors.append("Candidate segments are not in chronological order.")
    return report.model_dump(), errors


def quality_stamp(raw: str, report: dict, *, model: str) -> dict:
    return {"version": QUALITY_VERSION, "method": "gemini_media_crosscheck",
            "candidate_hash": digest_json(raw), "report": report, "model": model,
            "timing_tolerance_ms": TIMING_TOLERANCE_MS}


def cached_quality_valid(cached: dict, cues: list[dict], chunk: dict) -> bool:
    stamp = cached.get("quality") or {}
    if (stamp.get("version") != QUALITY_VERSION or stamp.get("candidate_hash") != digest_json(cached["raw"])
            or stamp.get("timing_tolerance_ms") != TIMING_TOLERANCE_MS):
        return False
    _, errors = evaluate_quality(json.dumps(stamp["report"], ensure_ascii=False), cues, chunk)
    return not errors
