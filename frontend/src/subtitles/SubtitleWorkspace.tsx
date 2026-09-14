import {
  ChevronLeft,
  ChevronRight,
  Magnet,
  Maximize2,
  Pause,
  Play,
  Scissors,
  Trash2,
  Undo2,
  Volume2,
  VolumeX,
} from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { SubtitleBurnOptions } from "../api";
import { LibassPreview } from "./LibassPreview";
import { PlaybackClock } from "./PlaybackClock";
import { usePlaybackDuration, usePlaybackSnapshot } from "./playback-hooks";
import { PreviewStage } from "./PreviewStage";
import { SubtitleTimeline } from "./SubtitleTimeline";
import { PlaybackClockStore } from "./playback-store";
import { formatCompactTimecode, formatTimecode } from "./time";
import {
  DEFAULT_FRAME_TIMING,
  type MediaMetadata,
  type OverlayLayout,
  type PreviewMode,
  type SubtitleCueV2,
  type SubtitleMaskRegion,
  type VideoClip,
} from "./types";
import {
  findVideoClipAt,
  keptVideoDurationMs,
  nearestKeptVideoTimeMs,
} from "./video-clips";

type SubtitleTransportProps = {
  clock: PlaybackClockStore;
  hasVideo: boolean;
  previewMode: PreviewMode;
  hasRenderedVideo: boolean;
  onSeek: (milliseconds: number) => void;
  onTogglePlay: () => void;
  isMuted: boolean;
  volume: number;
  onToggleMute: () => void;
  onPreviewModeChange: (mode: PreviewMode) => void;
};

function SubtitleTransport({
  clock,
  hasVideo,
  previewMode,
  hasRenderedVideo,
  onSeek,
  onTogglePlay,
  isMuted,
  volume,
  onToggleMute,
  onPreviewModeChange,
}: SubtitleTransportProps) {
  const snapshot = usePlaybackSnapshot(clock);
  return (
    <section className="subtitle-transport" aria-label="Điều khiển phát video">
      <div className="transport-mode-switch" aria-label="Chế độ xem trước">
        <button
          type="button"
          className={previewMode === "live" ? "is-active" : ""}
          aria-pressed={previewMode === "live"}
          onClick={() => onPreviewModeChange("live")}
        >
          Live
        </button>
        <button
          type="button"
          className={previewMode === "rendered" ? "is-active" : ""}
          aria-pressed={previewMode === "rendered"}
          disabled={!hasRenderedVideo}
          title={hasRenderedVideo ? "Xem video đã render" : "Chưa có bản render"}
          onClick={() => onPreviewModeChange("rendered")}
        >
          Rendered
        </button>
      </div>

      <div className="transport-playback-controls">
        <span className="transport-timecode">{formatTimecode(snapshot.currentMs)}</span>
        <button
          type="button"
          className="transport-icon-button"
          disabled={!hasVideo}
          aria-label="Lùi một giây"
          title="Lùi 1 giây"
          onClick={() => onSeek(snapshot.currentMs - 1000)}
        >
          <ChevronLeft size={19} />
        </button>
        <button
          type="button"
          className="transport-play-button"
          disabled={!hasVideo}
          aria-label={snapshot.isPlaying ? "Tạm dừng" : "Phát"}
          title={snapshot.isPlaying ? "Tạm dừng (Space)" : "Phát (Space)"}
          onClick={onTogglePlay}
        >
          {snapshot.isPlaying ? (
            <Pause size={19} fill="currentColor" />
          ) : (
            <Play size={19} fill="currentColor" />
          )}
        </button>
        <button
          type="button"
          className="transport-icon-button"
          disabled={!hasVideo}
          aria-label="Tiến một giây"
          title="Tiến 1 giây"
          onClick={() => onSeek(snapshot.currentMs + 1000)}
        >
          <ChevronRight size={19} />
        </button>
        <button
          type="button"
          className={`transport-icon-button ${isMuted || volume === 0 ? "is-muted" : ""}`}
          disabled={!hasVideo}
          aria-label={isMuted || volume === 0 ? "Bật tiếng preview" : "Tắt tiếng preview"}
          aria-pressed={isMuted || volume === 0}
          title={
            isMuted || volume === 0
              ? "Bật tiếng preview"
              : `Tắt tiếng preview · âm lượng ${Math.round(volume * 100)}%`
          }
          onClick={onToggleMute}
        >
          {isMuted || volume === 0 ? <VolumeX size={19} /> : <Volume2 size={19} />}
        </button>
        <span className="transport-timecode is-duration">
          {formatTimecode(snapshot.durationMs)}
        </span>
      </div>

      <span className="transport-state-label" aria-live="polite">
        {previewMode === "live" ? "Preview trực tiếp" : "Video đã render"}
      </span>
    </section>
  );
}

