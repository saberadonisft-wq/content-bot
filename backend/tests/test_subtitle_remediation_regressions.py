"""Behavioral regressions from the OCR/ASR review; no network or user media."""
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pytest

from app.schemas import SubtitleOcrRegion
from app.services import subtitle_ocr as ocr
from app.services.subtitle_translate import (
    SubtitleTranslateError,
    _validate_batch_translations,
)


class Frame:
    def __init__(self, pts):
        self.pts = pts

    def to_ndarray(self, format):
        return np.full((100, 200, 3), (self.pts // 10) % 250, dtype=np.uint8)


class Container:
    def __init__(self, times):
        self.times = times
        self.streams = SimpleNamespace(video=[SimpleNamespace(
            time_base=Fraction(1, 1000), width=200, height=100,
            codec_context=SimpleNamespace(thread_count=0),
            start_time=times[0] if times else 0)])

    def decode(self, video=0):
        return iter(Frame(t) for t in self.times)

    def close(self):
        pass


def extract(tmp_path, monkeypatch, texts, *, times=None, **kwargs):
    video = tmp_path / 'fixture.mp4'
    video.write_bytes(b'fixture')
    times = times or list(range(0, len(texts) * 200, 200))
    monkeypatch.setattr(ocr.av, 'open', lambda *a: Container(times))
    monkeypatch.setattr(ocr, '_get_ocr_engine', lambda: object())
    observations = iter((t, .95, ()) for t in texts)
    monkeypatch.setattr(ocr, '_extract_crop_text', lambda *a: next(observations))
    return ocr.extract_subtitles_ocr(video, SubtitleOcrRegion(),
        {'fingerprint': 'fixture', 'duration_ms': len(texts) * 200,
         'source_start_ms': times[0]}, sample_fps=5, min_duration_ms=100,
        glyph_cache=False, **kwargs)


def test_stable_changed_number_is_a_new_display(tmp_path, monkeypatch):
    result = extract(tmp_path, monkeypatch, ['I have 100 apples'] * 3 + ['I have 200 apples'] * 3)
    assert [c['source_text'] for c in result['document']['segments']] == [
        'I have 100 apples', 'I have 200 apples']


def test_nonzero_pts_use_video_relative_timeline(tmp_path, monkeypatch):
    result = extract(tmp_path, monkeypatch, ['Hello'] * 3, times=[5000, 5200, 5400])
    assert len(result['document']['segments']) == 1
    assert result['document']['segments'][0]['start_ms'] == 0


def test_auto_probe_applies_detected_region_even_with_request_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, 'probe_subtitle_y_band', lambda *a, **kw: (
        SubtitleOcrRegion(x=5, y=40, width=90, height=10), 'zh'))
    result = extract(tmp_path, monkeypatch, ['Hello'] * 3, auto_probe=True)
    assert result['region']['y'] == 40


def test_gap_policy_changes_invalidate_cache(tmp_path, monkeypatch):
    texts = ['Hello', 'Hello', '', '', 'Hello', 'Hello']
    cache = tmp_path / 'cache'
    first = extract(tmp_path, monkeypatch, texts, max_gap_ms=50, cache_dir=cache)
    changed = extract(tmp_path, monkeypatch, texts, max_gap_ms=500, cache_dir=cache)
    fresh = extract(tmp_path, monkeypatch, texts, max_gap_ms=500)
    assert first['cache_key'] != changed['cache_key']
    assert changed['document'] == fresh['document']


@pytest.mark.parametrize('returned', [
    [{'id': 'c1', 'translation': 'One'}, {'id': 'c1', 'translation': 'Two'}],
    [{'id': 'c1', 'translation': 'One'}, {'id': 'extra', 'translation': 'Two'}],
    [{'id': 'c1', 'translation': 123}],
])
def test_translation_rejects_invalid_batch_atomically(returned):
    with pytest.raises(SubtitleTranslateError):
        _validate_batch_translations(['c1'], returned)


def test_decoder_error_remains_actionable(tmp_path, monkeypatch):
    video = tmp_path / 'broken.mp4'
    video.write_bytes(b'broken')
    monkeypatch.setattr(ocr, '_get_ocr_engine', lambda: object())
    with pytest.raises(ocr.SubtitleOcrError, match='video|PyAV'):
        ocr.extract_subtitles_ocr(video, SubtitleOcrRegion(), {'duration_ms': 1000})
