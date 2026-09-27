import array
import math
import wave
from pathlib import Path

from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.dsp_cache import prepare_clip_dsp


def _source(path: Path) -> dict:
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
        values = array.array(
            "h",
            [
                round(5000 * math.sin(index * 2 * math.pi * 440 / 48_000))
                for index in range(48_000)
            ],
        )
        wav.writeframes(values.tobytes())
    return audio_metadata(path)


def test_clip_dsp_cache_keys_gain_and_reuses_verified_pcm(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    metadata = _source(source)
    cache = tmp_path / "cache"

    first = prepare_clip_dsp(
        source,
        checksum=metadata["checksum"],
        rate=1.25,
        gain=1,
        fade_in_ms=3,
        cache_dir=cache,
    )
    second = prepare_clip_dsp(
        source,
        checksum=metadata["checksum"],
        rate=1.25,
        gain=1,
        fade_in_ms=3,
        cache_dir=cache,
    )
    changed_gain = prepare_clip_dsp(
        source,
        checksum=metadata["checksum"],
        rate=1.25,
        gain=0.5,
        fade_in_ms=3,
        cache_dir=cache,
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["path"] == first["path"]
    assert changed_gain["cache_hit"] is False
    assert changed_gain["path"] != first["path"]
    with wave.open(str(changed_gain["path"]), "rb") as wav:
        assert wav.getframerate() == 48_000
        assert wav.getnchannels() == 1
        assert wav.getnframes() > 0


def test_clip_dsp_cache_allows_intentional_silence(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    metadata = _source(source)
    result = prepare_clip_dsp(
        source,
        checksum=metadata["checksum"],
        rate=1,
        gain=0,
        fade_in_ms=0,
        cache_dir=tmp_path / "cache",
    )
    assert result["path"].is_file()
    with wave.open(str(result["path"]), "rb") as wav:
        assert max(abs(value) for value in array.array("h", wav.readframes(wav.getnframes()))) == 0
