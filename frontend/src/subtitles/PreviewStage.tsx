import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import type { SubtitleBurnOptions } from "../api";
import { fitOverlayLayout } from "./overlay";
import { SubtitleMaskLayer } from "./SubtitleMaskLayer";
import { previewSubtitleMetrics } from "./preview-scale";
import { hexToRgba } from "./style";
import { SubtitleIntervalIndex, sortCues } from "./time";
import type { PlaybackClockStore } from "./playback-store";
import type {
  OverlayLayout,
  PreviewMode,
  SubtitleCueV2,
  SubtitleMaskRegion,
  VideoDimensions,
} from "./types";
import { useActiveCueId } from "./useActiveCue";

type PreviewStageProps = {
  videoElement: HTMLVideoElement | null;
  videoUrl: string | null;
  previewMode: PreviewMode;
  libassActive: boolean;
  onLibassHostElementChange: (host: HTMLDivElement | null) => void;
  cues: readonly SubtitleCueV2[];
  selectedCueId: string | null;
  options: SubtitleBurnOptions;
  overlayImage: string | null;
  overlayName: string;
  overlayLayout: OverlayLayout;
  subtitleMasks: readonly SubtitleMaskRegion[];
  selectedMaskId: string | null;
  clock: PlaybackClockStore;
  onVideoElementChange: (video: HTMLVideoElement | null) => void;
  onTogglePlay: () => void;
  onSelectCue: (cueId: string | null) => void;
  onUpdateCueText: (cueId: string, text: string) => void;
  onOptionsCommit: (options: SubtitleBurnOptions) => void;
  onOverlayLayoutCommit: (layout: OverlayLayout) => void;
  onSelectMask: (maskId: string | null) => void;
  onMaskChange: (maskId: string, patch: Partial<SubtitleMaskRegion>) => void;
  onMaskDelete: (maskId: string) => void;
};

type PositionDrag = {
  pointerId: number;
  clientX: number;
  clientY: number;
  startX: number;
  startY: number;
  pointerX: number;
  pointerY: number;
  resultX: number;
  resultY: number;
};

type ResizeDrag = {
  pointerId: number;
  clientY: number;
  startSize: number;
  pointerY: number;
  resultSize: number;
};

type OverlayPositionDrag = {
  pointerId: number;
  clientX: number;
  clientY: number;
  pointerX: number;
  pointerY: number;
  startLayout: OverlayLayout;
  resultLayout: OverlayLayout;
};

type OverlayResizeDrag = {
  pointerId: number;
  clientX: number;
  pointerX: number;
  directionX: -1 | 1;
  startLayout: OverlayLayout;
  resultLayout: OverlayLayout;
};

type PreviewFrameBox = {
  width: number;
  height: number;
};

