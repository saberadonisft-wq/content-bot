import { FormEvent, useEffect, useRef, useState } from "react";
import {
  Activity,
  CircleStop,
  Command,
  Database,
  Download,
  LoaderCircle,
  LogIn,
  Play,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Settings2,
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

type View = "workbench" | "sources" | "settings";
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
    queued: "Queued",
    checking_source: "Checking source",
    starting: "Starting scan",
    searching: "Scanning public items",
    opening_browser: "Opening Cốc Cốc",
    waiting_login: "Waiting for login",
    authenticated: "Login confirmed",
    scanning: "Scanning after login",
    completed: "Completed",
    skipped: "Skipped",
    cancelled: "Cancelled",
    recovering_browser: "Reopening Cốc Cốc",
    browser_closed: "Cốc Cốc was closed",
    failed: "Failed",
  };
  return labels[run.phase] ?? run.phase.replaceAll("_", " ");
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

export default function App() {
  const [view, setView] = useState<View>("workbench");
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
  const [sourceFilter, setSourceFilter] = useState("");
  const [languageFilter, setLanguageFilter] = useState("");
  const [sentimentFilter, setSentimentFilter] = useState("");
  const [topicFilter, setTopicFilter] = useState("");
  const clusterPollAt = useRef(0);

  const selected =
    keywords.find((keyword) => keyword.id === selectedId) ?? null;
  const readySources = sources.filter((source) => source.state === "ready");
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
        // Browsers retry EventSource automatically, while the snapshot poll keeps the UI moving.
        eventSource?.close();
        eventSource = null;
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
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to workspace
      </a>
      <header className="masthead">
        <div className="brand">
          <span className="brand-mark">
            <Radar size={15} />
          </span>{" "}
          Content Bot
        </div>
        <button
          className="command"
          type="button"
          onClick={() => setPaletteOpen(true)}
          aria-label="Open command palette"
        >
          <Command size={15} /> Jump to a command <kbd>Ctrl K</kbd>
        </button>
        <div className="status-line">
          LOCAL / SQLITE / {readySources.length} READY
        </div>
      </header>
      <main id="main" className="main">
        <nav className="toolbar" aria-label="Workspace navigation">
          <button
            className={`button secondary ${view === "workbench" ? "active" : ""}`}
            onClick={() => setView("workbench")}
          >
            <Activity size={15} /> Workbench
          </button>
          <button
            className={`button secondary ${view === "sources" ? "active" : ""}`}
            onClick={() => setView("sources")}
          >
            <Database size={15} /> Sources
          </button>
          <button
            className={`button secondary ${view === "settings" ? "active" : ""}`}
            onClick={() => setView("settings")}
          >
            <Settings2 size={15} /> Settings
          </button>
        </nav>
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
            onDelete={async () => {
              if (
                selected &&
                confirm(`Delete tracked game “${selected.name}”?`)
              ) {
                await api.deleteKeyword(selected.id);
                await load();
              }
            }}
          />
        )}
      </main>
      <footer className="footer">
        <span>Public metadata only · 90-day retention · no CAPTCHA bypass</span>
        <span>
          {batch
            ? `RUN ${batch.id.slice(0, 8)} / ${batch.state.toUpperCase()}`
            : "IDLE"}
        </span>
      </footer>
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
      <section className="page-head">
        <div>
          <p className="eyebrow">Game social listening</p>
          <h1>Signal workbench</h1>
          <p className="subtle">
            Rank public game discussion by relevance, engagement and recency.
          </p>
        </div>
        <div className="toolbar">
          <button className="button secondary" onClick={onRefresh}>
            <RefreshCw size={15} /> Refresh
          </button>
          <a
            className="button secondary"
            href={onExport}
            aria-disabled={!selected}
          >
            <Download size={15} /> CSV
          </a>
          <button className="button" onClick={onRun} disabled={running}>
            {running ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <Search size={15} />
            )}
            {running ? "Queuing" : "Run now"}
          </button>
        </div>
      </section>
      <section className="workspace">
        <aside className="panel">
          <div className="panel-head">
            <h2>Tracked games</h2>
            <button
              className="button secondary"
              onClick={onNew}
              aria-label="Add tracked game"
            >
              <Plus size={15} />
            </button>
          </div>
          <div className="panel-body">
            <ul className="keyword-list">
              {keywords.map((keyword) => (
                <li key={keyword.id}>
                  <button
                    className={selected?.id === keyword.id ? "active" : ""}
                    onClick={() => onSelect(keyword.id)}
                  >
                    <span className="keyword-name">{keyword.name}</span>
                    <span className="keyword-meta">
                      {keyword.enabled
                        ? `${keyword.interval_minutes} min`
                        : "manual"}{" "}
                      · {keyword.source_ids.length} sources
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {!keywords.length && !loading && (
              <p className="subtle">No tracked games yet.</p>
            )}
          </div>
        </aside>
        <section className="panel">
          <div className="panel-head">
            <h2>{selected ? selected.name : "No selection"}</h2>
          </div>
          {selected && (
            <div className="analysis-filters" aria-label="Result filters">
              <select
                className="filter"
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value)}
                aria-label="Filter by source"
              >
                <option value="">All sources</option>
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
                aria-label="Filter by language"
              >
                <option value="">All languages</option>
                <option value="vi">Vietnamese</option>
                <option value="en">English</option>
                <option value="zh">Chinese</option>
                <option value="ja">Japanese</option>
                <option value="ko">Korean</option>
                <option value="und">Unknown</option>
              </select>
              <select
                className="filter"
                value={sentimentFilter}
                onChange={(event) => setSentimentFilter(event.target.value)}
                aria-label="Filter by sentiment"
              >
                <option value="">All sentiments</option>
                <option value="positive">Positive</option>
                <option value="negative">Negative</option>
                <option value="mixed">Mixed</option>
                <option value="neutral">Neutral</option>
              </select>
              <select
                className="filter"
                value={topicFilter}
                onChange={(event) => setTopicFilter(event.target.value)}
                aria-label="Filter by game topic"
              >
                <option value="">All topics</option>
                <option value="bugs">Bugs &amp; crashes</option>
                <option value="performance">Performance</option>
                <option value="gameplay">Gameplay</option>
                <option value="updates">Updates &amp; content</option>
                <option value="monetization">Monetization</option>
                <option value="story">Story &amp; lore</option>
                <option value="community">Community</option>
              </select>
            </div>
          )}
          {selected && (
            <InsightOverview
              summary={summary}
              loading={summaryLoading}
              sources={sources}
            />
          )}
          {selected && (
            <StoryClusters
              data={clusters}
              loading={clustersLoading}
              error={clustersError}
              sources={sources}
              onRetry={onRefresh}
            />
          )}
          {!selected ? (
            <Empty onNew={onNew} />
          ) : (
            <div className="table-wrap">
              {batch && (
                <div className="notice">
                  <span>
                    Latest run: <strong>{batch.state}</strong> ·{" "}
                    {
                      batch.source_runs.filter(
                        (run) => run.state === "succeeded",
                      ).length
                    }
                    /{batch.source_runs.length} source jobs completed.
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
                      {canceling ? "Canceling" : "Cancel run"}
                    </button>
                  )}
                </div>
              )}
              {!!runs.length && (
                <ol className="run-list" aria-label="Recent scan history">
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
                        <span className={`badge ${run.state}`}>{run.state}</span>
                        <span className="mono">{fmt(run.started_at)}</span>
                        <span className="run-summary">
                          {ingested.toLocaleString("vi-VN")} items · {run.source_runs.length} sources
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
              <table>
                <thead>
                  <tr>
                    <th>Public item</th>
                    <th>Source</th>
                    <th>Metrics</th>
                    <th>Trend</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <a
                          className="item-title"
                          href={item.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {item.title}
                        </a>
                        <div className="mono">
                          {item.author || "Unknown author"} ·{" "}
                          {fmt(item.published_at)}
                        </div>
                        <div
                          className="insight-line"
                          aria-label={`Rule-based analysis: ${item.insights.language.label}, ${item.insights.sentiment.label} sentiment${item.insights.topics.length ? `, topics ${item.insights.topics.map((topic) => topic.label).join(", ")}` : ""}`}
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
                            Signals: {insightEvidence(item).join(", ")}
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
                <Empty onNew={onRun} action="Run a scan" compact />
              )}
            </div>
          )}
        </section>
      </section>
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
            <Waypoints size={15} aria-hidden="true" /> Story clusters
          </h3>
          <p>
            Shared links or strong title overlap across distinct origins.
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
          <span>Story clusters failed to load. {error}</span>
          <button className="button secondary compact-button" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" /> Try again
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
                  {cluster.item_count.toLocaleString("vi-VN")} items ·{" "}
                  {cluster.origin_count.toLocaleString("vi-VN")} origins · peak{" "}
                  {cluster.max_trend_score.toFixed(1)}
                </span>
              </summary>
              <div className="story-cluster-body">
                <dl>
                  <div>
                    <dt>Item hosts</dt>
                    <dd>{cluster.item_hosts.join(", ")}</dd>
                  </div>
                  <div>
                    <dt>Sources</dt>
                    <dd>
                      {cluster.source_ids
                        .map((id) => sourceLabels.get(id) ?? id)
                        .join(", ")}
                    </dd>
                  </div>
                  <div>
                    <dt>Why grouped</dt>
                    <dd>{cluster.match_reasons.join(" · ")}</dd>
                  </div>
                </dl>
                <ol className="story-members">
                  {cluster.items.slice(0, 6).map((item) => (
                    <li key={item.id}>
                      <a
                        href={item.canonical_url}
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
                    {cluster.items.length - 6} more members are available from
                    the API.
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
            ? `No repeated story passed the conservative evidence threshold across ${data.total_items.toLocaleString("vi-VN")} filtered items.`
            : "No filtered items are available yet. Scan at least two public origins to look for repeated stories."}
        </p>
      )}
      {data && (
        <p className="story-caveat">
          Review every member before treating a cluster as one story. Method:{" "}
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
          <h3 id="insight-overview-title">Filtered overview</h3>
          <p>
            {loading && !summary
              ? "Updating the filtered sample…"
              : `${summary?.total_items.toLocaleString("vi-VN") ?? 0} public items in this view`}
          </p>
        </div>
        {summary && (
          <span className="summary-coverage">
            {summary.topic_coverage_count.toLocaleString("vi-VN")} topic-tagged ·{" "}
            {summary.topic_coverage_percentage.toLocaleString("vi-VN")}%
          </span>
        )}
      </div>
      {summary && summary.total_items > 0 ? (
        <>
          <div className="insight-overview-grid">
            <div className="insight-group">
              <h4>Sentiment</h4>
              <SummaryBuckets rows={summary.sentiments} />
            </div>
            <div className="insight-group">
              <h4>Game topics</h4>
              <SummaryBuckets rows={summary.topics.slice(0, 5)} />
            </div>
            <div className="insight-group">
              <h4>Sources</h4>
              <SummaryBuckets
                rows={summary.sources.slice(0, 5)}
                labelFor={(row) => sourceLabels.get(row.id) ?? row.label}
              />
            </div>
            <div className="insight-group">
              <h4>Top signals</h4>
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
                <p className="summary-empty">No rule signals detected.</p>
              )}
            </div>
          </div>
          {!!summary.top_items.length && (
            <div className="rising-strip">
              <h4>Highest trend scores</h4>
              <ol>
                {summary.top_items.slice(0, 5).map((item) => (
                  <li key={item.id}>
                    <a
                      href={item.canonical_url}
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
            No public items match the current filter combination.
          </p>
        )
      )}
      {summary && (
        <p className="summary-caveat">
          {summary.caveat} Method: {summary.method}.
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
    return <p className="summary-empty">No categories detected.</p>;
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
        {compact ? "No matching public items yet" : "Start with one game"}
      </h2>
      <p>
        {compact
          ? "The result table stays empty until a configured source returns public content."
          : "Create a keyword set, then run a local collection pass against your configured sources."}
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
          <h1>Sources</h1>
          <p className="subtle">
            Run public connectors directly, or open one supervised login
            source at a time.
          </p>
        </div>
      </section>
      <section className="source-guidance" aria-label="Scan guidance">
        <div>
          <strong>
            {selected
              ? `Selected game: “${selected.name}”`
              : "Select a game first"}
          </strong>
          <p>
            {selected
              ? "Login sources open a visible browser after a 20–30 second startup. Keep the batch running, then complete QR, phone or slider confirmation there; Content Bot never bypasses it."
              : "Open Workbench and choose a tracked game. Source controls stay disabled until a game is selected."}
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
            {canceling ? "Canceling" : "Cancel run"}
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
            <strong>Batch {batch.id.slice(0, 8)}</strong>
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
                      {phaseLabel(sourceRun)} · fetched {sourceRun.fetched_count} ·
                      stored {sourceRun.ingested_count}
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
                        " · QR/phone confirmation is shown in Cốc Cốc"}
                    </span>
                    <span className="mono">
                      {progressPercent(sourceRun) !== null
                        ? `${Math.round(progressPercent(sourceRun) ?? 0)}%`
                        : "in progress"}
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
                  {source.requires_login ? "VISIBLE LOGIN" : "PUBLIC"}
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
                  ? "Running"
                  : pendingSourceId === source.id
                    ? "Queuing"
                  : unavailable
                    ? "Needs setup"
                    : source.requires_login
                      ? "Login & scan"
                      : "Scan source"}
              </button>
            </article>
          );
        })}
        {loading && <p className="subtle">Checking local configuration…</p>}
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
          <p className="eyebrow">Tracking configuration</p>
          <h1>Settings</h1>
          <p className="subtle">
            The selected game controls scope, scheduling and source caps.
          </p>
        </div>
      </section>
      {selected ? (
        <section className="panel">
          <div className="panel-body stack">
            <div>
              <strong>{selected.name}</strong>
              <p className="subtle">
                Includes: {selected.include_terms.join(", ") || "keyword name"}
              </p>
              <p className="subtle">
                Excludes: {selected.exclude_terms.join(", ") || "none"}
              </p>
              <p className="subtle">
                Per source cap: {selected.max_items_per_source} · cadence:{" "}
                {selected.enabled
                  ? `${selected.interval_minutes} min`
                  : "manual"}
              </p>
            </div>
            <div className="toolbar">
              <button className="button" onClick={onEdit}>
                Edit tracking
              </button>
              <button className="button danger" onClick={onDelete}>
                Delete tracking
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
    <dialog ref={ref} onClose={onClose} aria-label="Command palette">
      <div className="modal-head">
        <strong>Command palette</strong>
        <button className="button secondary" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
      <div className="modal-body stack">
        <button className="command" onClick={onNew}>
          <Plus size={15} /> Add tracked game
        </button>
        <button className="command" onClick={onRun}>
          <Search size={15} /> Run selected game
        </button>
        <button
          className="command"
          onClick={() => {
            onView("sources");
            onClose();
          }}
        >
          <Database size={15} /> Open sources
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
    <dialog ref={ref} onClose={onClose} aria-label="Tracked game form">
      <form onSubmit={(event) => void submit(event)}>
        <div className="modal-head">
          <strong>{initial ? "Edit tracking" : "Add tracked game"}</strong>
          <button
            className="button secondary"
            type="button"
            onClick={onClose}
            aria-label="Close tracked game form"
          >
            <X size={15} />
          </button>
        </div>
        <div className="modal-body form-grid">
          <label className="field wide">
            Game / tracked keyword
            <input
              required
              name="name"
              defaultValue={initial?.name}
              placeholder="e.g. Hades II"
              autoFocus
            />
          </label>
          <label className="field">
            Include terms, comma-separated
            <input
              name="include_terms"
              defaultValue={initial?.include_terms.join(", ")}
              placeholder="Hades 2, Supergiant"
            />
          </label>
          <label className="field">
            Exclude terms, comma-separated
            <input
              name="exclude_terms"
              defaultValue={initial?.exclude_terms.join(", ")}
              placeholder="giveaway, unrelated term"
            />
          </label>
          <label className="field">
            Schedule
            <select
              name="interval_minutes"
              defaultValue={initial?.interval_minutes ?? 360}
            >
              <option value="60">Every hour</option>
              <option value="360">Every 6 hours</option>
              <option value="1440">Daily</option>
            </select>
          </label>
          <label className="field">
            Maximum items/source
            <input
              name="max_items_per_source"
              type="number"
              min="1"
              max="500"
              defaultValue={initial?.max_items_per_source ?? 100}
            />
          </label>
          <fieldset className="field wide">
            <legend>Sources</legend>
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
                    {source.state === "ready" ? "READY" : "SETUP"}
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
              Enable scheduled runs
            </span>
          </label>
        </div>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="button" disabled={saving}>
            {saving ? "Saving…" : "Save tracking"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
