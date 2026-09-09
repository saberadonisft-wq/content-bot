import { AudioLines, Film, Image, Subtitles } from "lucide-react";
import { VoiceTrack } from "../voiceover/VoiceTrack";
import type { VoiceController } from "../voiceover/useVoiceover";
import {
  memo,
  startTransition,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import type { PlaybackClockStore } from "./playback-store";
import {
  SubtitleIntervalIndex,
  findOverlapCueIds,
  formatCompactTimecode,
  formatTimecode,
  keyboardTargetMs,
  rulerStepMs,
  snapMsToFrame,
  sortCues,
} from "./time";
import type { FrameTiming, SubtitleCueV2, VideoClip } from "./types";
import { useVideoThumbnailSprite } from "./useVideoThumbnailSprite";
import { useActiveCueId } from "./useActiveCue";
import { deletedVideoRanges } from "./video-clips";

const MIN_CUE_VISUAL_WIDTH = 14;
const TIMELINE_OVERSCAN_FACTOR = 0.1;
const SNAP_THRESHOLD_PX = 8;

type CueDragMode = "move" | "trim-start" | "trim-end";
type TimelineSnapPoint = {
  milliseconds: number;
  label: "Cue" | "Playhead" | "Project";
  cueId?: string;
};
type TimelineSnap = {
  targetMs: number;
  deltaMs: number;
  label: TimelineSnapPoint["label"] | "Frame";
};

const nearestSnap = (
  milliseconds: number,
  cueId: string,
  points: readonly TimelineSnapPoint[],
  frameTiming: FrameTiming,
  thresholdMs: number,
  playheadMs: number,
): TimelineSnap | null => {
  let best: TimelineSnap | null = null;
  for (const point of points) {
    if (point.cueId === cueId) continue;
    const deltaMs = point.milliseconds - milliseconds;
    if (Math.abs(deltaMs) > thresholdMs) continue;
    if (!best || Math.abs(deltaMs) < Math.abs(best.deltaMs)) {
      best = { targetMs: point.milliseconds, deltaMs, label: point.label };
    }
  }
  const playheadDelta = playheadMs - milliseconds;
  if (
    Math.abs(playheadDelta) <= thresholdMs &&
    (!best || Math.abs(playheadDelta) < Math.abs(best.deltaMs))
  ) {
    best = { targetMs: playheadMs, deltaMs: playheadDelta, label: "Playhead" };
  }
  const frameTarget = snapMsToFrame(milliseconds, frameTiming);
  const frameDelta = frameTarget - milliseconds;
  if (
    Math.abs(frameDelta) <= thresholdMs &&
    (!best || Math.abs(frameDelta) < Math.abs(best.deltaMs))
  ) {
    best = { targetMs: frameTarget, deltaMs: frameDelta, label: "Frame" };
  }
  return best;
};

type SubtitleCueBlockProps = {
  cue: SubtitleCueV2;
  pixelsPerSecond: number;
  durationMs: number;
  selected: boolean;
  active: boolean;
  warning: boolean;
  snapEnabled: boolean;
  snapPoints: readonly TimelineSnapPoint[];
  frameTiming: FrameTiming;
  clock: PlaybackClockStore;
  snapGuideRef: RefObject<HTMLDivElement | null>;
  onSelect: (cueId: string) => void;
  onSeek: (milliseconds: number) => void;
  onCommit: (cueId: string, startMs: number, endMs: number) => void;
};

const SubtitleCueBlock = memo(function SubtitleCueBlock({
  cue,
  pixelsPerSecond,
  durationMs,
  selected,
  active,
  warning,
  snapEnabled,
  snapPoints,
  frameTiming,
  clock,
  snapGuideRef,
  onSelect,
  onSeek,
  onCommit,
}: SubtitleCueBlockProps) {
  const blockRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    mode: CueDragMode;
    pointerId: number;
    clientX: number;
    startMs: number;
    endMs: number;
    nextStartMs: number;
    nextEndMs: number;
    moved: boolean;
    snap: TimelineSnap | null;
  } | null>(null);
  const frameRef = useRef<number | null>(null);
  const pendingClientXRef = useRef(0);
  const pointerAbortRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      pointerAbortRef.current?.abort();
    },
    [],
  );

  const paintDragPreview = () => {
    frameRef.current = null;
    const drag = dragRef.current;
    const block = blockRef.current;
    if (!drag || !block) return;
    const deltaMs = Math.round(
      ((pendingClientXRef.current - drag.clientX) / pixelsPerSecond) * 1000,
    );
    const originalDuration = drag.endMs - drag.startMs;
    let nextStartMs = drag.startMs;
    let nextEndMs = drag.endMs;

    if (drag.mode === "move") {
      nextStartMs = Math.max(
        0,
        Math.min(Math.max(0, durationMs - originalDuration), drag.startMs + deltaMs),
      );
      nextEndMs = nextStartMs + originalDuration;
    } else if (drag.mode === "trim-start") {
      nextStartMs = Math.max(0, Math.min(drag.endMs - 1, drag.startMs + deltaMs));
    } else {
      nextEndMs = Math.max(
        drag.startMs + 1,
        Math.min(durationMs || Number.MAX_SAFE_INTEGER, drag.endMs + deltaMs),
      );
    }

    let snap: TimelineSnap | null = null;
    if (snapEnabled) {
      const thresholdMs = Math.max(1, Math.round((SNAP_THRESHOLD_PX / pixelsPerSecond) * 1000));
      const playheadMs = clock.getSnapshot().currentMs;
      if (drag.mode === "move") {
        const options = [
          nearestSnap(nextStartMs, cue.id, snapPoints, frameTiming, thresholdMs, playheadMs),
          nearestSnap(nextEndMs, cue.id, snapPoints, frameTiming, thresholdMs, playheadMs),
        ]
          .filter((candidate): candidate is TimelineSnap => Boolean(candidate))
          .filter(
            (candidate) =>
              nextStartMs + candidate.deltaMs >= 0 &&
              nextEndMs + candidate.deltaMs <= durationMs,
          )
          .sort((left, right) => Math.abs(left.deltaMs) - Math.abs(right.deltaMs));
        snap = options[0] ?? null;
        if (snap) {
          nextStartMs += snap.deltaMs;
          nextEndMs += snap.deltaMs;
        }
      } else if (drag.mode === "trim-start") {
        snap = nearestSnap(nextStartMs, cue.id, snapPoints, frameTiming, thresholdMs, playheadMs);
        if (snap && snap.targetMs < nextEndMs) nextStartMs = snap.targetMs;
        else snap = null;
      } else {
        snap = nearestSnap(nextEndMs, cue.id, snapPoints, frameTiming, thresholdMs, playheadMs);
        if (snap && snap.targetMs > nextStartMs && snap.targetMs <= durationMs) {
          nextEndMs = snap.targetMs;
        } else snap = null;
      }
    }

    drag.nextStartMs = Math.round(nextStartMs);
    drag.nextEndMs = Math.round(nextEndMs);
    drag.moved ||= Math.abs(pendingClientXRef.current - drag.clientX) > 2;
    drag.snap = snap;

    const originalLeft = (drag.startMs / 1000) * pixelsPerSecond;
    const nextLeft = (drag.nextStartMs / 1000) * pixelsPerSecond;
    const originalWidth = Math.max(
      MIN_CUE_VISUAL_WIDTH,
      (originalDuration / 1000) * pixelsPerSecond,
    );
    const nextWidth = Math.max(
      MIN_CUE_VISUAL_WIDTH,
      ((drag.nextEndMs - drag.nextStartMs) / 1000) * pixelsPerSecond,
    );
    block.style.transformOrigin = drag.mode === "trim-start" ? "right center" : "left center";
    block.style.transform = `translateX(${nextLeft}px) scaleX(${nextWidth / originalWidth})`;
    block.dataset.dragTime = `${formatTimecode(drag.nextStartMs)} → ${formatTimecode(drag.nextEndMs)}`;
    const snapGuide = snapGuideRef.current;
    if (snapGuide) {
      snapGuide.hidden = !snap;
      if (snap) {
        snapGuide.style.transform = `translateX(${(snap.targetMs / 1000) * pixelsPerSecond}px)`;
        const label = snapGuide.firstElementChild;
        if (label) {
          const sign = snap.deltaMs > 0 ? "+" : "";
          label.textContent = `${snap.label} · ${sign}${snap.deltaMs} ms`;
        }
      }
    }
    if (drag.mode === "move" && originalLeft === nextLeft) block.style.transformOrigin = "left center";
  };

  const handlePointerMove = (event: PointerEvent) => {
    if (!dragRef.current || event.pointerId !== dragRef.current.pointerId) return;
    pendingClientXRef.current = event.clientX;
    if (frameRef.current === null) frameRef.current = requestAnimationFrame(paintDragPreview);
  };

  const finishDrag = (event: PointerEvent) => {
    const drag = dragRef.current;
    const block = blockRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      paintDragPreview();
    }
    pointerAbortRef.current?.abort();
    pointerAbortRef.current = null;
    dragRef.current = null;
    if (block) {
      block.removeAttribute("data-dragging");
      delete block.dataset.dragTime;
    }
    if (snapGuideRef.current) snapGuideRef.current.hidden = true;
    if (drag.moved) {
      const { nextStartMs, nextEndMs } = drag;
      // Let the final pointer event and compositor paint finish first. React can
      // then reconcile the one changed cue concurrently without freezing drag.
      window.setTimeout(() => {
        startTransition(() => onCommit(cue.id, nextStartMs, nextEndMs));
      }, 0);
    }
    else if (drag.mode === "move") onSeek(cue.start_ms);
  };

  const startDrag = (
    event: ReactPointerEvent<HTMLElement>,
    mode: CueDragMode,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    onSelect(cue.id);
    blockRef.current?.setAttribute("data-dragging", "");
    if (snapGuideRef.current) snapGuideRef.current.hidden = true;
    pendingClientXRef.current = event.clientX;
    dragRef.current = {
      mode,
      pointerId: event.pointerId,
      clientX: event.clientX,
      startMs: cue.start_ms,
      endMs: cue.end_ms,
      nextStartMs: cue.start_ms,
      nextEndMs: cue.end_ms,
      moved: false,
      snap: null,
    };
    pointerAbortRef.current?.abort();
    const pointerController = new AbortController();
    pointerAbortRef.current = pointerController;
    window.addEventListener("pointermove", handlePointerMove, {
      passive: true,
      signal: pointerController.signal,
    });
    window.addEventListener("pointerup", finishDrag, {
      once: true,
      signal: pointerController.signal,
    });
    window.addEventListener("pointercancel", finishDrag, {
      once: true,
      signal: pointerController.signal,
    });
  };

  const width = Math.max(
    MIN_CUE_VISUAL_WIDTH,
    ((cue.end_ms - cue.start_ms) / 1000) * pixelsPerSecond,
  );
  const left = (cue.start_ms / 1000) * pixelsPerSecond;

  return (
    <div
      ref={blockRef}
      className={`subtitle-timeline-cue ${selected ? "is-selected" : ""} ${active ? "is-active" : ""} ${warning ? "has-warning" : ""}`}
      style={{ width, transform: `translateX(${left}px)` }}
      role="button"
      tabIndex={0}
      aria-label={`${cue.text}, ${formatTimecode(cue.start_ms)} đến ${formatTimecode(cue.end_ms)}`}
      aria-pressed={selected}
      title={`${cue.text}\n${formatTimecode(cue.start_ms)} → ${formatTimecode(cue.end_ms)}`}
      onPointerDown={(event) => startDrag(event, "move")}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          onSelect(cue.id);
          onSeek(cue.start_ms);
        }
      }}
    >
      <span
        className="subtitle-cue-trim-handle is-start"
        role="presentation"
        onPointerDown={(event) => startDrag(event, "trim-start")}
      />
      <span className="subtitle-cue-copy">{cue.text}</span>
      {warning && <span className="subtitle-cue-warning" aria-label="Cue chồng lấn">!</span>}
      <span
        className="subtitle-cue-trim-handle is-end"
        role="presentation"
        onPointerDown={(event) => startDrag(event, "trim-end")}
      />
    </div>
  );
}, (previous, next) =>
  previous.cue === next.cue &&
  previous.pixelsPerSecond === next.pixelsPerSecond &&
  previous.durationMs === next.durationMs &&
  previous.selected === next.selected &&
  previous.active === next.active &&
  previous.warning === next.warning &&
  previous.snapEnabled === next.snapEnabled &&
  previous.snapPoints === next.snapPoints &&
  previous.frameTiming === next.frameTiming &&
  previous.clock === next.clock,
);

