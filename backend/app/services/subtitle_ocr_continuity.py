"""Conservative evidence for joining noisy readings of one displayed caption.

Text similarity alone is never sufficient. Unsupported text colours, missing
geometry and ambiguous images retain the existing split behaviour.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import cv2
import numpy as np

from .subtitle_ocr_tracking import _local_change


def _compact(text: str) -> str:
    # Keep punctuation, case and accents: they can change the meaning.
    return "".join(unicodedata.normalize("NFC", text).split())


def _compatible_text(left: str, right: str) -> bool:
    left, right = _compact(left), _compact(right)
    if left == right:
        return bool(left)
    if len(left) != len(right) or len(left) < 8:
        return False
    changes = [(a, b) for a, b in zip(left, right) if a != b]
    if len(changes) != 1:
        return False
    # Do not smooth numbers, negations, Latin accents, insertions or deletions.
    protected = set("零〇一二三四五六七八九十百千万亿两不没无未非别莫勿")
    return all("\u4e00" <= char <= "\u9fff" and char not in protected for char in changes[0])


def _light_strokes(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    low, high = image.min(axis=2), image.max(axis=2)
    # Hysteresis tolerates compression/antialiasing without blurring away a dot.
    strong = (low >= 225) & (high - low <= 30)
    weak = (low >= 170) & (high - low <= 60)
    if not 0.03 <= float(strong.mean()) <= 0.55:
        return np.zeros(strong.shape, dtype=bool), weak
    # Bright moving scenery is not a glyph. Retain white components with a
    # dark outline, including small dots/accents; never filter them by area.
    _, labels, stats, _ = cv2.connectedComponentsWithStats(strong.astype(np.uint8))
    selected = np.zeros(strong.shape, dtype=bool)
    if len(stats) > 1025:
        return selected, weak
    for label, (x, y, width, height, _) in enumerate(stats[1:], 1):
        left, top = max(0, x - 4), max(0, y - 4)
        right, bottom = min(image.shape[1], x + width + 4), min(image.shape[0], y + height + 4)
        component = (labels[top:bottom, left:right] == label).astype(np.uint8)
        outer = cv2.dilate(component, np.ones((7, 7), np.uint8))
        inner = cv2.dilate(component, np.ones((3, 3), np.uint8))
        ring = (outer != 0) & (inner == 0)
        if ring.any() and float(np.mean(high[top:bottom, left:right][ring] < 110)) >= 0.4:
            selected[top:bottom, left:right] |= component != 0
    return selected, weak


@dataclass
class CaptionContinuity:
    text: str
    shape: tuple[int, ...]
    boxes: tuple[tuple[int, int, int, int], ...]
    masks: list[tuple[np.ndarray, np.ndarray]]

    @classmethod
    def create(
        cls, text: str, image: np.ndarray,
        boxes: tuple[tuple[int, int, int, int], ...],
    ) -> CaptionContinuity | None:
        if not text or not 0 < len(boxes) <= 4:
            return None
        masks = []
        height, width = image.shape[:2]
        padded = []
        for x1, y1, x2, y2 in boxes:
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                return None
            if y2 - y1 < 12 or x2 - x1 < 24:
                return None
            x1, y1, x2, y2 = max(0, x1 - 6), max(0, y1 - 6), min(width, x2 + 6), min(height, y2 + 6)
            strong, weak = _light_strokes(image[y1:y2, x1:x2])
            if not 0.03 <= float(strong.mean()) <= 0.55:
                return None
            masks.append((strong, weak))
            padded.append((x1, y1, x2, y2))
        return cls(text, image.shape, tuple(padded), masks)

    def matches(
        self, text: str, image: np.ndarray,
        boxes: tuple[tuple[int, int, int, int], ...],
    ) -> bool:
        if image.shape != self.shape or not _compatible_text(self.text, text):
            return False
        if len(boxes) != len(self.boxes):
            return False
        for old_box, new_box, (strong, weak) in zip(self.boxes, boxes, self.masks):
            x1, y1, x2, y2 = old_box
            height = y2 - y1
            # REC-only padding can enlarge boxes slightly; compare pixels at
            # the fixed original coordinates, never resize or roll the anchor.
            if max(abs(a - b) for a, b in zip(old_box, new_box)) > max(4, height // 4):
                return False
            current, current_weak = _light_strokes(image[y1:y2, x1:x2])
            if not 0.03 <= float(current.mean()) <= 0.55:
                return False
            changed = (strong & ~current_weak) | (current & ~weak)
            if np.count_nonzero(changed) > max(2, np.count_nonzero(strong) * 0.005):
                return False
            # A changed dot must not disappear in a whole-line average.
            if _local_change(changed):
                return False
        return True