type SubtitleStatusBarProps = {
  cueCount: number;
  durationMs: number;
  media: MediaMetadata | null;
  pixelsPerSecond: number;
  snapEnabled: boolean;
  onPixelsPerSecondChange: (value: number) => void;
  onSnapEnabledChange: (value: boolean) => void;
  onFullscreen: () => void;
};

function SubtitleStatusBar({
  cueCount,
  durationMs,
  media,
  pixelsPerSecond,
  snapEnabled,
  onPixelsPerSecondChange,
  onSnapEnabledChange,
  onFullscreen,
}: SubtitleStatusBarProps) {
  return (
    <footer className="subtitle-status-bar">
      <div className="subtitle-project-stats">
        <span>{cueCount} cue</span>
        <span>{formatCompactTimecode(durationMs)}</span>
        {media && (
          <span>
            {media.is_vfr ? "VFR" : "CFR"} ·{" "}
            {(media.frame_rate_numerator / media.frame_rate_denominator).toFixed(3)} fps
          </span>
        )}
        <span>{pixelsPerSecond} px/giây</span>
      </div>
      <div className="subtitle-zoom-control">
        <button
          type="button"
          className={`timeline-snap-toggle ${snapEnabled ? "is-active" : ""}`}
          aria-label={snapEnabled ? "Tắt hít mốc timeline" : "Bật hít mốc timeline"}
          aria-pressed={snapEnabled}
          title={snapEnabled ? "Hít mốc đang bật" : "Hít mốc đang tắt"}
          onClick={() => onSnapEnabledChange(!snapEnabled)}
        >
          <Magnet size={16} />
        </button>
        <button
          type="button"
          aria-label="Thu nhỏ timeline"
          onClick={() => onPixelsPerSecondChange(Math.max(12, pixelsPerSecond - 4))}
        >
          −
        </button>
        <input
          type="range"
          min={12}
          max={180}
          step={1}
          value={pixelsPerSecond}
          aria-label="Độ phóng đại timeline theo pixel trên giây"
          onChange={(event) => onPixelsPerSecondChange(Number(event.target.value))}
        />
        <button
          type="button"
          aria-label="Phóng to timeline"
          onClick={() => onPixelsPerSecondChange(Math.min(180, pixelsPerSecond + 4))}
        >
          +
        </button>
        <button
          type="button"
          className="subtitle-fullscreen-button"
          onClick={onFullscreen}
          aria-label="Toàn màn hình vùng dựng"
          title="Toàn màn hình"
        >
          <Maximize2 size={16} />
        </button>
      </div>
    </footer>
  );
}

import { useVoicePlayback } from "../voiceover/useVoicePlayback";