function TimelinePlayhead({
  clock,
  pixelsPerSecond,
  viewportRef,
}: {
  clock: PlaybackClockStore;
  pixelsPerSecond: number;
  viewportRef: RefObject<HTMLDivElement | null>;
}) {
  const playheadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sync = (currentMs: number) => {
      const x = (currentMs / 1000) * pixelsPerSecond;
      if (playheadRef.current) {
        playheadRef.current.style.transform = `translateX(${x}px)`;
      }
      const viewport = viewportRef.current;
      if (!viewport || !clock.getSnapshot().isPlaying) return;
      const localX = x - viewport.scrollLeft;
      if (localX > viewport.clientWidth * 0.86 || localX < viewport.clientWidth * 0.06) {
        viewport.scrollLeft = Math.max(0, x - viewport.clientWidth * 0.2);
      }
    };
    sync(clock.getSnapshot().currentMs);
    return clock.subscribeFrame(sync);
  }, [clock, pixelsPerSecond, viewportRef]);

  return (
    <div ref={playheadRef} className="studio-playhead" aria-hidden="true">
      <span className="studio-playhead-head" />
      <span className="studio-playhead-line" />
    </div>
  );
}

export type SubtitleTimelineProps = {
  voice?: VoiceController;
  onOpenVoice?: () => void;
  cues: readonly SubtitleCueV2[];
  selectedCueId: string | null;
  durationMs: number;
  frameTiming: FrameTiming;
  pixelsPerSecond: number;
  clock: PlaybackClockStore;
  videoUrl: string | null;
  thumbnailCacheKey: string | null;
  hasOverlayTrack: boolean;
  snapEnabled: boolean;
  videoClips: readonly VideoClip[];
  selectedVideoClipId: string | null;
  onSelectCue: (cueId: string) => void;
  onSeek: (milliseconds: number) => void;
  onTogglePlay: () => void;
  onCueTimingCommit: (cueId: string, startMs: number, endMs: number) => void;
  onSelectVideoClip: (clipId: string) => void;
  onSplitVideo: () => void;
  onDeleteVideoClip: () => void;
};

