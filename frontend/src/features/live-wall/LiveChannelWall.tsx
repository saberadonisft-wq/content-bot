import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  LogIn,
  Maximize2,
  Minimize2,
  Monitor,
  RefreshCw,
  Save,
  Settings2,
  SquareX,
} from "lucide-react";
import {
  api,
  type ChannelSubscription,
  type Keyword,
  type LiveWallSlots,
  type Source,
} from "../../api";
import {
  channelSupportsEmbed,
  initialLiveWallChannelIds,
  liveWallPage,
  tiktokCreatorUsername,
  xProfileUsername,
} from "../../liveWallModel";
import {
  desktopCanRenderSource,
  type DesktopLiveWallStatus,
  useDesktopLiveWall,
} from "../../desktopLiveWall";
import { useAuth } from "../../useAuth";

const SCRIPT_LOADS = new Map<string, Promise<void>>();

function loadExternalScript(src: string, key: string): Promise<void> {
  const existing = SCRIPT_LOADS.get(key);
  if (existing) return existing;
  const promise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.dataset.liveWallScript = key;
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => reject(new Error(`Không tải được ${src}`)), {
      once: true,
    });
    document.head.appendChild(script);
  }).catch((error: unknown) => {
    SCRIPT_LOADS.delete(key);
    document.querySelector(`script[data-live-wall-script="${key}"]`)?.remove();
    throw error;
  });
  SCRIPT_LOADS.set(key, promise);
  return promise;
}

type EmbedProps = {
  url: string;
  refreshKey?: number;
  onError?: () => void;
};

export function XTimelineEmbed({ url, refreshKey = 0, onError }: EmbedProps) {
  const username = xProfileUsername(url);
  const mountRef = useRef<HTMLDivElement>(null);
  const onErrorRef = useRef(onError);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount || !username) {
      setState("error");
      onErrorRef.current?.();
      return undefined;
    }
    mount.replaceChildren();
    setState("loading");
    let finished = false;
    const markReady = () => {
      if (finished) return;
      finished = true;
      setState("ready");
    };
    const markError = () => {
      if (finished) return;
      finished = true;
      setState("error");
      onErrorRef.current?.();
    };
    const timeout = window.setTimeout(markError, 15_000);
    void loadExternalScript(
      "https://platform.twitter.com/widgets.js",
      "x-widgets-v3",
    )
      .then(async () => {
        const twitter = (
          window as Window & {
            twttr?: {
              widgets?: {
                createTimeline?: (
                  source: { sourceType: "profile"; screenName: string },
                  element: HTMLElement,
                  options: Record<string, string | number | boolean>,
                ) => Promise<HTMLElement | undefined>;
              };
            };
          }
        ).twttr;
        if (!twitter?.widgets?.createTimeline) {
          throw new Error("X widgets API không khả dụng.");
        }
        const iframe = await twitter.widgets.createTimeline(
          { sourceType: "profile", screenName: username },
          mount,
          {
            chrome: "noheader nofooter",
            dnt: true,
            height: 560,
          },
        );
        if (!iframe || !mount.querySelector("iframe")) {
          throw new Error("X không tạo timeline.");
        }
        markReady();
      })
      .catch(markError);
    return () => {
      finished = true;
      window.clearTimeout(timeout);
      mount.replaceChildren();
    };
  }, [refreshKey, username]);

  if (!username) {
    return <div className="live-embed-state error" role="alert">URL profile X không hợp lệ.</div>;
  }

  return (
    <div className="live-embed-frame x-timeline-embed">
      {state === "loading" && <span className="live-embed-state live-embed-overlay" role="status">Đang tải timeline X…</span>}
      {state === "error" && (
        <span className="live-embed-state error" role="alert">X không trả về timeline công khai.</span>
      )}
      <div className="x-timeline-mount" ref={mountRef} />
    </div>
  );
}

