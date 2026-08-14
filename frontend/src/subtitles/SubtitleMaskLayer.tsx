import {
  useCallback,
  useEffect,
  useRef,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import { normalizeSubtitleMask } from "./masks";
import type { SubtitleMaskRegion } from "./types";

type ResizeHandle = "nw" | "ne" | "sw" | "se";

type MaskDrag = {
  pointerId: number;
  mode: "move" | "resize";
  handle?: ResizeHandle;
  startClientX: number;
  startClientY: number;
  startRegion: SubtitleMaskRegion;
};

type SubtitleMaskLayerProps = {
  video: HTMLVideoElement | null;
  masks: readonly SubtitleMaskRegion[];
  selectedMaskId: string | null;
  enabled: boolean;
  onSelectMask: (maskId: string | null) => void;
  onChangeMask: (maskId: string, patch: Partial<SubtitleMaskRegion>) => void;
  onDeleteMask: (maskId: string) => void;
};

const drawRoundedRect = (
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) => {
  const fittedRadius = Math.max(0, Math.min(radius, width / 2, height / 2));
  context.moveTo(x + fittedRadius, y);
  context.arcTo(x + width, y, x + width, y + height, fittedRadius);
  context.arcTo(x + width, y + height, x, y + height, fittedRadius);
  context.arcTo(x, y + height, x, y, fittedRadius);
  context.arcTo(x, y, x + width, y, fittedRadius);
  context.closePath();
};

const addMaskPath = (
  context: CanvasRenderingContext2D,
  mask: SubtitleMaskRegion,
  width: number,
  height: number,
) => {
  const x = (mask.x / 100) * width;
  const y = (mask.y / 100) * height;
  const maskWidth = (mask.width / 100) * width;
  const maskHeight = (mask.height / 100) * height;
  context.beginPath();
  if (mask.shape === "ellipse") {
    context.ellipse(
      x + maskWidth / 2,
      y + maskHeight / 2,
      maskWidth / 2,
      maskHeight / 2,
      0,
      0,
      Math.PI * 2,
    );
  } else if (mask.shape === "rounded") {
    drawRoundedRect(
      context,
      x,
      y,
      maskWidth,
      maskHeight,
      (Math.min(maskWidth, maskHeight) * mask.cornerRadius) / 100,
    );
  } else {
    context.rect(x, y, maskWidth, maskHeight);
  }
};

const sizeCanvas = (canvas: HTMLCanvasElement, width: number, height: number) => {
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
};

export function SubtitleMaskLayer({
  video,
  masks,
  selectedMaskId,
  enabled,
  onSelectMask,
  onChangeMask,
  onDeleteMask,
}: SubtitleMaskLayerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const effectCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const maskCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const pixelCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const dragRef = useRef<MaskDrag | null>(null);

  const paint = useCallback(() => {
    const canvas = canvasRef.current;
    const root = rootRef.current;
    if (!canvas || !root) return;
    const rect = root.getBoundingClientRect();
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(rect.width * pixelRatio));
    const height = Math.max(1, Math.round(rect.height * pixelRatio));
    sizeCanvas(canvas, width, height);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, width, height);
    if (!enabled || !video || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;

    effectCanvasRef.current ??= document.createElement("canvas");
    maskCanvasRef.current ??= document.createElement("canvas");
    pixelCanvasRef.current ??= document.createElement("canvas");
    const effectCanvas = effectCanvasRef.current;
    const maskCanvas = maskCanvasRef.current;
    const pixelCanvas = pixelCanvasRef.current;
    sizeCanvas(effectCanvas, width, height);
    sizeCanvas(maskCanvas, width, height);
    const effectContext = effectCanvas.getContext("2d");
    const maskContext = maskCanvas.getContext("2d");
    const pixelContext = pixelCanvas.getContext("2d");
    if (!effectContext || !maskContext || !pixelContext) return;

    for (const mask of masks) {
      effectContext.clearRect(0, 0, width, height);
      effectContext.globalCompositeOperation = "source-over";
      effectContext.globalAlpha = 1;
      effectContext.filter = "none";
      if (mask.effect === "solid" || mask.effect === "darken") {
        effectContext.fillStyle = mask.effect === "solid" ? mask.color : "#000000";
        effectContext.fillRect(0, 0, width, height);
      } else if (mask.effect === "pixelate") {
        const blockSize = Math.max(2, Math.round(mask.strength * pixelRatio));
        const smallWidth = Math.max(1, Math.round(width / blockSize));
        const smallHeight = Math.max(1, Math.round(height / blockSize));
        sizeCanvas(pixelCanvas, smallWidth, smallHeight);
        pixelContext.imageSmoothingEnabled = false;
        pixelContext.clearRect(0, 0, smallWidth, smallHeight);
        pixelContext.drawImage(video, 0, 0, smallWidth, smallHeight);
        effectContext.imageSmoothingEnabled = false;
        effectContext.drawImage(pixelCanvas, 0, 0, width, height);
        effectContext.imageSmoothingEnabled = true;
      } else {
        effectContext.filter = `blur(${Math.max(1, mask.strength * 1.5 * pixelRatio)}px)`;
        effectContext.drawImage(video, 0, 0, width, height);
        effectContext.filter = "none";
      }

      maskContext.clearRect(0, 0, width, height);
      maskContext.filter = mask.feather > 0
        ? `blur(${mask.feather * pixelRatio}px)`
        : "none";
      maskContext.fillStyle = "#ffffff";
      addMaskPath(maskContext, mask, width, height);
      maskContext.fill();
      maskContext.filter = "none";

      effectContext.globalCompositeOperation = "destination-in";
      effectContext.drawImage(maskCanvas, 0, 0);
      effectContext.globalCompositeOperation = "source-over";
      context.save();
      // A partially transparent blur leaves a sharp copy of the old subtitle
      // underneath it. Blur and pixelate therefore replace the source region;
      // opacity remains meaningful only for solid and darken effects.
      context.globalAlpha = mask.effect === "blur" || mask.effect === "pixelate"
        ? 1
        : mask.opacity;
      context.drawImage(effectCanvas, 0, 0);
      context.restore();
    }
  }, [enabled, masks, video]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const observer = new ResizeObserver(paint);
    observer.observe(root);
    paint();
    return () => observer.disconnect();
  }, [paint]);

  useEffect(() => {
    if (!video || !enabled) return undefined;
    let stopped = false;
    let frameHandle = 0;
    let animationHandle = 0;
    const drawNextFrame = () => {
      if (stopped) return;
      paint();
      if ("requestVideoFrameCallback" in video) {
        frameHandle = video.requestVideoFrameCallback(drawNextFrame);
      } else {
        animationHandle = requestAnimationFrame(drawNextFrame);
      }
    };
    drawNextFrame();
    return () => {
      stopped = true;
      if (frameHandle && "cancelVideoFrameCallback" in video) {
        video.cancelVideoFrameCallback(frameHandle);
      }
      if (animationHandle) cancelAnimationFrame(animationHandle);
    };
  }, [enabled, paint, video]);

  const updateFromPointer = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const root = rootRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !root) return;
    const rect = root.getBoundingClientRect();
    const deltaX = ((event.clientX - drag.startClientX) / Math.max(1, rect.width)) * 100;
    const deltaY = ((event.clientY - drag.startClientY) / Math.max(1, rect.height)) * 100;
    const start = drag.startRegion;
    let candidate: SubtitleMaskRegion;
    if (drag.mode === "move") {
      candidate = {
        ...start,
        x: start.shape === "band" ? 0 : start.x + deltaX,
        y: start.y + deltaY,
      };
    } else {
      const west = drag.handle?.includes("w");
      const north = drag.handle?.includes("n");
      candidate = {
        ...start,
        x: start.shape === "band" ? 0 : west ? start.x + deltaX : start.x,
        y: north ? start.y + deltaY : start.y,
        width: start.shape === "band"
          ? 100
          : west
            ? start.width - deltaX
            : start.width + deltaX,
        height: north ? start.height - deltaY : start.height + deltaY,
      };
    }
    onChangeMask(start.id, normalizeSubtitleMask(candidate));
  };

  const startDrag = (
    event: PointerEvent<HTMLDivElement | HTMLSpanElement>,
    mask: SubtitleMaskRegion,
    mode: MaskDrag["mode"],
    handle?: ResizeHandle,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    onSelectMask(mask.id);
    dragRef.current = {
      pointerId: event.pointerId,
      mode,
      handle,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startRegion: mask,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handleKeyDown = (
    event: KeyboardEvent<HTMLDivElement>,
    mask: SubtitleMaskRegion,
  ) => {
    const step = event.shiftKey ? 2 : 0.5;
    let patch: Partial<SubtitleMaskRegion> | null = null;
    if (event.key === "ArrowLeft") patch = { x: mask.x - step };
    else if (event.key === "ArrowRight") patch = { x: mask.x + step };
    else if (event.key === "ArrowUp") patch = { y: mask.y - step };
    else if (event.key === "ArrowDown") patch = { y: mask.y + step };
    else if (event.key === "+" || event.key === "=") {
      patch = mask.shape === "band"
        ? { height: mask.height + step }
        : { width: mask.width + step, height: mask.height + step };
    } else if (event.key === "-" || event.key === "_") {
      patch = mask.shape === "band"
        ? { height: mask.height - step }
        : { width: mask.width - step, height: mask.height - step };
    } else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onDeleteMask(mask.id);
      return;
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelectMask(mask.id);
      return;
    }
    if (!patch) return;
    event.preventDefault();
    onSelectMask(mask.id);
    onChangeMask(mask.id, patch);
  };

  return (
    <div
      ref={rootRef}
      className="subtitle-mask-layer"
      onClick={(event) => event.stopPropagation()}
    >
      <canvas ref={canvasRef} className="subtitle-mask-canvas" aria-hidden="true" />
      {enabled && masks.map((mask, index) => (
        <div
          key={mask.id}
          className={`subtitle-mask-region shape-${mask.shape} ${selectedMaskId === mask.id ? "is-selected" : ""}`}
          style={{
            left: `${mask.x}%`,
            top: `${mask.y}%`,
            width: `${mask.width}%`,
            height: `${mask.height}%`,
          }}
          role="button"
          tabIndex={0}
          aria-label={`Vùng che phụ đề cũ ${index + 1}; dùng phím mũi tên để di chuyển, phím cộng trừ để đổi kích thước`}
          onClick={(event) => {
            event.stopPropagation();
            onSelectMask(mask.id);
          }}
          onPointerDown={(event) => startDrag(event, mask, "move")}
          onPointerMove={updateFromPointer}
          onPointerUp={(event) => {
            if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
          }}
          onPointerCancel={() => { dragRef.current = null; }}
          onKeyDown={(event) => handleKeyDown(event, mask)}
        >
          <span className="subtitle-mask-shape-outline" aria-hidden="true" />
          {(["nw", "ne", "sw", "se"] as const).map((handle) => (
            <span
              key={handle}
              className={`subtitle-mask-resize-handle is-${handle}`}
              aria-hidden="true"
              onPointerDown={(event) => startDrag(event, mask, "resize", handle)}
              onPointerMove={updateFromPointer}
              onPointerUp={(event) => {
                if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
              }}
              onPointerCancel={() => { dragRef.current = null; }}
            />
          ))}
          {selectedMaskId === mask.id && (
            <output className="subtitle-mask-size-label">
              {mask.width.toFixed(1)}% × {mask.height.toFixed(1)}%
            </output>
          )}
        </div>
      ))}
    </div>
  );
}
