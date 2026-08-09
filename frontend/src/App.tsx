import "./canva-home.css";
import { FormEvent, useEffect, useRef, useState } from "react";
import {
  Activity,
  CircleStop,
  Database,
  Download,
  Film,
  LoaderCircle,
  LogIn,
  Play,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Settings2,
  Video,
  Waypoints,
  X,
} from "lucide-react";
import {
  API_BASE,
  api,
  Batch,
  InsightBucket,
  InsightSummary,
  Item,
  Keyword,
  RunProgressEvent,
  Source,
  SourceRun,
  TrendClusters,
  runEventsUrl,
} from "./api";

import { SubtitleStudio } from "./SubtitleStudioV2";
import { VideoLibrary } from "./VideoLibrary";

type View = "workbench" | "sources" | "settings" | "subtitles" | "videos";
const splitTerms = (value: string) =>
  value
    .split(",")
    .map((term) => term.trim())
    .filter(Boolean);
const fmt = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat("vi-VN", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
const metric = (item: Item) =>
  [
    item.metrics.view_count &&
      `${item.metrics.view_count.toLocaleString()} views`,
    item.metrics.like_count &&
      `${item.metrics.like_count.toLocaleString()} likes`,
    item.metrics.comment_count &&
      `${item.metrics.comment_count.toLocaleString()} replies`,
  ]
    .filter(Boolean)
    .join(" · ") || "No public metrics";

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
  const [view, setView] = useState<View>(() => {
    const requestedView = new URLSearchParams(window.location.search).get("view");
    return requestedView === "subtitles" || requestedView === "videos"
      ? requestedView
      : "workbench";
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
  const [canceling, setCanceling] = useState(false);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [keywordDialogOpen, setKeywordDialogOpen] = useState(false);
  const [keywordDraft, setKeywordDraft] = useState<Keyword | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [sourceFilter, setSourceFilter] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [sentimentFilter, setSentimentFilter] = useState("");
  const [topicFilter, setTopicFilter] = useState("");
  const clusterPollAt = useRef(0);

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
    const loadingTimer = window.setTimeout(() => {
      if (active) setSummaryLoading(true);
    }, 0);
    void Promise.all([
      api.items(selectedId, {
        source: sourceFilter || undefined,
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      }),
      api.insightSummary(selectedId, {
        source: sourceFilter || undefined,
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      }),
      api.runs(selectedId),
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
      window.clearTimeout(loadingTimer);
    };
  }, [
    selectedId,
    sourceFilter,
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
        language: languageFilter || undefined,
        sentiment: sentimentFilter || undefined,
        topic: topicFilter || undefined,
      })
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
      window.clearTimeout(loadingTimer);
    };
  }, [
    selectedId,
    sourceFilter,
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

    const filters = {
      source: sourceFilter || undefined,
      language: languageFilter || undefined,
      sentiment: sentimentFilter || undefined,
      topic: topicFilter || undefined,
    };

    const refreshResults = async () => {
      if (!keywordId) return;
      try {
        const [itemData, summaryData, runData] = await Promise.all([
          api.items(keywordId, filters),
          api.insightSummary(keywordId, filters),
          api.runs(keywordId),
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
        .insightClusters(keywordId, filters)
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
        const nextBatch = await api.run(batchId);
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
      if (event.type === "batch" || event.type === "source-run") {
        void refreshSnapshot();
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
      window.clearInterval(timer);
      eventSource?.close();
    };
  }, [
    activeBatchId,
    activeBatchState,
    selectedId,
    sourceFilter,
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
    const runnableSourceIds = selected.source_ids.filter((sourceId) =>
      sources.some(
        (source) =>
          source.id === sourceId &&
          source.state === "ready" &&
          !source.requires_login,
      ),
    );
    if (!runnableSourceIds.length) {
      setToast(
        "Không có nguồn public chạy nền. Mở Sources và dùng “Login & scan” cho từng nguồn cần đăng nhập.",
      );
      setView("sources");
      return;
    }
    setRunning(true);
    try {
      setBatch(await api.startRun(selected.id, runnableSourceIds));
      setToast(
        `Đã bắt đầu scan ${runnableSourceIds.length} nguồn sẵn sàng.`,
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
      setView("workbench");
      return;
    }
    const source = sources.find((candidate) => candidate.id === sourceId);
    if (!source || source.state !== "ready") {
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
      setBatch(await api.startRun(selected.id, [source.id]));
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
  if (languageFilter) exportParams.set("language", languageFilter);
  if (sentimentFilter) exportParams.set("sentiment", sentimentFilter);
  if (topicFilter) exportParams.set("topic", topicFilter);
  const exported = selected
    ? `${API_BASE}/export.csv?${exportParams}`
    : "#";

  return (
    <div className={`canva-home-app ${view === "subtitles" ? "editor-mode" : "home-mode"}`}>
      <aside className="canva-home-sidebar">
        <div className="canva-home-sidebar-header">
          <Radar size={24} color="#7c3aed" /> Content Bot
        </div>
          
          <button 
            className="canva-home-create-btn"
            onClick={() => {
              setView("workbench");
              setKeywordDraft(null);
              setKeywordDialogOpen(true);
            }}
          >
            <Plus size={18} /> Tạo dự án mới
          </button>

          <nav className="canva-home-nav">
            <button
              className={`canva-home-nav-item ${view === "workbench" ? "active" : ""}`}
              onClick={() => setView("workbench")}
            >
              <Activity size={20} /> Trang chủ
            </button>
            <button
              className={`canva-home-nav-item ${view === "sources" ? "active" : ""}`}
              onClick={() => setView("sources")}
            >
              <Database size={20} /> Nguồn dữ liệu
            </button>
            <button
              className={`canva-home-nav-item ${view === "subtitles" ? "active" : ""}`}
              onClick={() => setView("subtitles")}
            >
              <Film size={20} /> Phụ đề Video
            </button>
            <button
              className={`canva-home-nav-item ${view === "videos" ? "active" : ""}`}
              onClick={() => setView("videos")}
            >
              <Video size={20} /> Quản lý Video
            </button>
            <button
              className={`canva-home-nav-item ${view === "settings" ? "active" : ""}`}
              onClick={() => setView("settings")}
            >
              <Settings2 size={20} /> Cấu hình
            </button>
          </nav>

        <div className="canva-home-sidebar-footer">
          <div className="canva-home-profile">V</div>
          <div style={{flex: 1}}>
            <div style={{fontSize: 13, fontWeight: 600}}>VHC Team</div>
            <div style={{fontSize: 11, color: '#6b7280'}}>Gói Pro</div>
          </div>
        </div>
      </aside>
      
      <main className="canva-home-main">
        {view === "workbench" && (
          <Workbench
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
            onSelect={setSelectedId}
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
          />
        )}
        {view === "sources" && (
          <Sources
            sources={sources}
            loading={loading}
            selected={selected}
            batch={activeBatch ?? batch}
            running={running}
            pendingSourceId={pendingSourceId}
            canceling={canceling}
            onRunSource={(sourceId) => void startSourceRun(sourceId)}
            onCancel={() => void cancelRun()}
          />
        )}
        {view === "settings" && (
          <Settings
            selected={selected}
            onEdit={() => {
              setKeywordDraft(selected);
              setKeywordDialogOpen(true);
            }}
            onDelete={() => {
              if (selected) {
                setDeleteConfirmOpen(true);
              }
            }}
          />
        )}
        {view === "subtitles" && <SubtitleStudio onBack={() => setView("workbench")} />}
        {view === "videos" && <VideoLibrary />}
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

function Workbench({
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
}) {
  return (
    <>
      <div className="canva-home-header">
        <h1>Các dự án dành cho bạn</h1>
      </div>
      
      <div className="canva-section-title">
        <span>Thiết kế gần đây</span>
        <button type="button" className="canva-see-all" onClick={onNew}>+ Thêm dự án mới</button>
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
                <Radar /> {keyword.source_ids.length} nguồn · {keyword.enabled ? `${keyword.interval_minutes} phút` : "thủ công"}
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
            <h2>Chi tiết dự án: {selected.name}</h2>
            <div className="toolbar" style={{display: 'flex', gap: 8}}>
              <button className="button secondary" onClick={onRefresh}>
                <RefreshCw size={15} />
              </button>
              <a className="button secondary" href={onExport} aria-disabled={!selected}>
                <Download size={15} />
              </a>
              <button className="button" onClick={onRun} disabled={running}>
                {running ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}
              </button>
            </div>
          </div>
          {selected && (
            <div className="analysis-filters" aria-label="Result filters">
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
                      Lần quét gần nhất: <strong>{stateLabel(batch.state)}</strong> ·{" "}
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
                              {ingested.toLocaleString("vi-VN")} bài viết · {run.source_runs.length} nguồn
                            </span>
                            {failure && (
                              <span className="run-error" role="alert">
                                {failure.source_id}: {failure.error_message}
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
                        <td className="metrics">{metric(item)}</td>
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
function Sources({
  sources,
  loading,
  selected,
  batch,
  running,
  pendingSourceId,
  canceling,
  onRunSource,
  onCancel,
}: {
  sources: Source[];
  loading: boolean;
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingSourceId: string | null;
  canceling: boolean;
  onRunSource: (sourceId: string) => void;
  onCancel: () => void;
}) {
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
      <section className="page-head">
        <div>
          <h1>Nguồn dữ liệu</h1>
          <p className="subtle">
            Chạy các nguồn công khai trực tiếp, hoặc mở từng nguồn cần đăng nhập.
          </p>
        </div>
      </section>
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
          const sourceIsActive =
            activeBatch &&
            sourceRun &&
            ["queued", "running"].includes(sourceRun.state);
          const unavailable = source.state !== "ready";
          return (
            <article className="source-card" key={source.id}>
              <div className="toolbar">
                <h3>{source.label}</h3>
                <span className={`badge ${source.state}`}>
                  {source.state.replace("_", " ")}
                </span>
              </div>
              <p id={`source-detail-${source.id}`}>{source.detail}</p>
              <div className="source-card-meta">
                <span className="mono">{source.group}</span>
                <span className="mono">
                  {source.requires_login ? "CÔNG KHÔNG - CẦN ĐĂNG NHẬP" : "CÔNG KHAI"}
                </span>
              </div>
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
                  : unavailable
                    ? "Cần cấu hình"
                    : source.requires_login
                      ? "Đăng nhập & quét"
                      : "Quét nguồn"}
              </button>
            </article>
          );
        })}
        {loading && <p className="subtle">Đang kiểm tra cấu hình…</p>}
      </div>
    </>
  );
}
function Settings({
  selected,
  onEdit,
  onDelete,
}: {
  selected: Keyword | null;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <section className="page-head">
        <div>
          <p className="eyebrow">Cấu hình theo dõi</p>
          <h1>Cấu hình</h1>
          <p className="subtle">
            Game được chọn sẽ áp dụng giới hạn nguồn, từ khóa tìm kiếm và chu kỳ tự động.
          </p>
        </div>
      </section>
      {selected ? (
        <section className="panel">
          <div className="panel-body stack">
            <div>
              <strong>{selected.name}</strong>
              <p className="subtle">
                Từ khóa bao gồm: {selected.include_terms.join(", ") || "tên game"}
              </p>
              <p className="subtle">
                Từ khóa loại trừ: {selected.exclude_terms.join(", ") || "không có"}
              </p>
              <p className="subtle">
                Giới hạn mỗi nguồn: {selected.max_items_per_source} bài · chu kỳ:{" "}
                {selected.enabled
                  ? `${selected.interval_minutes} phút`
                  : "thủ công"}
              </p>
            </div>
            <div className="toolbar">
              <button className="button" onClick={onEdit}>
                Chỉnh sửa theo dõi
              </button>
              <button className="button danger" onClick={onDelete}>
                Xóa theo dõi
              </button>
            </div>
          </div>
        </section>
      ) : (
        <Empty onNew={onEdit} />
      )}
    </>
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
            onView("sources");
            onClose();
          }}
        >
          <Database size={15} /> Mở nguồn dữ liệu
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
    const sourceIds = data.getAll("source_ids").map(String);
    const payload = {
      name: String(data.get("name") || "").trim(),
      include_terms: splitTerms(String(data.get("include_terms") || "")),
      exclude_terms: splitTerms(String(data.get("exclude_terms") || "")),
      source_ids: sourceIds,
      enabled: data.get("enabled") === "on",
      interval_minutes: Number(data.get("interval_minutes")),
      max_items_per_source: Number(data.get("max_items_per_source")),
    };
    if (!payload.name) return;
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
            Tên game / Từ khóa chính
            <input
              required
              name="name"
              defaultValue={initial?.name}
              placeholder="ví dụ: Hades II"
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
          <fieldset className="field wide">
            <legend>Nguồn áp dụng</legend>
            <div className="check-list">
              {sources.map((source) => (
                <label
                  className={`check ${source.state !== "ready" ? "unavailable" : ""}`}
                  key={source.id}
                >
                  <input
                    type="checkbox"
                    name="source_ids"
                    value={source.id}
                    disabled={source.state !== "ready"}
                    defaultChecked={
                      source.state === "ready" &&
                      (initial
                        ? initial.source_ids.includes(source.id)
                        : !source.requires_login)
                    }
                  />{" "}
                  {source.label}{" "}
                  <span className="mono">
                    {source.state === "ready" ? "SẴN SÀNG" : "CẦN CẤU HÌNH"}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
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
