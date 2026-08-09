import { useEffect, useState } from "react";

export type VideoThumbnailSprite = {
  url: string;
  frameCount: number;
  frameWidth: number;
  frameHeight: number;
};

type CachedSprite = Omit<VideoThumbnailSprite, "url"> & { blob: Blob };

const spriteCache = new Map<string, CachedSprite>();

const decodeSprite = async (url: string, signal: AbortSignal) => {
  const image = new Image();
  image.decoding = "async";
  image.src = url;
  try {
    await image.decode();
    if (signal.aborted) {
      throw new DOMException("Thumbnail decode was cancelled", "AbortError");
    }
  } finally {
    image.removeAttribute("src");
  }
};

const fetchServerSprite = async (
  videoUrl: string,
  signal: AbortSignal,
): Promise<CachedSprite> => {
  const url = new URL(videoUrl, window.location.href);
  url.pathname = `${url.pathname.replace(/\/$/, "")}/thumbnail-sprite`;
  url.search = "";
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`Thumbnail sprite request failed (${response.status})`);
  const frameCount = Number(response.headers.get("X-Sprite-Frames"));
  const frameWidth = Number(response.headers.get("X-Sprite-Frame-Width"));
  const frameHeight = Number(response.headers.get("X-Sprite-Frame-Height"));
  if (
    !Number.isSafeInteger(frameCount) ||
    !Number.isSafeInteger(frameWidth) ||
    !Number.isSafeInteger(frameHeight) ||
    frameCount < 1 ||
    frameWidth < 1 ||
    frameHeight < 1
  ) {
    throw new Error("Thumbnail sprite response has invalid metadata");
  }
  return { blob: await response.blob(), frameCount, frameWidth, frameHeight };
};

const waitForEvent = (
  element: HTMLMediaElement,
  eventName: "loadedmetadata" | "seeked",
  signal: AbortSignal,
) =>
  new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      element.removeEventListener(eventName, handleEvent);
      signal.removeEventListener("abort", handleAbort);
    };
    const handleEvent = () => {
      cleanup();
      resolve();
    };
    const handleAbort = () => {
      cleanup();
      reject(new DOMException("Thumbnail extraction was cancelled", "AbortError"));
    };
    element.addEventListener(eventName, handleEvent, { once: true });
    signal.addEventListener("abort", handleAbort, { once: true });
  });

const createSprite = async (
  videoUrl: string,
  signal: AbortSignal,
): Promise<CachedSprite | null> => {
  const video = document.createElement("video");
  video.crossOrigin = "anonymous";
  video.preload = "metadata";
  video.muted = true;
  video.src = videoUrl;
  try {
    if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
      await waitForEvent(video, "loadedmetadata", signal);
    }
    if (!Number.isFinite(video.duration) || video.duration <= 0) return null;

    const frameCount = Math.min(16, Math.max(8, Math.ceil(video.duration / 8)));
    const frameWidth = 120;
    const frameHeight = 68;
    const canvas = document.createElement("canvas");
    canvas.width = frameWidth * frameCount;
    canvas.height = frameHeight;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) return null;

    for (let index = 0; index < frameCount; index += 1) {
      if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
      const sampleTime = Math.min(
        Math.max(0, video.duration - 0.04),
        (index / Math.max(1, frameCount - 1)) * video.duration,
      );
      if (Math.abs(video.currentTime - sampleTime) > 0.001) {
        video.currentTime = sampleTime;
        await waitForEvent(video, "seeked", signal);
      }
      context.drawImage(video, index * frameWidth, 0, frameWidth, frameHeight);
    }

    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", 0.72),
    );
    return blob ? { blob, frameCount, frameWidth, frameHeight } : null;
  } finally {
    video.removeAttribute("src");
    video.load();
  }
};

export function useVideoThumbnailSprite(
  videoUrl: string | null,
  cacheKey: string | null,
) {
  const [spriteState, setSpriteState] = useState<{
    key: string;
    sprite: VideoThumbnailSprite;
  } | null>(null);

  useEffect(() => {
    if (!videoUrl || !cacheKey) {
      return undefined;
    }
    const controller = new AbortController();
    let objectUrl: string | null = null;
    const cachedSprite = spriteCache.get(cacheKey);
    const request = cachedSprite
      ? Promise.resolve(cachedSprite)
      : fetchServerSprite(videoUrl, controller.signal)
          .catch(() => createSprite(videoUrl, controller.signal))
          .then((created) => {
            if (created) spriteCache.set(cacheKey, created);
            return created;
          });

    request
      .then(async (cached) => {
        if (!cached || controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(cached.blob);
        // Decode against the exact object URL before mounting CSS frames. This
        // keeps the first image decode from blocking a timeline pointer gesture.
        await decodeSprite(objectUrl, controller.signal);
        setSpriteState({
          key: cacheKey,
          sprite: {
            url: objectUrl,
            frameCount: cached.frameCount,
            frameWidth: cached.frameWidth,
            frameHeight: cached.frameHeight,
          },
        });
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (!controller.signal.aborted) setSpriteState(null);
      });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [cacheKey, videoUrl]);

  return cacheKey && spriteState?.key === cacheKey ? spriteState.sprite : null;
}
