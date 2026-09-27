"""High-CTR video thumbnail selector using sharpness, colorfulness, and face detection."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import av
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

try:
    import cv2
except ImportError:  # Face bonus is optional; frame scoring works without OpenCV.
    cv2 = None

logger = logging.getLogger("content_bot.thumbnail_selector")

_FACE_CASCADE = None


def _get_face_classifier():
    if cv2 is None:
        return None
    global _FACE_CASCADE
    if _FACE_CASCADE is None:
        try:
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            _FACE_CASCADE = cv2.CascadeClassifier(cascade_path)
        except Exception:
            _FACE_CASCADE = None
    return _FACE_CASCADE


def score_video_frame(img_bgr: np.ndarray) -> tuple[float, dict[str, float]]:
    """Score a single video frame for thumbnail quality (higher is more click-worthy).

    Factors:
    - Sharpness (Laplacian variance): penalizes motion blur
    - Colorfulness (Standard deviation of channels)
    - Brightness penalty (too dark or blown out highlights)
    - Face detection bonus (close-up faces increase CTR significantly)
    """
    if img_bgr.ndim != 3 or img_bgr.shape[2] != 3:
        raise ValueError("Khung hình phải là mảng BGR 3 kênh.")
    gray = (
        img_bgr[..., 0] * 0.114
        + img_bgr[..., 1] * 0.587
        + img_bgr[..., 2] * 0.299
    ).astype(np.float32)
    h, _w = gray.shape

    # 1. Sharpness
    laplacian = (
        -4 * gray
        + np.roll(gray, 1, axis=0)
        + np.roll(gray, -1, axis=0)
        + np.roll(gray, 1, axis=1)
        + np.roll(gray, -1, axis=1)
    )
    sharpness = float(laplacian.var())

    # 2. Color richness
    colorfulness = float(np.std(img_bgr))

    # 3. Brightness
    mean_brightness = float(np.mean(gray))
    brightness_penalty = 0.0
    if mean_brightness < 45.0:
        brightness_penalty = (45.0 - mean_brightness) * 2.0
    elif mean_brightness > 215.0:
        brightness_penalty = (mean_brightness - 215.0) * 2.0

    # 4. Face detection
    face_bonus = 0.0
    classifier = _get_face_classifier()
    if classifier is not None:
        try:
            # Scale down for fast face detection
            scale = max(1, h // 360)
            small_gray = gray[::scale, ::scale]
            faces = classifier.detectMultiScale(small_gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
            if len(faces) > 0:
                # Largest face height relative to frame
                max_face_h = max(f[3] * scale for f in faces)
                face_ratio = max_face_h / float(h)
                # Up to 80 points bonus for prominent face
                face_bonus = min(80.0, face_ratio * 160.0)
        except Exception:
            logger.debug("Không nhận diện được khuôn mặt trong khung", exc_info=True)

    total_score = (
        min(120.0, sharpness * 0.4)
        + min(50.0, colorfulness * 0.8)
        - brightness_penalty
        + face_bonus
    )

    metrics = {
        "sharpness": round(sharpness, 1),
        "colorfulness": round(colorfulness, 1),
        "brightness": round(mean_brightness, 1),
        "face_bonus": round(face_bonus, 1),
        "total_score": round(total_score, 1),
    }
    return total_score, metrics


def select_best_thumbnail(
    video_path: Path,
    output_image_path: Path,
    *,
    n_candidates: int = 15,
    anti_duplicate: bool = False,
) -> dict[str, Any]:
    """Scan video candidates, select the best frame, and save high-resolution thumbnail."""
    if not video_path.exists():
        raise RuntimeError(f"Video not found: {video_path}")

    container = av.open(str(video_path))
    if not container.streams.video:
        container.close()
        raise RuntimeError("No video stream found")

    stream = container.streams.video[0]
    duration_s = float(stream.duration * stream.time_base) if stream.duration and stream.time_base else 30.0

    # Omit 8% intro and 8% outro
    start_s = duration_s * 0.08
    end_s = duration_s * 0.92
    if end_s <= start_s:
        target_times = [duration_s / 2.0]
    else:
        target_times = [
            start_s + i * (end_s - start_s) / max(1, n_candidates - 1)
            for i in range(n_candidates)
        ]

    best_score = -999.0
    best_frame_bgr: np.ndarray | None = None
    best_time_s = 0.0
    best_metrics: dict[str, float] = {}

    target_idx = 0
    time_base = float(stream.time_base) if stream.time_base else 1.0 / 30.0

    for frame in container.decode(video=0):
        if target_idx >= len(target_times):
            break

        pts_s = float(frame.pts * time_base) if frame.pts is not None else float(frame.time or 0.0)
        if pts_s < target_times[target_idx]:
            continue

        target_idx += 1
        img_bgr = frame.to_ndarray(format="bgr24")
        score, metrics = score_video_frame(img_bgr)

        if score > best_score or best_frame_bgr is None:
            best_score = score
            best_frame_bgr = img_bgr.copy()
            best_time_s = pts_s
            best_metrics = metrics

    container.close()

    if best_frame_bgr is None:
        raise RuntimeError("Could not decode any valid video frames")

    final_img = best_frame_bgr
    if anti_duplicate:
        # Flip horizontal and slight contrast bump to create unique pixel hash
        image = Image.fromarray(final_img[..., ::-1])
        image = ImageEnhance.Contrast(ImageOps.mirror(image)).enhance(1.03)
        image = ImageEnhance.Brightness(image).enhance(1.01)
        final_img = np.asarray(image)[..., ::-1].copy()

    output_image_path.parent.mkdir(parents=True, exist_ok=True)
    # Save using PIL RGB to avoid non-ASCII path encoding issues with cv2.imwrite
    img_rgb = final_img[..., ::-1]
    pil_img = Image.fromarray(img_rgb)
    pil_img.save(str(output_image_path), quality=95)

    return {
        "thumbnail_path": str(output_image_path),
        "best_timestamp_s": round(best_time_s, 2),
        "score": best_score,
        "metrics": best_metrics,
    }
