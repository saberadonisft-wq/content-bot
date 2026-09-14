import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SubtitleUploadResult } from "./api/types";

export type DesktopSelectedVideo = { name: string; size: number; lastModified: number; upload: SubtitleUploadResult };

export type DesktopLiveWallState = "queued" | "loading" | "ready" | "error";

export type DesktopLiveWallStatus = {
  channelId: string;
  state: DesktopLiveWallState;
  detail: string;
};

type DesktopViewBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type DesktopLiveWallView = {
  channelId: string;
  sourceId: string;
  url: string;
  bounds: DesktopViewBounds;
  visible: boolean;
};

type DesktopLiveWallBridge = {
  pickProjectVideo?: (payload: { apiBase: string; accessToken?: string }) => Promise<DesktopSelectedVideo | null>;
  environment: () => Promise<{
    available: boolean;
    platform: string;
    version: string;
  }>;
  syncLiveWall: (payload: {
    ownerId: string;
    keywordId: number;
    views: DesktopLiveWallView[];
  }) => Promise<DesktopLiveWallStatus[]>;
  closeLiveWall: () => Promise<{ closed: boolean }>;
  reloadLiveWallView: (channelId: string) => Promise<{ reloaded: boolean }>;
  loginXLiveWallView: (channelId: string) => Promise<{ started: boolean }>;
  downloadFile: (payload: {
    url: string;
    filename: string;
    accessToken?: string;
  }) => Promise<{
    state: "completed" | "cancelled" | "interrupted";
    savePath?: string;
  }>;
  onLiveWallStatus: (
    callback: (status: DesktopLiveWallStatus) => void,
  ) => () => void;
  startGoogleLogin: () => Promise<{ started: boolean }>;
  onGoogleAuthResult: (
    callback: (result: {
      accessToken?: string;
      refreshToken?: string;
      error?: string;
    }) => void,
  ) => () => void;
};

declare global {
  interface Window {
    contentBotDesktop?: DesktopLiveWallBridge;
  }
}

export type DesktopLiveWallChannel = {
  id: string;
  sourceId: string;
  url: string;
};

// Keep this list narrower than the backend allow-list. Unknown/custom URLs stay on
// the existing official-embed or Cốc Cốc fallback path.
const DESKTOP_SOURCE_IDS = new Set([
  "bilibili",
  "bluesky",
  "douyin",
  "facebook",
  "instagram",
  "kuaishou",
  "reddit",
  "steam",
  "tieba",
  "tiktok",
  "weibo",
  "x",
  "xhs",
  "youtube",
  "zhihu",
]);

export function desktopCanRenderSource(sourceId: string) {
  return DESKTOP_SOURCE_IDS.has(sourceId.toLowerCase());
}

export function desktopViewPlacement(
  rect: Pick<DOMRect, "left" | "top" | "right" | "bottom" | "width" | "height">,
  viewport: { width: number; height: number },
  unobscured = true,
): { bounds: DesktopViewBounds; visible: boolean } | null {
  const width = Math.floor(rect.width);
  const height = Math.floor(rect.height);
  if (width < 80 || height < 80) return null;
  return {
    bounds: {
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width,
      height,
    },
    visible:
      unobscured &&
      rect.right > 0 &&
      rect.bottom > 0 &&
      rect.left < viewport.width &&
      rect.top < viewport.height,
  };
}

function hostIsUnobscured(
  element: HTMLElement,
  rect: Pick<DOMRect, "left" | "top" | "right" | "bottom">,
  viewport: { width: number; height: number },
) {
  if (document.visibilityState === "hidden" || !element.isConnected) return false;

  // WebContentsView is native Electron chrome and always paints above renderer
  // DOM. Hide it while a modal is open because an HTML backdrop cannot cover it.
  const modal = document.querySelector("dialog[open], [aria-modal='true']");
  if (modal && !modal.contains(element)) return false;

  const left = Math.max(0, rect.left);
  const top = Math.max(0, rect.top);
  const right = Math.min(viewport.width, rect.right);
  const bottom = Math.min(viewport.height, rect.bottom);
  if (right <= left || bottom <= top) return false;

  const topElement = document.elementFromPoint(
    Math.floor((left + right) / 2),
    Math.floor((top + bottom) / 2),
  );
  return !topElement || topElement === element || element.contains(topElement);
}