export function PreviewStage({
  videoElement,
  videoUrl,
  previewMode,
  libassActive,
  onLibassHostElementChange,
  cues,
  selectedCueId,
  options,
  overlayImage,
  overlayName,
  overlayLayout,
  subtitleMasks,
  selectedMaskId,
  clock,
  onVideoElementChange,
  onTogglePlay,
  onSelectCue,
  onUpdateCueText,
  onOptionsCommit,
  onOverlayLayoutCommit,
  onSelectMask,
  onMaskChange,
  onMaskDelete,
}: PreviewStageProps) {
  const [dimensions, setDimensions] = useState<VideoDimensions>({ width: 16, height: 9 });
  const [frameBox, setFrameBox] = useState<PreviewFrameBox>({ width: 16, height: 9 });
  const [isEditing, setIsEditing] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [isOverlaySelected, setIsOverlaySelected] = useState(false);
  const [isOverlayDragging, setIsOverlayDragging] = useState(false);
  const stageSurfaceRef = useRef<HTMLDivElement>(null);
  const mediaFrameRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const imageOverlayRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const verticalGuideRef = useRef<HTMLSpanElement>(null);
  const horizontalGuideRef = useRef<HTMLSpanElement>(null);
  const positionDragRef = useRef<PositionDrag | null>(null);
  const resizeDragRef = useRef<ResizeDrag | null>(null);
  const imagePositionDragRef = useRef<OverlayPositionDrag | null>(null);
  const imageResizeDragRef = useRef<OverlayResizeDrag | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const pointerAbortRef = useRef<AbortController | null>(null);
  const sortedCues = useMemo(() => sortCues(cues), [cues]);
  const intervalIndex = useMemo(
    () => new SubtitleIntervalIndex(sortedCues, true),
    [sortedCues],
  );
  const activeCueId = useActiveCueId(clock, intervalIndex);
  const activeCue = useMemo(
    () => sortedCues.find((cue) => cue.id === activeCueId) ?? null,
    [activeCueId, sortedCues],
  );
  const previewMetrics = previewSubtitleMetrics(
    frameBox.height,
    options.font_size,
    options.outline_width,
    options.shadow_width,
  );
  const previewScale = previewMetrics.scale;

  const fitImageLayout = useCallback((layout: OverlayLayout) => {
    const frame = mediaFrameRef.current;
    const image = imageRef.current;
    if (!frame || !image) return layout;
    const rect = frame.getBoundingClientRect();
    return fitOverlayLayout(layout, {
      frameWidth: rect.width,
      frameHeight: rect.height,
      imageWidth: image.naturalWidth || image.width || 1,
      imageHeight: image.naturalHeight || image.height || 1,
    });
  }, []);

  const applyImageLayout = (layout: OverlayLayout) => {
    const imageOverlay = imageOverlayRef.current;
    if (!imageOverlay) return;
    imageOverlay.style.left = `${layout.x}%`;
    imageOverlay.style.top = `${layout.y}%`;
    imageOverlay.style.width = `${layout.width}%`;
    const sizeLabel = imageOverlay.querySelector<HTMLOutputElement>(
      ".preview-image-size-label",
    );
    if (sizeLabel) sizeLabel.value = `${layout.width.toFixed(1)}%`;
  };

  useEffect(() => {
    if (!overlayImage || !imageRef.current?.complete) return;
    const fitted = fitImageLayout(overlayLayout);
    if (
      fitted.x !== overlayLayout.x ||
      fitted.y !== overlayLayout.y ||
      fitted.width !== overlayLayout.width
    ) {
      onOverlayLayoutCommit(fitted);
    }
  }, [
    fitImageLayout,
    frameBox.height,
    frameBox.width,
    onOverlayLayoutCommit,
    overlayImage,
    overlayLayout,
  ]);

  useEffect(() => {
    const surface = stageSurfaceRef.current;
    if (!surface) return undefined;
    const fitFrame = (availableWidth: number, availableHeight: number) => {
      if (availableWidth <= 0 || availableHeight <= 0) return;
      const aspect = dimensions.width / Math.max(1, dimensions.height);
      const availableAspect = availableWidth / availableHeight;
      const width = availableAspect > aspect ? availableHeight * aspect : availableWidth;
      const height = availableAspect > aspect ? availableHeight : availableWidth / aspect;
      setFrameBox((current) =>
        Math.abs(current.width - width) < 0.5 && Math.abs(current.height - height) < 0.5
          ? current
          : { width, height },
      );
    };
    const observer = new ResizeObserver(([entry]) => {
      fitFrame(entry.contentRect.width, entry.contentRect.height);
    });
    observer.observe(surface);
    return () => observer.disconnect();
  }, [dimensions.height, dimensions.width]);

  useEffect(
    () => () => {
      pointerAbortRef.current?.abort();
      if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    },
    [],
  );

  const paintPositionDrag = () => {
    animationFrameRef.current = null;
    const drag = positionDragRef.current;
    const frame = mediaFrameRef.current;
    const overlay = overlayRef.current;
    if (!drag || !frame || !overlay) return;
    const rect = frame.getBoundingClientRect();
    let nextX = drag.startX + ((drag.pointerX - drag.clientX) / Math.max(1, rect.width)) * 100;
    let nextY = drag.startY + ((drag.pointerY - drag.clientY) / Math.max(1, rect.height)) * 100;
    if (Math.abs(nextX - 50) < 1.5) nextX = 50;
    if (Math.abs(nextY - 50) < 1.5) nextY = 50;
    nextX = Math.max(0, Math.min(100, nextX));
    nextY = Math.max(0, Math.min(100, nextY));
    drag.resultX = nextX;
    drag.resultY = nextY;
    overlay.style.left = `${nextX}%`;
    overlay.style.top = `${nextY}%`;
    if (verticalGuideRef.current) verticalGuideRef.current.style.left = `${nextX}%`;
    if (horizontalGuideRef.current) horizontalGuideRef.current.style.top = `${nextY}%`;
  };

  const paintResizeDrag = () => {
    animationFrameRef.current = null;
    const drag = resizeDragRef.current;
    const overlay = overlayRef.current;
    if (!drag || !overlay) return;
    drag.resultSize = Math.max(
      14,
      Math.min(
        96,
        Math.round(
          drag.startSize + ((drag.clientY - drag.pointerY) * 0.3) / previewScale,
        ),
      ),
    );
    overlay.style.fontSize = `${Math.max(10, drag.resultSize * previewScale)}px`;
  };

  const paintImagePositionDrag = () => {
    animationFrameRef.current = null;
    const drag = imagePositionDragRef.current;
    const frame = mediaFrameRef.current;
    if (!drag || !frame) return;
    const rect = frame.getBoundingClientRect();
    let nextX = drag.startLayout.x +
      ((drag.pointerX - drag.clientX) / Math.max(1, rect.width)) * 100;
    let nextY = drag.startLayout.y +
      ((drag.pointerY - drag.clientY) / Math.max(1, rect.height)) * 100;
    if (Math.abs(nextX - 50) < 1.5) nextX = 50;
    if (Math.abs(nextY - 50) < 1.5) nextY = 50;
    drag.resultLayout = fitImageLayout({
      ...drag.startLayout,
      x: nextX,
      y: nextY,
    });
    applyImageLayout(drag.resultLayout);
    if (verticalGuideRef.current) {
      verticalGuideRef.current.style.left = `${drag.resultLayout.x}%`;
    }
    if (horizontalGuideRef.current) {
      horizontalGuideRef.current.style.top = `${drag.resultLayout.y}%`;
    }
  };

  const paintImageResizeDrag = () => {
    animationFrameRef.current = null;
    const drag = imageResizeDragRef.current;
    const frame = mediaFrameRef.current;
    if (!drag || !frame) return;
    const rect = frame.getBoundingClientRect();
    const deltaWidth =
      ((drag.pointerX - drag.clientX) / Math.max(1, rect.width)) *
      100 *
      drag.directionX;
    drag.resultLayout = fitImageLayout({
      ...drag.startLayout,
      width: drag.startLayout.width + deltaWidth,
    });
    applyImageLayout(drag.resultLayout);
  };

  const schedulePointerPaint = () => {
    if (animationFrameRef.current !== null) return;
    if (positionDragRef.current) {
      animationFrameRef.current = requestAnimationFrame(paintPositionDrag);
    } else if (resizeDragRef.current) {
      animationFrameRef.current = requestAnimationFrame(paintResizeDrag);
    } else if (imagePositionDragRef.current) {
      animationFrameRef.current = requestAnimationFrame(paintImagePositionDrag);
    } else if (imageResizeDragRef.current) {
      animationFrameRef.current = requestAnimationFrame(paintImageResizeDrag);
    }
  };

  const handlePointerMove = (event: globalThis.PointerEvent) => {
    if (positionDragRef.current?.pointerId === event.pointerId) {
      positionDragRef.current.pointerX = event.clientX;
      positionDragRef.current.pointerY = event.clientY;
      schedulePointerPaint();
    }
    if (resizeDragRef.current?.pointerId === event.pointerId) {
      resizeDragRef.current.pointerY = event.clientY;
      schedulePointerPaint();
    }
    if (imagePositionDragRef.current?.pointerId === event.pointerId) {
      imagePositionDragRef.current.pointerX = event.clientX;
      imagePositionDragRef.current.pointerY = event.clientY;
      schedulePointerPaint();
    }
    if (imageResizeDragRef.current?.pointerId === event.pointerId) {
      imageResizeDragRef.current.pointerX = event.clientX;
      schedulePointerPaint();
    }
  };

  const finishPointerInteraction = (event: globalThis.PointerEvent) => {
    if (animationFrameRef.current !== null) {
      cancelAnimationFrame(animationFrameRef.current);
      if (positionDragRef.current) paintPositionDrag();
      else if (resizeDragRef.current) paintResizeDrag();
      else if (imagePositionDragRef.current) paintImagePositionDrag();
      else if (imageResizeDragRef.current) paintImageResizeDrag();
    }
    if (positionDragRef.current?.pointerId === event.pointerId) {
      const drag = positionDragRef.current;
      positionDragRef.current = null;
      setIsDragging(false);
      onOptionsCommit({
        ...options,
        position: "custom",
        pos_x: Math.round(drag.resultX * 10) / 10,
        pos_y: Math.round(drag.resultY * 10) / 10,
      });
    }
    if (resizeDragRef.current?.pointerId === event.pointerId) {
      const drag = resizeDragRef.current;
      resizeDragRef.current = null;
      onOptionsCommit({ ...options, font_size: drag.resultSize });
    }
    if (imagePositionDragRef.current?.pointerId === event.pointerId) {
      const drag = imagePositionDragRef.current;
      imagePositionDragRef.current = null;
      setIsOverlayDragging(false);
      onOverlayLayoutCommit(drag.resultLayout);
    }
    if (imageResizeDragRef.current?.pointerId === event.pointerId) {
      const drag = imageResizeDragRef.current;
      imageResizeDragRef.current = null;
      setIsOverlayDragging(false);
      onOverlayLayoutCommit(drag.resultLayout);
    }
    pointerAbortRef.current?.abort();
    pointerAbortRef.current = null;
  };

  const listenForPointer = () => {
    pointerAbortRef.current?.abort();
    const pointerController = new AbortController();
    pointerAbortRef.current = pointerController;
    window.addEventListener("pointermove", handlePointerMove, {
      passive: true,
      signal: pointerController.signal,
    });
    window.addEventListener("pointerup", finishPointerInteraction, {
      once: true,
      signal: pointerController.signal,
    });
    window.addEventListener("pointercancel", finishPointerInteraction, {
      once: true,
      signal: pointerController.signal,
    });
  };

  const startPositionDrag = (event: PointerEvent<HTMLElement>) => {
    if (previewMode !== "live" || isEditing) return;
    event.preventDefault();
    event.stopPropagation();
    positionDragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      startX: options.pos_x,
      startY: options.pos_y,
      pointerX: event.clientX,
      pointerY: event.clientY,
      resultX: options.pos_x,
      resultY: options.pos_y,
    };
    setIsDragging(true);
    listenForPointer();
  };

  const startResize = (event: PointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    resizeDragRef.current = {
      pointerId: event.pointerId,
      clientY: event.clientY,
      startSize: options.font_size,
      pointerY: event.clientY,
      resultSize: options.font_size,
    };
    listenForPointer();
  };

  const selectImageOverlay = () => {
    setIsOverlaySelected(true);
    onSelectCue(null);
    onSelectMask(null);
  };

  const startImagePositionDrag = (event: PointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const fitted = fitImageLayout(overlayLayout);
    selectImageOverlay();
    imagePositionDragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      pointerX: event.clientX,
      pointerY: event.clientY,
      startLayout: fitted,
      resultLayout: fitted,
    };
    setIsOverlayDragging(true);
    listenForPointer();
  };

  const startImageResize = (
    event: PointerEvent<HTMLElement>,
    directionX: -1 | 1,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    const fitted = fitImageLayout(overlayLayout);
    selectImageOverlay();
    imageResizeDragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      pointerX: event.clientX,
      directionX,
      startLayout: fitted,
      resultLayout: fitted,
    };
    setIsOverlayDragging(true);
    listenForPointer();
  };

  const handleImageKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const movementStep = event.shiftKey ? 5 : 1;
    let candidate: OverlayLayout | null = null;
    if (event.key === "ArrowLeft") {
      candidate = { ...overlayLayout, x: overlayLayout.x - movementStep };
    } else if (event.key === "ArrowRight") {
      candidate = { ...overlayLayout, x: overlayLayout.x + movementStep };
    } else if (event.key === "ArrowUp") {
      candidate = { ...overlayLayout, y: overlayLayout.y - movementStep };
    } else if (event.key === "ArrowDown") {
      candidate = { ...overlayLayout, y: overlayLayout.y + movementStep };
    } else if (event.key === "+" || event.key === "=") {
      candidate = { ...overlayLayout, width: overlayLayout.width + movementStep };
    } else if (event.key === "-" || event.key === "_") {
      candidate = { ...overlayLayout, width: overlayLayout.width - movementStep };
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectImageOverlay();
      return;
    }
    if (!candidate) return;
    event.preventDefault();
    selectImageOverlay();
    onOverlayLayoutCommit(fitImageLayout(candidate));
  };

  const overlayStyle: CSSProperties = {
    fontFamily: options.font_name,
    fontSize: `${previewMetrics.fontSize}px`,
    color: options.font_color,
    fontWeight: options.bold ? 700 : 400,
    fontStyle: options.italic ? "italic" : "normal",
    textDecoration: `${options.underline ? "underline" : ""} ${options.strikethrough ? "line-through" : ""}`.trim() || "none",
    textAlign: options.alignment_type || "center",
    lineHeight: options.line_spacing || 1.2,
    textTransform: options.uppercase ? "uppercase" : "none",
    letterSpacing: `${options.spacing * previewScale}px`,
    left: `${options.pos_x}%`,
    top: `${options.pos_y}%`,
    transform: `translate(${options.alignment_type === "left" ? "0%" : options.alignment_type === "right" ? "-100%" : "-50%"}, -50%)`,
    paintOrder: "stroke fill",
    WebkitTextStroke:
      options.outline_width > 0
        ? `${previewMetrics.outlineWidth}px ${options.outline_color}`
        : "none",
    textShadow:
      options.shadow_width > 0
        ? `${previewMetrics.shadowWidth}px ${previewMetrics.shadowWidth}px ${previewMetrics.shadowBlur}px ${options.shadow_color}`
        : "none",
    backgroundColor: options.bg_enabled
      ? hexToRgba(options.bg_color, options.bg_opacity)
      : "transparent",
    padding: `${previewMetrics.paddingY}px ${previewMetrics.paddingX}px`,
  };
  const interactiveOverlayStyle: CSSProperties =
    libassActive && !isEditing
      ? {
          ...overlayStyle,
          color: "transparent",
          backgroundColor: "transparent",
          WebkitTextStroke: "0 transparent",
          textShadow: "none",
        }
      : overlayStyle;

  return (
    <section className="preview-stage" aria-label="Khung xem trước video">
      <div ref={stageSurfaceRef} className="preview-stage-surface">
        <div
          ref={mediaFrameRef}
          className="preview-media-frame"
          style={{
            width: `${frameBox.width}px`,
            height: `${frameBox.height}px`,
            aspectRatio: `${dimensions.width} / ${dimensions.height}`,
          }}
        >
          {videoUrl ? (
            <video
              ref={onVideoElementChange}
              crossOrigin="anonymous"
              src={videoUrl}
              preload="metadata"
              playsInline
              onClick={onTogglePlay}
              onLoadedMetadata={(event) => {
                setDimensions({
                  width: event.currentTarget.videoWidth || 16,
                  height: event.currentTarget.videoHeight || 9,
                });
              }}
            />
          ) : (
            <div className="preview-empty-state">
              <span>Chưa có video</span>
              <p>Tải video lên để kiểm tra timing và bố cục phụ đề.</p>
            </div>
          )}

          <SubtitleMaskLayer
            video={videoElement}
            masks={subtitleMasks}
            selectedMaskId={selectedMaskId}
            enabled={previewMode === "live"}
            onSelectMask={(maskId) => {
              setIsOverlaySelected(false);
              onSelectCue(null);
              onSelectMask(maskId);
            }}
            onChangeMask={onMaskChange}
            onDeleteMask={onMaskDelete}
          />

          <div
            ref={onLibassHostElementChange}
            className="preview-libass-host"
            aria-hidden="true"
          />

          {previewMode === "live" && overlayImage && (
            <div
              ref={imageOverlayRef}
              className={`preview-image-overlay ${isOverlaySelected && selectedCueId === null ? "is-selected" : ""} ${isOverlayDragging ? "is-dragging" : ""}`}
              style={{
                left: `${overlayLayout.x}%`,
                top: `${overlayLayout.y}%`,
                width: `${overlayLayout.width}%`,
              }}
              role="button"
              tabIndex={0}
              aria-label={`Ảnh phủ ${overlayName || "video"}; dùng phím mũi tên để di chuyển, phím cộng hoặc trừ để đổi kích thước`}
              title="Kéo để di chuyển; kéo góc để đổi kích thước"
              onClick={(event) => {
                event.stopPropagation();
                selectImageOverlay();
              }}
              onPointerDown={startImagePositionDrag}
              onKeyDown={handleImageKeyDown}
            >
              <img
                ref={imageRef}
                src={overlayImage}
                alt={overlayName || "Ảnh phủ video"}
                draggable={false}
                onLoad={() => {
                  setIsOverlaySelected(true);
                  setIsOverlayDragging(false);
                  const fitted = fitImageLayout(overlayLayout);
                  if (
                    fitted.x !== overlayLayout.x ||
                    fitted.y !== overlayLayout.y ||
                    fitted.width !== overlayLayout.width
                  ) {
                    onOverlayLayoutCommit(fitted);
                  }
                }}
              />
              <span
                className="preview-image-resize-handle is-nw"
                aria-hidden="true"
                onPointerDown={(event) => startImageResize(event, -1)}
              />
              <span
                className="preview-image-resize-handle is-ne"
                aria-hidden="true"
                onPointerDown={(event) => startImageResize(event, 1)}
              />
              <span
                className="preview-image-resize-handle is-sw"
                aria-hidden="true"
                onPointerDown={(event) => startImageResize(event, -1)}
              />
              <span
                className="preview-image-resize-handle is-se"
                aria-hidden="true"
                onPointerDown={(event) => startImageResize(event, 1)}
              />
              <output className="preview-image-size-label">{overlayLayout.width.toFixed(1)}%</output>
            </div>
          )}

          {(isDragging || isOverlayDragging) && (
            <>
              <span ref={verticalGuideRef} className="preview-snap-guide is-vertical" />
              <span ref={horizontalGuideRef} className="preview-snap-guide is-horizontal" />
            </>
          )}

          {previewMode === "live" && activeCue && (
            <div
              ref={overlayRef}
              className={`preview-subtitle-overlay ${selectedCueId === activeCue.id ? "is-selected" : ""} ${isDragging ? "is-dragging" : ""}`}
              style={interactiveOverlayStyle}
              role="button"
              tabIndex={0}
              aria-label="Chọn phụ đề đang hiển thị; nhấp đúp để sửa"
              onClick={(event) => {
                event.stopPropagation();
                setIsOverlaySelected(false);
                onSelectMask(null);
                onSelectCue(activeCue.id);
              }}
              onDoubleClick={(event) => {
                event.stopPropagation();
                setIsEditing(true);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") onSelectCue(activeCue.id);
              }}
            >
              <span className="preview-subtitle-hit-area" onPointerDown={startPositionDrag} />
              <span className="preview-resize-handle is-nw" onPointerDown={startResize} />
              <span className="preview-resize-handle is-ne" onPointerDown={startResize} />
              <span className="preview-resize-handle is-sw" onPointerDown={startResize} />
              <span className="preview-resize-handle is-se" onPointerDown={startResize} />
              <span
                className="preview-subtitle-copy"
                contentEditable={isEditing}
                suppressContentEditableWarning
                onPointerDown={(event) => {
                  if (!isEditing) startPositionDrag(event);
                  else event.stopPropagation();
                }}
                onBlur={(event) => {
                  setIsEditing(false);
                  const nextText = event.currentTarget.innerText.trim();
                  if (nextText) onUpdateCueText(activeCue.id, nextText);
                }}
              >
                {options.uppercase ? activeCue.text.toUpperCase() : activeCue.text}
              </span>
              {activeCue.secondary_text && (
                <span className="preview-subtitle-secondary">{activeCue.secondary_text}</span>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