export function SubtitleTimeline({
  voice,
  onOpenVoice,
  cues,
  selectedCueId,
  durationMs,
  frameTiming,
  pixelsPerSecond,
  clock,
  videoUrl,
  thumbnailCacheKey,
  hasOverlayTrack,
  snapEnabled,
  videoClips,
  selectedVideoClipId,
  onSelectCue,
  onSeek,
  onTogglePlay,
  onCueTimingCommit,
  onSelectVideoClip,
  onSplitVideo,
  onDeleteVideoClip,
}: SubtitleTimelineProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const snapGuideRef = useRef<HTMLDivElement>(null);
  const previousPixelsPerSecondRef = useRef(pixelsPerSecond);
  const [scrollLeft, setScrollLeft] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(0);
  const scrollFrameRef = useRef<number | null>(null);
  const rulerScrubFrameRef = useRef<number | null>(null);
  const pendingRulerClientXRef = useRef(0);
  const sortedCues = useMemo(() => sortCues(cues), [cues]);
  const intervalIndex = useMemo(
    () => new SubtitleIntervalIndex(sortedCues, true),
    [sortedCues],
  );
  const activeCueId = useActiveCueId(clock, intervalIndex);
  const warningCueIds = useMemo(() => findOverlapCueIds(sortedCues), [sortedCues]);
  const sprite = useVideoThumbnailSprite(videoUrl, thumbnailCacheKey);
  const cueTimelineEndMs = useMemo(
    () => sortedCues.reduce((maximum, cue) => Math.max(maximum, cue.end_ms), 0),
    [sortedCues],
  );
  const timelineDurationMs = Math.max(durationMs, cueTimelineEndMs, 1);
  const deletedRanges = useMemo(
    () => deletedVideoRanges(videoClips, durationMs),
    [durationMs, videoClips],
  );
  const snapPoints = useMemo<TimelineSnapPoint[]>(
    () => [
      { milliseconds: 0, label: "Project" },
      { milliseconds: timelineDurationMs, label: "Project" },
      ...sortedCues.flatMap((cue) => [
        { milliseconds: cue.start_ms, label: "Cue" as const, cueId: cue.id },
        { milliseconds: cue.end_ms, label: "Cue" as const, cueId: cue.id },
      ]),
    ],
    [sortedCues, timelineDurationMs],
  );

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;
    const observer = new ResizeObserver(([entry]) => setViewportWidth(entry.contentRect.width));
    observer.observe(viewport);
    setViewportWidth(viewport.clientWidth);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    const previous = previousPixelsPerSecondRef.current;
    if (!viewport || previous === pixelsPerSecond) return;
    const anchorMs = clock.getSnapshot().currentMs;
    const previousAnchorX = (anchorMs / 1000) * previous;
    const localAnchorX = previousAnchorX - viewport.scrollLeft;
    const nextAnchorX = (anchorMs / 1000) * pixelsPerSecond;
    viewport.scrollLeft = Math.max(0, nextAnchorX - localAnchorX);
    previousPixelsPerSecondRef.current = pixelsPerSecond;
  }, [clock, pixelsPerSecond]);

  useEffect(
    () => () => {
      if (scrollFrameRef.current !== null) cancelAnimationFrame(scrollFrameRef.current);
      if (rulerScrubFrameRef.current !== null) cancelAnimationFrame(rulerScrubFrameRef.current);
    },
    [],
  );

  const contentWidth = Math.max(
    viewportWidth,
    (timelineDurationMs / 1000) * pixelsPerSecond,
  );
  const overscanPx = viewportWidth * TIMELINE_OVERSCAN_FACTOR;
  const visibleStartMs = Math.max(
    0,
    ((scrollLeft - overscanPx) / pixelsPerSecond) * 1000,
  );
  const visibleEndMs = Math.min(
    timelineDurationMs,
    ((scrollLeft + viewportWidth + overscanPx) / pixelsPerSecond) * 1000,
  );
  const visibleCues = intervalIndex.inRange(visibleStartMs, visibleEndMs);
  const tickStepMs = rulerStepMs(pixelsPerSecond);
  const firstTickMs = Math.max(
    0,
    Math.floor(visibleStartMs / tickStepMs) * tickStepMs,
  );
  const rulerTicks: number[] = [];
  for (
    let milliseconds = firstTickMs;
    milliseconds <= visibleEndMs + tickStepMs;
    milliseconds += tickStepMs
  ) {
    rulerTicks.push(milliseconds);
  }

  const visibleSpriteFrames = useMemo(() => {
    if (!sprite || durationMs <= 0 || visibleStartMs >= durationMs) return [];
    const videoTrackWidth = (durationMs / 1000) * pixelsPerSecond;
    const segmentWidth = videoTrackWidth / sprite.frameCount;
    const first = Math.max(0, Math.floor(visibleStartMs / durationMs * sprite.frameCount));
    const last = Math.min(
      sprite.frameCount - 1,
      Math.ceil(Math.min(visibleEndMs, durationMs) / durationMs * sprite.frameCount),
    );
    return Array.from({ length: Math.max(0, last - first + 1) }, (_, offset) => ({
      index: first + offset,
      left: (first + offset) * segmentWidth,
      width: segmentWidth,
    }));
  }, [durationMs, pixelsPerSecond, sprite, visibleEndMs, visibleStartMs]);

  const seekFromRuler = (clientX: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    onSeek(
      Math.max(
        0,
        Math.min(
          timelineDurationMs,
          Math.round(((clientX - rect.left + viewport.scrollLeft) / pixelsPerSecond) * 1000),
        ),
      ),
    );
  };

  const handleKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    const snapshot = clock.getSnapshot();
    if (event.code === "Space") {
      event.preventDefault();
      onTogglePlay();
      return;
    }
    if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      onSplitVideo();
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      onDeleteVideoClip();
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      onSeek(event.key === "Home" ? 0 : timelineDurationMs);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    onSeek(
      Math.max(
        0,
        Math.min(
          durationMs,
          keyboardTargetMs(
            snapshot.currentMs,
            direction,
            event.nativeEvent,
            frameTiming,
          ),
        ),
      ),
    );
  };

  return (
    <section className={`subtitle-timeline${voice ? ' has-voice-track' : ''}`} aria-label="Timeline phụ đề">
      <div className="subtitle-timeline-rail" aria-label="Các lớp timeline">
        <div className="subtitle-timeline-rail-spacer" />
        {voice && <button type="button" className="timeline-track-button voice-track-label" onClick={onOpenVoice} aria-label="Mở công cụ giọng đọc AI" title="Giọng đọc AI"><AudioLines size={17} /><span>AI</span></button>}
        <button type="button" className="timeline-track-button is-active" aria-label="Lớp phụ đề" title="Phụ đề">
          <Subtitles size={17} />
          <span>CC</span>
        </button>
        {hasOverlayTrack && (
          <button type="button" className="timeline-track-button is-active" aria-label="Lớp ảnh" title="Ảnh phủ">
            <Image size={17} />
          </button>
        )}
        <button type="button" className="timeline-track-button" aria-label="Lớp video" title="Video">
          <Film size={17} />
        </button>
      </div>

      <div
        ref={viewportRef}
        className="subtitle-timeline-viewport"
        tabIndex={0}
        onKeyDown={handleKeyboard}
        onScroll={(event) => {
          const nextScroll = event.currentTarget.scrollLeft;
          if (scrollFrameRef.current !== null) return;
          scrollFrameRef.current = requestAnimationFrame(() => {
            scrollFrameRef.current = null;
            setScrollLeft(nextScroll);
          });
        }}
      >
        <div className="subtitle-timeline-content" style={{ width: contentWidth }}>
          <div
            className="subtitle-ruler"
            onPointerDown={(event) => {
              if (event.button !== 0) return;
              event.currentTarget.setPointerCapture(event.pointerId);
              seekFromRuler(event.clientX);
            }}
            onPointerMove={(event) => {
              if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
              pendingRulerClientXRef.current = event.clientX;
              if (rulerScrubFrameRef.current !== null) return;
              rulerScrubFrameRef.current = requestAnimationFrame(() => {
                rulerScrubFrameRef.current = null;
                seekFromRuler(pendingRulerClientXRef.current);
              });
            }}
            onPointerUp={(event) => {
              if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
              if (rulerScrubFrameRef.current !== null) {
                cancelAnimationFrame(rulerScrubFrameRef.current);
                rulerScrubFrameRef.current = null;
              }
              seekFromRuler(event.clientX);
              event.currentTarget.releasePointerCapture(event.pointerId);
            }}
            onPointerCancel={(event) => {
              if (rulerScrubFrameRef.current !== null) {
                cancelAnimationFrame(rulerScrubFrameRef.current);
                rulerScrubFrameRef.current = null;
              }
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId);
              }
            }}
          >
            {rulerTicks.map((milliseconds) => (
              <span
                key={milliseconds}
                className="subtitle-ruler-tick"
                style={{ transform: `translateX(${(milliseconds / 1000) * pixelsPerSecond}px)` }}
              >
                <span>{formatCompactTimecode(milliseconds)}</span>
              </span>
            ))}
          </div>

          <TimelinePlayhead
            clock={clock}
            pixelsPerSecond={pixelsPerSecond}
            viewportRef={viewportRef}
          />

          <div ref={snapGuideRef} className="timeline-snap-guide" hidden>
            <span />
          </div>

          {voice && <VoiceTrack clips={voice.document?.clips ?? []} job={voice.job} selectedId={voice.selectedId}
            pixelsPerSecond={pixelsPerSecond} startMs={visibleStartMs} endMs={visibleEndMs}
            onSelect={id => {
              voice.setSelectedId(id); onOpenVoice?.();
              const clip = voice.document?.clips.find(c => c.id === id);
              if (clip?.source_cue_ids[0]) onSelectCue(clip.source_cue_ids[0]);
            }}
            onMove={(id, offset_ms) => voice.edit(doc => ({ ...doc, clips: doc.clips.map(c => c.id === id ? { ...c, offset_ms } : c) }))} />}
          <div className="subtitle-track-row">
            {visibleCues.map((cue) => (
              <SubtitleCueBlock
                key={cue.id}
                cue={cue}
                pixelsPerSecond={pixelsPerSecond}
                durationMs={timelineDurationMs}
                selected={cue.id === selectedCueId}
                active={cue.id === activeCueId}
                warning={warningCueIds.has(cue.id)}
                snapEnabled={snapEnabled}
                snapPoints={snapPoints}
                frameTiming={frameTiming}
                clock={clock}
                snapGuideRef={snapGuideRef}
                onSelect={onSelectCue}
                onSeek={onSeek}
                onCommit={onCueTimingCommit}
              />
            ))}
            {cues.length === 0 && <span className="subtitle-track-empty">Chưa có cue phụ đề</span>}
          </div>

          {hasOverlayTrack && <div className="overlay-track-row"><span>Ảnh phủ</span></div>}

          <div className="video-track-row">
            {sprite ? (
              visibleSpriteFrames.map((frame) => (
                <span
                  key={frame.index}
                  className="video-sprite-frame"
                  style={{
                    left: frame.left,
                    width: frame.width,
                    backgroundImage: `url(${sprite.url})`,
                    backgroundPosition: `${(frame.index / Math.max(1, sprite.frameCount - 1)) * 100}% center`,
                    backgroundSize: `${sprite.frameCount * 100}% 100%`,
                  }}
                />
              ))
            ) : (
              <span className="video-track-placeholder">
                {videoUrl ? "Đang tạo sprite thumbnail…" : "Kéo video vào để bắt đầu"}
              </span>
            )}
            {deletedRanges.map((range) => (
              <span
                key={`${range.start_ms}-${range.end_ms}`}
                className="video-deleted-range"
                style={{
                  width: ((range.end_ms - range.start_ms) / 1000) * pixelsPerSecond,
                  transform: `translateX(${(range.start_ms / 1000) * pixelsPerSecond}px)`,
                }}
                aria-label={`${formatTimecode(range.start_ms)} đến ${formatTimecode(range.end_ms)} đã xóa`}
              >
                <span>Đã xóa</span>
              </span>
            ))}
            {videoClips.map((clip, index) => (
              <button
                key={clip.id}
                type="button"
                className={`video-timeline-clip ${clip.id === selectedVideoClipId ? "is-selected" : ""}`}
                style={{
                  width: Math.max(2, ((clip.end_ms - clip.start_ms) / 1000) * pixelsPerSecond),
                  transform: `translateX(${(clip.start_ms / 1000) * pixelsPerSecond}px)`,
                }}
                aria-pressed={clip.id === selectedVideoClipId}
                aria-label={`Đoạn video ${index + 1}, ${formatTimecode(clip.start_ms)} đến ${formatTimecode(clip.end_ms)}`}
                title={`Đoạn ${index + 1}\n${formatTimecode(clip.start_ms)} → ${formatTimecode(clip.end_ms)}`}
                onFocus={() => onSelectVideoClip(clip.id)}
                onClick={(event) => {
                  onSelectVideoClip(clip.id);
                  const rect = event.currentTarget.getBoundingClientRect();
                  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
                  onSeek(clip.start_ms + ratio * (clip.end_ms - clip.start_ms));
                }}
                onKeyDown={(event) => {
                  if (event.key === "Delete" || event.key === "Backspace") {
                    event.preventDefault();
                    onDeleteVideoClip();
                  } else if (event.key.toLowerCase() === "s") {
                    event.preventDefault();
                    onSplitVideo();
                  }
                }}
              >
                <span>Đoạn {index + 1}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
