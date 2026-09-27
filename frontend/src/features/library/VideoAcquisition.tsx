import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Check,
  Download,
  ExternalLink,
  FileKey,
  LoaderCircle,
  Pause,
  Pencil,
  Play,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { api, type AcquisitionCandidate, type AcquisitionCapabilities, type AcquisitionChannel, type AcquisitionMode, type AcquisitionRun, type AcquisitionSelection, type VideoDownloadQuality } from "../../api";
import "./video-acquisition.css";

const activeStates = new Set(["queued", "running"]);
const runStateLabel = (state: string) => ({
  queued: "Đang chờ", running: "Đang cào", completed: "Hoàn tất",
  failed: "Có lỗi", partial: "Hoàn tất một phần", paused: "Tạm dừng",
  canceled: "Đã hủy", skipped: "Chưa quét · hết ngân sách",
}[state] ?? state);
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Có lỗi xảy ra. Vui lòng thử lại.";

const dateTimeLabel = (value: string | null) => {
  if (!value) return "Chưa rõ ngày";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "Chưa rõ ngày"
    : date.toLocaleString("vi-VN", { dateStyle: "medium", timeStyle: "short" });
};

const durationLabel = (value: number | null) => {
  if (value === null || !Number.isFinite(value)) return "—";
  const total = Math.max(0, Math.round(value));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

const scanTimeLabel = (value?: string | null) => {
  if (!value) return "Chưa có";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Chưa rõ" : date.toLocaleString("vi-VN");
};

const retryAfterLabel = (value?: number | null) => {
  if (!value || !Number.isFinite(value) || value < 0) return "";
  const seconds = Math.ceil(value);
  if (seconds < 60) return `Có thể thử lại sau khoảng ${seconds} giây.`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `Có thể thử lại sau khoảng ${minutes} phút.`;
  const hours = Math.ceil(minutes / 60);
  return `Có thể thử lại sau khoảng ${hours} giờ.`;
};

const subscriptionStatusLabel = (subscription: AcquisitionChannel["subscription"]) => {
  if (!subscription.enabled) return "Đã tắt theo dõi";
  const labels: Record<string, string> = {
    queued: "Đã xếp lượt quét",
    succeeded: "Quét gần nhất hoàn tất",
    failed: "Lượt quét có lỗi",
    catching_up: "Đang chờ quét bù",
    waiting_capability: "Chờ nguồn sẵn sàng",
    waiting_capacity: "Chờ hàng đợi trống",
    deferred_feature: "Tạm hoãn tự tải",
  };
  return labels[subscription.last_status ?? ""] ?? "Đang theo dõi";
};

const sourceLabel = (source: string) => ({
  youtube: "YouTube",
  bilibili: "Bilibili",
  douyin: "Douyin",
  tiktok: "TikTok",
  xhs: "XHS",
}[source] ?? source);

const operationEnabled = (
  capabilities: AcquisitionCapabilities | null,
  sourceId: string,
  operation: string,
) => capabilities?.items.find((item) => item.source_id === sourceId)?.operations[operation]?.enabled ?? false;

function AcquisitionThumbnail({ candidate }: { candidate: AcquisitionCandidate }) {
  const thumbnailUrl = candidate.thumbnail_url?.trim() || null;
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const showImage = Boolean(thumbnailUrl && thumbnailUrl !== failedUrl);

  return (
    <div
      className={`acquisition-thumb${showImage ? "" : " is-fallback"}`}
      role={showImage ? undefined : "img"}
      aria-label={showImage ? undefined : "Chưa có ảnh xem trước"}
    >
      {showImage ? (
        <img
          src={thumbnailUrl ?? undefined}
          alt={`Ảnh xem trước: ${candidate.title || candidate.external_id}`}
          loading="lazy"
          decoding="async"
          onError={() => setFailedUrl(thumbnailUrl)}
        />
      ) : (
        <Search aria-hidden="true" size={24} />
      )}
      <span className="acquisition-thumb-duration">
        {candidate.media_type === "video" ? durationLabel(candidate.duration_seconds) : candidate.media_type}
      </span>
    </div>
  );
}

export function VideoAcquisition() {
  const [mode, setMode] = useState<AcquisitionMode>("video");
  const [targets, setTargets] = useState("");
  const [query, setQuery] = useState("");
  const [sourceId, setSourceId] = useState("youtube");
  const [connectionId, setConnectionId] = useState("");
  const [maxCandidates, setMaxCandidates] = useState(20);
  const [onlyNotDownloaded, setOnlyNotDownloaded] = useState(true);
  const [titleContains, setTitleContains] = useState("");
  const [minDurationSeconds, setMinDurationSeconds] = useState("");
  const [maxDurationSeconds, setMaxDurationSeconds] = useState("");
  const [publishedAfter, setPublishedAfter] = useState("");
  const [quality, setQuality] = useState<VideoDownloadQuality>("1080");
  const [cookieFile, setCookieFile] = useState<File | null>(null);
  const [run, setRun] = useState<AcquisitionRun | null>(null);
  const [recentRuns, setRecentRuns] = useState<AcquisitionRun[]>([]);
  const [candidates, setCandidates] = useState<AcquisitionCandidate[]>([]);
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [selection, setSelection] = useState<AcquisitionSelection | null>(null);
  const [recentSelections, setRecentSelections] = useState<AcquisitionSelection[]>([]);
  const [savedChannels, setSavedChannels] = useState<AcquisitionChannel[]>([]);
  const [visibleChannelCount, setVisibleChannelCount] = useState(6);
  const [channelListError, setChannelListError] = useState("");
  const [capabilities, setCapabilities] = useState<AcquisitionCapabilities | null>(null);
  const [savingChannel, setSavingChannel] = useState(false);
  const [renamingChannelId, setRenamingChannelId] = useState<string | null>(null);
  const [removingChannelId, setRemovingChannelId] = useState<string | null>(null);
  const [savingSubscriptionId, setSavingSubscriptionId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    void api.acquisitionSelections().then((response) => {
      if (!disposed) setRecentSelections(response.items);
    }).catch((requestError) => {
      if (!disposed) setError(errorMessage(requestError));
    });
    return () => { disposed = true; };
  }, []);

  const openPreviousSelection = async (id: string) => {
    setBusy(true);
    setError("");
    setCookieFile(null);
    try {
      setSelection(await api.acquisitionSelection(id));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    let disposed = false;
    void api.acquisitionRuns().then((response) => {
      if (!disposed) setRecentRuns(response.items);
    }).catch((requestError) => {
      if (!disposed) setError(errorMessage(requestError));
    });
    return () => { disposed = true; };
  }, []);

  const restoreRun = (previous: AcquisitionRun) => {
    setCandidateOffset(0);
    setRun(previous);
    setMode(previous.mode);
    setTargets(Array.isArray(previous.request.targets) ? previous.request.targets.filter((value): value is string => typeof value === "string").join("\n") : "");
    setQuery(typeof previous.request.query === "string" ? previous.request.query : "");
    setSourceId(previous.source_id || "youtube");
    setConnectionId(typeof previous.request.connection_id === "string" ? previous.request.connection_id : "");
    const previousLimits = previous.request.limits;
    if (previousLimits && typeof previousLimits === "object" && "max_candidates" in previousLimits) {
      const value = Number(previousLimits.max_candidates);
      if ([20, 50, 100].includes(value)) setMaxCandidates(value);
    }
    const previousFilters = previous.request.filters;
    if (previousFilters && typeof previousFilters === "object") {
      const filters = previousFilters as Record<string, unknown>;
      setTitleContains(typeof filters.title_contains === "string" ? filters.title_contains : "");
      setMinDurationSeconds(filters.min_duration_seconds == null ? "" : String(filters.min_duration_seconds));
      setMaxDurationSeconds(filters.max_duration_seconds == null ? "" : String(filters.max_duration_seconds));
      if (typeof filters.published_after === "string") {
        const date = new Date(filters.published_after);
        setPublishedAfter(Number.isNaN(date.valueOf()) ? "" : new Date(date.valueOf() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16));
      } else {
        setPublishedAfter("");
      }
    } else {
      setTitleContains("");
      setMinDurationSeconds("");
      setMaxDurationSeconds("");
      setPublishedAfter("");
    }
    setCandidates([]);
    setSelectedIds(new Set());
    setSelection(null);
    setCookieFile(null);
    setError("");
  };

  const openPreviousRun = async (id: string) => {
    if (!id) return;
    setBusy(true);
    setError("");
    try {
      const previous = await api.acquisitionRun(id);
      restoreRun(previous);
      setRecentRuns((items) => [previous, ...items.filter((item) => item.id !== id)].slice(0, 50));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (savingChannel || savingSubscriptionId || removingChannelId || renamingChannelId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const response = await api.acquisitionChannels();
        if (disposed) return;
        setSavedChannels(response.items);
        setChannelListError("");
      } catch {
        if (!disposed) setChannelListError("Chưa cập nhật được trạng thái kênh. Đang tự thử lại; thông tin hiển thị có thể đã cũ.");
      } finally {
        if (!disposed) timer = setTimeout(() => void poll(), 15_000);
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [savingChannel, savingSubscriptionId, removingChannelId, renamingChannelId]);

  useEffect(() => {
    let disposed = false;
    void api.acquisitionCapabilities().then((response) => {
      if (!disposed) setCapabilities(response);
    }).catch(() => {
      // The backend remains authoritative; keep optional capability discovery
      // from making the acquisition form unusable during a rolling upgrade.
    });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    if (!run?.id) return undefined;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const [nextRun, nextCandidates] = await Promise.all([
          api.acquisitionRun(run.id),
          api.acquisitionCandidates(run.id, { onlyNotDownloaded, offset: candidateOffset }),
        ]);
        if (disposed) return;
        setRun(nextRun);
        setCandidates(nextCandidates.items);
        setCandidateTotal(nextCandidates.total);
        if (candidateOffset > 0 && candidateOffset >= nextCandidates.total) {
          setCandidateOffset(Math.max(0, Math.ceil(nextCandidates.total / 100) - 1) * 100);
        }
        if (activeStates.has(nextRun.state)) {
          timer = setTimeout(() => void poll(), 1200);
        }
      } catch (requestError) {
        if (!disposed) {
          setError(errorMessage(requestError));
          timer = setTimeout(() => void poll(), 3000);
        }
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [onlyNotDownloaded, candidateOffset, run?.id, run?.state]);

  useEffect(() => {
    if (!selection?.id || selection.state !== "queued") return undefined;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const nextSelection = await api.acquisitionSelection(selection.id);
        if (disposed) return;
        setSelection(nextSelection);
        if (nextSelection.state === "queued") {
          timer = setTimeout(() => void poll(), 1200);
        }
      } catch {
        if (!disposed) timer = setTimeout(() => void poll(), 3000);
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [selection?.id, selection?.state]);

  const selectableCandidates = useMemo(
    () => candidates.filter((candidate) => candidate.media_type === "video" && candidate.availability !== "live" && candidate.download_available !== false),
    [candidates],
  );
  const effectiveSourceId = sourceId === "bilibili"
    && capabilities
    && !operationEnabled(capabilities, "bilibili", "search")
    ? "youtube"
    : sourceId;
  const acquisitionEnabled = capabilities?.feature_enabled !== false;
  const skippedTargets = run?.children?.filter((child) => child.state === "skipped").map((child) => child.target) ?? [];
  const canRetryPartial = run?.state === "partial" && run.children?.some((child) => child.state === "failed");
  const allSelected = selectableCandidates.length > 0 && selectableCandidates.every((candidate) => selectedIds.has(candidate.id));

  const submitRun = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSelection(null);
    setCandidates([]);
    setCandidateOffset(0);
    setSelectedIds(new Set());
    try {
      const links = targets.split(/[\r\n,]+/).map((value) => value.trim()).filter(Boolean);
      const filters: Record<string, string | number | boolean> = {
        media_type: "video",
        only_not_downloaded: onlyNotDownloaded,
      };
      if (titleContains.trim()) filters.title_contains = titleContains.trim();
      const parseDuration = (value: string, label: string) => {
        if (!value.trim()) return undefined;
        const number = Number(value);
        if (!Number.isFinite(number) || number < 0 || number > 86400) {
          throw new Error(`${label} phải nằm trong khoảng 0–86400 giây.`);
        }
        return number;
      };
      const minDuration = parseDuration(minDurationSeconds, "Thời lượng tối thiểu");
      const maxDuration = parseDuration(maxDurationSeconds, "Thời lượng tối đa");
      if (minDuration !== undefined) filters.min_duration_seconds = minDuration;
      if (maxDuration !== undefined) filters.max_duration_seconds = maxDuration;
      if (minDuration !== undefined && maxDuration !== undefined && minDuration > maxDuration) {
        throw new Error("Thời lượng tối thiểu không được lớn hơn thời lượng tối đa.");
      }
      if (publishedAfter) {
        const publishedDate = new Date(publishedAfter);
        if (Number.isNaN(publishedDate.valueOf())) {
          throw new Error("Ngày đăng sau không hợp lệ.");
        }
        filters.published_after = publishedDate.toISOString();
      }
      const nextRun = await api.createAcquisitionRun({
        mode,
        targets: mode === "search" ? [] : links,
        query: mode === "search" ? query.trim() : undefined,
        source_id: mode === "search" ? effectiveSourceId : undefined,
        provider_id: mode === "search" && effectiveSourceId === "bilibili"
          ? "cbce_bilibili"
          : mode === "search" && effectiveSourceId === "douyin"
            ? "cbce_douyin"
            : undefined,
        connection_id: connectionId.trim() || undefined,
        limits: { max_candidates: maxCandidates, max_pages: 20, deadline_seconds: 300 },
        filters,
      });
      setRun(nextRun);
      setRecentRuns((current) => [nextRun, ...current.filter((item) => item.id !== nextRun.id)].slice(0, 50));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const toggleCandidate = (id: string) => {
    if (!selectedIds.has(id) && selectedIds.size >= 100) {
      setError("Mỗi nhóm tải tối đa 100 video. Hãy gửi nhóm đã chọn trước.");
      return;
    }
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    const next = new Set(selectedIds);
    for (const candidate of selectableCandidates) {
      if (allSelected) next.delete(candidate.id);
      else next.add(candidate.id);
    }
    if (next.size > 100) {
      setError("Mỗi nhóm tải tối đa 100 video. Hãy gửi nhóm đã chọn trước.");
      return;
    }
    setSelectedIds(next);
  };

  const selectDownloads = async () => {
    const ids = [...selectedIds];
    if (!ids.length) {
      setError("Hãy chọn ít nhất một video để tải.");
      return;
    }
    if (cookieFile && cookieFile.size > 1_000_000) {
      setError("File cookies phải nhỏ hơn hoặc bằng 1 MB.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const runConnectionId = typeof run?.request?.connection_id === "string"
        ? run.request.connection_id.trim()
        : "";
      const selectedConnectionId = connectionId.trim() || runConnectionId;
      const cookieText = selectedConnectionId
        ? undefined
        : (cookieFile ? await cookieFile.text() : undefined);
      const nextSelection = await api.selectAcquisitionDownloads({
        candidate_ids: ids,
        quality,
        idempotency_key: crypto.randomUUID(),
        cookie_text: cookieText,
        connection_id: selectedConnectionId || undefined,
      });
      setSelection(nextSelection);
      if (run) {
        const refreshed = await api.acquisitionCandidates(run.id, { onlyNotDownloaded, offset: candidateOffset });
        setCandidates(refreshed.items);
        setCandidateTotal(refreshed.total);
      }
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const stopRun = async () => {
    if (!run) return;
    setBusy(true);
    try {
      setRun(await api.cancelAcquisitionRun(run.id));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const saveChannel = async () => {
    const channelTargets = [...new Set(targets.split(/[\r\n,]+/).map((value) => value.trim()).filter(Boolean))];
    if (channelTargets.length > 1) {
      setError("Chọn một link kênh để lưu. Danh sách nhiều kênh dùng nút Cào và xem trước.");
      return;
    }
    const firstTarget = channelTargets[0];
    if (!firstTarget) {
      setError("Nhập một link kênh hoặc playlist trước khi lưu.");
      return;
    }
    setSavingChannel(true);
    setError("");
    try {
      const channel = await api.saveAcquisitionChannel(
        firstTarget,
        "",
        connectionId,
      );
      setSavedChannels((current) => [channel, ...current.filter((item) => item.id !== channel.id)]);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSavingChannel(false);
    }
  };

  const removeChannel = async (channel: AcquisitionChannel) => {
    if (!window.confirm(`Bỏ theo dõi ${channel.label}? File/video đã tải sẽ được giữ nguyên.`)) return;
    setRemovingChannelId(channel.id);
    setError("");
    try {
      await api.deleteAcquisitionChannel(channel.id);
      setSavedChannels((items) => items.filter((item) => item.id !== channel.id));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setRemovingChannelId(null);
    }
  };

  const renameChannel = async (channel: AcquisitionChannel) => {
    const nextLabel = window.prompt("Tên hiển thị của kênh", channel.label);
    if (nextLabel === null || !nextLabel.trim() || nextLabel.trim() === channel.label) return;
    setRenamingChannelId(channel.id);
    setError("");
    try {
      const updated = await api.updateAcquisitionChannel(channel.id, { label: nextLabel.trim() });
      setSavedChannels((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setRenamingChannelId(null);
    }
  };

  const updateChannelSubscription = async (
    channel: AcquisitionChannel,
    changes: Partial<AcquisitionChannel["subscription"]>,
  ) => {
    const current = channel.subscription;
    setSavingSubscriptionId(channel.id);
    setError("");
    try {
      const updated = await api.updateAcquisitionSubscription(channel.id, {
        ...current,
        ...changes,
      });
      setSavedChannels((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSavingSubscriptionId(null);
    }
  };

  const pauseOrResume = async () => {
    if (!run) return;
    setBusy(true);
    try {
      setRun(await (run.state === "paused" ? api.resumeAcquisitionRun(run.id) : api.pauseAcquisitionRun(run.id)));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const retryRun = async () => {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      setRun(await api.resumeAcquisitionRun(run.id));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const continueRun = async () => {
    if (!run?.pagination) return;
    setBusy(true);
    setError("");
    try {
      setRun(await api.continueAcquisitionRun(run.id, run.pagination.generation));
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  const controlSelection = async (action: "pause" | "resume" | "cancel") => {
    if (!selection) return;
    setBusy(true);
    setError("");
    try {
      let nextSelection: AcquisitionSelection;
      if (action === "pause") nextSelection = await api.pauseAcquisitionSelection(selection.id);
      else if (action === "cancel") nextSelection = await api.cancelAcquisitionSelection(selection.id);
      else {
        const cookieText = selection.connection_id
          ? undefined
          : (cookieFile ? await cookieFile.text() : undefined);
        nextSelection = await api.resumeAcquisitionSelection(selection.id, cookieText);
      }
      setSelection(nextSelection);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="video-acquisition">
      {!acquisitionEnabled && (
        <div className="acquisition-alert" role="status">
          Tính năng cào video đang tắt trong rollout. Bạn vẫn có thể xem lịch sử và hủy công việc đang dở.
        </div>
      )}
      {recentSelections.length > 0 && (
        <label className="acquisition-form">
          <span>Mở lại nhóm tải đã lưu</span>
          <select value={selection && recentSelections.some((item) => item.id === selection.id) ? selection.id : ""} disabled={busy} onChange={(event) => void openPreviousSelection(event.target.value)}>
            <option value="" disabled>Chọn nhóm tải</option>
            {recentSelections.map((item) => <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString("vi-VN")} · {item.total} video</option>)}
          </select>
        </label>
      )}
      {recentRuns.length > 0 && (
        <label className="acquisition-form">
          <span>Mở lại lượt cào gần đây</span>
          <select value={run?.id && recentRuns.some((item) => item.id === run.id) ? run.id : ""} onChange={(event) => void openPreviousRun(event.target.value)} disabled={busy}>
            <option value="" disabled>Chọn lượt cào đã lưu</option>
            {recentRuns.map((item) => <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString("vi-VN")} · {item.mode} · {item.provider_id}</option>)}
          </select>
        </label>
      )}
      <form className="acquisition-form" onSubmit={(event) => void submitRun(event)}>
        <div className="acquisition-form-head">
          <div>
            <p className="eyebrow">Cào metadata trước khi tải</p>
            <h2>Chọn đúng video cần đưa vào thư viện</h2>
          </div>
          <span className="acquisition-badge"><Search size={14} /> P1 · YouTube/Bilibili</span>
        </div>
        <div className="acquisition-fields">
          <label>
            <span>Kiểu target</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as AcquisitionMode)} disabled={busy}>
              <option value="video">Link video</option>
              <option value="creator">Kênh / danh sách kênh</option>
              <option value="playlist">Playlist / danh sách</option>
              <option value="search">Tìm kiếm YouTube / Bilibili / Douyin</option>
            </select>
          </label>
          {mode === "search" ? (
            <>
              <label>
                <span>Nền tảng</span>
                <select value={effectiveSourceId} onChange={(event) => setSourceId(event.target.value)} disabled={busy}>
                  <option value="youtube">YouTube</option>
                  <option value="bilibili" disabled={!operationEnabled(capabilities, "bilibili", "search")}>
                    Bilibili · CBCE/browser{capabilities && !operationEnabled(capabilities, "bilibili", "search") ? " (chưa sẵn sàng)" : ""}
                  </option>
                  <option value="douyin" disabled={!operationEnabled(capabilities, "douyin", "search")}>
                    Douyin · CBCE/browser{capabilities && !operationEnabled(capabilities, "douyin", "search") ? " (chưa sẵn sàng)" : ""}
                  </option>
                </select>
              </label>
              <label className="acquisition-wide-field">
                <span>Từ khóa</span>
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ví dụ: review máy ảnh" disabled={busy} />
              </label>
            </>
          ) : (
            <label className="acquisition-wide-field">
              <span id="acquisition-target-label">{mode === "video" ? "Link video (mỗi dòng một link)" : "Link kênh hoặc playlist (mỗi dòng một link)"}</span>
              <textarea aria-labelledby="acquisition-target-label" aria-describedby={mode !== "video" ? "acquisition-target-help" : undefined} value={targets} onChange={(event) => setTargets(event.target.value)} placeholder="https://www.youtube.com/watch?v=…" rows={3} disabled={busy} />
              {mode !== "video" && <small id="acquisition-target-help">Tối đa 20 kênh; ngân sách metadata và thời gian dùng chung cho cả nhóm. Lỗi từng kênh hiển thị riêng.</small>}
            </label>
          )}
          {(mode === "video" || mode === "creator" || (mode === "search" && effectiveSourceId === "bilibili")) && (
            <label>
              <span>Connection profile (tùy chọn)</span>
              <input
                value={connectionId}
                onChange={(event) => setConnectionId(event.target.value)}
                placeholder="Ví dụ: bilibili-main"
                maxLength={128}
                autoComplete="off"
                disabled={busy}
              />
              <small className="acquisition-field-helper">
                Dùng profile CBCE riêng cho video/creator/search Bilibili; không nhập cookie vào ô này.
              </small>
            </label>
          )}
        </div>
        <div className="acquisition-options">
          <label>
            <span>Chất lượng tải</span>
            <select value={quality} onChange={(event) => setQuality(event.target.value as VideoDownloadQuality)} disabled={busy}>
              <option value="1080">Tối đa 1080p</option>
              <option value="720">Tối đa 720p</option>
              <option value="480">Tối đa 480p</option>
              <option value="best">Cao nhất</option>
            </select>
          </label>
          <label>
            <span>Số metadata tối đa</span>
            <select value={maxCandidates} onChange={(event) => setMaxCandidates(Number(event.target.value))} disabled={busy}>
              <option value={20}>20 video</option>
              <option value={50}>50 video</option>
              <option value={100}>100 video</option>
            </select>
          </label>
          <label className="acquisition-check">
            <input type="checkbox" checked={onlyNotDownloaded} onChange={(event) => setOnlyNotDownloaded(event.target.checked)} />
            <span>Ưu tiên video chưa có trong thư viện</span>
          </label>
          <div className="acquisition-filter-grid">
            <label>
              <span>Tiêu đề chứa</span>
              <input value={titleContains} onChange={(event) => setTitleContains(event.target.value)} placeholder="Ví dụ: review" disabled={busy || !acquisitionEnabled} />
            </label>
            <label>
              <span>Thời lượng từ (giây)</span>
              <input type="number" min={0} max={86400} value={minDurationSeconds} onChange={(event) => setMinDurationSeconds(event.target.value)} placeholder="Không giới hạn" disabled={busy || !acquisitionEnabled} />
            </label>
            <label>
              <span>Thời lượng đến (giây)</span>
              <input type="number" min={0} max={86400} value={maxDurationSeconds} onChange={(event) => setMaxDurationSeconds(event.target.value)} placeholder="Không giới hạn" disabled={busy || !acquisitionEnabled} />
            </label>
            <label>
              <span>Đăng sau</span>
              <input type="datetime-local" value={publishedAfter} onChange={(event) => setPublishedAfter(event.target.value)} disabled={busy || !acquisitionEnabled} />
            </label>
          </div>
          <label className="acquisition-cookie">
            <FileKey size={15} />
            <span>{cookieFile ? cookieFile.name : "Cookies tùy chọn (.txt)"}</span>
            <input type="file" accept=".txt,text/plain" onChange={(event) => setCookieFile(event.target.files?.[0] ?? null)} />
          </label>
           <button className="button" type="submit" disabled={busy || !acquisitionEnabled}>
            {busy && !run ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />}
            Cào và xem trước
          </button>
          {mode === "creator" || mode === "playlist" ? (
             <button className="button secondary" type="button" onClick={() => void saveChannel()} disabled={savingChannel || busy || !acquisitionEnabled}>
              {savingChannel ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Lưu kênh
            </button>
          ) : null}
        </div>
      </form>

      {channelListError && <p className="acquisition-alert" role="status">{channelListError}</p>}
      {savedChannels.length > 0 && (
        <section className="acquisition-channels">
          <div>
            <p className="eyebrow">Kênh nguồn đã lưu</p>
            <h2>Theo dõi video mới theo từng kênh, tự tải có thể bật riêng</h2>
          </div>
          <div className="acquisition-channel-list">
            {savedChannels.slice(0, visibleChannelCount).map((channel) => (
              <article className="acquisition-channel-wrap" key={channel.id}>
                <button className="acquisition-channel" type="button" onClick={() => {
                  setMode(channel.target_kind);
                  setTargets(channel.canonical_url);
                  setConnectionId(channel.connection_id ?? "");
                }}>
                  <strong>{channel.label}</strong>
                  <span>{sourceLabel(channel.source_id)} · {channel.target_kind}</span>
                </button>
                <div className="acquisition-channel-status">
                  <strong>{subscriptionStatusLabel(channel.subscription)}</strong>
                  <span>Quét gần nhất: {scanTimeLabel(channel.subscription.last_scanned_at)}</span>
                  {channel.subscription.enabled && <span>Lịch tiếp theo: {scanTimeLabel(channel.subscription.next_run_at)}</span>}
                  {channel.subscription.last_error && <p className="acquisition-error">{channel.subscription.last_error}</p>}
                  {(channel.subscription.active_run_id || channel.subscription.last_run_id) && (
                    <button className="button secondary" type="button" disabled={busy} onClick={() => void openPreviousRun(channel.subscription.active_run_id || channel.subscription.last_run_id || "")}>
                      Xem lượt quét {channel.subscription.active_run_id ? "hiện tại" : "gần nhất"}
                    </button>
                  )}
                </div>
                <div className="acquisition-channel-actions">
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => void renameChannel(channel)}
                    disabled={renamingChannelId === channel.id || removingChannelId === channel.id}
                  >
                    <Pencil size={14} /> Đổi tên
                  </button>
                  <button
                    className="button danger"
                    type="button"
                    onClick={() => void removeChannel(channel)}
                    disabled={removingChannelId === channel.id || savingSubscriptionId === channel.id}
                  >
                    <Trash2 size={14} /> Bỏ theo dõi
                  </button>
                </div>
                <label className="acquisition-channel-toggle">
                  <input
                    type="checkbox"
                    checked={channel.subscription.enabled}
                     disabled={savingSubscriptionId === channel.id || (!acquisitionEnabled && !channel.subscription.enabled)}
                    onChange={(event) => void updateChannelSubscription(channel, { enabled: event.target.checked })}
                  />
                  Theo dõi mới
                </label>
                <label className="acquisition-channel-toggle">
                  <input
                    type="checkbox"
                    checked={channel.subscription.auto_download}
                     disabled={!channel.subscription.enabled || savingSubscriptionId === channel.id || !acquisitionEnabled}
                    onChange={(event) => void updateChannelSubscription(channel, { auto_download: event.target.checked })}
                  />
                  Tự tải mới
                </label>
                <div className="acquisition-channel-settings">
                  <label>
                    <span>Lần đầu</span>
                    <select
                      value={channel.subscription.initial_policy}
                       disabled={savingSubscriptionId === channel.id || !acquisitionEnabled}
                      onChange={(event) => void updateChannelSubscription(channel, { initial_policy: event.target.value as "baseline" | "backfill" })}
                    >
                      <option value="baseline">Chỉ lấy video mới</option>
                      <option value="backfill">Tải lại lịch sử</option>
                    </select>
                  </label>
                  <label>
                    <span>Chu kỳ</span>
                    <select
                      value={channel.subscription.interval_minutes}
                       disabled={savingSubscriptionId === channel.id || !acquisitionEnabled}
                      onChange={(event) => void updateChannelSubscription(channel, { interval_minutes: Number(event.target.value) })}
                    >
                      <option value={30}>30 phút</option>
                      <option value={60}>1 giờ</option>
                      <option value={360}>6 giờ</option>
                      <option value={1440}>24 giờ</option>
                    </select>
                  </label>
                  <label>
                    <span>Chất lượng tự tải</span>
                    <select
                      value={channel.subscription.quality ?? "1080"}
                       disabled={!channel.subscription.enabled || savingSubscriptionId === channel.id || !acquisitionEnabled}
                      onChange={(event) => void updateChannelSubscription(channel, { quality: event.target.value as VideoDownloadQuality })}
                    >
                      <option value="best">Cao nhất</option>
                      <option value="1080">1080p</option>
                      <option value="720">720p</option>
                      <option value="480">480p</option>
                    </select>
                  </label>
                  <label>
                    <span>Số video/lượt</span>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={channel.subscription.max_items ?? 20}
                       disabled={!channel.subscription.enabled || savingSubscriptionId === channel.id || !acquisitionEnabled}
                      onChange={(event) => void updateChannelSubscription(channel, { max_items: Math.min(100, Math.max(1, Number(event.target.value) || 1)) })}
                    />
                  </label>
                </div>
              </article>
            ))}
          </div>
          {savedChannels.length > visibleChannelCount && (
            <button className="button secondary" type="button" onClick={() => setVisibleChannelCount((count) => count + 6)}>
              Xem thêm kênh ({savedChannels.length - visibleChannelCount} còn lại)
            </button>
          )}
        </section>
      )}

      {error && <div className="acquisition-alert" role="alert"><X size={16} /> {error}</div>}

      {run && (
        <section className="acquisition-results" aria-live="polite">
          <div className="acquisition-results-head">
            <div>
              <p className="eyebrow">{run.provider_id} · {run.mode}</p>
              <h2>{run.state === "completed" ? "Kết quả xem trước" : run.state === "partial" ? "Kết quả một phần" : run.state === "failed" ? "Lượt cào có lỗi" : activeStates.has(run.state) ? "Đang lấy metadata…" : runStateLabel(run.state)}</h2>
              <p className="subtle">Đã quét {run.counters.scanned} · mới {run.counters.new} · trùng {run.counters.duplicate}</p>
            </div>
            <div className="acquisition-actions">
              {run.can_continue && <button className="button secondary" type="button" onClick={() => void continueRun()} disabled={busy || !acquisitionEnabled}><Search size={15} /> Cào thêm video</button>}
              {activeStates.has(run.state) && <button className="button secondary" type="button" onClick={() => void pauseOrResume()} disabled={busy}><Pause size={15} /> Tạm dừng</button>}
               {run.state === "paused" && <button className="button secondary" type="button" onClick={() => void pauseOrResume()} disabled={busy || !acquisitionEnabled}><Play size={15} /> Tiếp tục</button>}
              {activeStates.has(run.state) && <button className="button danger" type="button" onClick={() => void stopRun()} disabled={busy}><Square size={14} /> Hủy</button>}
               {(run.state === "failed" || run.state === "canceled" || canRetryPartial) && <button className="button secondary" type="button" onClick={() => void retryRun()} disabled={busy || !acquisitionEnabled}><Play size={15} /> {run.children?.length ? "Thử lại kênh chưa hoàn tất" : "Thử lại lượt cào"}</button>}
              {skippedTargets.length > 0 && <button className="button secondary" type="button" disabled={busy || !acquisitionEnabled} onClick={() => {
                setMode(run.mode);
                setTargets(skippedTargets.join("\n"));
                setConnectionId(typeof run.request.connection_id === "string" ? run.request.connection_id : "");
                document.querySelector(".acquisition-form-head")?.scrollIntoView({ block: "start" });
              }}>Đưa kênh chưa quét vào lượt mới</button>}
            </div>
          </div>
          {run.error && <p className="acquisition-error">{run.error_code ? `${run.error_code}: ` : ""}{run.error} {retryAfterLabel(run.retry_after)}</p>}
          {run.pagination && <p className="subtle">Cào theo vị trí danh sách, chống trùng theo ID; thứ tự nguồn có thể thay đổi. Giới hạn 5.000 vị trí mỗi lượt cào. {run.pagination.exhausted ? "Đã hết kết quả trong cửa sổ nguồn." : run.pagination.last_stop_reason === "no_new_items" ? "Đã dừng sau hai cửa sổ không có video mới phù hợp." : "Mỗi lần cào thêm giữ ngân sách của lượt ban đầu."}</p>}
          {!!run.children?.length && (
            <ul className="acquisition-child-runs" aria-label="Trạng thái từng kênh">
              {run.children.map((child) => (
                <li key={child.id}>
                  <span>{child.target}</span>
                  <strong>{runStateLabel(child.state)} · {child.counters.new} kết quả</strong>
                  {child.error && <p>{child.error} {retryAfterLabel(child.retry_after)}</p>}
                </li>
              ))}
            </ul>
          )}
          {candidates.length > 0 && (
            <>
              <div className="acquisition-selection-bar">
                <label className="acquisition-check">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                  <span>Chọn video trên trang này</span>
                </label>
                <span>{selectedIds.size} đã chọn trên các trang · {candidateTotal} kết quả</span>
                 <button className="button" type="button" onClick={() => void selectDownloads()} disabled={busy || !selectedIds.size || !acquisitionEnabled}>
                  {busy ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />} Tải đã chọn
                </button>
              </div>
              <div className="acquisition-grid">
                {candidates.map((candidate) => (
                  <article className={`acquisition-card ${selectedIds.has(candidate.id) ? "selected" : ""}`} key={candidate.id}>
                    <label className="acquisition-card-check">
                      <input type="checkbox" checked={selectedIds.has(candidate.id)} onChange={() => toggleCandidate(candidate.id)} disabled={candidate.media_type !== "video" || candidate.availability === "live" || candidate.download_available === false} />
                      <span className="sr-only">Chọn {candidate.title || candidate.external_id}</span>
                    </label>
                    <AcquisitionThumbnail candidate={candidate} />
                    <div className="acquisition-card-body">
                      <h3 title={candidate.title}>{candidate.title || "Không có tiêu đề"}</h3>
                      <p>{sourceLabel(candidate.source_id)} · {candidate.uploader || "Không rõ creator"}</p>
                      <p>{dateTimeLabel(candidate.published_at)} · {candidate.download_available === false ? "Chỉ metadata · Chưa có tải" : candidate.download?.state === "succeeded" ? "Đã tải" : candidate.download?.state === "queued" ? "Đang chờ tải" : "Chưa tải"}</p>
                      {candidate.download_available === false && candidate.download_unavailable_reason && <p className="subtle">{candidate.download_unavailable_reason}</p>}
                      <a href={candidate.canonical_url} target="_blank" rel="noreferrer"><ExternalLink size={13} /> Mở nguồn</a>
                    </div>
                    {selectedIds.has(candidate.id) && <Check className="acquisition-selected-icon" size={17} />}
                  </article>
                ))}
              </div>
              {candidateTotal > 100 && <div className="acquisition-actions">
                <button className="button secondary" type="button" disabled={busy || candidateOffset === 0} onClick={() => setCandidateOffset((offset) => Math.max(0, offset - 100))}>Trang trước</button>
                <span>Trang {Math.floor(candidateOffset / 100) + 1}/{Math.ceil(candidateTotal / 100)}</span>
                <button className="button secondary" type="button" disabled={busy || candidateOffset + 100 >= candidateTotal} onClick={() => setCandidateOffset((offset) => offset + 100)}>Trang sau</button>
              </div>}
            </>
          )}
          {run.state === "completed" && candidates.length === 0 && <div className="acquisition-empty"><Search size={28} /><h3>Không có candidate phù hợp</h3><p>Kiểm tra target, bộ lọc hoặc quyền truy cập nguồn.</p></div>}
        </section>
      )}
          {selection && (
            <div className="acquisition-selection-result">
              <span><Check size={16} /> Selection tải: {selection.total} video · {Object.entries(selection.counts).map(([key, value]) => `${key}: ${value}`).join(" · ")}</span>
              <span className="acquisition-selection-state">{selection.state}</span>
              <span className="acquisition-selection-actions">
                {selection.state === "queued" && <button className="button secondary" type="button" onClick={() => void controlSelection("pause")} disabled={busy}><Pause size={14} /> Tạm dừng tải</button>}
                 {selection.state === "paused" && <button className="button secondary" type="button" onClick={() => void controlSelection("resume")} disabled={busy || !acquisitionEnabled}><Play size={14} /> Tiếp tục tải</button>}
                {(selection.state === "queued" || selection.state === "paused" || selection.state === "needs_cookies") && <button className="button danger" type="button" onClick={() => void controlSelection("cancel")} disabled={busy}><Square size={13} /> Hủy nhóm</button>}
                 {(selection.state === "failed" || selection.state === "canceled" || selection.state === "needs_cookies") && <button className="button secondary" type="button" onClick={() => void controlSelection("resume")} disabled={busy || !acquisitionEnabled}><Play size={14} /> Thử lại nhóm</button>}
              </span>
            </div>
          )}
    </div>
  );
}
