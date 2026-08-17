import "./canva-home.css";
import { FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  Activity,
  CircleStop,
  Database,
  Download,
  Film,
  LoaderCircle,
  LogIn,
  LogOut,
  Play,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Trash2,
  Users,
  Video,
  Waypoints,
  X,
} from "lucide-react";
import {
  API_BASE,
  api,
  Batch,
  ChannelSubscription,
  CrawlerLoginStatus,
  InsightBucket,
  InsightSummary,
  Item,
  Keyword,
  RunProgressEvent,
  Source,
  SourceRun,
  TikTokOAuthStatus,
  TrendClusters,
  UpdateCheckResponse,
  runEventsUrl,
} from "./api";
import { useAuth } from "./AuthContext";
import { LoginPage } from "./LoginPage";
import { PendingApprovalPage } from "./PendingApprovalPage";
import { AdminUsersModal } from "./AdminUsersModal";
import { SettingsModal } from "./SettingsModal";
import { UpdateBanner, UpdateModal } from "./UpdateModal";

const SubtitleStudio = lazy(() =>
  import("./SubtitleStudio").then((module) => ({ default: module.SubtitleStudio })),
);
const VideoLibrary = lazy(() =>
  import("./VideoLibrary").then((module) => ({ default: module.VideoLibrary })),
);

type View = "topics" | "library" | "subtitles";
type LibraryTab = "channels" | "connections" | "videos";
const splitTerms = (value: string) =>
  value
    .split(",")
    .map((term) => term.trim())
    .filter(Boolean);

const sourceOperation = (source: Source, operationId: string) =>
  source.operations?.find(
    (operation) => operation.id === operationId && operation.enabled,
  ) ?? source.operations?.find((operation) => operation.id === operationId);

const sourceCanRun = (source: Source, operationId: string) =>
  source.operations?.some(
    (operation) => operation.id === operationId && operation.enabled,
  ) ?? false;

const channelNeedsPaidAccess = (channel: ChannelSubscription) =>
  channel.source_id === "x" &&
  channel.last_error?.startsWith("PAYMENT_OR_ACCESS_REQUIRED:") === true;

const channelErrorDetail = (channel: ChannelSubscription) =>
  channelNeedsPaidAccess(channel)
    ? "X API chưa có credits hoặc access tier phù hợp. Kênh vẫn có thể mở để xem; chỉ thử quét lại sau khi đã cấp quyền trong X Developer Console."
    : channel.last_error;

