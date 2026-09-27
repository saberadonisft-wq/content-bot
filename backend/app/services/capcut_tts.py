"""CapCut / ByteDance Volcengine Text-to-Speech connector."""
from __future__ import annotations

import json
import logging
import random
import string
import time
from pathlib import Path

import httpx

logger = logging.getLogger("content_bot.capcut_tts")

CAPCUT_BASE_URL = "https://editor-api-sg.capcutapi.com"
PATH_NEW_TASK = "/lv/v1/common_task/new"
PATH_QUERY_TASK = "/lv/v1/common_task/query"

# Pre-mapped standard ByteDance / CapCut voices
CAPCUT_VOICE_MAP: dict[str, dict[str, str]] = {
    # Vietnamese voices
    "vi_female_tram": {
        "name": "Thanh Trúc (Trầm ấm)",
        "voice_type": "vi_female_1",
        "resource_id": "vi_female_1",
        "lang": "vi",
    },
    "vi_female_nhe": {
        "name": "Mai Anh (Truyền cảm)",
        "voice_type": "vi_female_2",
        "resource_id": "vi_female_2",
        "lang": "vi",
    },
    "vi_male_nam": {
        "name": "Nam Minh (Bản tin)",
        "voice_type": "vi_male_1",
        "resource_id": "vi_male_1",
        "lang": "vi",
    },
    # English voices
    "en_female_pleasant": {
        "name": "Grace (English UK)",
        "voice_type": "en_female_grace",
        "resource_id": "en_female_grace",
        "lang": "en",
    },
    "en_male_story": {
        "name": "Adam (English US)",
        "voice_type": "en_male_adam",
        "resource_id": "en_male_adam",
        "lang": "en",
    },
}


class CapCutTtsError(RuntimeError):
    pass


def _random_device_id() -> str:
    """Generate a pseudo-random numeric device ID."""
    return "".join(random.choices(string.digits, k=19))


def _build_ssml(text: str, voice_name: str, resource_id: str, rate: float = 1.0) -> str:
    """Build standard SSML envelope for ByteDance speech synthesis."""
    # Escape XML entities in text
    escaped_text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    rate_str = f"{rate:.2f}"
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">\n'
        f'    <voice name="{voice_name}" mock_tone_info="" platform="pc" resource_id="{resource_id}" '
        f'emotion="" emotion_scale="0" style="" role="" moyin_emotion="" is_clone_tone="false" need_subtitle_timestamp="true">\n'
        f'        <prosody rate="{rate_str}">\n'
        f"            {escaped_text}\n"
        f"        </prosody>\n"
        f"    </voice>\n"
        f"</speak>"
    )


def synthesize_capcut_speech(
    text: str,
    output_path: Path,
    *,
    voice_key: str = "vi_female_tram",
    rate: float = 1.0,
    timeout_seconds: float = 30.0,
) -> Path:
    """Synthesize speech using CapCut online API and save to mp3/wav."""
    clean_text = text.strip()
    if not clean_text:
        raise CapCutTtsError("Văn bản đầu vào rỗng")

    voice_meta = CAPCUT_VOICE_MAP.get(voice_key, CAPCUT_VOICE_MAP["vi_female_tram"])
    voice_name = voice_meta["voice_type"]
    resource_id = voice_meta["resource_id"]

    ssml = _build_ssml(clean_text, voice_name, resource_id, rate=rate)
    device_id = _random_device_id()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "device-time": str(int(time.time())),
        "device_id": device_id,
        "device_platform": "windows",
        "appvr": "5.9.0",
    }

    params = {
        "device_id": device_id,
        "device_platform": "windows",
        "aid": "359692",
    }

    payload = {
        "task_type": "text_to_speech",
        "extra": json.dumps(
            {
                "speak": ssml,
                "type": "text_to_speech",
            },
            ensure_ascii=False,
        ),
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(
                f"{CAPCUT_BASE_URL}{PATH_NEW_TASK}",
                params=params,
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                raise CapCutTtsError(f"HTTP {resp.status_code}: {resp.text[:150]}")

            res_json = resp.json()
            task_id = res_json.get("data", {}).get("task_id")
            if not task_id:
                # Direct result or error
                err_msg = res_json.get("message") or "Không nhận được task_id"
                raise CapCutTtsError(f"CapCut API Error: {err_msg}")

            # Poll for completion
            poll_start = time.time()
            audio_url = None

            while time.time() - poll_start < timeout_seconds:
                time.sleep(1.0)
                q_resp = client.post(
                    f"{CAPCUT_BASE_URL}{PATH_QUERY_TASK}",
                    params=params,
                    json={"task_id": task_id},
                    headers=headers,
                )
                if q_resp.status_code != 200:
                    continue

                q_json = q_resp.json()
                task_status = q_json.get("data", {}).get("status")

                if task_status == "success" or task_status == 1:
                    raw_result = q_json.get("data", {}).get("result", "{}")
                    if isinstance(raw_result, str):
                        try:
                            parsed_result = json.loads(raw_result)
                        except Exception:
                            parsed_result = {}
                    else:
                        parsed_result = raw_result

                    audio_url = parsed_result.get("audio_url") or parsed_result.get("speech_url")
                    break
                elif task_status == "failed" or task_status == -1:
                    raise CapCutTtsError(f"Task TTS thất bại trên server CapCut: {q_json}")

            if not audio_url:
                raise CapCutTtsError("Hết thời gian chờ kết quả TTS từ CapCut")

            # Download audio
            audio_resp = client.get(audio_url)
            if audio_resp.status_code != 200:
                raise CapCutTtsError(f"Không thể tải file audio từ {audio_url}")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_resp.content)
            return output_path

    except Exception as exc:
        if isinstance(exc, CapCutTtsError):
            raise
        raise CapCutTtsError(f"Lỗi kết nối CapCut TTS: {exc}") from exc