type SubtitleWorkspaceProps = {
  voice?: import("../voiceover/useVoiceover").VoiceController;
  onOpenVoice?: () => void;
  originalVideoUrl: string | null;
  renderedVideoUrl: string | null;
  thumbnailCacheKey: string | null;
  media: MediaMetadata | null;
  liveAssContent: string | null;
  cues: readonly SubtitleCueV2[];
  selectedCueId: string | null;
  options: SubtitleBurnOptions;
  overlayImage: string | null;
  overlayName: string;
  overlayLayout: OverlayLayout;
  subtitleMasks: readonly SubtitleMaskRegion[];
  selectedMaskId: string | null;
  videoClips: readonly VideoClip[];
  selectedVideoClipId: string | null;
  canRestoreVideoClip: boolean;
  onDurationChange: (durationMs: number) => void;
  onSelectCue: (cueId: string | null) => void;
  onUpdateCueText: (cueId: string, text: string) => void;
  onCuePositionChange: (cueId: string, position: { x: number; y: number }) => void;
  onActiveCueChange: (cueId: string | null) => void;
  onCueTimingCommit: (cueId: string, startMs: number, endMs: number) => void;
  onOptionsChange: (options: SubtitleBurnOptions) => void;
  onOverlayLayoutChange: (layout: OverlayLayout) => void;
  onSelectMask: (maskId: string | null) => void;
  onMaskChange: (maskId: string, patch: Partial<SubtitleMaskRegion>) => void;
  onMaskDelete: (maskId: string) => void;
  onSelectVideoClip: (clipId: string) => void;
  onSplitVideo: () => void;
  onDeleteVideoClip: () => void;
  onRestoreVideoClip: () => void;
};

const seekVideo = (video: HTMLVideoElement, seconds: number) => {
  video.currentTime = seconds;
};

export type SubtitleWorkspaceHandle = {
  seekTo: (milliseconds: number) => void;
  togglePlay: () => void;
  getCurrentMs: () => number;
};

export const SubtitleWorkspace = forwardRef<
  SubtitleWorkspaceHandle,
  SubtitleWorkspaceProps
