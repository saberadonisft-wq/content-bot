import {
Database,
LoaderCircle,
Plus,
Search,
X
} from "lucide-react";
import { FormEvent,lazy,Suspense,useEffect,useRef,useState } from "react";
import { AppShell,type View } from "./AppShell";


import {
api,
API_BASE,
Keyword,
Source,
UpdateCheckResponse
} from "./api";
import "./canva-home.css";
import { channelNeedsPaidAccess,LIBRARY_TABS,LibraryTab,sourceCanRun,sourceOperation,splitTerms } from "./features/shared/presentation";
import { TopicsWorkspace } from "./features/topics/TopicsWorkspace";
import { useDashboardData } from "./features/topics/useDashboardData";
import { useAuth } from "./useAuth";

const LoginPage = lazy(() => import("./LoginPage").then(module => ({ default: module.LoginPage })));
const PendingApprovalPage = lazy(() => import("./PendingApprovalPage").then(module => ({ default: module.PendingApprovalPage })));
const SubtitleStudio = lazy(() =>
  import("./SubtitleStudio").then((module) => ({ default: module.SubtitleStudio })),
);
const ContentLibrary = lazy(() => import("./features/library/ContentLibrary").then(module => ({ default: module.ContentLibrary })));
const AdminUsersModal = lazy(() => import("./AdminUsersModal").then(module => ({ default: module.AdminUsersModal })));
const SettingsModal = lazy(() => import("./SettingsModal").then(module => ({ default: module.SettingsModal })));
const UpdateModal = lazy(() => import("./UpdateModal").then(module => ({ default: module.UpdateModal })));
const UpdateBanner = lazy(() => import("./UpdateModal").then(module => ({ default: module.UpdateBanner })));


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
    checkStatus,
  } = useAuth();
  const [adminModalOpen, setAdminModalOpen] = useState(false);
  const [settingsModalOpen, setSettingsModalOpen] = useState(false);
  const [settingsInitialTab, setSettingsInitialTab] = useState<"overview" | "gemini">("overview");
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResponse | null>(null);
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [updateModalOpen, setUpdateModalOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem("content-bot.sidebar-collapsed") === "true",
  );

  const openSettings = (tab: "overview" | "gemini" = "overview") => {
    setSettingsInitialTab(tab);
    setSettingsModalOpen(true);
  };

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
    return ["channels", "acquisition", "live", "connections", "videos"].includes(requestedTab ?? "")
      ? (requestedTab as LibraryTab)
      : "channels";
  });
  const [sources, setSources] = useState<Source[]>([]);
  const [keywords, setKeywords] = useState<Keyword[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [running, setRunning] = useState(false);
  const [pendingSourceId, setPendingSourceId] = useState<string | null>(null);
  const [pendingChannelId, setPendingChannelId] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
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

  const { items, summary, clusters, runs, batch, setBatch, loading: summaryLoading, clustersError } = useDashboardData({
    keywordId: selectedId,
    filters: { source: sourceFilter || undefined, session: sessionFilter || undefined,
      language: languageFilter || undefined, sentiment: sentimentFilter || undefined, topic: topicFilter || undefined },
    enabled: view === "topics" && isApproved,
    refreshKey,
    onError: setToast,
    onCompleted: () => setCanceling(false),
    onAuthRequired: async () => Boolean(await checkStatus()),
  });
  const clustersLoading = summaryLoading;


  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    if (view === "library") url.searchParams.set("tab", libraryTab);
    else url.searchParams.delete("tab");
    window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  }, [libraryTab, view]);

  useEffect(() => {
    window.localStorage.setItem(
      "content-bot.sidebar-collapsed",
      String(sidebarCollapsed),
    );
  }, [sidebarCollapsed]);

  const selected =
    keywords.find((keyword) => keyword.id === selectedId) ?? null;
  const activeBatch =
    batch &&
    selected &&
    batch.keyword_id === selected.id &&
    ["queued", "running"].includes(batch.state)
      ? batch
      : null;
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
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
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
    return <Suspense fallback={<div role="status">Đang tải đăng nhập…</div>}><LoginPage /></Suspense>;
  }

  if (isPending || !isApproved || isBanned) {
    return <Suspense fallback={<div role="status">Đang tải tài khoản…</div>}><PendingApprovalPage /></Suspense>;
  }

  return (
    <AppShell view={view} setView={setView} sidebarCollapsed={sidebarCollapsed} setSidebarCollapsed={setSidebarCollapsed}
      onNewTopic={() => { setKeywordDraft(null); setKeywordDialogOpen(true); }}
      onOpenAdmin={() => setAdminModalOpen(true)} onOpenSettings={() => openSettings()}
      user={user} isAdmin={isAdmin} logout={logout}>
      <main className="canva-home-main" style={{ display: "flex", flexDirection: "column" }}>
        {updateInfo?.update_available && !updateDismissed && (
          <Suspense fallback={null}>
          <UpdateBanner
            updateInfo={updateInfo}
            onOpenModal={() => setUpdateModalOpen(true)}
            onDismiss={() => setUpdateDismissed(true)}
          />
          </Suspense>
        )}
        {view !== "subtitles" && (
          <ContentLibraryTabs
            active={view === "library"}
            tab={libraryTab}
            onTabChange={(nextTab) => {
              setLibraryTab(nextTab);
              setView("library");
            }}
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
          <Suspense fallback={<div className="library-loading">Đang tải kho nội dung…</div>}>
          <ContentLibrary
            tab={libraryTab}
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
          </Suspense>
        )}
        {view === "subtitles" && (
          <Suspense fallback={<main className="app-shell">Đang tải Subtitle Studio…</main>}>
            <SubtitleStudio onBack={() => setView("topics")} onOpenSettings={() => openSettings("gemini")} />
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
      <Suspense fallback={<div role="status" className="toast">Đang tải cửa sổ…</div>}>
      {adminModalOpen && <AdminUsersModal
        isOpen={adminModalOpen}
        onClose={() => setAdminModalOpen(false)}
      />}
      {settingsModalOpen && <SettingsModal
        key={`${settingsInitialTab}-${settingsModalOpen ? "open" : "closed"}`}
        isOpen={settingsModalOpen}
        onClose={() => setSettingsModalOpen(false)}
        initialTab={settingsInitialTab}
      />}
      {updateModalOpen && <UpdateModal
        open={updateModalOpen}
        onClose={() => setUpdateModalOpen(false)}
        updateInfo={updateInfo}
      />}
      </Suspense>
      {toast && (
        <div role="status" className="toast">
          {toast}
          <button aria-label="Dismiss" onClick={() => setToast(null)}>
            <X size={14} />
          </button>
        </div>
      )}
    </AppShell>
  );
}

function ContentLibraryTabs({
  active,
  tab,
  onTabChange,
}: {
  active: boolean;
  tab: LibraryTab;
  onTabChange: (tab: LibraryTab) => void;
}) {
  return (
    <nav className="content-library-tabs persistent" aria-label="Nguồn và video">
      {LIBRARY_TABS.map(({ id, label, icon: Icon }) => {
        const isCurrent = active && tab === id;
        return (
          <button
            key={id}
            type="button"
            aria-current={isCurrent ? "page" : undefined}
            className={`content-library-tab ${isCurrent ? "active" : ""}`}
            onClick={() => onTabChange(id)}
          >
            <Icon size={17} aria-hidden="true" /> {label}
          </button>
        );
      })}
    </nav>
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