function XTimelineEmbed({ url }: { url: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const loadWidgets = () => {
      const twitter = (
        window as Window & {
          twttr?: { widgets?: { load: (element?: HTMLElement) => void } };
        }
      ).twttr;
      twitter?.widgets?.load(containerRef.current ?? undefined);
    };
    const existing = document.querySelector<HTMLScriptElement>(
      'script[src="https://platform.twitter.com/widgets.js"]',
    );
    if (existing) {
      loadWidgets();
      existing.addEventListener("load", loadWidgets);
      return () => existing.removeEventListener("load", loadWidgets);
    }
    const script = document.createElement("script");
    script.src = "https://platform.twitter.com/widgets.js";
    script.async = true;
    script.addEventListener("load", loadWidgets);
    document.head.appendChild(script);
    return () => script.removeEventListener("load", loadWidgets);
  }, [url]);
  return (
    <div className="x-timeline-embed" ref={containerRef}>
      <a
        className="twitter-timeline"
        data-height="480"
        data-chrome="noheader nofooter"
        href={url}
      >
        Đang tải bài đăng công khai từ X…
      </a>
    </div>
  );
}
const fmt = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat("vi-VN", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
const humanMetricLabel = (key: string) => {
  const labels: Record<string, string> = {
    view_count: "Views",
    like_count: "Likes",
    comment_count: "Comments",
    share_count: "Shares",
    favorite_count: "Favorites",
    reaction_count: "Reactions",
  };
  return (
    labels[key] ??
    key
      .replace(/_count$/, "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase())
  );
};

const metric = (item: Item, sources: Source[]) => {
  const descriptors =
    sources.find((source) => source.id === item.source_id)?.metrics ?? [];
  const parts: string[] = [];
  const usedKeys = new Set<string>();
  const append = (key: string, label: string) => {
    const value = item.metrics[key];
    if (
      usedKeys.has(key) ||
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      value === 0
    ) {
      return;
    }
    usedKeys.add(key);
    parts.push(`${value.toLocaleString("vi-VN")} ${label}`);
  };

  descriptors.forEach((descriptor) => {
    if (Object.hasOwn(item.metrics, descriptor.id)) {
      append(descriptor.id, descriptor.label);
    }
  });
  descriptors.forEach((descriptor) => {
    const key = descriptor.legacy_key;
    if (!key || usedKeys.has(key) || !Object.hasOwn(item.metrics, key)) return;
    const sharedAlias =
      descriptors.filter((candidate) => candidate.legacy_key === key).length > 1;
    append(key, sharedAlias ? humanMetricLabel(key) : descriptor.label);
  });
  Object.keys(item.metrics).forEach((key) => {
    append(key, humanMetricLabel(key));
  });
  return parts.join(" · ") || "No public metrics";
};

const phaseLabel = (run: SourceRun) => {
  const labels: Record<string, string> = {
    queued: "Đang chờ",
    checking_source: "Kiểm tra nguồn",
    starting: "Khởi động quét",
    searching: "Đang tìm kiếm",
    opening_browser: "Mở trình duyệt Cốc Cốc",
    waiting_login: "Chờ đăng nhập",
    authenticated: "Đã xác thực",
    scanning: "Đang quét sau đăng nhập",
    completed: "Hoàn thành",
    skipped: "Bỏ qua",
    cancelled: "Đã hủy",
    recovering_browser: "Mở lại Cốc Cốc",
    browser_closed: "Cốc Cốc đã đóng",
    parser_drift: "Cấu trúc nguồn đã thay đổi",
    failed: "Thất bại",
  };
  return labels[run.phase] ?? run.phase.replaceAll("_", " ");
};

const stateLabel = (state: string) => {
  const labels: Record<string, string> = {
    queued: "Đang chờ",
    running: "Đang chạy",
    succeeded: "Hoàn thành",
    failed: "Thất bại",
    cancelled: "Đã hủy",
  };
  return labels[state] ?? state;
};

const progressPercent = (run: SourceRun) => {
  if (run.progress_percent !== null) return run.progress_percent;
  if (run.state === "succeeded") return 100;
  if (run.progress_total && run.progress_mode === "determinate") {
    return Math.min((run.progress_current / run.progress_total) * 100, 99);
  }
  return null;
};

const insightEvidence = (item: Item) =>
  Array.from(
    new Set([
      ...item.insights.sentiment.reasons.map(
        (reason) => reason.split(": ").at(-1) ?? reason,
      ),
      ...item.insights.topics.flatMap((topic) => topic.reasons),
    ]),
  ).slice(0, 6);

const safeExternalUrl = (value: string) => {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
};

export default function App() {
  const {
    user,
    isAuthenticated,
    isPending,
    isApproved,
    isBanned,
    isLoading: authLoading,
    isAdmin,
    logout,
  } = useAuth();
  const [adminModalOpen, setAdminModalOpen] = useState(false);
  const [settingsModalOpen, setSettingsModalOpen] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResponse | null>(null);
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [updateModalOpen, setUpdateModalOpen] = useState(false);

  useEffect(() => {
    let isMounted = true;
    const checkUpdates = async () => {
      try {
        const res = await api.checkForUpdate();
        if (isMounted && res && res.update_available) {
          setUpdateInfo(res);
        }
      } catch {
        // Silently ignore update check errors
      }
    };
    void checkUpdates();
    const interval = setInterval(() => {
      void checkUpdates();
    }, 30 * 60 * 1000);
    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, []);

  const [view, setView] = useState<View>(() => {
    const requestedView = new URLSearchParams(window.location.search).get("view");
    if (requestedView === "subtitles") return "subtitles";
    if (["library", "sources", "videos"].includes(requestedView ?? "")) {
      return "library";
    }
    return "topics";
  });
  const [libraryTab, setLibraryTab] = useState<LibraryTab>(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedView = params.get("view");
    const requestedTab = params.get("tab");
    if (requestedView === "videos") return "videos";
    if (requestedView === "sources") return "connections";
    return ["channels", "connections", "videos"].includes(requestedTab ?? "")
      ? (requestedTab as LibraryTab)
      : "channels";
  });
  const [sources, setSources] = useState<Source[]>([]);
  const [keywords, setKeywords] = useState<Keyword[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [summary, setSummary] = useState<InsightSummary | null>(null);
  const [clusters, setClusters] = useState<TrendClusters | null>(null);
  const [runs, setRuns] = useState<Batch[]>([]);
  const [loading, setLoading] = useState(true);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [clustersLoading, setClustersLoading] = useState(false);
  const [clustersError, setClustersError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [running, setRunning] = useState(false);
  const [pendingSourceId, setPendingSourceId] = useState<string | null>(null);
  const [pendingChannelId, setPendingChannelId] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [keywordDialogOpen, setKeywordDialogOpen] = useState(false);
  const [keywordDraft, setKeywordDraft] = useState<Keyword | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [sourceFilter, setSourceFilter] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [sentimentFilter, setSentimentFilter] = useState("");
  const [topicFilter, setTopicFilter] = useState("");
  const clusterPollAt = useRef(0);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    if (view === "library") url.searchParams.set("tab", libraryTab);
    else url.searchParams.delete("tab");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }, [libraryTab, view]);

  const selected =
    keywords.find((keyword) => keyword.id === selectedId) ?? null;
  const activeBatch =
    batch &&
    selected &&
    batch.keyword_id === selected.id &&
    ["queued", "running"].includes(batch.state)
      ? batch
      : null;
  const activeBatchId = batch?.id;
  const activeBatchState = batch?.state;
  const load = async () => {
    setLoading(true);
    try {
      const [nextSources, nextKeywords] = await Promise.all([
        api.sources(),
        api.keywords(),
      ]);
      setSources(nextSources);
      setKeywords(nextKeywords);
      setSelectedId((current) =>
        current && nextKeywords.some((k) => k.id === current)
          ? current
          : (nextKeywords[0]?.id ?? null),
      );
    } catch (error) {
      setToast(
        error instanceof Error
          ? error.message
          : "Unable to reach the local API.",
      );
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => {
    const url = new URL(window.location.href);
    const outcome = url.searchParams.get("tiktok_oauth");
    if (!outcome) return;
    const reason = url.searchParams.get("tiktok_reason");
    url.searchParams.delete("tiktok_oauth");
    url.searchParams.delete("tiktok_reason");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    const timer = window.setTimeout(() => {
      if (outcome === "connected") {
        setToast("Đã kết nối TikTok cho tài khoản creator vừa cấp quyền.");
      } else if (outcome === "denied") {
        setToast(
          "TikTok chưa cấp đủ user.info.basic, user.info.profile và video.list.",
        );
      } else {
        setToast(
          reason === "AUTH_REQUIRED"
            ? "Phiên kết nối TikTok hết hạn. Hãy thử kết nối lại."
            : "Không thể hoàn tất kết nối TikTok. Kiểm tra quyền app rồi thử lại.",
        );
      }
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => {
    if (!selectedId) {
      const timer = window.setTimeout(() => {
        setItems([]);
        setSummary(null);
        setRuns([]);
        setSummaryLoading(false);
      }, 0);
      return () => window.clearTimeout(timer);
    }
    let active = true;
    const controller = new AbortController();
    const loadingTimer = window.setTimeout(() => {
      if (active) setSummaryLoading(true);
    }, 0);
    void Promise.all([
      api.items(selectedId, {
        source: sourceFilter || undefined,
        session: sessionFilter || undefined,
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      }, controller.signal),
      api.insightSummary(selectedId, {
        source: sourceFilter || undefined,
        session: sessionFilter || undefined,
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      }, controller.signal),
      api.runs(selectedId, controller.signal),
    ])
      .then(([itemData, summaryData, runData]) => {
        if (!active) return;
        setItems(itemData.items);
        setSummary(summaryData);
        setRuns(runData);
        setBatch(runData[0] ?? null);
      })
      .catch((error) => {
        if (active) setToast(error.message);
      })
      .finally(() => {
        if (active) setSummaryLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(loadingTimer);
    };
  }, [
    selectedId,
    sourceFilter,
    sessionFilter,
    languageFilter,
    sentimentFilter,
    topicFilter,
    refreshKey,
  ]);
  useEffect(() => {
    if (!selectedId) {
      const timer = window.setTimeout(() => {
        setClusters(null);
        setClustersError(null);
        setClustersLoading(false);
      }, 0);
      return () => window.clearTimeout(timer);
    }
    let active = true;
    const controller = new AbortController();
    const loadingTimer = window.setTimeout(() => {
      if (active) {
        setClusters(null);
        setClustersLoading(true);
        setClustersError(null);
      }
    }, 0);
    void api
      .insightClusters(selectedId, {
        source: sourceFilter || undefined,
        session: sessionFilter || undefined,
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      }, controller.signal)
      .then((data) => {
        if (!active) return;
        setClusters(data);
      })
      .catch((error) => {
        if (!active) return;
        setClusters(null);
        setClustersError(
          error instanceof Error
            ? error.message
            : "Story clusters could not be loaded.",
        );
      })
      .finally(() => {
        if (active) setClustersLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(loadingTimer);
    };
  }, [
    selectedId,
    sourceFilter,
    sessionFilter,
    languageFilter,
    sentimentFilter,
    topicFilter,
    refreshKey,
  ]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  useEffect(() => {
    if (!activeBatchId || !["queued", "running"].includes(activeBatchState ?? "")) {
      return;
    }
    let active = true;
    const batchId = activeBatchId;
    const keywordId = selectedId;
    let eventSource: EventSource | null = null;
    let refreshTimer: number | null = null;
    const controller = new AbortController();

    const filters = {
      source: sourceFilter || undefined,
      session: sessionFilter || undefined,
      language: languageFilter || undefined,
      sentiment: sentimentFilter || undefined,
      topic: topicFilter || undefined,
    };

    const refreshResults = async () => {
      if (!keywordId) return;
      try {
        const [itemData, summaryData, runData] = await Promise.all([
          api.items(keywordId, filters, controller.signal),
          api.insightSummary(keywordId, filters, controller.signal),
          api.runs(keywordId, controller.signal),
        ]);
        if (!active) return;
        setItems(itemData.items);
        setSummary(summaryData);
        setRuns(runData);
      } catch {
        // The progress stream remains useful if an insight refresh is briefly unavailable.
      }
      if (Date.now() - clusterPollAt.current < 5000) return;
      clusterPollAt.current = Date.now();
      setClustersLoading(true);
      void api
        .insightClusters(keywordId, filters, controller.signal)
        .then((clusterData) => {
          if (!active) return;
          setClusters(clusterData);
          setClustersError(null);
        })
        .catch((error) => {
          if (!active) return;
          setClustersError(
            error instanceof Error
              ? error.message
              : "Story clusters could not be refreshed.",
          );
        })
        .finally(() => {
          if (active) setClustersLoading(false);
        });
    };

    const refreshSnapshot = async () => {
      try {
        const nextBatch = await api.run(batchId, controller.signal);
        if (!active) return;
        setBatch(nextBatch);
        if (!["queued", "running"].includes(nextBatch.state)) {
          eventSource?.close();
          setCanceling(false);
          setRefreshKey((current) => current + 1);
        }
        await refreshResults();
      } catch {
        // The next SSE event or polling tick will retry the snapshot.
      }
    };

    const scheduleSnapshotRefresh = () => {
      if (refreshTimer !== null) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void refreshSnapshot();
      }, 400);
    };

    const applyProgressEvent = (event: RunProgressEvent) => {
      if (!active || (event.batch_id && event.batch_id !== batchId)) return;
      if (event.type === "source-progress" && event.source_run_id) {
        setBatch((current) => {
          if (!current || current.id !== batchId) return current;
          return {
            ...current,
            source_runs: current.source_runs.map((sourceRun) => {
              if (sourceRun.id !== event.source_run_id) return sourceRun;
              return {
                ...sourceRun,
                ...(event.state ? { state: event.state } : {}),
                ...(event.phase ? { phase: event.phase } : {}),
                ...(event.progress_mode
                  ? { progress_mode: event.progress_mode }
                  : {}),
                ...(event.progress_current !== undefined
                  ? { progress_current: event.progress_current ?? 0 }
                  : {}),
                ...(event.progress_total !== undefined
                  ? { progress_total: event.progress_total ?? null }
                  : {}),
                ...(event.progress_percent !== undefined
                  ? { progress_percent: event.progress_percent ?? null }
                  : {}),
                ...(event.message !== undefined
                  ? { message: event.message ?? null }
                  : {}),
                ...(event.browser_state !== undefined
                  ? { browser_state: event.browser_state ?? null }
                  : {}),
                ...(event.fetched_count !== undefined
                  ? { fetched_count: event.fetched_count }
                  : {}),
                ...(event.ingested_count !== undefined
                  ? { ingested_count: event.ingested_count }
                  : {}),
              };
            }),
          };
        });
      }
      if (
        event.type === "batch" ||
        event.type === "source-run" ||
        event.type === "parser-drift-alert"
      ) {
        scheduleSnapshotRefresh();
      }
    };

    try {
      eventSource = new EventSource(runEventsUrl(batchId));
      eventSource.onmessage = (event) => {
        try {
          applyProgressEvent(JSON.parse(event.data) as RunProgressEvent);
        } catch {
          // Ignore malformed keep-alive data and wait for the next event.
        }
      };
      eventSource.onerror = () => {
        // Keep the connection open so EventSource can retry; polling remains the fallback.
      };
    } catch {
      eventSource = null;
    }

    const timer = window.setInterval(() => void refreshSnapshot(), 5000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(timer);
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      eventSource?.close();
    };
  }, [
    activeBatchId,
    activeBatchState,
    selectedId,
    sourceFilter,
    sessionFilter,
    languageFilter,
    sentimentFilter,
    topicFilter,
  ]);

  const startRun = async () => {
    if (!selected) {
      setKeywordDraft(null);
      setKeywordDialogOpen(true);
      return;
    }
    const enabledChannelIds = selected.channels
      .filter((channel) => {
        const source = sources.find((candidate) => candidate.id === channel.source_id);
        return (
          channel.enabled &&
          !channelNeedsPaidAccess(channel) &&
          Boolean(source && sourceCanRun(source, "scan_channel"))
        );
      })
      .map((channel) => channel.id)
      .filter((channelId): channelId is string => Boolean(channelId));
    const runnableSourceIds = selected.source_ids.filter((sourceId) =>
      sources.some(
        (source) =>
          source.id === sourceId &&
          sourceCanRun(source, "search") &&
          !source.requires_login,
      ),
    );
    if (!runnableSourceIds.length && !enabledChannelIds.length) {
      setToast(
        "Không có global source hoặc channel nào đang có backend handler sẵn sàng.",
      );
      setLibraryTab("connections");
      setView("library");
      return;
    }
    setRunning(true);
    try {
      setBatch(await api.startRun(selected.id, runnableSourceIds, enabledChannelIds));
      setToast(
        `Đã bắt đầu ${runnableSourceIds.length} global source và ${enabledChannelIds.length} channel.`,
      );
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Could not queue run.");
    } finally {
      setRunning(false);
    }
  };
  const startSourceRun = async (sourceId: string) => {
    if (!selected) {
      setToast("Chọn một game trong Workbench trước khi scan nguồn.");
      setView("topics");
      return;
    }
    const source = sources.find((candidate) => candidate.id === sourceId);
    if (!source || !sourceCanRun(source, "search")) {
      setToast("Nguồn này chưa sẵn sàng. Xem hướng dẫn cấu hình trên thẻ nguồn.");
      return;
    }
    if (activeBatch) {
      setToast("Game này đang có một batch chạy. Hãy đợi hoặc hủy batch hiện tại.");
      return;
    }
    setRunning(true);
    setPendingSourceId(source.id);
    try {
      setBatch(await api.startRun(selected.id, [source.id], []));
      setToast(
        source.requires_login
          ? `Đang khởi động ${source.label}; cửa sổ đăng nhập có thể mất 20–30 giây để xuất hiện. Hoàn tất QR hoặc xác nhận điện thoại ở đó.`
          : `Đã bắt đầu scan ${source.label} cho ${selected.name}.`,
      );
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Could not queue run.");
    } finally {
      setRunning(false);
      setPendingSourceId(null);
    }
  };
  const startChannelRun = async (channelSelection: string | string[]) => {
    if (!selected) {
      setToast("Chọn một chủ đề trước khi quét kênh.");
      setView("topics");
      return;
    }
    const channelIds = Array.isArray(channelSelection)
      ? channelSelection
      : [channelSelection];
    if (!channelIds.length) {
      setToast("Hãy chọn ít nhất một kênh để quét.");
      return;
    }
    if (activeBatch) {
      setToast("Chủ đề này đang có một phiên quét. Hãy đợi hoặc hủy phiên hiện tại.");
      return;
    }
    setRunning(true);
    setPendingChannelId(channelIds.length === 1 ? channelIds[0] : "__bulk__");
    try {
      setBatch(await api.startRun(selected.id, [], channelIds));
      setToast(`Đã bắt đầu quét ${channelIds.length} kênh đã chọn.`);
    } catch (error) {
      setToast(error instanceof Error ? error.message : "Could not queue channel run.");
    } finally {
      setRunning(false);
      setPendingChannelId(null);
    }
  };
  const cancelRun = async () => {
    if (!activeBatch) return;
    setCanceling(true);
    try {
      await api.cancelRun(activeBatch.id);
      setToast("Đã yêu cầu hủy. Trình duyệt crawler sẽ được đóng an toàn.");
    } catch (error) {
      setCanceling(false);
      setToast(
        error instanceof Error ? error.message : "Could not cancel the run.",
      );
    }
  };
  const exportParams = new URLSearchParams();
  if (selected) exportParams.set("keyword_id", String(selected.id));
  if (sourceFilter) exportParams.set("source_id", sourceFilter);
  if (sessionFilter) exportParams.set("session_id", sessionFilter);
  if (languageFilter) exportParams.set("language", languageFilter);
  if (sentimentFilter) exportParams.set("sentiment", sentimentFilter);
  if (topicFilter) exportParams.set("topic", topicFilter);
  const exported = selected
    ? `${API_BASE}/export.csv?${exportParams}`
    : "#";

  if (authLoading) {
    return (
      <div className="auth-container">
        <div style={{ textAlign: "center", color: "#94a3b8" }}>
          <LoaderCircle size={36} className="animate-spin" style={{ margin: "0 auto 16px", color: "#38bdf8" }} />
          <div>Đang kết nối hệ thống xác thực...</div>
        </div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginPage />;
  }

  if (isPending || !isApproved || isBanned) {
    return <PendingApprovalPage />;
  }

  return (
    <div className={`canva-home-app ${view === "subtitles" ? "editor-mode" : "home-mode"}`}>
      <aside className="canva-home-sidebar">
        <div className="canva-home-sidebar-header">
          <Radar size={24} color="#7c3aed" /> Content Bot
        </div>
          
          <button 
            className="canva-home-create-btn"
            onClick={() => {
              setView("topics");
              setKeywordDraft(null);
              setKeywordDialogOpen(true);
            }}
          >
            <Plus size={18} /> Tạo chủ đề mới
          </button>

          <nav className="canva-home-nav">
            <button
              className={`canva-home-nav-item ${view === "topics" ? "active" : ""}`}
              onClick={() => setView("topics")}
            >
              <Activity size={20} /> Chủ đề
            </button>
            <button
              className={`canva-home-nav-item ${view === "library" ? "active" : ""}`}
              onClick={() => setView("library")}
            >
              <Database size={20} /> Nguồn &amp; Video
            </button>
            <button
              className={`canva-home-nav-item ${view === "subtitles" ? "active" : ""}`}
              onClick={() => setView("subtitles")}
            >
              <Film size={20} /> Phụ đề Video
            </button>
          </nav>

        <div className="canva-home-sidebar-footer" style={{ flexDirection: "column", gap: 10, alignItems: "stretch", padding: "14px 12px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {user?.avatar_url ? (
              <img
                src={user.avatar_url}
                alt="Avatar"
                style={{ width: 34, height: 34, borderRadius: "50%", objectFit: "cover" }}
              />
            ) : (
              <div className="canva-home-profile" style={{ width: 34, height: 34, fontSize: 13, background: "#0f5ea8", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: "50%" }}>
                {(user?.display_name || user?.email || "U").charAt(0).toUpperCase()}
              </div>
            )}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {user?.display_name || user?.email?.split("@")[0]}
              </div>
              <div style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }}>
                <span className={`role-tag role-tag-${user?.role || "user"}`}>
                  {(user?.role || "user").toUpperCase()}
                </span>
                <span style={{ color: "#6b7280", fontSize: 10 }}>{user?.auth_provider}</span>
              </div>
            </div>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <button
              type="button"
              className="button secondary"
              style={{ flex: 1, minWidth: 80, fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
              onClick={() => setSettingsModalOpen(true)}
              title="Quản lý Master Password & API Keys cục bộ"
            >
              <Settings2 size={13} color="#7c3aed" /> Cài đặt API
            </button>
            {isAdmin && (
              <button
                type="button"
                className="button secondary"
                style={{ flex: 1, minWidth: 80, fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
                onClick={() => setAdminModalOpen(true)}
                title="Quản lý người dùng & phê duyệt"
              >
                <Users size={13} color="#0f5ea8" /> Quản lý User
              </button>
            )}
            <button
              type="button"
              className="button secondary"
              style={{ fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
              onClick={logout}
              title="Đăng xuất khỏi hệ thống"
            >
              <LogOut size={13} color="#b42318" /> Đăng xuất
            </button>
          </div>
        </div>
      </aside>
      
      <main className="canva-home-main" style={{ display: "flex", flexDirection: "column" }}>
        {updateInfo?.update_available && !updateDismissed && (
          <UpdateBanner
            updateInfo={updateInfo}
            onOpenModal={() => setUpdateModalOpen(true)}
            onDismiss={() => setUpdateDismissed(true)}
          />
        )}
        {view === "topics" && (
          <TopicsWorkspace
            selected={selected}
            keywords={keywords}
            items={items}
            summary={summary}
            summaryLoading={summaryLoading}
            clusters={clusters}
            clustersLoading={clustersLoading}
            clustersError={clustersError}
            runs={runs}
            sources={sources}
            sourceFilter={sourceFilter}
            setSourceFilter={setSourceFilter}
            sessionFilter={sessionFilter}
            setSessionFilter={setSessionFilter}
            languageFilter={languageFilter}
            setLanguageFilter={setLanguageFilter}
            sentimentFilter={sentimentFilter}
            setSentimentFilter={setSentimentFilter}
            topicFilter={topicFilter}
            setTopicFilter={setTopicFilter}
            loading={loading}
            running={running}
            batch={batch}
            canceling={canceling}
            onSelect={(id) => {
              setSelectedId(id);
              setSessionFilter("");
            }}
            onNew={() => {
              setKeywordDraft(null);
              setKeywordDialogOpen(true);
            }}
            onRun={() => void startRun()}
            onCancel={() => void cancelRun()}
            onExport={exported}
            onRefresh={() => {
              void load();
              setRefreshKey((current) => current + 1);
            }}
            onEdit={() => {
              setKeywordDraft(selected);
              setKeywordDialogOpen(true);
            }}
            onDelete={() => {
              if (selected) setDeleteConfirmOpen(true);
            }}
          />
        )}
        {view === "library" && (
          <ContentLibrary
            tab={libraryTab}
            onTabChange={setLibraryTab}
            sources={sources}
            loading={loading}
            selected={selected}
            batch={activeBatch ?? batch}
            running={running}
            pendingSourceId={pendingSourceId}
            canceling={canceling}
            onRunSource={(sourceId) => void startSourceRun(sourceId)}
            pendingChannelId={pendingChannelId}
            onRunChannel={(channelId) => void startChannelRun(channelId)}
            onRunChannels={(channelIds) => void startChannelRun(channelIds)}
            onConnectionsChanged={load}
            onCancel={() => void cancelRun()}
          />
        )}
        {view === "subtitles" && (
          <Suspense fallback={<main className="app-shell">Đang tải Subtitle Studio…</main>}>
            <SubtitleStudio onBack={() => setView("topics")} />
          </Suspense>
        )}
      </main>

      {keywordDialogOpen && (
        <KeywordDialog
          open
          onClose={() => setKeywordDialogOpen(false)}
          initial={keywordDraft}
          sources={sources}
          onSaved={async () => {
            setKeywordDialogOpen(false);
            await load();
          }}
        />
      )}
      {deleteConfirmOpen && selected && (
        <DeleteConfirmDialog
          open
          name={selected.name}
          onClose={() => setDeleteConfirmOpen(false)}
          onConfirm={async () => {
            await api.deleteKeyword(selected.id);
            setDeleteConfirmOpen(false);
            setSessionFilter("");
            await load();
            setToast(`Đã xóa game theo dõi “${selected.name}”.`);
          }}
        />
      )}
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onView={setView}
        onNew={() => {
          setPaletteOpen(false);
          setKeywordDraft(null);
          setKeywordDialogOpen(true);
        }}
        onRun={() => {
          setPaletteOpen(false);
          void startRun();
        }}
      />
      <AdminUsersModal
        isOpen={adminModalOpen}
        onClose={() => setAdminModalOpen(false)}
      />
      <SettingsModal
        isOpen={settingsModalOpen}
        onClose={() => setSettingsModalOpen(false)}
      />
      <UpdateModal
        open={updateModalOpen}
        onClose={() => setUpdateModalOpen(false)}
        updateInfo={updateInfo}
      />
      {toast && (
        <div role="status" className="toast">
          {toast}
          <button aria-label="Dismiss" onClick={() => setToast(null)}>
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

function TopicsWorkspace({
  selected,
  keywords,
  items,
  summary,
  summaryLoading,
  clusters,
  clustersLoading,
  clustersError,
  runs,
  sources,
  sourceFilter,
  setSourceFilter,
  sessionFilter,
  setSessionFilter,
  languageFilter,
  setLanguageFilter,
  sentimentFilter,
  setSentimentFilter,
  topicFilter,
  setTopicFilter,
  loading,
  running,
  batch,
  canceling,
  onSelect,
  onNew,
  onRun,
  onCancel,
  onExport,
  onRefresh,
  onEdit,
  onDelete,
}: {
  selected: Keyword | null;
  keywords: Keyword[];
  items: Item[];
  summary: InsightSummary | null;
  summaryLoading: boolean;
  clusters: TrendClusters | null;
  clustersLoading: boolean;
  clustersError: string | null;
  runs: Batch[];
  sources: Source[];
  sourceFilter: string;
  setSourceFilter: (value: string) => void;
  sessionFilter: string;
  setSessionFilter: (value: string) => void;
  languageFilter: string;
  setLanguageFilter: (value: string) => void;
  sentimentFilter: string;
  setSentimentFilter: (value: string) => void;
  topicFilter: string;
  setTopicFilter: (value: string) => void;
  loading: boolean;
  running: boolean;
  batch: Batch | null;
  canceling: boolean;
  onSelect: (id: number) => void;
  onNew: () => void;
  onRun: () => void;
  onCancel: () => void;
  onExport: string;
  onRefresh: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <div className="canva-home-header">
        <h1>Chủ đề theo dõi</h1>
        <p className="subtle">
          Chọn một chủ đề để xem bài mới, lịch sử phiên và cấu hình thu thập.
        </p>
      </div>
      
      <div className="canva-section-title">
        <span>Chủ đề gần đây</span>
        <button type="button" className="canva-see-all" onClick={onNew}>
          <Plus size={16} /> Thêm chủ đề mới
        </button>
      </div>

      <div className="canva-design-grid">
        {keywords.map((keyword) => (
          <div
            key={keyword.id}
            className={`canva-design-card ${selected?.id === keyword.id ? "active" : ""}`}
            onClick={() => onSelect(keyword.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(keyword.id);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="canva-design-thumbnail">
              <Activity />
            </div>
            <div className="canva-design-info">
              <h3 className="canva-design-title">{keyword.name}</h3>
              <div className="canva-design-meta">
                <Radar /> {keyword.channels.length} kênh · {keyword.enabled ? `${keyword.interval_minutes} phút` : "thủ công"}
              </div>
            </div>
          </div>
        ))}
      </div>

      {!keywords.length && !loading && (
        <p style={{color: '#6b7280', marginTop: 16}}>Chưa có dự án nào. Hãy tạo một dự án mới.</p>
      )}

      {selected && (
        <section className="panel" style={{marginTop: 40}}>
          <div className="panel-head" style={{display: 'flex', justifyContent: 'space-between'}}>
            <h2>Chi tiết chủ đề: {selected.name}</h2>
            <div className="toolbar" style={{display: 'flex', gap: 8}}>
              <button
                className="button secondary"
                onClick={onEdit}
                aria-label="Chỉnh sửa cấu hình chủ đề"
              >
                <Settings2 size={15} /> Chỉnh sửa
              </button>
              <button className="button secondary" onClick={onRefresh} aria-label="Làm mới dữ liệu">
                <RefreshCw size={15} />
              </button>
              <a className="button secondary" href={onExport} aria-disabled={!selected} aria-label="Xuất dữ liệu CSV">
                <Download size={15} />
              </a>
              <button className="button" onClick={onRun} disabled={running} aria-label="Quét chủ đề">
                {running ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}
                Quét ngay
              </button>
            </div>
          </div>
          <TopicSettingsSummary
            selected={selected}
            onEdit={onEdit}
            onDelete={onDelete}
          />
          <div className="registered-channel-list" aria-label="Kênh đã đăng ký">
            {selected.channels.map((channel) => (
              <div className="registered-channel-entry" key={channel.id ?? channel.url}>
                <a
                  className="registered-channel"
                  href={channel.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span>
                    <strong>{channel.label || channel.source_id || "Kênh"}</strong>
                    <small>{channel.url}</small>
                  </span>
                  <span className={`badge ${channel.last_status ?? ""}`}>
                    {channel.mode === "embed_only"
                      ? "xem trực tiếp"
                      : channel.last_status === "succeeded"
                        ? "đã quét"
                        : "đã lưu"}
                  </span>
                </a>
                {channel.source_id === "x" && channel.mode === "embed_only" && (
                  <XTimelineEmbed url={channel.url} />
                )}
              </div>
            ))}
          </div>
          {selected && (
            <div className="analysis-filters" aria-label="Result filters">
              <select
                className="filter"
                value={sessionFilter}
                onChange={(event) => setSessionFilter(event.target.value)}
                aria-label="Lọc theo phiên"
              >
                <option value="">5 phiên gần nhất</option>
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    Phiên #{run.session_number} · {fmt(run.started_at)} · {run.new_item_count} bài mới
                  </option>
                ))}
              </select>
              <select
                className="filter"
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value)}
                aria-label="Filter by source"
              >
                <option value="">Tất cả nguồn</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.label}
                  </option>
                ))}
              </select>
              <select
                className="filter"
                value={languageFilter}
                onChange={(event) => setLanguageFilter(event.target.value)}
                aria-label="Lọc theo ngôn ngữ"
              >
                <option value="">Tất cả ngôn ngữ</option>
                <option value="vi">Tiếng Việt</option>
                <option value="en">Tiếng Anh</option>
                <option value="zh">Tiếng Trung</option>
                <option value="ja">Tiếng Nhật</option>
                <option value="ko">Tiếng Hàn</option>
                <option value="und">Không xác định</option>
              </select>
              <select
                className="filter"
                value={sentimentFilter}
                onChange={(event) => setSentimentFilter(event.target.value)}
                aria-label="Lọc theo cảm xúc"
              >
                <option value="">Tất cả cảm xúc</option>
                <option value="positive">Tích cực</option>
                <option value="negative">Tiêu cực</option>
                <option value="mixed">Hỗn hợp</option>
                <option value="neutral">Trung lập</option>
              </select>
              <select
                className="filter"
                value={topicFilter}
                onChange={(event) => setTopicFilter(event.target.value)}
                aria-label="Lọc theo chủ đề game"
              >
                <option value="">Tất cả chủ đề</option>
                <option value="bugs">Lỗi &amp; sự cố</option>
                <option value="performance">Hiệu năng</option>
                <option value="gameplay">Gameplay</option>
                <option value="updates">Cập nhật &amp; nội dung</option>
                <option value="monetization">Kiếm tiền hóa</option>
                <option value="story">Cốt truyện &amp; lịch sử</option>
                <option value="community">Cộng đồng</option>
              </select>
            </div>
          )}
          {selected && (
            <>
              {/* ── Zone 1: Trạng thái quét ── */}
              {batch && (
                <section className={`zone zone-scan ${batch.state}`} aria-label="Trạng thái quét">
                  <h3 className="zone-title">🔄 Trạng thái quét</h3>
                  <div className="notice">
                    <span>
                      Phiên #{batch.session_number}: <strong>{stateLabel(batch.state)}</strong> ·{" "}
                      {
                        batch.source_runs.filter(
                          (run) => run.state === "succeeded",
                        ).length
                      }
                      /{batch.source_runs.length} nguồn hoàn thành.
                    </span>
                    {["queued", "running"].includes(batch.state) && (
                      <button
                        className="button danger compact-button"
                        onClick={onCancel}
                        disabled={canceling}
                      >
                        {canceling ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : (
                          <CircleStop size={15} />
                        )}
                        {canceling ? "Đang hủy" : "Hủy quét"}
                      </button>
                    )}
                  </div>
                  {!!runs.length && (
                    <ol className="run-list" aria-label="Lịch sử quét gần đây">
                      {runs.slice(0, 3).map((run) => {
                        const ingested = run.source_runs.reduce(
                          (total, sourceRun) => total + sourceRun.ingested_count,
                          0,
                        );
                        const failure = run.source_runs.find(
                          (sourceRun) => sourceRun.error_message,
                        );
                        return (
                          <li key={run.id}>
                            <span className={`badge ${run.state}`}>{stateLabel(run.state)}</span>
                            <span className="mono">{fmt(run.started_at)}</span>
                            <span className="run-summary">
                              Phiên #{run.session_number} · {ingested.toLocaleString("vi-VN")} bài mới · {run.source_runs.length} kênh
                            </span>
                            {failure && (
                              <span className="run-error" role="alert">
                                {failure.channel_label || failure.source_id}: {failure.error_message}
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ol>
                  )}
                </section>
              )}

              {/* ── Zone 2: Tổng quan phân tích ── */}
              <section className="zone zone-analytics" aria-label="Tổng quan phân tích">
                <InsightOverview
                  summary={summary}
                  loading={summaryLoading}
                  sources={sources}
                />
                <StoryClusters
                  data={clusters}
                  loading={clustersLoading}
                  error={clustersError}
                  sources={sources}
                  onRetry={onRefresh}
                />
              </section>

              {/* ── Zone 3: Danh sách bài viết ── */}
              <section className="zone zone-content" aria-label="Danh sách bài viết">
                <h3 className="zone-title">📋 Danh sách bài viết</h3>
                <table>
                  <thead>
                    <tr>
                      <th>Bài viết</th>
                      <th>Nguồn</th>
                      <th>Tương tác</th>
                      <th>Xu hướng</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <a
                            className="item-title"
                            href={safeExternalUrl(item.canonical_url)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {item.title}
                          </a>
                          <div className="mono">
                            {item.author || "Không rõ tác giả"} ·{" "}
                            {fmt(item.published_at)}
                          </div>
                          <div
                            className="insight-line"
                            aria-label={`Phân tích: ${item.insights.language.label}, cảm xúc ${item.insights.sentiment.label}${item.insights.topics.length ? `, chủ đề ${item.insights.topics.map((topic) => topic.label).join(", ")}` : ""}`}
                          >
                            <span className="insight-chip language">
                              {item.insights.language.code.toUpperCase()} ·{" "}
                              {Math.round(
                                item.insights.language.confidence * 100,
                              )}
                              %
                            </span>
                            <span
                              className={`insight-chip sentiment ${item.insights.sentiment.label}`}
                            >
                              {item.insights.sentiment.label}
                            </span>
                            {item.insights.topics.slice(0, 3).map((topic) => (
                              <span className="insight-chip" key={topic.id}>
                                {topic.label}
                              </span>
                            ))}
                          </div>
                          {!!insightEvidence(item).length && (
                            <div className="insight-evidence">
                              Tín hiệu: {insightEvidence(item).join(", ")}
                            </div>
                          )}
                        </td>
                        <td>
                          <span className="badge">{item.source_id}</span>
                        </td>
                        <td className="metrics">{metric(item, sources)}</td>
                        <td>
                          <span className="score">
                            {item.trend_score.toFixed(1)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!items.length && !loading && (
                  <Empty onNew={onRun} action="Quét ngay" compact />
                )}
              </section>
            </>
          )}
        </section>
      )}
    </>
  );
}

function StoryClusters({
  data,
  loading,
  error,
  sources,
  onRetry,
}: {
  data: TrendClusters | null;
  loading: boolean;
  error: string | null;
  sources: Source[];
  onRetry: () => void;
}) {
  const sourceLabels = new Map(
    sources.map((source) => [source.id, source.label]),
  );
  return (
    <section
      className="story-clusters"
      aria-labelledby="story-clusters-title"
      aria-busy={loading}
    >
      <div className="story-clusters-head">
        <div>
          <h3 id="story-clusters-title">
            <Waypoints size={15} aria-hidden="true" /> 📰 Nhóm tin tức liên quan
          </h3>
          <p>
            Liên kết chung hoặc tiêu đề tương tự giữa các nguồn khác nhau.
          </p>
        </div>
        {data && (
          <span className="story-cluster-count">
            {data.truncated
              ? `Top ${data.returned_cluster_count.toLocaleString("vi-VN")} of ${data.cluster_count.toLocaleString("vi-VN")}`
              : `${data.cluster_count.toLocaleString("vi-VN")} clusters`} ·{" "}
            {data.returned_clustered_items.toLocaleString("vi-VN")} shown
          </span>
        )}
      </div>
      {loading && !data && (
        <div className="story-skeleton" aria-label="Loading story clusters">
          <span />
          <span />
        </div>
      )}
      {error && (
        <div className="story-error" role="alert">
          <span>Không tải được nhóm tin. {error}</span>
          <button className="button secondary compact-button" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" /> Thử lại
          </button>
        </div>
      )}
      {data && data.clusters.length > 0 && (
        <div className="story-cluster-list">
          {data.clusters.map((cluster) => (
            <details className="story-cluster" key={cluster.id}>
              <summary>
                <span className="story-cluster-label" title={cluster.label}>
                  {cluster.label}
                </span>
                <span className="story-cluster-meta">
                  {cluster.item_count.toLocaleString("vi-VN")} bài ·{" "}
                  {cluster.origin_count.toLocaleString("vi-VN")} nguồn · đỉnh{" "}
                  {cluster.max_trend_score.toFixed(1)}
                </span>
              </summary>
              <div className="story-cluster-body">
                <dl>
                  <div>
                    <dt>Nguồn gốc</dt>
                    <dd>{cluster.item_hosts.join(", ")}</dd>
                  </div>
                  <div>
                    <dt>Nguồn dữ liệu</dt>
                    <dd>
                      {cluster.source_ids
                        .map((id) => sourceLabels.get(id) ?? id)
                        .join(", ")}
                    </dd>
                  </div>
                  <div>
                    <dt>Lý do nhóm</dt>
                    <dd>{cluster.match_reasons.join(" · ")}</dd>
                  </div>
                </dl>
                <ol className="story-members">
                  {cluster.items.slice(0, 6).map((item) => (
                    <li key={item.id}>
                      <a
                        href={safeExternalUrl(item.canonical_url)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {item.title}
                      </a>
                      <span className="mono">
                        {item.item_host} · {item.sentiment} ·{" "}
                        {item.trend_score.toFixed(1)}
                      </span>
                    </li>
                  ))}
                </ol>
                {cluster.items.length > 6 && (
                  <p className="story-more">
                    {cluster.items.length - 6} thành viên khác có sẵn từ API.
                  </p>
                )}
              </div>
            </details>
          ))}
        </div>
      )}
      {data && !data.clusters.length && !error && (
        <p className="story-empty">
          {data.total_items > 0
            ? `Không có nhóm tin lặp lại vượt ngưỡng từ ${data.total_items.toLocaleString("vi-VN")} bài viết đã lọc.`
            : "Chưa có bài viết nào. Quét ít nhất hai nguồn công khai để tìm nhóm tin."}
        </p>
      )}
      {data && (
        <p className="story-caveat">
          Kiểm tra từng thành viên trước khi coi nhóm này là một câu chuyện. Phương pháp:{" "}
          {data.method}.
        </p>
      )}
    </section>
  );
}

function InsightOverview({
  summary,
  loading,
  sources,
}: {
  summary: InsightSummary | null;
  loading: boolean;
  sources: Source[];
}) {
  const sourceLabels = new Map(
    sources.map((source) => [source.id, source.label]),
  );
  return (
    <section
      className="insight-overview"
      aria-labelledby="insight-overview-title"
      aria-busy={loading}
    >
      <div className="insight-overview-head">
        <div>
          <h3 id="insight-overview-title">📈 Tổng quan phân tích</h3>
          <p>
            {loading && !summary
              ? "Đang cập nhật dữ liệu…"
              : `${summary?.total_items.toLocaleString("vi-VN") ?? 0} bài viết công khai`}
          </p>
        </div>
        {summary && (
          <span className="summary-coverage">
            {summary.topic_coverage_count.toLocaleString("vi-VN")} đã gắn chủ đề ·{" "}
            {summary.topic_coverage_percentage.toLocaleString("vi-VN")}%
          </span>
        )}
      </div>
      {summary && summary.total_items > 0 ? (
        <>
          <div className="insight-overview-grid">
            <div className="insight-group">
              <h4>Cảm xúc</h4>
              <SummaryBuckets rows={summary.sentiments} />
            </div>
            <div className="insight-group">
              <h4>Chủ đề game</h4>
              <SummaryBuckets rows={summary.topics.slice(0, 5)} />
            </div>
            <div className="insight-group">
              <h4>Nguồn</h4>
              <SummaryBuckets
                rows={summary.sources.slice(0, 5)}
                labelFor={(row) => sourceLabels.get(row.id) ?? row.label}
              />
            </div>
            <div className="insight-group">
              <h4>Từ khóa nổi bật</h4>
              {summary.top_signals.length ? (
                <ul className="signal-list">
                  {summary.top_signals.slice(0, 6).map((signal) => (
                    <li key={signal.id}>
                      <span>{signal.label}</span>
                      <span className="mono">{signal.count}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="summary-empty">Không phát hiện tín hiệu nào.</p>
              )}
            </div>
          </div>
          {!!summary.top_items.length && (
            <div className="rising-strip">
              <h4>🔥 Bài viết xu hướng cao nhất</h4>
              <ol>
                {summary.top_items.slice(0, 5).map((item) => (
                  <li key={item.id}>
                    <a
                      href={safeExternalUrl(item.canonical_url)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.title}
                    </a>
                    <span className="mono">
                      {sourceLabels.get(item.source_id) ?? item.source_id} ·{" "}
                      {item.sentiment} · {item.trend_score.toFixed(1)}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </>
      ) : (
        !loading && (
          <p className="summary-empty">
            Không có bài viết nào phù hợp với bộ lọc hiện tại.
          </p>
        )
      )}
      {summary && (
        <p className="summary-caveat">
          {summary.caveat} Phương pháp: {summary.method}.
        </p>
      )}
    </section>
  );
}

function SummaryBuckets({
  rows,
  labelFor = (row) => row.label,
}: {
  rows: InsightBucket[];
  labelFor?: (row: InsightBucket) => string;
}) {
  if (!rows.length) {
    return <p className="summary-empty">Không phát hiện danh mục nào.</p>;
  }
  return (
    <ul className="summary-buckets">
      {rows.map((row) => (
        <li key={row.id}>
          <div className="bucket-label">
            <span>{labelFor(row)}</span>
            <span className="mono">
              {row.count.toLocaleString("vi-VN")} ·{" "}
              {row.percentage.toLocaleString("vi-VN")}%
            </span>
          </div>
          <span className="bucket-track" aria-hidden="true">
            <span style={{ width: `${Math.min(row.percentage, 100)}%` }} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function Empty({
  onNew,
  action = "Add a tracked game",
  compact = false,
}: {
  onNew: () => void;
  action?: string;
  compact?: boolean;
}) {
  return (
    <div className={`empty ${compact ? "compact" : ""}`}>
      <Radar size={30} color="var(--cobalt)" />
      <h2>
        {compact ? "Chưa có bài viết phù hợp" : "Bắt đầu với một game"}
      </h2>
      <p>
        {compact
          ? "Bảng dữ liệu sẽ trống cho đến khi nguồn được cấu hình trả về nội dung công khai."
          : "Tạo bộ từ khóa, sau đó chạy quét từ các nguồn đã cấu hình."}
      </p>
      <button className="button" onClick={onNew}>
        <Plus size={15} /> {action}
      </button>
    </div>
  );
}
function ContentLibrary({
  tab,
  onTabChange,
  sources,
  loading,
  selected,
  batch,
  running,
  pendingSourceId,
  pendingChannelId,
  canceling,
  onRunSource,
  onRunChannel,
  onRunChannels,
  onConnectionsChanged,
  onCancel,
}: {
  tab: LibraryTab;
  onTabChange: (tab: LibraryTab) => void;
  sources: Source[];
  loading: boolean;
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingSourceId: string | null;
  pendingChannelId: string | null;
  canceling: boolean;
  onRunSource: (sourceId: string) => void;
  onRunChannel: (channelId: string) => void;
  onRunChannels: (channelIds: string[]) => void;
  onConnectionsChanged: () => Promise<void>;
  onCancel: () => void;
}) {
  const tabs: { id: LibraryTab; label: string; icon: typeof Database }[] = [
    { id: "channels", label: "Kênh theo dõi", icon: Waypoints },
    { id: "connections", label: "Kết nối nền tảng", icon: Database },
    { id: "videos", label: "Video", icon: Video },
  ];
  return (
    <>
      <section className="page-head content-library-head">
        <div>
          <p className="eyebrow">Kho nội dung</p>
          <h1>Nguồn &amp; Video</h1>
          <p className="subtle">
            Quản lý kênh thu thập, kết nối nền tảng và toàn bộ video trong cùng một nơi.
          </p>
        </div>
        {selected && <span className="library-topic-context">Chủ đề: {selected.name}</span>}
      </section>
      <div className="content-library-tabs" role="tablist" aria-label="Nguồn và video">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            aria-controls={`library-panel-${id}`}
            className={`content-library-tab ${tab === id ? "active" : ""}`}
            onClick={() => onTabChange(id)}
          >
            <Icon size={17} /> {label}
          </button>
        ))}
      </div>
      <section
        id={`library-panel-${tab}`}
        className="content-library-panel"
        role="tabpanel"
      >
        {tab === "channels" && (
          <RegisteredChannels
            key={selected?.id ?? "none"}
            selected={selected}
            batch={batch}
            running={running}
            pendingChannelId={pendingChannelId}
            onRunChannel={onRunChannel}
            onRunChannels={onRunChannels}
          />
        )}
        {tab === "connections" && (
          <Sources
            embedded
            sources={sources}
            loading={loading}
            selected={selected}
            batch={batch}
            running={running}
            pendingSourceId={pendingSourceId}
            canceling={canceling}
            onRunSource={onRunSource}
            onConnectionsChanged={onConnectionsChanged}
            onCancel={onCancel}
          />
        )}
        {tab === "videos" && (
          <Suspense fallback={<div className="library-loading"><LoaderCircle className="spin" size={24} /> Đang tải thư viện video…</div>}>
            <VideoLibrary embedded />
          </Suspense>
        )}
      </section>
    </>
  );
}

function RegisteredChannels({
  selected,
  batch,
  running,
  pendingChannelId,
  onRunChannel,
  onRunChannels,
}: {
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingChannelId: string | null;
  onRunChannel: (channelId: string) => void;
  onRunChannels: (channelIds: string[]) => void;
}) {
  const activeBatch =
    batch && selected && batch.keyword_id === selected.id &&
    ["queued", "running"].includes(batch.state)
      ? batch
      : null;
  const scannableIds = (selected?.channels ?? [])
    .filter((channel) => {
      const mode = channel.mode ?? "manual";
      return Boolean(channel.id) && !["embed_only", "manual", "setup_required"].includes(mode);
    })
    .map((channel) => channel.id as string);
  const [selectedChannelIds, setSelectedChannelIds] = useState<string[]>(scannableIds);
  const currentSelectedIds = selectedChannelIds.filter((id) => scannableIds.includes(id));
  const allSelected = scannableIds.length > 0 &&
    scannableIds.every((id) => currentSelectedIds.includes(id));
  const toggleChannel = (channelId: string) => {
    setSelectedChannelIds((current) =>
      current.includes(channelId)
        ? current.filter((id) => id !== channelId)
        : [...current, channelId],
    );
  };
  const toggleAll = () => {
    setSelectedChannelIds(allSelected ? [] : scannableIds);
  };

  if (!selected) {
    return (
      <div className="empty compact">
        <Radar size={30} color="var(--cobalt)" />
        <h2>Chưa chọn chủ đề</h2>
        <p>Chọn một chủ đề ở mục Chủ đề để xem và quét các kênh đã đăng ký.</p>
      </div>
    );
  }
  if (!selected.channels.length) {
    return (
      <div className="empty compact">
        <Waypoints size={30} color="var(--cobalt)" />
        <h2>Chủ đề này chưa có kênh</h2>
        <p>Mở trang Chủ đề và chỉnh sửa cấu hình để thêm link kênh cần theo dõi.</p>
      </div>
    );
  }
  return (
    <div>
      <div className="registered-channel-selection" aria-label="Chọn kênh để quét">
        <label className="channel-select-all">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={toggleAll}
            disabled={!scannableIds.length || Boolean(activeBatch) || running}
          />
          <span>Chọn tất cả</span>
        </label>
        <span className="channel-selection-count">
          {currentSelectedIds.length}/{scannableIds.length} kênh có thể quét
        </span>
        <button
          className="button"
          type="button"
          onClick={() => onRunChannels(currentSelectedIds)}
          disabled={!currentSelectedIds.length || Boolean(activeBatch) || running}
        >
          <Play size={15} /> Quét kênh đã chọn
        </button>
      </div>
      {!scannableIds.length && (
        <p className="channel-selection-hint">
          Các kênh hiện tại chỉ xem hoặc cần cấu hình nên chưa thể quét tự động.
        </p>
      )}
      <div className="registered-channel-grid">
        {selected.channels.map((channel) => {
        const channelRun = batch?.source_runs.find(
          (run) => run.channel_id === channel.id,
        );
        const cannotScan = ["embed_only", "manual", "setup_required"].includes(
          channel.mode ?? "manual",
        );
        const needsPaidAccess = channelNeedsPaidAccess(channel);
        const canSelect = Boolean(channel.id) && !cannotScan;
        const isSelected = Boolean(channel.id && currentSelectedIds.includes(channel.id));
        return (
          <article className="registered-channel-card" key={channel.id ?? channel.url}>
            <div className="registered-channel-card-head">
              <label className="channel-select-checkbox">
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => channel.id && toggleChannel(channel.id)}
                  disabled={!canSelect || Boolean(activeBatch) || running}
                  aria-label={`Chọn kênh ${channel.label || channel.url}`}
                />
              </label>
              <div>
                <span className="eyebrow">{channel.source_id ?? "web"}</span>
                <h2>{channel.label || channel.url}</h2>
              </div>
              <span className={`badge ${channel.last_status ?? ""}`}>
                {channel.mode === "embed_only"
                  ? "Chỉ xem"
                  : channel.last_status === "succeeded"
                    ? "Đã quét"
                    : channel.last_status === "failed"
                      ? "Có lỗi"
                      : "Đã lưu"}
              </span>
            </div>
            <a href={channel.url} target="_blank" rel="noreferrer" className="channel-url">
              {channel.url}
            </a>
            <dl className="channel-meta-grid">
              <div>
                <dt>Quét gần nhất</dt>
                <dd>{fmt(channel.last_scanned_at)}</dd>
              </div>
              <div>
                <dt>Bài mới gần nhất</dt>
                <dd>{channelRun?.ingested_count ?? 0}</dd>
              </div>
            </dl>
            {channel.last_error && channel.mode !== "embed_only" && (
              <p className="channel-error">{channelErrorDetail(channel)}</p>
            )}
            <div className="toolbar registered-channel-actions">
              <a className="button secondary" href={channel.url} target="_blank" rel="noreferrer">
                Mở kênh
              </a>
              {!cannotScan && channel.id && (
                <button
                  className="button"
                  type="button"
                  onClick={() => onRunChannel(channel.id as string)}
                  disabled={Boolean(activeBatch) || running}
                >
                  {pendingChannelId === channel.id ? (
                    <LoaderCircle className="spin" size={15} />
                  ) : needsPaidAccess ? (
                    <RefreshCw size={15} />
                  ) : (
                    <Play size={15} />
                  )}
                  {pendingChannelId === channel.id
                    ? "Đang khởi động"
                    : needsPaidAccess
                      ? "Thử lại X API"
                      : "Quét kênh"}
                </button>
              )}
            </div>
          </article>
        );
        })}
      </div>
    </div>
  );
}

function Sources({
  embedded = false,
  sources,
  loading,
  selected,
  batch,
  running,
  pendingSourceId,
  canceling,
  onRunSource,
  onConnectionsChanged,
  onCancel,
}: {
  embedded?: boolean;
  sources: Source[];
  loading: boolean;
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingSourceId: string | null;
  canceling: boolean;
  onRunSource: (sourceId: string) => void;
  onConnectionsChanged: () => Promise<void>;
  onCancel: () => void;
}) {
  const [tiktokStatus, setTikTokStatus] = useState<TikTokOAuthStatus | null>(null);
  const [tiktokUsername, setTikTokUsername] = useState("");
  const [tiktokBusy, setTikTokBusy] = useState(false);
  const [tiktokError, setTikTokError] = useState("");
  const [crawlerLogins, setCrawlerLogins] = useState<
    Record<string, CrawlerLoginStatus>
  >({});
  const [crawlerLoginBusy, setCrawlerLoginBusy] = useState<string | null>(null);
  const [crawlerLoginErrors, setCrawlerLoginErrors] = useState<
    Record<string, string>
  >({});
  useEffect(() => {
    if (!sources.some((source) => source.id === "tiktok")) return;
    let active = true;
    void api
      .tiktokOAuthStatus()
      .then((status) => {
        if (!active) return;
        setTikTokStatus(status);
        setTikTokError("");
        if (status.authorized_username) {
          setTikTokUsername(status.authorized_username);
        }
      })
      .catch(() => {
        if (active) {
          setTikTokStatus(null);
          setTikTokError("Không đọc được trạng thái OAuth TikTok.");
        }
      });
    return () => {
      active = false;
    };
  }, [sources]);

  const activeCrawlerLoginIds = Object.values(crawlerLogins)
    .filter((session) =>
      ["opening", "waiting_for_user", "verifying"].includes(session.state),
    )
    .map((session) => session.source_id)
    .sort()
    .join(",");
  useEffect(() => {
    if (!activeCrawlerLoginIds) return;
    let active = true;
    const sourceIds = activeCrawlerLoginIds.split(",");
    const poll = async () => {
      const results = await Promise.allSettled(
        sourceIds.map((sourceId) => api.crawlerLoginStatus(sourceId)),
      );
      if (!active) return;
      setCrawlerLogins((current) => {
        const next = { ...current };
        results.forEach((result, index) => {
          if (result.status === "fulfilled") {
            next[sourceIds[index]] = result.value;
          }
        });
        return next;
      });
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeCrawlerLoginIds]);

  const connectTikTok = () => {
    const username = tiktokUsername.trim().replace(/^@/, "");
    if (!/^[A-Za-z0-9._]{2,24}$/.test(username)) {
      setTikTokError("Username TikTok phải dài 2–24 ký tự và chỉ gồm chữ, số, dấu chấm hoặc gạch dưới.");
      return;
    }
    setTikTokError("");
    window.location.assign(api.tiktokOAuthStartUrl(username));
  };
  const refreshTikTok = async () => {
    setTikTokBusy(true);
    try {
      setTikTokStatus(await api.refreshTikTokOAuth());
      setTikTokError("");
      await onConnectionsChanged();
    } catch (error) {
      setTikTokError(
        error instanceof Error ? error.message : "Không thể làm mới token TikTok.",
      );
    } finally {
      setTikTokBusy(false);
    }
  };
  const disconnectTikTok = async () => {
    if (!window.confirm("Thu hồi quyền TikTok và xóa token đã mã hóa?")) return;
    setTikTokBusy(true);
    try {
      await api.disconnectTikTokOAuth();
      setTikTokStatus(await api.tiktokOAuthStatus());
      setTikTokError("");
      await onConnectionsChanged();
    } catch (error) {
      setTikTokError(
        error instanceof Error ? error.message : "Không thể ngắt kết nối TikTok.",
      );
    } finally {
      setTikTokBusy(false);
    }
  };
  const startCrawlerLogin = async (sourceId: string) => {
    setCrawlerLoginBusy(sourceId);
    try {
      const session = await api.startCrawlerLogin(sourceId);
      setCrawlerLogins((current) => ({ ...current, [sourceId]: session }));
      setCrawlerLoginErrors((current) => ({ ...current, [sourceId]: "" }));
    } catch (error) {
      setCrawlerLoginErrors((current) => ({
        ...current,
        [sourceId]:
          error instanceof Error
            ? error.message
            : "Không thể mở profile trình duyệt v2.",
      }));
    } finally {
      setCrawlerLoginBusy(null);
    }
  };
  const stopCrawlerLogin = async (sourceId: string) => {
    setCrawlerLoginBusy(sourceId);
    try {
      const session = await api.stopCrawlerLogin(sourceId);
      setCrawlerLogins((current) => ({ ...current, [sourceId]: session }));
      setCrawlerLoginErrors((current) => ({ ...current, [sourceId]: "" }));
    } catch (error) {
      setCrawlerLoginErrors((current) => ({
        ...current,
        [sourceId]:
          error instanceof Error
            ? error.message
            : "Không thể đóng phiên đăng nhập.",
      }));
    } finally {
      setCrawlerLoginBusy(null);
    }
  };
  const activeBatch =
    batch &&
    selected &&
    batch.keyword_id === selected.id &&
    ["queued", "running"].includes(batch.state)
      ? batch
      : null;
  const sourceRuns = new Map(
    (batch?.source_runs ?? []).map((sourceRun) => [
      sourceRun.source_id,
      sourceRun,
    ]),
  );
  return (
    <>
      {!embedded && <section className="page-head">
        <div>
          <h1>Nguồn dữ liệu</h1>
          <p className="subtle">
            Chạy các nguồn công khai trực tiếp, hoặc mở từng nguồn cần đăng nhập.
          </p>
        </div>
      </section>}
      <section className="source-guidance" aria-label="Scan guidance">
        <div>
          <strong>
            {selected
              ? `Game đang chọn: "${selected.name}"`
              : "Chọn game trước"}
          </strong>
          <p>
            {selected
              ? "Nguồn cần đăng nhập sẽ mở trình duyệt sau 20–30 giây. Giữ batch chạy, hoàn tất QR hoặc xác nhận điện thoại trong đó; Content Bot không bao giờ vượt qua bước này."
              : "Mở Phân tích và chọn một game đang theo dõi. Các nút nguồn sẽ bị tắt cho đến khi chọn game."}
          </p>
        </div>
        {activeBatch && (
          <button
            className="button danger"
            onClick={onCancel}
            disabled={canceling}
          >
            {canceling ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <CircleStop size={15} />
            )}
            {canceling ? "Đang hủy" : "Hủy quét"}
          </button>
        )}
      </section>
      {batch && selected && batch.keyword_id === selected.id && (
        <section
          className="source-progress"
          aria-label="Current source run"
          aria-live="polite"
        >
          <div className="source-progress-head">
            <strong>Đợt quét #{batch.id.slice(0, 8)}</strong>
            <span className={`badge ${batch.state}`}>{batch.state}</span>
          </div>
          <ul>
            {batch.source_runs.map((sourceRun) => {
              const source = sources.find(
                (candidate) => candidate.id === sourceRun.source_id,
              );
              return (
                <li key={sourceRun.id}>
                  <span>
                    <strong>{source?.label ?? sourceRun.source_id}</strong>
                    <span className="mono">
                      {phaseLabel(sourceRun)} · đã lấy {sourceRun.fetched_count} ·
                      đã lưu {sourceRun.ingested_count}
                    </span>
                  </span>
                  <div
                    className={`run-progress-track ${
                      sourceRun.progress_mode === "indeterminate"
                        ? "indeterminate"
                        : ""
                    }`}
                    role="progressbar"
                    aria-label={`${source?.label ?? sourceRun.source_id} progress`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    {...(progressPercent(sourceRun) !== null
                      ? {
                          "aria-valuenow": Math.round(
                            progressPercent(sourceRun) ?? 0,
                          ),
                        }
                      : {})}
                  >
                    <span
                      className="run-progress-fill"
                      style={
                        progressPercent(sourceRun) !== null
                          ? { width: `${progressPercent(sourceRun)}%` }
                          : undefined
                      }
                    />
                  </div>
                  <span className="source-progress-meta">
                    <span>
                      {sourceRun.message || phaseLabel(sourceRun)}
                      {sourceRun.browser_state === "waiting_login" &&
                        " · QR/xác nhận điện thoại đang hiển thị trong Cốc Cốc"}
                    </span>
                    <span className="mono">
                      {progressPercent(sourceRun) !== null
                        ? `${Math.round(progressPercent(sourceRun) ?? 0)}%`
                        : "đang xử lý"}
                    </span>
                  </span>
                  {sourceRun.error_message && (
                    <span className="source-run-error" role="alert">
                      {sourceRun.error_code && (
                        <strong>{sourceRun.error_code}: </strong>
                      )}
                      {sourceRun.error_message}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <div className="source-grid">
        {sources.map((source) => {
          const sourceRun = sourceRuns.get(source.id);
          const primary = sourceOperation(source, source.primary_operation);
          const embedOnly =
            sourceCanRun(source, "render_embed") &&
            !sourceCanRun(source, "search");
          const channelOnly =
            source.primary_operation === "scan_channel" &&
            sourceCanRun(source, "scan_channel") &&
            !sourceCanRun(source, "search");
          const sourceIsActive =
            activeBatch &&
            sourceRun &&
            ["queued", "running"].includes(sourceRun.state);
          const unavailable = !sourceCanRun(source, "search");
          const hasCbceProfile =
            source.requires_login &&
            source.provider_selection.includes(`cbce_${source.id}`);
          const crawlerLogin = crawlerLogins[source.id];
          const crawlerLoginActive =
            crawlerLogin &&
            ["opening", "waiting_for_user", "verifying"].includes(
              crawlerLogin.state,
            );
          const authLabel = primary?.auth_modes.includes("oauth")
            ? "OAUTH"
            : source.requires_login
              ? "CẦN ĐĂNG NHẬP TƯƠNG TÁC"
              : "CÔNG KHAI";
          return (
            <article className="source-card" key={source.id}>
              <div className="toolbar">
                <h3>{source.label}</h3>
                <span className={`badge ${source.state}`}>
                  {source.state.replace("_", " ")}
                </span>
              </div>
              <p id={`source-detail-${source.id}`}>
                {primary?.detail || source.coverage_disclaimer || source.detail}
              </p>
              <div className="source-card-meta">
                <span className="mono">{source.group}</span>
                <span className="mono">
                  {authLabel}
                </span>
                {primary?.budget_limits && (
                  <span className="mono">
                    Tối đa {primary.budget_limits.max_items} bài / lượt
                  </span>
                )}
              </div>
              {source.id === "tiktok" ? (
                <div className="source-oauth-controls" aria-busy={tiktokBusy}>
                  {tiktokError && (
                    <span className="source-oauth-error" role="alert">
                      {tiktokError}
                    </span>
                  )}
                  {tiktokStatus?.connected ? (
                    <>
                      <span className="source-connection-account">
                        Đã kết nối @{tiktokStatus.authorized_username}
                      </span>
                      <div className="source-oauth-actions">
                        <button
                          className="button secondary"
                          type="button"
                          disabled={tiktokBusy}
                          onClick={() => void refreshTikTok()}
                        >
                          {tiktokBusy ? (
                            <LoaderCircle className="spin" size={15} />
                          ) : (
                            <RefreshCw size={15} />
                          )}
                          Làm mới token
                        </button>
                        <button
                          className="button danger"
                          type="button"
                          disabled={tiktokBusy}
                          onClick={() => void disconnectTikTok()}
                        >
                          {tiktokBusy ? (
                            <LoaderCircle className="spin" size={15} />
                          ) : (
                            <Trash2 size={15} />
                          )}
                          Ngắt kết nối
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <label className="source-oauth-username" htmlFor="tiktok-oauth-username">
                        TikTok creator đã được duyệt
                      </label>
                      <input
                        id="tiktok-oauth-username"
                        className="source-oauth-input"
                        value={tiktokUsername}
                        onChange={(event) => setTikTokUsername(event.target.value)}
                        placeholder="game.dev"
                        maxLength={24}
                        autoComplete="off"
                        aria-describedby="tiktok-oauth-helper"
                        aria-invalid={Boolean(tiktokError)}
                        disabled={!tiktokStatus?.configured || tiktokBusy}
                      />
                      <small id="tiktok-oauth-helper" className="source-oauth-helper">
                        Chỉ video công khai của chính tài khoản cấp quyền được đọc.
                      </small>
                      <button
                        className="button secondary source-run-button"
                        type="button"
                        onClick={connectTikTok}
                        disabled={
                          !tiktokStatus?.configured ||
                          !tiktokUsername.trim() ||
                          tiktokBusy
                        }
                      >
                        {tiktokBusy ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : (
                          <LogIn size={15} />
                        )}
                        {tiktokStatus === null
                          ? "Đang kiểm tra OAuth"
                          : tiktokStatus.configured
                            ? "Kết nối bằng TikTok"
                            : "Cần cấu hình app TikTok"}
                      </button>
                    </>
                  )}
                </div>
              ) : (
                <div className="source-oauth-controls">
                  <button
                    className="button secondary source-run-button"
                    type="button"
                    onClick={() => onRunSource(source.id)}
                    disabled={
                      !selected || unavailable || Boolean(activeBatch) || running
                    }
                    aria-describedby={`source-detail-${source.id}`}
                  >
                    {sourceIsActive || pendingSourceId === source.id ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : source.requires_login ? (
                      <LogIn size={15} />
                    ) : (
                      <Play size={15} />
                    )}
                    {sourceIsActive
                      ? "Đang chạy"
                      : pendingSourceId === source.id
                        ? "Đang khởi động"
                        : embedOnly
                          ? "Chỉ xem embed"
                          : channelOnly
                            ? "Dùng mục Kênh đã lưu"
                            : unavailable
                              ? primary?.implementation === "planned"
                                ? "Đang lên kế hoạch"
                                : "Cần cấu hình"
                              : source.requires_login
                                ? "Đăng nhập & quét"
                                : "Quét nguồn"}
                  </button>
                  {hasCbceProfile && (
                    <>
                      {crawlerLoginErrors[source.id] && (
                        <span className="source-oauth-error" role="alert">
                          {crawlerLoginErrors[source.id]}
                        </span>
                      )}
                      <small className="source-oauth-helper">
                        {crawlerLogin?.detail ||
                          "Mở profile v2 riêng để đăng nhập thủ công trước khi review contract."}
                      </small>
                      <button
                        className={`button ${crawlerLoginActive ? "danger" : "secondary"}`}
                        type="button"
                        disabled={
                          crawlerLoginBusy === source.id ||
                          Boolean(activeBatch) ||
                          running
                        }
                        onClick={() =>
                          void (crawlerLoginActive
                            ? stopCrawlerLogin(source.id)
                            : startCrawlerLogin(source.id))
                        }
                      >
                        {crawlerLoginBusy === source.id ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : crawlerLoginActive ? (
                          <CircleStop size={15} />
                        ) : (
                          <LogIn size={15} />
                        )}
                        {crawlerLoginActive
                          ? crawlerLogin?.state === "verifying"
                            ? "Dừng xác minh profile"
                            : "Đóng phiên đăng nhập"
                          : "Mở profile CBCE v2"}
                      </button>
                    </>
                  )}
                </div>
              )}
            </article>
          );
        })}
        {loading && <p className="subtle">Đang kiểm tra cấu hình…</p>}
      </div>
    </>
  );
}
function TopicSettingsSummary({
  selected,
  onEdit,
  onDelete,
}: {
  selected: Keyword | null;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    selected ? (
      <section className="topic-settings-summary" aria-label="Cấu hình chủ đề">
        <div className="topic-settings-heading">
          <div>
            <span className="eyebrow">Cấu hình chủ đề</span>
            <strong>{selected.enabled ? "Tự động đang bật" : "Đang chạy thủ công"}</strong>
          </div>
          <div className="toolbar">
            <button className="button secondary" onClick={onEdit}>
              <Settings2 size={15} /> Sửa cấu hình
            </button>
            <button className="button danger" onClick={onDelete}>
              <Trash2 size={15} /> Xóa chủ đề
            </button>
          </div>
        </div>
        <dl className="topic-settings-grid">
          <div>
            <dt>Từ khóa bao gồm</dt>
            <dd>{selected.include_terms.join(", ") || selected.name}</dd>
          </div>
          <div>
            <dt>Từ khóa loại trừ</dt>
            <dd>{selected.exclude_terms.join(", ") || "Không có"}</dd>
          </div>
          <div>
            <dt>Giới hạn</dt>
            <dd>{selected.max_items_per_source} bài / kênh</dd>
          </div>
          <div>
            <dt>Chu kỳ</dt>
            <dd>{selected.enabled ? `${selected.interval_minutes} phút` : "Thủ công"}</dd>
          </div>
        </dl>
      </section>
    ) : null
  );
}
function CommandPalette({
  open,
  onClose,
  onView,
  onNew,
  onRun,
}: {
  open: boolean;
  onClose: () => void;
  onView: (view: View) => void;
  onNew: () => void;
  onRun: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open) ref.current?.showModal();
    else ref.current?.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-label="Bảng lệnh nhanh">
      <div className="modal-head">
        <strong>Bảng lệnh nhanh</strong>
        <button className="button secondary" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      <div className="modal-body stack">
        <button className="command" onClick={onNew}>
          <Plus size={15} /> Thêm game theo dõi
        </button>
        <button className="command" onClick={onRun}>
          <Search size={15} /> Quét game đang chọn
        </button>
        <button
          className="command"
          onClick={() => {
            onView("library");
            onClose();
          }}
        >
          <Database size={15} /> Mở Nguồn &amp; Video
        </button>
      </div>
    </dialog>
  );
}
function KeywordDialog({
  open,
  onClose,
  initial,
  sources,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  initial: Keyword | null;
  sources: Source[];
  onSaved: () => Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (open) ref.current?.showModal();
    else ref.current?.close();
  }, [open]);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const previousChannels = new Map(
      (initial?.channels ?? []).map((channel) => [
        channel.url.trim().replace(/\/$/, "").toLocaleLowerCase(),
        channel,
      ]),
    );
    const channelUrls = String(data.get("channel_urls") || "")
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    const includeReplies = data.get("include_replies") === "on";
    const includeReposts = data.get("include_reposts") === "on";
    const channels = Array.from(new Set(channelUrls)).map((url) => {
      const previous = previousChannels.get(
        url.replace(/\/$/, "").toLocaleLowerCase(),
      );
      return {
        ...(previous ?? {
          id: null,
          normalized_url: null,
          label: "",
          source_id: null,
          mode: null,
          enabled: true,
          include_replies: false,
          include_reposts: false,
          last_scanned_at: null,
          last_status: null,
          last_error: null,
        }),
        url,
        enabled: true,
        include_replies: includeReplies,
        include_reposts: includeReposts,
      };
    });
    const payload = {
      name: String(data.get("name") || "").trim(),
      include_terms: splitTerms(String(data.get("include_terms") || "")),
      exclude_terms: splitTerms(String(data.get("exclude_terms") || "")),
      source_ids: data.getAll("source_ids").map(String),
      channels,
      enabled: data.get("enabled") === "on",
      interval_minutes: Number(data.get("interval_minutes")),
      max_items_per_source: Number(data.get("max_items_per_source")),
    };
    if (!payload.name || (!payload.channels.length && !payload.source_ids.length)) return;
    setSaving(true);
    try {
      if (initial) await api.updateKeyword(initial.id, payload);
      else await api.createKeyword(payload);
      await onSaved();
    } finally {
      setSaving(false);
    }
  };
  return (
    <dialog ref={ref} onClose={onClose} aria-label="Biểu mẫu game theo dõi">
      <form onSubmit={(event) => void submit(event)}>
        <div className="modal-head">
          <strong>{initial ? "Sửa thông tin theo dõi" : "Thêm game theo dõi"}</strong>
          <button
            className="button secondary"
            type="button"
            onClick={onClose}
            aria-label="Đóng biểu mẫu"
          >
            <X size={15} />
          </button>
        </div>
        <div className="modal-body form-grid">
          <label className="field wide">
            Tên chủ đề
            <input
              required
              name="name"
              defaultValue={initial?.name}
              placeholder="ví dụ: Game NTE"
              autoFocus
            />
          </label>
          <label className="field">
            Từ khóa phụ (phân cách bằng dấu phẩy)
            <input
              name="include_terms"
              defaultValue={initial?.include_terms.join(", ")}
              placeholder="Hades 2, Supergiant"
            />
          </label>
          <label className="field">
            Từ khóa loại trừ (phân cách bằng dấu phẩy)
            <input
              name="exclude_terms"
              defaultValue={initial?.exclude_terms.join(", ")}
              placeholder="giveaway, spam"
            />
          </label>
          <label className="field">
            Chu kỳ tự động
            <select
              name="interval_minutes"
              defaultValue={initial?.interval_minutes ?? 360}
            >
              <option value="60">Mỗi 1 giờ</option>
              <option value="360">Mỗi 6 giờ</option>
              <option value="1440">Mỗi ngày</option>
            </select>
          </label>
          <label className="field">
            Số bài tối đa / nguồn
            <input
              name="max_items_per_source"
              type="number"
              min="1"
              max="500"
              defaultValue={initial?.max_items_per_source ?? 100}
            />
          </label>
          <fieldset className="field wide source-picker">
            <legend>Global sources dùng để tìm theo từ khóa</legend>
            <div className="source-picker-grid">
              {sources
                .filter((source) => source.global_search)
                .map((source) => {
                  const search = sourceOperation(source, "search");
                  const ready = sourceCanRun(source, "search");
                  return (
                    <label className="source-picker-option" key={source.id}>
                      <input
                        type="checkbox"
                        name="source_ids"
                        value={source.id}
                        defaultChecked={
                          initial
                            ? initial.source_ids.includes(source.id)
                            : ready && !source.requires_login
                        }
                      />
                      <span>
                        <strong>{source.label}</strong>
                        <small>
                          {ready
                            ? source.requires_login
                              ? "Chạy thủ công, cần đăng nhập"
                              : "Sẵn sàng chạy nền"
                            : search?.detail || "Chưa sẵn sàng"}
                        </small>
                      </span>
                    </label>
                  );
                })}
            </div>
            <small>
              Global source và link channel là hai nhóm độc lập; một phiên có thể chạy cả hai.
            </small>
          </fieldset>
          <label className="field wide">
            Link kênh cần theo dõi (mỗi dòng một link)
            <textarea
              name="channel_urls"
              rows={6}
              defaultValue={initial?.channels.map((channel) => channel.url).join("\n")}
              placeholder={"https://x.com/NTE_Ani_Info\nhttps://www.youtube.com/@YourChannel"}
            />
            <small>
              Hệ thống tự nhận diện nền tảng. Kênh thiếu API vẫn được lưu để mở xem,
              nhưng sẽ không tự tải bài.
            </small>
          </label>
          <div className="field wide channel-options">
            <label className="check">
              <input
                type="checkbox"
                name="include_replies"
                defaultChecked={initial?.channels.some((channel) => channel.include_replies)}
              />{" "}
              Lấy cả bài trả lời
            </label>
            <label className="check">
              <input
                type="checkbox"
                name="include_reposts"
                defaultChecked={initial?.channels.some((channel) => channel.include_reposts)}
              />{" "}
              Lấy cả bài đăng lại
            </label>
          </div>
          <label className="field wide">
            <span>
              <input
                type="checkbox"
                name="enabled"
                defaultChecked={initial?.enabled ?? true}
              />{" "}
              Kích hoạt chạy tự động theo chu kỳ
            </span>
          </label>
        </div>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            Hủy
          </button>
          <button className="button" disabled={saving}>
            {saving ? "Đang lưu…" : "Lưu thay đổi"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function DeleteConfirmDialog({
  open,
  name,
  onClose,
  onConfirm,
}: {
  open: boolean;
  name: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (open) ref.current?.showModal();
    else ref.current?.close();
  }, [open]);

  const handleConfirm = async () => {
    setDeleting(true);
    try {
      await onConfirm();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <dialog ref={ref} onClose={onClose} aria-label="Xác nhận xóa game">
      <div className="modal-head">
        <strong>Xác nhận xóa game theo dõi</strong>
        <button
          className="button secondary"
          type="button"
          onClick={onClose}
          aria-label="Đóng"
        >
          <X size={15} />
        </button>
      </div>
      <div className="modal-body stack">
        <p>
          Bạn có chắc chắn muốn xóa game <strong>“{name}”</strong> khỏi danh sách theo dõi?
        </p>
        <p className="subtle">
          Tất cả thiết lập từ khóa và dữ liệu phân tích liên quan đến game này sẽ bị loại bỏ khỏi danh sách hiển thị.
        </p>
      </div>
      <div className="modal-actions">
        <button
          type="button"
          className="button secondary"
          onClick={onClose}
          disabled={deleting}
        >
          Hủy
        </button>
        <button
          type="button"
          className="button danger"
          onClick={() => void handleConfirm()}
          disabled={deleting}
        >
          {deleting ? "Đang xóa…" : "Xóa game"}
        </button>
      </div>
    </dialog>
  );
}