>(function SubtitleWorkspace({
  voice,
  onOpenVoice,
  originalVideoUrl,
  renderedVideoUrl,
  thumbnailCacheKey,
  media,
  liveAssContent,
  cues,
  selectedCueId,
  options,
  overlayImage,
  overlayName,
  overlayLayout,
  subtitleMasks,
  selectedMaskId,
  videoClips,
  selectedVideoClipId,
  canRestoreVideoClip,
  onDurationChange,
  onSelectCue,
  onUpdateCueText,
  onCuePositionChange,
  onActiveCueChange,
  onCueTimingCommit,
  onOptionsChange,
  onOverlayLayoutChange,
  onSelectMask,
  onMaskChange,
  onMaskDelete,
  onSelectVideoClip,
  onSplitVideo,
  onDeleteVideoClip,
  onRestoreVideoClip,
}: SubtitleWorkspaceProps, ref) {
  const workspaceRef = useRef<HTMLDivElement>(null);
  const [clock] = useState(() => new PlaybackClockStore());
  const videoElementRef = useRef<HTMLVideoElement | null>(null);
  const [videoElement, setVideoElement] = useState<HTMLVideoElement | null>(null);
  const [libassHost, setLibassHost] = useState<HTMLDivElement | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewMode>("live");
  const [isMuted, setIsMuted] = useState(false);
  const [libassReady, setLibassReady] = useState(false);
  const [pixelsPerSecond, setPixelsPerSecond] = useState(42);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const durationMs = usePlaybackDuration(clock);
  const effectiveDurationMs = media?.duration_ms ?? durationMs;
  const frameTiming = media ?? DEFAULT_FRAME_TIMING;
  const effectivePreviewMode: PreviewMode = renderedVideoUrl ? previewMode : "live";
  const activeVideoUrl =
    effectivePreviewMode === "rendered" && renderedVideoUrl
      ? renderedVideoUrl
      : originalVideoUrl;
  const previewVolume = Math.max(0, Math.min(1, options.volume ?? 1));
  useVoicePlayback(videoElement, voice?.document ?? null, effectivePreviewMode === "live", previewVolume, voice?.setError);
  const selectedVideoClip = videoClips.find((clip) => clip.id === selectedVideoClipId) ?? null;

  useEffect(() => {
    if (!originalVideoUrl) {
      clock.reset();
      onDurationChange(0);
    }
  }, [clock, onDurationChange, originalVideoUrl]);

  useEffect(() => {
    const video = videoElementRef.current;
    if (!video) return;
    video.volume = previewVolume;
    video.muted = isMuted;
  }, [isMuted, previewVolume, videoElement]);

  useEffect(() => {
    if (effectivePreviewMode !== "live" || !videoElement || videoClips.length === 0) {
      return undefined;
    }
    return clock.subscribeFrame((currentMs) => {
      if (!clock.getSnapshot().isPlaying || findVideoClipAt(videoClips, currentMs)) return;
      const nextClip = videoClips.find((clip) => clip.start_ms > currentMs);
      if (nextClip) {
        seekVideo(videoElement, nextClip.start_ms / 1000);
        clock.setCurrentMs(nextClip.start_ms);
      } else {
        videoElement.pause();
        const lastClip = videoClips.at(-1);
        if (lastClip) {
          const lastKeptMs = Math.max(lastClip.start_ms, lastClip.end_ms - 1);
          seekVideo(videoElement, lastKeptMs / 1000);
          clock.setCurrentMs(lastKeptMs);
        }
      }
    });
  }, [clock, effectivePreviewMode, videoClips, videoElement]);

  const handleVideoElementChange = useCallback(
    (element: HTMLVideoElement | null) => {
      videoElementRef.current = element;
      setVideoElement(element);
    },
    [],
  );

  const handleSeek = useCallback(
    (milliseconds: number) => {
      const durationMs = clock.getSnapshot().durationMs;
      const boundedMs = Math.max(0, Math.min(durationMs || 0, Math.round(milliseconds)));
      const nextMs = effectivePreviewMode === "live"
        ? nearestKeptVideoTimeMs(videoClips, boundedMs)
        : boundedMs;
      if (videoElement) seekVideo(videoElement, nextMs / 1000);
      clock.setCurrentMs(nextMs);
    },
    [clock, effectivePreviewMode, videoClips, videoElement],
  );

  const handleTogglePlay = useCallback(() => {
    if (!videoElement) return;
    if (videoElement.paused) void videoElement.play();
    else videoElement.pause();
  }, [videoElement]);

  const handleToggleMute = useCallback(() => {
    if (previewVolume === 0) {
      onOptionsChange({ ...options, volume: 1 });
      setIsMuted(false);
      return;
    }
    setIsMuted((current) => !current);
  }, [onOptionsChange, options, previewVolume]);

  const handleDurationChange = useCallback(
    (durationMs: number) => onDurationChange(durationMs),
    [onDurationChange],
  );

  useImperativeHandle(
    ref,
    () => ({
      seekTo: handleSeek,
      togglePlay: handleTogglePlay,
      getCurrentMs: () => clock.getSnapshot().currentMs,
    }),
    [clock, handleSeek, handleTogglePlay],
  );

  return (
    <div ref={workspaceRef} className={`subtitle-workspace${voice ? ' has-voice-track' : ''}`}>
      <PlaybackClock
        video={videoElement}
        sourceKey={activeVideoUrl ?? "empty"}
        store={clock}
        onDurationChange={handleDurationChange}
      />
      <LibassPreview
        key={originalVideoUrl ?? "empty"}
        video={videoElement}
        host={libassHost}
        assContent={cues.length ? liveAssContent : null}
        enabled={effectivePreviewMode === "live" && cues.length > 0 && Boolean(liveAssContent)}
        onReadyChange={setLibassReady}
      />
      <PreviewStage
        videoElement={videoElement}
        videoUrl={activeVideoUrl}
        previewMode={effectivePreviewMode}
        libassActive={effectivePreviewMode === "live" && cues.length > 0 && Boolean(liveAssContent) && libassReady}
        onLibassHostElementChange={setLibassHost}
        cues={cues}
        selectedCueId={selectedCueId}
        options={options}
        overlayImage={overlayImage}
        overlayName={overlayName}
        overlayLayout={overlayLayout}
        subtitleMasks={subtitleMasks}
        selectedMaskId={selectedMaskId}
        clock={clock}
        onVideoElementChange={handleVideoElementChange}
        onTogglePlay={handleTogglePlay}
        onSelectCue={onSelectCue}
        onUpdateCueText={onUpdateCueText}
        onOptionsCommit={onOptionsChange}
        onCuePositionChange={onCuePositionChange}
        onActiveCueChange={onActiveCueChange}
        onOverlayLayoutCommit={onOverlayLayoutChange}
        onSelectMask={onSelectMask}
        onMaskChange={onMaskChange}
        onMaskDelete={onMaskDelete}
      />
      <SubtitleTransport
        clock={clock}
        hasVideo={Boolean(originalVideoUrl)}
        previewMode={effectivePreviewMode}
        hasRenderedVideo={Boolean(renderedVideoUrl)}
        onSeek={handleSeek}
        onTogglePlay={handleTogglePlay}
        isMuted={isMuted}
        volume={previewVolume}
        onToggleMute={handleToggleMute}
        onPreviewModeChange={setPreviewMode}
      />
      <div className="video-edit-toolbar" aria-label="Công cụ chỉnh sửa video">
        <div className="video-edit-toolbar-actions">
          <button
            type="button"
            onClick={onSplitVideo}
            disabled={!originalVideoUrl}
            title="Tách video tại con trỏ (S)"
          >
            <Scissors size={17} /> <span>Tách tại con trỏ</span>
          </button>
          <button
            type="button"
            className="is-destructive"
            onClick={onDeleteVideoClip}
            disabled={!selectedVideoClip || videoClips.length <= 1}
            title="Xóa đoạn video đã chọn (Delete)"
          >
            <Trash2 size={17} /> <span>Xóa đoạn</span>
          </button>
          <button
            type="button"
            onClick={onRestoreVideoClip}
            disabled={!canRestoreVideoClip}
            title="Khôi phục đoạn vừa xóa"
          >
            <Undo2 size={17} /> <span>Khôi phục</span>
          </button>
        </div>
        <span className="video-edit-summary" aria-live="polite">
          {videoClips.length} đoạn · video xuất {formatCompactTimecode(keptVideoDurationMs(videoClips))}
        </span>
      </div>
      <SubtitleTimeline
        voice={voice}
        onOpenVoice={onOpenVoice}
        cues={cues}
        selectedCueId={selectedCueId}
        durationMs={effectiveDurationMs}
        frameTiming={frameTiming}
        pixelsPerSecond={pixelsPerSecond}
        clock={clock}
        videoUrl={originalVideoUrl}
        thumbnailCacheKey={thumbnailCacheKey}
        hasOverlayTrack={Boolean(overlayImage)}
        snapEnabled={snapEnabled}
        videoClips={videoClips}
        selectedVideoClipId={selectedVideoClipId}
        onSelectVideoClip={onSelectVideoClip}
        onSplitVideo={onSplitVideo}
        onDeleteVideoClip={onDeleteVideoClip}
        onSelectCue={(cueId) => onSelectCue(cueId)}
        onSeek={handleSeek}
        onTogglePlay={handleTogglePlay}
        onCueTimingCommit={onCueTimingCommit}
      />
      <SubtitleStatusBar
        cueCount={cues.length}
        durationMs={effectiveDurationMs}
        media={media}
        pixelsPerSecond={pixelsPerSecond}
        snapEnabled={snapEnabled}
        onPixelsPerSecondChange={setPixelsPerSecond}
        onSnapEnabledChange={setSnapEnabled}
        onFullscreen={() => {
          const element = workspaceRef.current;
          if (!element) return;
          if (document.fullscreenElement) void document.exitFullscreen();
          else void element.requestFullscreen();
        }}
      />
    </div>
  );
});