function TikTokCreatorEmbed({ url, refreshKey = 0, onError }: EmbedProps) {
  const username = tiktokCreatorUsername(url);
  const containerRef = useRef<HTMLDivElement>(null);
  const onErrorRef = useRef(onError);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !username) {
      setState("error");
      onErrorRef.current?.();
      return undefined;
    }
    setState("loading");
    let finished = false;
    const markReady = () => {
      if (finished) return;
      finished = true;
      setState("ready");
    };
    const markError = () => {
      if (finished) return;
      finished = true;
      setState("error");
      onErrorRef.current?.();
    };
    const observer = new MutationObserver(() => {
      if (container.querySelector("iframe")) markReady();
    });
    observer.observe(container, { childList: true, subtree: true });
    const timeout = window.setTimeout(markError, 15_000);
    void loadExternalScript(
      "https://www.tiktok.com/embed.js",
      `tiktok-embed-${refreshKey}`,
    ).then(() => {
      if (container.querySelector("iframe")) markReady();
    }).catch(markError);
    return () => {
      finished = true;
      observer.disconnect();
      window.clearTimeout(timeout);
    };
  }, [refreshKey, url, username]);

  if (!username) {
    return <div className="live-embed-state error">URL creator TikTok không hợp lệ.</div>;
  }
  return (
    <div className="live-embed-frame" ref={containerRef}>
      {state === "loading" && <span className="live-embed-state">Đang tải TikTok…</span>}
      {state === "error" && (
        <span className="live-embed-state error">TikTok không cho nhúng creator này.</span>
      )}
      <blockquote
        key={`${username}-${refreshKey}`}
        className="tiktok-embed"
        cite={url}
        data-unique-id={username}
        data-embed-type="creator"
      >
        <section>
          <a target="_blank" rel="noreferrer" href={url}>@{username}</a>
        </section>
      </blockquote>
    </div>
  );
}

function channelUrl(channel: ChannelSubscription) {
  return channel.normalized_url || channel.url;
}

function DesktopLiveViewHost({
  channelId,
  status,
  registerHost,
  reload,
}: {
  channelId: string;
  status?: DesktopLiveWallStatus;
  registerHost: (channelId: string, element: HTMLElement | null) => void;
  reload: (channelId: string) => unknown;
}) {
  const hostRef = useCallback(
    (element: HTMLDivElement | null) => registerHost(channelId, element),
    [channelId, registerHost],
  );
  return (
    <div
      className={`desktop-live-view-host ${status?.state ?? "loading"}`}
      ref={hostRef}
    >
      <div className="desktop-live-view-placeholder">
        {status?.state === "error" ? <SquareX size={32} /> : <Monitor size={32} />}
        <strong>
          {status?.state === "error"
            ? "Không tải được trang kênh"
            : status?.state === "ready"
              ? "Kênh đang hiển thị trong thẻ"
              : status?.state === "queued"
                ? "Đang chờ lượt tải…"
                : "Đang mở trang kênh…"}
        </strong>
        <p>{status?.detail ?? "Content Bot đang chuẩn bị trình duyệt tích hợp."}</p>
        {status?.state === "error" && (
          <button className="button secondary" type="button" onClick={() => void reload(channelId)}>
            <RefreshCw size={15} /> Thử lại
          </button>
        )}
      </div>
    </div>
  );
}

