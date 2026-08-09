import { useEffect } from "react";
import { secondsToMs } from "./time";
import type { PlaybackClockStore } from "./playback-store";

type FrameMetadata = { mediaTime: number };
type FrameVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (
    callback: (now: number, metadata: FrameMetadata) => void,
  ) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

type PlaybackClockProps = {
  video: HTMLVideoElement | null;
  sourceKey: string;
  store: PlaybackClockStore;
  onDurationChange?: (durationMs: number) => void;
};

const seekVideo = (video: HTMLVideoElement, seconds: number) => {
  video.currentTime = seconds;
};

export function PlaybackClock({
  video,
  sourceKey,
  store,
  onDurationChange,
}: PlaybackClockProps) {
  useEffect(() => {
    if (!video) {
      return undefined;
    }

    const frameVideo = video as FrameVideo;
    let frameHandle: number | null = null;
    let animationHandle: number | null = null;
    let stopped = false;

    const syncFromElement = () => store.setCurrentMs(secondsToMs(video.currentTime));
    const syncDuration = () => {
      const durationMs = secondsToMs(video.duration);
      store.setDurationMs(durationMs);
      onDurationChange?.(durationMs);
    };

    const cancelTick = () => {
      if (frameHandle !== null) {
        frameVideo.cancelVideoFrameCallback?.(frameHandle);
        frameHandle = null;
      }
      if (animationHandle !== null) {
        cancelAnimationFrame(animationHandle);
        animationHandle = null;
      }
    };

    const scheduleTick = () => {
      if (stopped || video.paused || video.ended) return;
      if (frameVideo.requestVideoFrameCallback) {
        frameHandle = frameVideo.requestVideoFrameCallback((_now, metadata) => {
          frameHandle = null;
          store.setCurrentMs(secondsToMs(metadata.mediaTime));
          scheduleTick();
        });
      } else {
        animationHandle = requestAnimationFrame(() => {
          animationHandle = null;
          syncFromElement();
          scheduleTick();
        });
      }
    };

    const handleLoadedMetadata = () => {
      syncDuration();
      const resumeAtMs = Math.min(
        store.getSnapshot().currentMs,
        secondsToMs(video.duration),
      );
      seekVideo(video, resumeAtMs / 1000);
      store.setCurrentMs(resumeAtMs);
    };
    const handlePlay = () => {
      store.setPlaying(true);
      cancelTick();
      scheduleTick();
    };
    const handlePause = () => {
      cancelTick();
      syncFromElement();
      store.setPlaying(false);
    };
    const handleEnded = () => {
      cancelTick();
      syncFromElement();
      store.setPlaying(false);
    };

    video.addEventListener("loadedmetadata", handleLoadedMetadata);
    video.addEventListener("durationchange", syncDuration);
    video.addEventListener("seeking", syncFromElement);
    video.addEventListener("seeked", syncFromElement);
    video.addEventListener("timeupdate", syncFromElement);
    video.addEventListener("play", handlePlay);
    video.addEventListener("pause", handlePause);
    video.addEventListener("ended", handleEnded);

    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) handleLoadedMetadata();
    if (!video.paused) handlePlay();

    return () => {
      stopped = true;
      cancelTick();
      video.removeEventListener("loadedmetadata", handleLoadedMetadata);
      video.removeEventListener("durationchange", syncDuration);
      video.removeEventListener("seeking", syncFromElement);
      video.removeEventListener("seeked", syncFromElement);
      video.removeEventListener("timeupdate", syncFromElement);
      video.removeEventListener("play", handlePlay);
      video.removeEventListener("pause", handlePause);
      video.removeEventListener("ended", handleEnded);
    };
  }, [onDurationChange, sourceKey, store, video]);

  return null;
}