function viewPlacement(element: HTMLElement) {
  const rect = element.getBoundingClientRect();
  const viewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };
  return desktopViewPlacement(
    rect,
    viewport,
    hostIsUnobscured(element, rect, viewport),
  );
}

export function useDesktopLiveWall({
  active,
  ownerId,
  keywordId,
  channels,
}: {
  active: boolean;
  ownerId: string;
  keywordId: number;
  channels: DesktopLiveWallChannel[];
}) {
  const bridge = window.contentBotDesktop;
  const hostsRef = useRef(new Map<string, HTMLElement>());
  const frameRef = useRef<number | null>(null);
  const [statuses, setStatuses] = useState<Record<string, DesktopLiveWallStatus>>({});
  const [error, setError] = useState<string | null>(null);
  const channelKey = useMemo(
    () => channels.map((channel) => `${channel.id}\0${channel.sourceId}\0${channel.url}`).join("\u0001"),
    [channels],
  );
  const channelsRef = useRef(channels);

  useEffect(() => {
    channelsRef.current = channels;
  }, [channelKey, channels]);

  const updateStatus = useCallback((status: DesktopLiveWallStatus) => {
    setStatuses((current) => ({ ...current, [status.channelId]: status }));
  }, []);

  const syncNow = useCallback(async () => {
    if (!bridge || !active || !ownerId || keywordId <= 0) return;
    const views = channelsRef.current.flatMap((channel) => {
      if (!desktopCanRenderSource(channel.sourceId)) return [];
      const host = hostsRef.current.get(channel.id);
      const placement = host ? viewPlacement(host) : null;
      if (!placement) return [];
      return [{
        channelId: channel.id,
        sourceId: channel.sourceId,
        url: channel.url,
        bounds: placement.bounds,
        visible: placement.visible,
      }];
    });
    try {
      const next = await bridge.syncLiveWall({ ownerId, keywordId, views });
      setError(null);
      setStatuses(Object.fromEntries(next.map((status) => [status.channelId, status])));
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Không đồng bộ được trình duyệt trong ứng dụng.",
      );
    }
  }, [active, bridge, keywordId, ownerId]);

  const scheduleSync = useCallback(() => {
    if (frameRef.current !== null) return;
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = null;
      void syncNow();
    });
  }, [syncNow]);

  const registerHost = useCallback(
    (channelId: string, element: HTMLElement | null) => {
      if (element) hostsRef.current.set(channelId, element);
      else hostsRef.current.delete(channelId);
      scheduleSync();
    },
    [scheduleSync],
  );

  useEffect(() => {
    if (!bridge) return undefined;
    return bridge.onLiveWallStatus(updateStatus);
  }, [bridge, updateStatus]);

  useEffect(() => {
    if (!bridge || !active) {
      if (bridge) void bridge.closeLiveWall();
      return undefined;
    }
    const observer = new ResizeObserver(scheduleSync);
    for (const host of hostsRef.current.values()) observer.observe(host);
    const visibilityObserver = new MutationObserver(scheduleSync);
    visibilityObserver.observe(document.body, {
      attributes: true,
      attributeFilter: ["aria-hidden", "aria-modal", "hidden", "open"],
      childList: true,
      subtree: true,
    });
    window.addEventListener("resize", scheduleSync);
    window.addEventListener("scroll", scheduleSync, true);
    document.addEventListener("visibilitychange", scheduleSync);
    scheduleSync();
    return () => {
      observer.disconnect();
      visibilityObserver.disconnect();
      window.removeEventListener("resize", scheduleSync);
      window.removeEventListener("scroll", scheduleSync, true);
      document.removeEventListener("visibilitychange", scheduleSync);
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      void bridge.closeLiveWall();
    };
  }, [active, bridge, channelKey, scheduleSync]);

  const reload = useCallback(
    (channelId: string) => bridge?.reloadLiveWallView(channelId),
    [bridge],
  );

  const loginX = useCallback(
    (channelId: string) => bridge?.loginXLiveWallView(channelId),
    [bridge],
  );

  return {
    available: Boolean(bridge),
    error,
    loginX,
    registerHost,
    reload,
    scheduleSync,
    statuses,
  };
}
