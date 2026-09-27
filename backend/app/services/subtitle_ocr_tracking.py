"""Bounded visual evidence for OCR reuse; timestamps always belong to the caller.

These signatures are conservative heuristics, not proof of equal text. They are
only valid near an independently recognized frame and never form a rolling cache
whose lifetime can be extended by a cache hit.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field


class OcrAcceleration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selective_refinement: bool = True
    recognition_reuse: bool = True
    refinement_batch_size: int = Field(default=4, ge=1, le=8)
    verify_interval_ms: int = Field(default=500, ge=50, le=1000)
    min_reuse_confidence: float = Field(default=0.85, ge=0.7, le=1.0)
    decode_threads: int = Field(default=2, ge=0, le=8)
    crop_before_bgr: bool = True


@dataclass
class GlyphFeatures:
    gray: np.ndarray
    strong: np.ndarray
    weak: np.ndarray
    regions: dict[int, list[tuple[int, int, int, int]] | None] = field(default_factory=dict)

    def text_regions(self, line_height: int) -> list[tuple[int, int, int, int]] | None:
        if line_height in self.regions:
            return self.regions[line_height]
        joined = cv2.morphologyEx(
            self.strong.astype(np.uint8), cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, line_height // 2), 3)),
        )
        _, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
        stats = stats[1:]
        widths, heights = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
        selected = stats[
            (heights >= max(3, line_height // 3)) & (heights <= line_height * 2)
            & (widths >= max(4, line_height // 2)) & (widths >= heights * 0.5)
        ]
        # Highly cluttered scenes are uncertain; bound Python metadata/work.
        regions = None if len(selected) > 256 else [tuple(int(v) for v in row[:4]) for row in selected]
        if len(self.regions) >= 8:
            self.regions.pop(next(iter(self.regions)))
        self.regions[line_height] = regions
        return regions


class FeatureCache:
    """Per-window lazy features; array identity is checked before an entry is used."""

    def __init__(self) -> None:
        self.entries: OrderedDict[int, tuple[np.ndarray, GlyphFeatures]] = OrderedDict()
        self.bytes = 0
        self.builds = 0
        self.hits = 0

    def get(self, image: np.ndarray) -> GlyphFeatures:
        key = id(image)
        entry = self.entries.get(key)
        if entry is not None and entry[0] is image:
            self.entries.move_to_end(key)
            self.hits += 1
            return entry[1]
        features = glyph_features(image)
        self.builds += 1
        # Include the owned BGR image retained by the cache in the budget.
        size = image.nbytes + features.gray.nbytes + features.strong.nbytes + features.weak.nbytes
        limit = 24 * 1024 * 1024
        if size <= limit:
            while self.entries and (len(self.entries) >= 32 or self.bytes + size > limit):
                _, (old_image, old) = self.entries.popitem(last=False)
                self.bytes -= old_image.nbytes + old.gray.nbytes + old.strong.nbytes + old.weak.nbytes
            self.entries[key] = (image, features)
            self.bytes += size
        return features

    def clear(self) -> None:
        self.entries.clear()
        self.bytes = 0


def glyph_features(image: np.ndarray) -> GlyphFeatures:
    # No thumbnail/downscale: a digit or accent must remain visible. Both
    # polarities allow bright and dark text; uncertain backgrounds fail closed.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    bright = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    dark = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    contrast = cv2.max(bright, dark)
    return GlyphFeatures(gray, contrast >= 48, contrast >= 24)


def _local_change(changed: np.ndarray, *, allowance: int = 2) -> bool:
    """Do not let a small changed character disappear in a whole-line average."""
    count = int(np.count_nonzero(changed))
    if count <= allowance:
        return False
    height, width = changed.shape
    padded = np.pad(changed, ((0, (-height) % 8), (0, (-width) % 8)))
    tiles = padded.reshape(padded.shape[0] // 8, 8, padded.shape[1] // 8, 8)
    return bool(np.any(tiles.sum(axis=(1, 3)) > allowance))


@dataclass
class VisualReading:
    timestamp_ms: int
    image: np.ndarray
    text: str
    confidence: float
    boxes: tuple[tuple[int, int, int, int], ...]
    features: GlyphFeatures | None
    padded_boxes: tuple[tuple[int, int, int, int], ...]
    verified_timestamp_ms: int

    @classmethod
    def create(
        cls,
        timestamp_ms: int,
        image: np.ndarray,
        text: str,
        confidence: float,
        boxes: tuple[tuple[int, int, int, int], ...],
        options: OcrAcceleration,
        feature_cache: FeatureCache | None = None,
        verified_timestamp_ms: int | None = None,
    ) -> VisualReading:
        features = None
        padded_boxes = []
        height, width = image.shape[:2]
        if (
            (options.selective_refinement or options.recognition_reuse)
            and text
            and confidence >= options.min_reuse_confidence
            and 0 < len(boxes) <= 8
        ):
            for x1, y1, x2, y2 in boxes:
                margin = max(3, min(8, (y2 - y1) // 5))
                # Text touching the selected ROI cannot establish safe geometry.
                if x1 < margin or y1 < margin or x2 + margin >= width or y2 + margin >= height:
                    padded_boxes = []
                    break
                box = (x1 - margin, y1 - margin, x2 + margin, y2 + margin)
                padded_boxes.append(box)
            if padded_boxes:
                features = feature_cache.get(image) if feature_cache is not None else glyph_features(image)
                for x1, y1, x2, y2 in boxes:
                    foreground = features.strong[y1:y2, x1:x2]
                    density = float(np.mean(foreground)) if foreground.size else 0.0
                    if np.count_nonzero(foreground) < 12 or not 0.005 <= density <= 0.6:
                        features = None
                        break
        verified = timestamp_ms if verified_timestamp_ms is None else verified_timestamp_ms
        return cls(timestamp_ms, image, text, confidence, boxes, features, tuple(padded_boxes), verified)

    def current(self, timestamp_ms: int, options: OcrAcceleration) -> bool:
        return (
            abs(timestamp_ms - self.timestamp_ms) <= options.verify_interval_ms
            and abs(timestamp_ms - self.verified_timestamp_ms) <= options.verify_interval_ms
        )

    def exact_match(self, image: np.ndarray) -> bool:
        return self.confidence >= 0 and image.shape == self.image.shape and np.array_equal(image, self.image)

    def matches(self, image: np.ndarray, features: GlyphFeatures | None) -> bool:
        if image.shape != self.image.shape or self.confidence < 0:
            return False
        # Blank observations are reusable ONLY for exactly equal images.
        if self.exact_match(image):
            return True
        if self.features is None or features is None:
            return False
        old = self.features
        for x1, y1, x2, y2 in self.padded_boxes:
            glyph = old.strong[y1:y2, x1:x2]
            changed = (glyph & ~features.weak[y1:y2, x1:x2]) | (
                features.strong[y1:y2, x1:x2] & ~old.weak[y1:y2, x1:x2]
            )
            if np.count_nonzero(changed) > max(2, int(np.count_nonzero(glyph) * 0.005)):
                return False
            if _local_change(changed):
                return False
            # Fade/scene changes can retain edges but change stroke intensity.
            delta = np.abs(features.gray[y1:y2, x1:x2].astype(np.int16) - old.gray[y1:y2, x1:x2])
            if np.any(glyph) and float(np.quantile(delta[glyph], 0.9)) > 16:
                return False
        return self._outside_unchanged(features)

    def _outside_unchanged(self, features: GlyphFeatures) -> bool:
        assert self.features is not None
        # Search the entire ROI for NEW text-like horizontal regions, rather
        # than vetoing reuse for arbitrary moving edges outside known text.
        # Periodic full detection remains authoritative; this is a heuristic.
        line_height = max(4, int(np.median([y2 - y1 for _, y1, _, y2 in self.boxes])))
        regions = features.text_regions(line_height)
        if regions is None:
            return False
        for x, y, width, height in regions:
            if any(x >= x1 and y >= y1 and x + width <= x2 and y + height <= y2
                   for x1, y1, x2, y2 in self.padded_boxes):
                continue
            strokes = features.strong[y:y + height, x:x + width]
            density = float(np.mean(strokes))
            if not 0.04 <= density <= 0.7 or np.count_nonzero(strokes) < 12:
                continue
            added = strokes & ~self.features.weak[y:y + height, x:x + width]
            for x1, y1, x2, y2 in self.padded_boxes:
                left, top = max(x, x1), max(y, y1)
                right, bottom = min(x + width, x2), min(y + height, y2)
                if left < right and top < bottom:
                    added[top-y:bottom-y, left-x:right-x] = False
            if _local_change(added) and np.count_nonzero(added) >= max(4, np.count_nonzero(strokes) * 0.15):
                return False
        return True

    def reusable_geometry(self, features: GlyphFeatures | None) -> bool:
        if self.features is None or features is None or not self._outside_unchanged(features):
            return False
        for x1, y1, x2, y2 in self.padded_boxes:
            mask = features.strong[y1:y2, x1:x2]
            if np.count_nonzero(mask) < 12:
                return False
            # Any new strokes on the crop boundary require full detection.
            old = self.features.weak[y1:y2, x1:x2]
            added = mask & ~old
            if added[:3].any() or added[-3:].any() or added[:, :3].any() or added[:, -3:].any():
                return False
        return True
