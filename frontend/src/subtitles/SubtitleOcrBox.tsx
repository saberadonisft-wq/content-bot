import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import {
  clampOcrRegion,
  fitOcrContentBox,
  MIN_OCR_DIMENSION,
  type OcrContentBox,
} from "./ocr-region";
import { ScanText } from 'lucide-react';
import type { SubtitleOcrRegion } from "./types";

type ResizeCorner = "nw" | "ne" | "sw" | "se";

const FALLBACK_CONTENT_BOX: OcrContentBox = { left: 0, top: 0, width: 1, height: 1 };

type OcrDragState = {
  pointerId: number;
  mode: "move" | "resize";
  corner?: ResizeCorner;
  startX: number;
  startY: number;
  startRegion: SubtitleOcrRegion;
};

type SubtitleOcrBoxProps = {
  region: SubtitleOcrRegion;
  onChange: (region: SubtitleOcrRegion) => void;
  visible?: boolean;
  disabled?: boolean;
};

export function SubtitleOcrBox({
  region,
  onChange,
  visible = true,
  disabled = false,
}: SubtitleOcrBoxProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<OcrDragState | null>(null);
  const [contentBox, setContentBox] = useState<OcrContentBox | null>(null);

  useLayoutEffect(() => {
    if (!visible) return undefined;
    const root = rootRef.current;
    const parent = root?.parentElement;
    if (!parent) return undefined;

    const update = () => {
      const frame = parent.getBoundingClientRect();
      if (frame.width <= 0 || frame.height <= 0) return;
      const video = parent.querySelector<HTMLVideoElement>("video");
      const videoWidth = video?.videoWidth ?? 0;
      const videoHeight = video?.videoHeight ?? 0;
      const next = fitOcrContentBox(frame.width, frame.height, videoWidth, videoHeight);
      setContentBox((current) => current &&
        Math.abs(current.left - next.left) < 0.25 &&
        Math.abs(current.top - next.top) < 0.25 &&
        Math.abs(current.width - next.width) < 0.25 &&
        Math.abs(current.height - next.height) < 0.25 ? current : next);
    };

    update();
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(update);
    observer?.observe(parent);
    // Capture media events on the frame: the video can be replaced after mount.
    parent.addEventListener("loadedmetadata", update, true);
    parent.addEventListener("emptied", update, true);
    return () => {
      observer?.disconnect();
      parent.removeEventListener("loadedmetadata", update, true);
      parent.removeEventListener("emptied", update, true);
      dragRef.current = null;
    };
  }, [visible]);

  const readContentBox = useCallback((): OcrContentBox => {
    const parent = rootRef.current?.parentElement;
    if (!parent) return FALLBACK_CONTENT_BOX;
    const frame = parent.getBoundingClientRect();
    const video = parent.querySelector<HTMLVideoElement>("video");
    const videoWidth = video?.videoWidth ?? 0;
    const videoHeight = video?.videoHeight ?? 0;
    return fitOcrContentBox(frame.width, frame.height, videoWidth, videoHeight);
  }, []);

  const startDrag = useCallback(
    (
      event: PointerEvent<HTMLElement>,
      mode: "move" | "resize",
      corner?: ResizeCorner,
    ) => {
      if (disabled) return;
      event.preventDefault();
      event.stopPropagation();

      dragRef.current = {
        pointerId: event.pointerId,
        mode,
        corner,
        startX: event.clientX,
        startY: event.clientY,
        startRegion: { ...region },
      };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    [disabled, region],
  );
  const startResizeDrag = useCallback((event: PointerEvent<HTMLElement>) => {
    const corner = event.currentTarget.dataset.corner as ResizeCorner | undefined;
    if (corner) startDrag(event, "resize", corner);
  }, [startDrag]);

  const onPointerMove = useCallback(
    (event: PointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (disabled || !drag || drag.pointerId !== event.pointerId || !rootRef.current) return;

      const box = readContentBox();
      if (box.width <= 0 || box.height <= 0) return;

      const deltaX = ((event.clientX - drag.startX) / box.width) * 100;
      const deltaY = ((event.clientY - drag.startY) / box.height) * 100;

      if (drag.mode === "move") {
        const nextX = drag.startRegion.x + deltaX;
        const nextY = drag.startRegion.y + deltaY;
        const clamped = clampOcrRegion({
          ...drag.startRegion,
          x: nextX,
          y: nextY,
        });
        onChange(clamped);
      } else if (drag.mode === "resize" && drag.corner) {
        let { x, y, width, height } = drag.startRegion;

        switch (drag.corner) {
          case "nw": {
            const rawX = x + deltaX;
            const rawY = y + deltaY;
            const newX = Math.max(0, Math.min(x + width - MIN_OCR_DIMENSION, rawX));
            const newY = Math.max(0, Math.min(y + height - MIN_OCR_DIMENSION, rawY));
            width = width + (x - newX);
            height = height + (y - newY);
            x = newX;
            y = newY;
            break;
          }
          case "ne": {
            const rawY = y + deltaY;
            const newY = Math.max(0, Math.min(y + height - MIN_OCR_DIMENSION, rawY));
            height = height + (y - newY);
            y = newY;
            width = Math.max(MIN_OCR_DIMENSION, Math.min(100 - x, width + deltaX));
            break;
          }
          case "sw": {
            const rawX = x + deltaX;
            const newX = Math.max(0, Math.min(x + width - MIN_OCR_DIMENSION, rawX));
            width = width + (x - newX);
            x = newX;
            height = Math.max(MIN_OCR_DIMENSION, Math.min(100 - y, height + deltaY));
            break;
          }
          case "se": {
            width = Math.max(MIN_OCR_DIMENSION, Math.min(100 - x, width + deltaX));
            height = Math.max(MIN_OCR_DIMENSION, Math.min(100 - y, height + deltaY));
            break;
          }
        }

        const clamped = clampOcrRegion({ x, y, width, height });
        onChange(clamped);
      }
    },
    [disabled, onChange, readContentBox],
  );

  const onPointerUp = useCallback((event: PointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) {
      dragRef.current = null;
    }
  }, []);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return;
      const step = event.shiftKey ? 2 : 0.5;
      let nextRegion: Partial<SubtitleOcrRegion> | null = null;

      if (event.key === "ArrowLeft") nextRegion = { x: region.x - step };
      else if (event.key === "ArrowRight") nextRegion = { x: region.x + step };
      else if (event.key === "ArrowUp") nextRegion = { y: region.y - step };
      else if (event.key === "ArrowDown") nextRegion = { y: region.y + step };
      else if (event.key === "+" || event.key === "=") {
        nextRegion = {
          width: region.width + step,
          height: region.height + step,
        };
      } else if (event.key === "-" || event.key === "_") {
        nextRegion = {
          width: Math.max(MIN_OCR_DIMENSION, region.width - step),
          height: Math.max(MIN_OCR_DIMENSION, region.height - step),
        };
      }

      if (nextRegion) {
        event.preventDefault();
        onChange(clampOcrRegion({ ...region, ...nextRegion }));
      }
    },
    [disabled, onChange, region],
  );

  if (!visible) return null;

  const box = contentBox ?? FALLBACK_CONTENT_BOX;

  return (
    <div ref={rootRef} className="subtitle-ocr-layer" aria-hidden={disabled}>
      <div
        className="subtitle-ocr-box"
        style={{
          left: `${box.left + (region.x / 100) * box.width}px`,
          top: `${box.top + (region.y / 100) * box.height}px`,
          width: `${(region.width / 100) * box.width}px`,
          height: `${(region.height / 100) * box.height}px`,
        }}
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label="Vùng OCR phụ đề; dùng các phím mũi tên để dịch chuyển hoặc phím cộng trừ để co giãn"
        onPointerDown={(e) => startDrag(e, "move")}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          dragRef.current = null;
        }}
        onKeyDown={handleKeyDown}
      >
        <span className="subtitle-ocr-badge">
          <ScanText size={14} aria-hidden="true" /> Vùng OCR ({Math.round(region.width)}% × {Math.round(region.height)}%)
        </span>

        {!disabled &&
          (["nw", "ne", "sw", "se"] as const).map((corner) => (
            <span
              key={corner}
              className={`subtitle-ocr-resize-handle is-${corner}`}
              data-corner={corner}
              aria-hidden="true"
              onPointerDown={startResizeDrag}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={() => {
                dragRef.current = null;
              }}
            />
          ))}
      </div>
    </div>
  );
}