export function LiveChannelWall({
  selected,
  sources,
  onChanged,
}: {
  selected: Keyword | null;
  sources: Source[];
  onChanged: () => Promise<void>;
}) {
  const { user } = useAuth();
  const [channelIds, setChannelIds] = useState<string[]>(
    () =>
      initialLiveWallChannelIds(
        selected?.channels ?? [],
        selected?.live_wall?.channel_ids ?? [],
      ),
  );
  const [slots, setSlots] = useState<LiveWallSlots>(
    () => selected?.live_wall?.slots ?? 4,
  );
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [page, setPage] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const [failedEmbeds, setFailedEmbeds] = useState<string[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [desktopEnabled, setDesktopEnabled] = useState(
    () => Boolean(window.contentBotDesktop),
  );
  const [desktopLoadAll, setDesktopLoadAll] = useState(true);
  const [desktopPriorityId, setDesktopPriorityId] = useState<string | null>(null);

  const byId = useMemo(
    () => new Map((selected?.channels ?? []).filter((channel) => channel.id).map((channel) => [channel.id as string, channel])),
    [selected?.channels],
  );
  const selectedChannels = channelIds.map((id) => byId.get(id)).filter(Boolean) as ChannelSubscription[];
  const { pageCount, safePage, channels: visibleChannels } = liveWallPage(
    selectedChannels,
    slots,
    page,
  );
  const desktopCandidateChannels = (expandedId
    ? visibleChannels.filter((channel) => channel.id === expandedId)
    : visibleChannels
  ).flatMap((channel) => {
    const id = channel.id;
    if (!id || !channel.source_id || !desktopCanRenderSource(channel.source_id)) {
      return [];
    }
    return [{ id, sourceId: channel.source_id, url: channelUrl(channel) }];
  });
  const desktopChannels = [
    ...desktopCandidateChannels.filter((channel) => channel.id === desktopPriorityId),
    ...desktopCandidateChannels.filter((channel) => channel.id !== desktopPriorityId),
  ].slice(0, expandedId || desktopLoadAll ? 6 : 2);
  const activeDesktopIds = new Set(desktopChannels.map((channel) => channel.id));
  const desktop = useDesktopLiveWall({
    active: desktopEnabled && Boolean(selected),
    ownerId: user?.id ?? "desktop-development-user",
    keywordId: selected?.id ?? 0,
    channels: desktopChannels,
  });
  const fallbackChannels = visibleChannels.filter(
    (channel) => !channelSupportsEmbed(channel, sources) || (channel.id && failedEmbeds.includes(channel.id)),
  );
  const statusDetail = message || desktop.error;

  const save = async () => {
    if (!selected) return;
    setSaving(true);
    setMessage(null);
    try {
      await api.updateLiveWall(selected.id, { channel_ids: channelIds, slots });
      await onChanged();
      setPickerOpen(false);
      setMessage("Đã lưu bố cục Live Wall cho chủ đề này.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không lưu được Live Wall.");
    } finally {
      setSaving(false);
    }
  };

  const openInCoccoc = async (channels: ChannelSubscription[]) => {
    if (!selected) return;
    const ids = channels.map((channel) => channel.id).filter(Boolean) as string[];
    if (!ids.length) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.openInCoccoc(selected.id, ids);
      setMessage(
        result.channel_count === 1
          ? "Đã mở kênh trong Cốc Cốc của bạn."
          : `Đã mở ${result.channel_count} kênh trong Cốc Cốc của bạn.`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không mở được Cốc Cốc Live Wall.");
    } finally {
      setBusy(false);
    }
  };

  const refreshVisibleChannels = () => {
    if (desktop.available && desktopEnabled) {
      for (const channel of desktopChannels) void desktop.reload(channel.id);
      return;
    }
    setFailedEmbeds([]);
    setRefreshKey((value) => value + 1);
  };

  const toggleChannel = (id: string) => {
    setChannelIds((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  };

  const move = (id: string, direction: -1 | 1) => {
    setChannelIds((current) => {
      const index = current.indexOf(id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  if (!selected) {
    return <div className="empty compact"><Monitor size={30} /><h2>Chưa chọn chủ đề</h2><p>Chọn một chủ đề để cấu hình Live Wall.</p></div>;
  }

  return (
    <div className="live-wall-shell">
      <div className="live-wall-toolbar">
        {desktop.available && (
          <button
            className={`button ${desktopEnabled ? "" : "secondary"}`}
            type="button"
            onClick={() => setDesktopEnabled((value) => !value)}
          >
            <Monitor size={15} />
            {desktopEnabled ? "Đang xem trong app" : "Hiện kênh trong app"}
          </button>
        )}
        {desktop.available && desktopEnabled && desktopCandidateChannels.length > 2 && (
          <button
            className="button secondary"
            type="button"
            onClick={() => setDesktopLoadAll((value) => !value)}
          >
            <Monitor size={15} />
            {desktopLoadAll ? "Tiết kiệm tài nguyên" : `Mở đủ ${desktopCandidateChannels.length} kênh`}
          </button>
        )}
        <button className="button secondary" type="button" onClick={() => setPickerOpen((value) => !value)}>
          <Settings2 size={15} /> Chọn kênh
        </button>
        <label className="live-wall-slot-control">
          <span>Số ô</span>
          <select value={slots} onChange={(event) => { setSlots(Number(event.target.value) as LiveWallSlots); setPage(0); }}>
            {[1, 2, 4, 6].map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <button className="button secondary" type="button" onClick={() => void save()} disabled={saving}>
          {saving ? <RefreshCw className="spin" size={15} /> : <Save size={15} />} Lưu bố cục
        </button>
        <button className="button secondary" type="button" onClick={refreshVisibleChannels} disabled={!visibleChannels.length}>
          <RefreshCw size={15} /> {desktop.available && desktopEnabled ? "Tải lại kênh" : "Tải lại embed"}
        </button>
        <button className="button secondary" type="button" onClick={() => void openInCoccoc(fallbackChannels)} disabled={busy || !fallbackChannels.length}>
          <Monitor size={15} /> Mở ô fallback
        </button>
        <button className="button" type="button" onClick={() => void openInCoccoc(visibleChannels)} disabled={busy || !visibleChannels.length}>
          <Monitor size={15} /> Mở trang này trong Cốc Cốc
        </button>
      </div>

      {pickerOpen && (
        <section className="live-wall-picker" aria-label="Chọn kênh Live Wall">
          <div className="live-wall-picker-list">
            {selected.channels.map((channel) => {
              if (!channel.id) return null;
              const selectedIndex = channelIds.indexOf(channel.id);
              return (
                <div className="live-wall-picker-row" key={channel.id}>
                  <label>
                    <input type="checkbox" checked={selectedIndex >= 0} onChange={() => toggleChannel(channel.id as string)} />
                    <span><strong>{channel.label || channel.source_id || "Kênh"}</strong><small>{channel.url}</small></span>
                  </label>
                  {selectedIndex >= 0 && (
                    <span className="live-wall-order-actions">
                      <button type="button" aria-label="Đưa kênh lên" onClick={() => move(channel.id as string, -1)} disabled={selectedIndex === 0}><ArrowUp size={14} /></button>
                      <button type="button" aria-label="Đưa kênh xuống" onClick={() => move(channel.id as string, 1)} disabled={selectedIndex === channelIds.length - 1}><ArrowDown size={14} /></button>
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {statusDetail && (
        <div className="live-wall-status">
          <span>{statusDetail}</span>
        </div>
      )}

      {selectedChannels.length > 0 && (
        <div className="live-wall-pagination">
          <button type="button" aria-label="Trang trước" disabled={safePage === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}><ChevronLeft size={16} /></button>
          <span>Trang {safePage + 1}/{pageCount} · {selectedChannels.length} kênh</span>
          <button type="button" aria-label="Trang sau" disabled={safePage >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}><ChevronRight size={16} /></button>
        </div>
      )}

      {!selectedChannels.length ? (
        <div className="empty compact"><Monitor size={30} /><h2>Chưa chọn kênh trực tiếp</h2><p>Bấm “Chọn kênh” để tạo Live Wall cho chủ đề này.</p></div>
      ) : (
        <div className={`live-wall-grid live-wall-count-${visibleChannels.length}`}>
          {visibleChannels.map((channel) => {
            const id = channel.id as string;
            const canEmbed = channelSupportsEmbed(channel, sources) && !failedEmbeds.includes(id);
            const url = channelUrl(channel);
            const desktopSupported = desktopCanRenderSource(channel.source_id ?? "");
            const desktopStatus = desktop.statuses[id];
            const desktopDeferred = desktop.available && desktopEnabled && desktopSupported && !activeDesktopIds.has(id);
            const useDesktopTile = desktop.available && desktopEnabled && desktopSupported && !desktopDeferred;
            return (
              <article className={`live-wall-tile ${expandedId === id ? "expanded" : ""}`} key={`${id}-${refreshKey}`}>
                <header>
                  <span>
                    <small>{channel.source_id ?? "web"}</small>
                    <strong>{channel.label || channel.source_id || "Kênh"}</strong>
                    <em className={`live-wall-channel-state ${useDesktopTile ? desktopStatus?.state ?? "loading" : desktopDeferred ? "paused" : ""}`}>
                      {useDesktopTile
                        ? desktopStatus?.state === "ready"
                          ? "Đang hiển thị trong app"
                          : desktopStatus?.state === "error"
                            ? "Không tải được trong app"
                            : "Đang mở trong app"
                        : desktopDeferred
                          ? "Tạm dừng để giảm tải máy"
                        : canEmbed
                          ? "Embed trong app"
                          : "Cần mở bằng Cốc Cốc"}
                    </em>
                  </span>
                  <span className="live-wall-tile-actions">
                    {useDesktopTile && channel.source_id === "x" && (
                      <button
                        className="live-wall-x-login"
                        type="button"
                        title="Đăng nhập X trong Content Bot"
                        aria-label="Đăng nhập X trong Content Bot"
                        onClick={() => void desktop.loginX(id)}
                      >
                        <LogIn size={15} />
                        <span>Đăng nhập X</span>
                      </button>
                    )}
                    <a href={url} target="_blank" rel="noreferrer" aria-label="Mở trang gốc"><ExternalLink size={15} /></a>
                    <button type="button" aria-label="Mở trong Cốc Cốc của bạn" onClick={() => void openInCoccoc([channel])} disabled={busy}><Monitor size={15} /></button>
                    <button type="button" aria-label={expandedId === id ? "Thu nhỏ" : "Phóng lớn"} onClick={() => setExpandedId((value) => value === id ? null : id)}>{expandedId === id ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
                  </span>
                </header>
                <div className="live-wall-tile-body">
                  {useDesktopTile ? (
                    <DesktopLiveViewHost
                      channelId={id}
                      status={desktopStatus}
                      registerHost={desktop.registerHost}
                      reload={desktop.reload}
                    />
                  ) : desktopDeferred ? (
                    <div className="live-wall-fallback desktop-live-deferred">
                      <Monitor size={32} />
                      <strong>Kênh đang tạm dừng</strong>
                      <p>Chế độ tiết kiệm chỉ giữ hai trang hoạt động. Bấm để ưu tiên kênh này.</p>
                      <button
                        className="button secondary"
                        type="button"
                        onClick={() => {
                          setDesktopLoadAll(false);
                          setDesktopPriorityId(id);
                        }}
                      >
                        Hiện kênh này
                      </button>
                    </div>
                  ) : canEmbed && channel.source_id === "x" ? (
                    <XTimelineEmbed url={url} refreshKey={refreshKey} onError={() => setFailedEmbeds((current) => current.includes(id) ? current : [...current, id])} />
                  ) : canEmbed && channel.source_id === "tiktok" ? (
                    <TikTokCreatorEmbed url={url} refreshKey={refreshKey} onError={() => setFailedEmbeds((current) => current.includes(id) ? current : [...current, id])} />
                  ) : (
                    <div className="live-wall-fallback">
                      <Monitor size={32} />
                      <strong>
                        {failedEmbeds.includes(id) && channel.source_id === "x"
                          ? "X embed bị chặn hoặc không phản hồi"
                          : failedEmbeds.includes(id)
                            ? "Embed không tải được"
                            : "Nền tảng chưa có channel embed"}
                      </strong>
                      <p>
                        {failedEmbeds.includes(id) && channel.source_id === "x"
                          ? "Hãy cho phép nội dung từ platform.x.com, hoặc mở kênh trong Cốc Cốc của bạn."
                          : "Mở kênh trong Cốc Cốc của bạn để xem trực tiếp."}
                      </p>
                      <button className="button" type="button" onClick={() => void openInCoccoc([channel])} disabled={busy}>Mở trong Cốc Cốc</button>
                    </div>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
