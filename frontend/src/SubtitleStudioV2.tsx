import {
  AlignCenter,
  AlignLeft,
  AlignRight,
  AudioLines,
  Check,
  Copy,
  Download,
  Film,
  FileText,
  Home,
  Image,
  LayoutTemplate,
  LoaderCircle,
  Move,
  PanelLeft,
  Redo2,
  Sparkles,
  Square,
  Type,
  Upload,
  Undo2,
  Video,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE,
  api,
  type MediaMetadata,
  type SubtitleBurnOptions,
  type SubtitleCueV2,
  type SubtitleJob,
  type SubtitleRenderJob,
  type SubtitleRenderOptionsV2,
  type SubtitleWarning,
} from "./api";
import {
  SubtitleWorkspace,
  type SubtitleWorkspaceHandle,
} from "./subtitles/SubtitleWorkspace";
import { VirtualSubtitleList } from "./subtitles/VirtualSubtitleList";
import {
  readSavedDraft,
  type SavedSubtitleDraft,
} from "./subtitles/draft";
import {
  createManualCue,
  mergeCueWithNext,
  splitCueById,
  updateCueById,
} from "./subtitles/model";
import {
  DEFAULT_OVERLAY_LAYOUT,
  normalizeOverlayLayout,
} from "./subtitles/overlay";
import { sortCues } from "./subtitles/time";
import {
  DEFAULT_FRAME_TIMING,
  type OverlayLayout,
} from "./subtitles/types";
import { useSubtitleHistory } from "./subtitles/useSubtitleHistory";
import "./subtitle-studio.css";

const GEMINI_PROMPT_TEMPLATE = `Bạn là biên tập viên phụ đề tiếng Việt. Hãy nghe toàn bộ video và tạo transcript theo từng câu nói.

MỤC TIÊU
- Chép đúng lời nói, không tóm tắt, không tự thêm thông tin.
- Giữ các từ đệm có nghe thấy như “ờ”, “ừm”, “à” khi chúng ảnh hưởng ngữ điệu.
- Mỗi đoạn phải có mốc bắt đầu và kết thúc tính từ đầu video.
- Mốc thời gian được biểu diễn bằng số nguyên mili-giây.

QUY TẮC TIMING
1. start_ms là lúc âm đầu tiên của câu bắt đầu; end_ms là lúc âm cuối cùng kết thúc.
2. start_ms < end_ms, không dùng số âm, các đoạn phải theo thứ tự thời gian.
3. Không để hai đoạn chồng lấn. Nếu hai người nói đè nhau, tách speaker nhưng vẫn ghi đúng khoảng nghe thấy.
4. Không bịa ba chữ số mili-giây. Nếu hệ thống chỉ xác định được đến giây, đặt timing_precision_ms = 1000 và dùng phần mili-giây 000.
5. Nếu xác định được gần 100 ms, đặt timing_precision_ms = 100. Chỉ đặt timing_precision_ms = 1 khi thật sự có dữ liệu ở độ phân giải mili-giây.
6. Nếu không nghe rõ một từ, ghi “[không rõ]”; không đoán.

QUY TẮC CHIA CÂU
- Một segment chứa một ý nói tự nhiên, ưu tiên 1–6 giây.
- Không quá 84 ký tự mỗi segment; ưu tiên vị trí dấu câu và khoảng nghỉ để tách.
- Không thêm lời mở đầu, giải thích, Markdown hoặc code fence.

CHỈ TRẢ VỀ MỘT JSON HỢP LỆ THEO MẪU
{
  "schema_version": 2,
  "language": "vi",
  "timebase": "milliseconds",
  "timing_source": "gemini_estimate",
  "timing_precision_ms": 1000,
  "segments": [
    {
      "id": "s0001",
      "start_ms": 0,
      "end_ms": 3000,
      "text": "Nội dung phụ đề.",
      "confidence": 0.90
    }
  ]
}

Trước khi trả kết quả, tự kiểm tra:
- JSON parse được.
- ID không trùng.
- start_ms/end_ms là integer.
- Không có đoạn rỗng, đảo thời gian hoặc chồng lấn.
- Không có văn bản nào ngoài JSON.`;

const DEFAULT_OPTIONS: SubtitleBurnOptions = {
  font_name: "Arimo",
  font_size: 38,
  font_color: "#FFFFFF",
  bold: true,
  italic: false,
  underline: false,
  strikethrough: false,
  uppercase: false,
  alignment_type: "center",
  outline_color: "#000000",
  outline_width: 2,
  shadow_color: "#000000",
  shadow_width: 2,
  bg_enabled: false,
  bg_color: "#000000",
  bg_opacity: 0.75,
  spacing: 0,
  line_spacing: 1.2,
  pos_x: 50,
  pos_y: 78,
  position: "custom",
  video_speed: 1,
  volume: 1,
  fade_in: 0,
  fade_out: 0,
  aspect_ratio: "original",
  bg_fill_type: "blur",
  trim_start: 0,
  trim_end: null,
  animation: "none",
};

const STYLE_PRESETS: Record<string, Partial<SubtitleBurnOptions>> = {
  readable: {
    font_name: "Arimo",
    font_size: 38,
    font_color: "#FFFFFF",
    bold: true,
    outline_color: "#000000",
    outline_width: 2,
    shadow_width: 1,
    bg_enabled: false,
  },
  compact: {
    font_name: "Arial",
    font_size: 30,
    font_color: "#FFFFFF",
    bold: true,
    outline_color: "#000000",
    outline_width: 1,
    bg_enabled: true,
    bg_color: "#000000",
    bg_opacity: 0.72,
  },
  emphasis: {
    font_name: "Impact",
    font_size: 42,
    font_color: "#FFE45C",
    bold: true,
    uppercase: true,
    outline_color: "#111827",
    outline_width: 3,
    bg_enabled: false,
  },
};

type StudioTab = "upload" | "transcript" | "style" | "video" | "position";

type SubtitleStudioProps = {
  onBack?: () => void;
};

const SUBTITLE_DRAFT_KEY = "content-bot:subtitle-studio:v2";

const readLocalDraft = () =>
  readSavedDraft(window.localStorage, SUBTITLE_DRAFT_KEY, DEFAULT_OPTIONS);

const getErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const absoluteApiUrl = (path: string) =>
  path.startsWith("/api/v1/")
    ? `${API_BASE.replace(/\/api\/v1$/, "")}${path}`
    : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;

export function SubtitleStudio({ onBack }: SubtitleStudioProps) {
  const [initialDraft] = useState(readLocalDraft);
  const workspaceRef = useRef<SubtitleWorkspaceHandle>(null);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const metadataControllerRef = useRef<AbortController | null>(null);
  const parseControllerRef = useRef<AbortController | null>(null);
  const alignmentControllerRef = useRef<AbortController | null>(null);
  const renderControllerRef = useRef<AbortController | null>(null);
  const overlayUploadControllerRef = useRef<AbortController | null>(null);
  const overlayObjectUrlRef = useRef<string | null>(null);
  const [activeTab, setActiveTab] = useState<StudioTab>("transcript");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [projectName, setProjectName] = useState(
    initialDraft?.projectName ?? "Dự án chưa đặt tên",
  );
  const [videoId, setVideoId] = useState<string | null>(initialDraft?.videoId ?? null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState<string | null>(() =>
    initialDraft?.videoId
      ? `${API_BASE}/subtitles/video/${initialDraft.videoId}`
      : null,
  );
  const [renderedVideoUrl, setRenderedVideoUrl] = useState<string | null>(null);
  const [mediaDurationMs, setMediaDurationMs] = useState(
    initialDraft?.mediaDurationMs ?? 0,
  );
  const [mediaMetadata, setMediaMetadata] = useState<MediaMetadata | null>(null);
  const [rawText, setRawText] = useState(initialDraft?.rawText ?? "");
  const {
    cues,
    commit: commitCues,
    reset: resetCues,
    undo,
    redo,
    canUndo,
    canRedo,
  } = useSubtitleHistory(initialDraft?.cues ?? []);
  const [selectedCueId, setSelectedCueId] = useState<string | null>(
    initialDraft?.selectedCueId ?? null,
  );
  const [warnings, setWarnings] = useState<SubtitleWarning[]>([]);
  const [activeAlignmentJobId, setActiveAlignmentJobId] = useState<string | null>(
    initialDraft?.activeAlignmentJobId ?? null,
  );
  const [alignmentJob, setAlignmentJob] = useState<SubtitleJob | null>(null);
  const [activeRenderJobId, setActiveRenderJobId] = useState<string | null>(
    initialDraft?.activeRenderJobId ?? null,
  );
  const [renderJob, setRenderJob] = useState<SubtitleRenderJob | null>(null);
  const [options, setOptions] = useState<SubtitleBurnOptions>(
    initialDraft?.options ?? DEFAULT_OPTIONS,
  );
  const [overlayId, setOverlayId] = useState<string | null>(
    initialDraft?.overlayId ?? null,
  );
  const [overlayImage, setOverlayImage] = useState<string | null>(() =>
    initialDraft?.overlayId
      ? `${API_BASE}/subtitles/overlays/${initialDraft.overlayId}`
      : null,
  );
  const [overlayName, setOverlayName] = useState(initialDraft?.overlayName ?? "");
  const [overlayLayout, setOverlayLayout] = useState<OverlayLayout>(
    initialDraft?.overlayLayout ?? DEFAULT_OVERLAY_LAYOUT,
  );
  const [uploading, setUploading] = useState(false);
  const [overlayUploading, setOverlayUploading] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sortedCues = useMemo(() => sortCues(cues), [cues]);
  const frameTiming = mediaMetadata ?? DEFAULT_FRAME_TIMING;
  const thumbnailCacheKey = mediaMetadata?.fingerprint ?? (
    videoFile && videoId
      ? `${videoId}:${videoFile.size}:${videoFile.lastModified}`
      : videoId
  );
  const overlayNeedsUpload = Boolean(overlayImage && !overlayId && !overlayUploading);
  const overlayReady = !overlayImage || Boolean(overlayId && !overlayUploading);
  const canRender = Boolean(
    videoId &&
      overlayReady &&
      sortedCues.length > 0 &&
      sortedCues.every(
        (cue) => cue.text.trim() && cue.start_ms >= 0 && cue.end_ms > cue.start_ms,
      ),
  );
  const alignmentRunning =
    alignmentJob?.state === "queued" || alignmentJob?.state === "running";
  const canAlign = Boolean(
    videoId &&
      mediaMetadata?.has_audio &&
      sortedCues.some((cue) => cue.timing_source !== "manual") &&
      !activeAlignmentJobId &&
      !alignmentRunning,
  );
  const rendering = Boolean(activeRenderJobId) ||
    renderJob?.state === "queued" ||
    renderJob?.state === "running";

  useEffect(
    () => () => {
      uploadControllerRef.current?.abort();
      metadataControllerRef.current?.abort();
      parseControllerRef.current?.abort();
      alignmentControllerRef.current?.abort();
      renderControllerRef.current?.abort();
      overlayUploadControllerRef.current?.abort();
      if (overlayObjectUrlRef.current) URL.revokeObjectURL(overlayObjectUrlRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!videoId || mediaMetadata) return undefined;
    metadataControllerRef.current?.abort();
    const controller = new AbortController();
    metadataControllerRef.current = controller;
    void api
      .subtitleVideoMetadata(videoId, controller.signal)
      .then((media) => {
        if (controller.signal.aborted) return;
        setMediaMetadata(media);
        setMediaDurationMs(media.duration_ms);
      })
      .catch((metadataError: unknown) => {
        if (!controller.signal.aborted) {
          setError(
            getErrorMessage(
              metadataError,
              "Không đọc được FPS/PTS của video đã lưu.",
            ),
          );
        }
      });
    return () => controller.abort();
  }, [mediaMetadata, videoId]);

  useEffect(() => {
    if (!activeAlignmentJobId) return undefined;
    let stopped = false;
    let timer: number | null = null;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const job = await api.subtitleJob(activeAlignmentJobId, controller.signal);
        if (stopped) return;
        setAlignmentJob(job);
        if (job.state === "succeeded") {
          const result = job.result;
          if (result) {
            const nextCues = sortCues(result.document.segments);
            resetCues(nextCues);
            setWarnings(result.warnings);
            setSelectedCueId((current) =>
              current && nextCues.some((cue) => cue.id === current)
                ? current
                : (nextCues[0]?.id ?? null),
            );
          }
          setActiveAlignmentJobId(null);
          return;
        }
        if (job.state === "failed" || job.state === "canceled") {
          if (job.state === "failed") {
            setError(job.error || "Alignment audio thất bại.");
          }
          setActiveAlignmentJobId(null);
          return;
        }
        timer = window.setTimeout(() => void poll(), 500);
      } catch (pollError: unknown) {
        if (controller.signal.aborted || stopped) return;
        setError(
          getErrorMessage(
            pollError,
            "Mất kết nối khi đọc tiến độ alignment; đang thử lại.",
          ),
        );
        timer = window.setTimeout(() => void poll(), 1200);
      }
    };

    void poll();
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [activeAlignmentJobId, resetCues]);

  useEffect(() => {
    if (!activeRenderJobId) return undefined;
    let stopped = false;
    let timer: number | null = null;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const job = await api.subtitleRenderJob(activeRenderJobId, controller.signal);
        if (stopped) return;
        setRenderJob(job);
        if (job.state === "succeeded") {
          if (job.result) {
            setRenderedVideoUrl(
              `${absoluteApiUrl(job.result.subtitled_video_url)}?t=${Date.now()}`,
            );
          }
          setActiveRenderJobId(null);
          return;
        }
        if (job.state === "failed" || job.state === "canceled") {
          if (job.state === "failed") {
            setError(job.error || "Render video thất bại.");
          }
          setActiveRenderJobId(null);
          return;
        }
        timer = window.setTimeout(() => void poll(), 500);
      } catch (pollError: unknown) {
        if (controller.signal.aborted || stopped) return;
        setError(
          getErrorMessage(
            pollError,
            "Mất kết nối khi đọc tiến độ render; đang thử lại.",
          ),
        );
        timer = window.setTimeout(() => void poll(), 1200);
      }
    };

    void poll();
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [activeRenderJobId]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const draft: SavedSubtitleDraft = {
        version: 2,
        videoId,
        projectName,
        mediaDurationMs,
        rawText,
        cues,
        selectedCueId,
        activeAlignmentJobId,
        activeRenderJobId,
        options,
        overlayId,
        overlayName,
        overlayLayout,
      };
      try {
        window.localStorage.setItem(SUBTITLE_DRAFT_KEY, JSON.stringify(draft));
      } catch {
        // Storage can be unavailable in private mode; editing remains functional.
      }
    }, 600);
    return () => window.clearTimeout(timer);
  }, [
    activeAlignmentJobId,
    activeRenderJobId,
    cues,
    mediaDurationMs,
    options,
    overlayId,
    overlayLayout,
    overlayName,
    projectName,
    rawText,
    selectedCueId,
    videoId,
  ]);

  useEffect(() => {
    const handleHistoryShortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select") ||
        target?.isContentEditable ||
        !(event.ctrlKey || event.metaKey) ||
        event.key.toLowerCase() !== "z"
      ) {
        return;
      }
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    };
    window.addEventListener("keydown", handleHistoryShortcut);
    return () => window.removeEventListener("keydown", handleHistoryShortcut);
  }, [redo, undo]);

  const handleDurationChange = useCallback(
    (durationMs: number) => {
      if (!mediaMetadata) setMediaDurationMs(durationMs);
    },
    [mediaMetadata],
  );

  const handleFileChange = async (file: File) => {
    uploadControllerRef.current?.abort();
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    setVideoFile(file);
    setProjectName(file.name);
    setUploading(true);
    setError(null);
    setRenderedVideoUrl(null);
    setMediaMetadata(null);
    setMediaDurationMs(0);
    alignmentControllerRef.current?.abort();
    renderControllerRef.current?.abort();
    setActiveAlignmentJobId(null);
    setAlignmentJob(null);
    setActiveRenderJobId(null);
    setRenderJob(null);
    resetCues([]);
    setSelectedCueId(null);
    setWarnings([]);
    try {
      const result = await api.uploadSubtitleVideo(file, controller.signal);
      if (controller.signal.aborted) return;
      setVideoId(result.video_id);
      setMediaMetadata(result.media);
      setMediaDurationMs(result.media.duration_ms);
      setOriginalVideoUrl(`${API_BASE}/subtitles/video/${result.video_id}`);
      setActiveTab("transcript");
    } catch (uploadError: unknown) {
      if (controller.signal.aborted) return;
      setError(getErrorMessage(uploadError, "Không tải được video. Kiểm tra định dạng và thử lại."));
    } finally {
      if (!controller.signal.aborted) setUploading(false);
    }
  };

  const handleOverlayChange = async (file: File) => {
    overlayUploadControllerRef.current?.abort();
    const controller = new AbortController();
    overlayUploadControllerRef.current = controller;
    const previousOverlay = overlayId
      ? { id: overlayId, image: overlayImage, name: overlayName }
      : { id: null, image: null, name: "" };
    if (overlayObjectUrlRef.current) URL.revokeObjectURL(overlayObjectUrlRef.current);
    const objectUrl = URL.createObjectURL(file);
    overlayObjectUrlRef.current = objectUrl;
    setOverlayId(null);
    setOverlayImage(objectUrl);
    setOverlayName(file.name);
    setSelectedCueId(null);
    setOverlayUploading(true);
    setError(null);
    try {
      const uploaded = await api.uploadSubtitleOverlay(file, controller.signal);
      if (controller.signal.aborted) return;
      if (overlayObjectUrlRef.current === objectUrl) {
        URL.revokeObjectURL(objectUrl);
        overlayObjectUrlRef.current = null;
      }
      setOverlayId(uploaded.overlay_id);
      setOverlayImage(absoluteApiUrl(uploaded.overlay_url));
      setOverlayName(uploaded.filename);
    } catch (uploadError: unknown) {
      if (controller.signal.aborted) return;
      if (overlayObjectUrlRef.current === objectUrl) {
        URL.revokeObjectURL(objectUrl);
        overlayObjectUrlRef.current = null;
      }
      setOverlayId(previousOverlay.id);
      setOverlayImage(previousOverlay.image);
      setOverlayName(previousOverlay.name);
      setError(
        getErrorMessage(
          uploadError,
          "Không tải được ảnh phủ. Hãy dùng PNG, JPEG hoặc WebP rồi thử lại.",
        ),
      );
    } finally {
      if (overlayUploadControllerRef.current === controller) {
        overlayUploadControllerRef.current = null;
        setOverlayUploading(false);
      }
    }
  };

  const handleCopyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(GEMINI_PROMPT_TEMPLATE);
      setCopiedPrompt(true);
      window.setTimeout(() => setCopiedPrompt(false), 2500);
    } catch {
      setError("Không sao chép được prompt. Hãy cấp quyền clipboard rồi thử lại.");
    }
  };

  const handleParse = async () => {
    const text = rawText.trim();
    if (!text) {
      setError("Chưa có kết quả Gemini. Dán JSON hoặc SRT/VTT rồi phân tích lại.");
      return;
    }
    parseControllerRef.current?.abort();
    const controller = new AbortController();
    parseControllerRef.current = controller;
    setParsing(true);
    setError(null);
    try {
      const result = await api.parseSubtitleTextV2(
        text,
        mediaDurationMs || null,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const nextCues = sortCues(result.document.segments);
      commitCues(nextCues);
      setWarnings(result.warnings);
      setSelectedCueId(nextCues[0]?.id ?? null);
      if (nextCues.length === 0) {
        setError("Không tìm thấy cue hợp lệ. Kiểm tra JSON hoặc mốc thời gian đầu vào.");
      }
    } catch (parseError: unknown) {
      if (controller.signal.aborted) return;
      setError(getErrorMessage(parseError, "Không phân tích được phụ đề. Kiểm tra dữ liệu và thử lại."));
    } finally {
      if (!controller.signal.aborted) setParsing(false);
    }
  };

  const handleCueChange = useCallback(
    (
      cueId: string,
      patch: Partial<Pick<SubtitleCueV2, "start_ms" | "end_ms" | "text">>,
    ) => {
      commitCues((current) =>
        updateCueById(current, cueId, patch, {
          markManual: true,
          durationMs: mediaDurationMs || undefined,
        }),
        "text" in patch ? `text:${cueId}` : `timing:${cueId}`,
      );
    },
    [commitCues, mediaDurationMs],
  );

  const handleAlign = useCallback(
    async (cueIds?: string[]) => {
      if (!videoId || !mediaMetadata) {
        setError("Hãy tải video và chờ đọc metadata trước khi căn timing.");
        return;
      }
      if (!mediaMetadata.has_audio) {
        setError("Video không có audio; hãy chỉnh timing thủ công.");
        return;
      }
      alignmentControllerRef.current?.abort();
      const controller = new AbortController();
      alignmentControllerRef.current = controller;
      setError(null);
      try {
        const timingSources = new Set(sortedCues.map((cue) => cue.timing_source));
        const document = {
          schema_version: 2 as const,
          language: "vi",
          timebase: "milliseconds" as const,
          timing_source:
            timingSources.size === 1
              ? sortedCues[0]?.timing_source ?? "gemini_estimate"
              : ("gemini_estimate" as const),
          timing_precision_ms: Math.max(
            1,
            ...sortedCues.map((cue) => cue.timing_precision_ms),
          ),
          segments: sortedCues,
        };
        const job = await api.alignSubtitleDocument(
          videoId,
          document,
          { engine: "auto" },
          cueIds,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setAlignmentJob(job);
        setActiveAlignmentJobId(job.id);
      } catch (alignmentError: unknown) {
        if (!controller.signal.aborted) {
          setError(
            getErrorMessage(alignmentError, "Không thể bắt đầu alignment audio."),
          );
        }
      }
    },
    [mediaMetadata, sortedCues, videoId],
  );

  const handleCancelAlignment = async () => {
    if (!alignmentJob || !alignmentRunning) return;
    try {
      const job = await api.cancelSubtitleJob(alignmentJob.id);
      setAlignmentJob(job);
    } catch (cancelError: unknown) {
      setError(getErrorMessage(cancelError, "Không hủy được alignment."));
    }
  };

  const handleCueTimingCommit = useCallback(
    (cueId: string, startMs: number, endMs: number) =>
      handleCueChange(cueId, { start_ms: startMs, end_ms: endMs }),
    [handleCueChange],
  );

  const handleAddCue = () => {
    const cue = createManualCue(sortedCues, mediaDurationMs);
    commitCues((current) => sortCues([...current, cue]));
    setSelectedCueId(cue.id);
    workspaceRef.current?.seekTo(cue.start_ms);
  };

  const handleDeleteCue = (cueId: string) => {
    commitCues((current) => current.filter((cue) => cue.id !== cueId));
    if (selectedCueId === cueId) setSelectedCueId(null);
  };

  const handleSplitCue = (cueId: string) => {
    const cue = sortedCues.find((item) => item.id === cueId);
    if (!cue) return;
    const playheadMs = workspaceRef.current?.getCurrentMs() ?? cue.start_ms;
    const splitMs =
      playheadMs > cue.start_ms && playheadMs < cue.end_ms
        ? playheadMs
        : Math.round((cue.start_ms + cue.end_ms) / 2);
    commitCues((current) => splitCueById(current, cueId, splitMs));
    workspaceRef.current?.seekTo(splitMs);
  };

  const handleMergeCue = (cueId: string) => {
    commitCues((current) => mergeCueWithNext(current, cueId));
    setSelectedCueId(cueId);
  };

  const handleRender = async () => {
    if (!videoId || !canRender) return;
    renderControllerRef.current?.abort();
    const controller = new AbortController();
    renderControllerRef.current = controller;
    setError(null);
    try {
      const timingSources = new Set(sortedCues.map((cue) => cue.timing_source));
      const document = {
        schema_version: 2 as const,
        language: "vi",
        timebase: "milliseconds" as const,
        timing_source:
          timingSources.size === 1
            ? sortedCues[0]?.timing_source ?? "manual"
            : ("manual" as const),
        timing_precision_ms: Math.max(
          1,
          ...sortedCues.map((cue) => cue.timing_precision_ms),
        ),
        segments: sortedCues,
      };
      const animation = options.animation ?? "none";
      const renderOptions: SubtitleRenderOptionsV2 = {
        render_mode: animation === "none" ? "precision" : "effects",
        profile: "fast",
        encoder: "auto",
        font_name: options.font_name,
        font_size: options.font_size,
        font_color: options.font_color,
        bold: options.bold,
        italic: options.italic,
        underline: options.underline ?? false,
        strikethrough: options.strikethrough ?? false,
        uppercase: options.uppercase,
        alignment_type: options.alignment_type ?? "center",
        outline_color: options.outline_color,
        outline_width: options.outline_width,
        shadow_color: options.shadow_color,
        shadow_width: options.shadow_width,
        bg_enabled: options.bg_enabled,
        bg_color: options.bg_color,
        bg_opacity: options.bg_opacity,
        spacing: options.spacing,
        line_spacing: options.line_spacing ?? 1.2,
        pos_x: options.pos_x,
        pos_y: options.pos_y,
        position: options.position,
        video_speed: options.video_speed ?? 1,
        volume: options.volume ?? 1,
        aspect_ratio: options.aspect_ratio ?? "original",
        bg_fill_type: options.bg_fill_type ?? "blur",
        trim_start_ms: Math.max(0, Math.round((options.trim_start ?? 0) * 1000)),
        trim_end_ms:
          options.trim_end == null ? null : Math.round(options.trim_end * 1000),
        fade_in_ms: Math.max(0, Math.round((options.fade_in ?? 0) * 1000)),
        fade_out_ms: Math.max(0, Math.round((options.fade_out ?? 0) * 1000)),
        animation,
      };
      const job = await api.renderSubtitleDocument(
        videoId,
        document,
        renderOptions,
        overlayId ? { overlay_id: overlayId, ...overlayLayout } : null,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setRenderedVideoUrl(null);
      setRenderJob(job);
      setActiveRenderJobId(job.id);
    } catch (renderError: unknown) {
      if (!controller.signal.aborted) {
        setError(
          getErrorMessage(
            renderError,
            "Không bắt đầu được render. Kiểm tra timeline rồi thử lại.",
          ),
        );
      }
    }
  };

  const handleCancelRender = async () => {
    const jobId = renderJob?.id ?? activeRenderJobId;
    if (!jobId || !rendering) return;
    try {
      const job = await api.cancelSubtitleRenderJob(jobId);
      setRenderJob(job);
    } catch (cancelError: unknown) {
      setError(getErrorMessage(cancelError, "Không hủy được render."));
    }
  };

  const clearOverlay = () => {
    overlayUploadControllerRef.current?.abort();
    overlayUploadControllerRef.current = null;
    if (overlayObjectUrlRef.current) URL.revokeObjectURL(overlayObjectUrlRef.current);
    overlayObjectUrlRef.current = null;
    setOverlayUploading(false);
    setOverlayId(null);
    setOverlayImage(null);
    setOverlayName("");
    setOverlayLayout(DEFAULT_OVERLAY_LAYOUT);
  };

  const tabs: { id: StudioTab; label: string; icon: typeof Upload }[] = [
    { id: "upload", label: "Tải lên", icon: Upload },
    { id: "transcript", label: "Phụ đề", icon: FileText },
    { id: "style", label: "Kiểu chữ", icon: Type },
    { id: "video", label: "Video", icon: Video },
    { id: "position", label: "Bố cục", icon: Move },
  ];

  return (
    <div className="subtitle-studio-shell">
      <header className="subtitle-studio-header">
        <div className="subtitle-studio-header-start">
          <button type="button" className="studio-header-icon" onClick={onBack} aria-label="Về trang chính">
            <Home size={19} />
          </button>
          <button
            type="button"
            className="studio-header-icon is-sidebar-toggle"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-label={sidebarOpen ? "Đóng bảng công cụ" : "Mở bảng công cụ"}
            aria-expanded={sidebarOpen}
          >
            <PanelLeft size={19} />
          </button>
          <div className="subtitle-studio-title">
            <strong>Subtitle Studio</strong>
            <span>{projectName}</span>
          </div>
          <div className="subtitle-history-controls" aria-label="Lịch sử chỉnh sửa">
            <button type="button" className="studio-header-icon" disabled={!canUndo} onClick={undo} aria-label="Hoàn tác" title="Hoàn tác (Ctrl+Z)"><Undo2 size={17} /></button>
            <button type="button" className="studio-header-icon" disabled={!canRedo} onClick={redo} aria-label="Làm lại" title="Làm lại (Ctrl+Shift+Z)"><Redo2 size={17} /></button>
          </div>
        </div>
        <div className="subtitle-studio-header-status" aria-live="polite">
          {rendering && renderJob && (
            <span>
              <LoaderCircle className="spin" size={15} /> {renderJob.message} ·{" "}
              {renderJob.progress}%
            </span>
          )}
        </div>
        <div className="subtitle-studio-header-actions">
          {renderedVideoUrl && (
            <a
              className="studio-header-action is-secondary"
              href={renderedVideoUrl}
              download={renderJob?.result?.output_filename ?? `subtitled_${videoId}.mp4`}
            >
              <Download size={17} /> <span>Tải video</span>
            </a>
          )}
          <button
            type="button"
            className="studio-header-action is-primary"
            disabled={!rendering && (!canRender || alignmentRunning)}
            onClick={() =>
              rendering ? void handleCancelRender() : void handleRender()
            }
            title={
              overlayUploading
                ? "Đợi ảnh phủ tải xong trước khi xuất"
                : overlayNeedsUpload
                  ? "Hãy chọn lại ảnh phủ để đồng bộ với file xuất"
                  : !canRender
                    ? "Cần video và cue hợp lệ trước khi xuất"
                    : "Xuất video có phụ đề và ảnh phủ"
            }
          >
            {rendering ? <Square size={14} fill="currentColor" /> : <Film size={17} />}
            <span>{rendering ? "Hủy xuất" : "Xuất video"}</span>
          </button>
        </div>
      </header>

      {error && (
        <div className="subtitle-studio-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="Đóng thông báo lỗi"><X size={16} /></button>
        </div>
      )}

      <div className="subtitle-studio-body">
        <nav className="subtitle-tool-rail" aria-label="Công cụ Subtitle Studio">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={activeTab === id ? "is-active" : ""}
              aria-label={label}
              aria-pressed={activeTab === id}
              onClick={() => {
                setActiveTab(id);
                setSidebarOpen(true);
              }}
            >
              <Icon size={20} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <aside className={`subtitle-tool-panel ${sidebarOpen ? "is-open" : ""}`} aria-label="Bảng công cụ">
          {activeTab === "upload" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading">
                <h2>Tệp dự án</h2>
                <p>Video gốc và ảnh phủ được tải một lần, sau đó tái sử dụng trong preview.</p>
              </div>
              <label className={`studio-file-drop ${uploading ? "is-loading" : ""}`}>
                <input
                  type="file"
                  accept="video/*"
                  disabled={uploading}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void handleFileChange(file);
                  }}
                />
                {uploading ? <LoaderCircle className="spin" size={22} /> : <Upload size={22} />}
                <strong>{uploading ? "Đang tải video" : videoFile?.name ?? "Chọn video"}</strong>
                <span>MP4, MOV hoặc WebM</span>
              </label>
              <label className={`studio-file-drop is-compact ${overlayUploading ? "is-loading" : ""}`}>
                <input
                  type="file"
                  accept=".png,.jpg,.jpeg,.webp"
                  disabled={overlayUploading}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.currentTarget.value = "";
                    if (file) void handleOverlayChange(file);
                  }}
                />
                {overlayUploading ? <LoaderCircle className="spin" size={20} /> : <Image size={20} />}
                <strong>{overlayUploading ? "Đang đồng bộ ảnh phủ" : overlayName || "Thêm ảnh phủ"}</strong>
                <span>PNG, JPEG hoặc WebP · tối đa 10 MB</span>
              </label>
              {overlayImage && (
                <div className="studio-overlay-controls" aria-label="Điều chỉnh ảnh phủ">
                  <p>
                    {overlayUploading
                      ? "Ảnh đang được chuẩn bị cho file xuất…"
                      : overlayNeedsUpload
                        ? "Hãy chọn lại ảnh phủ để đồng bộ với file xuất."
                        : "Kéo ảnh trên video để di chuyển hoặc kéo một góc để đổi kích thước."}
                  </p>
                  <label className="studio-range-field">
                    <span>Kích thước ảnh phủ <output>{overlayLayout.width.toFixed(1)}%</output></span>
                    <input
                      type="range"
                      min={4}
                      max={90}
                      step={0.1}
                      value={overlayLayout.width}
                      aria-label="Kích thước ảnh phủ theo phần trăm chiều rộng video"
                      onChange={(event) => setOverlayLayout((current) => normalizeOverlayLayout({
                        ...current,
                        width: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <div className="studio-overlay-actions">
                    <button
                      type="button"
                      className="studio-secondary-button"
                      onClick={() => setOverlayLayout(DEFAULT_OVERLAY_LAYOUT)}
                    >
                      Đặt lại ảnh phủ
                    </button>
                    <button type="button" className="studio-secondary-button" onClick={clearOverlay}>Xóa ảnh phủ</button>
                  </div>
                </div>
              )}
            </div>
          )}

          {activeTab === "transcript" && (
            <div className="studio-panel-section is-transcript">
              <div className="studio-panel-heading">
                <h2>Tạo phụ đề</h2>
                <p>Gemini tạo transcript nháp; parser giữ nguyên độ chính xác mà nguồn thực sự cung cấp.</p>
              </div>
              <button
                type="button"
                className={`studio-copy-prompt ${copiedPrompt ? "is-success" : ""}`}
                onClick={() => void handleCopyPrompt()}
              >
                {copiedPrompt ? <Check size={17} /> : <Copy size={17} />}
                {copiedPrompt ? "Đã sao chép" : "Sao chép prompt JSON V2"}
              </button>
              <label className="studio-textarea-field">
                <span>Kết quả Gemini, SRT hoặc VTT</span>
                <textarea
                  rows={6}
                  value={rawText}
                  placeholder='Dán JSON bắt đầu bằng { "schema_version": 2, … }'
                  onChange={(event) => setRawText(event.target.value)}
                />
              </label>
              <button
                type="button"
                className="studio-primary-button"
                disabled={parsing || !rawText.trim()}
                aria-busy={parsing}
                onClick={() => void handleParse()}
              >
                {parsing ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
                {parsing ? "Đang phân tích" : "Phân tích phụ đề"}
              </button>
              {sortedCues.length > 0 && (
                <div className="subtitle-alignment-panel">
                  <div className="subtitle-alignment-heading">
                    <div>
                      <strong>Căn timing theo audio</strong>
                      <span>
                        {mediaMetadata?.has_audio
                          ? "Cue thủ công được khóa; cache theo audio + transcript."
                          : "Video này không có audio để căn tự động."}
                      </span>
                    </div>
                    {alignmentRunning ? (
                      <button
                        type="button"
                        className="studio-icon-button is-danger"
                        aria-label="Hủy alignment"
                        title="Hủy alignment"
                        onClick={() => void handleCancelAlignment()}
                      >
                        <Square size={14} fill="currentColor" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="studio-secondary-button"
                        disabled={!canAlign}
                        onClick={() => void handleAlign()}
                      >
                        <AudioLines size={16} />
                        Căn tất cả
                      </button>
                    )}
                  </div>
                  {alignmentJob && (
                    <div
                      className={`subtitle-job-progress state-${alignmentJob.state}`}
                      aria-live="polite"
                    >
                      <div>
                        <span>{alignmentJob.message}</span>
                        <strong>{alignmentJob.progress}%</strong>
                      </div>
                      <progress max={100} value={alignmentJob.progress} />
                      {alignmentJob.state === "succeeded" && alignmentJob.result && (
                        <small>
                          {alignmentJob.result.aligned_cue_count} cue ·{" "}
                          {alignmentJob.result.engine === "energy"
                            ? "Energy VAD"
                            : "Faster Whisper"}
                          {alignmentJob.result.cache_hit ? " · cache" : ""}
                        </small>
                      )}
                    </div>
                  )}
                </div>
              )}
              {warnings.length > 0 && (
                <details className="subtitle-warning-summary">
                  <summary>{warnings.length} cảnh báo timing</summary>
                  <ul>
                    {warnings.slice(0, 12).map((warning, index) => (
                      <li key={`${warning.code}-${warning.cue_id ?? index}`}>{warning.message}</li>
                    ))}
                  </ul>
                </details>
              )}
              {sortedCues.length > 0 && (
                <VirtualSubtitleList
                  cues={sortedCues}
                  selectedCueId={selectedCueId}
                  durationMs={mediaDurationMs}
                  frameTiming={frameTiming}
                  onAdd={handleAddCue}
                  onSelect={setSelectedCueId}
                  onSeek={(milliseconds) => workspaceRef.current?.seekTo(milliseconds)}
                  onDelete={handleDeleteCue}
                  onSplit={handleSplitCue}
                  onMergeNext={handleMergeCue}
                  onChange={handleCueChange}
                  onAlignCue={(cueId) => void handleAlign([cueId])}
                />
              )}
            </div>
          )}

          {activeTab === "style" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading"><h2>Kiểu phụ đề</h2><p>Preview trực tiếp dùng cùng lựa chọn font, màu và khoảng cách với renderer.</p></div>
              <div className="studio-preset-grid">
                {Object.entries(STYLE_PRESETS).map(([name, preset]) => (
                  <button key={name} type="button" onClick={() => setOptions((current) => ({ ...current, ...preset }))}>
                    <LayoutTemplate size={17} />
                    {name === "readable" ? "Dễ đọc" : name === "compact" ? "Gọn" : "Nhấn mạnh"}
                  </button>
                ))}
              </div>
              <label className="studio-field"><span>Font</span><select value={options.font_name} onChange={(event) => setOptions({ ...options, font_name: event.target.value })}><option>Arimo</option><option>Arial</option><option>Impact</option><option>Segoe UI</option></select></label>
              <div className="studio-field-grid">
                <label className="studio-field"><span>Cỡ chữ</span><input type="number" min={14} max={96} value={options.font_size} onChange={(event) => setOptions({ ...options, font_size: Number(event.target.value) })} /></label>
                <label className="studio-field"><span>Màu chữ</span><input type="color" value={options.font_color} onChange={(event) => setOptions({ ...options, font_color: event.target.value })} /></label>
              </div>
              <div className="studio-toggle-row">
                <label><input type="checkbox" checked={options.bold} onChange={(event) => setOptions({ ...options, bold: event.target.checked })} /> Đậm</label>
                <label><input type="checkbox" checked={options.uppercase} onChange={(event) => setOptions({ ...options, uppercase: event.target.checked })} /> Viết hoa</label>
                <label><input type="checkbox" checked={options.bg_enabled} onChange={(event) => setOptions({ ...options, bg_enabled: event.target.checked })} /> Nền</label>
              </div>
              <div className="studio-align-row" aria-label="Căn chữ">
                {(["left", "center", "right"] as const).map((alignment) => {
                  const Icon = alignment === "left" ? AlignLeft : alignment === "center" ? AlignCenter : AlignRight;
                  return <button key={alignment} type="button" className={options.alignment_type === alignment ? "is-active" : ""} aria-label={`Căn ${alignment}`} aria-pressed={options.alignment_type === alignment} onClick={() => setOptions({ ...options, alignment_type: alignment })}><Icon size={18} /></button>;
                })}
              </div>
              <label className="studio-range-field"><span>Viền <output>{options.outline_width}px</output></span><input type="range" min={0} max={8} step={1} value={options.outline_width} onChange={(event) => setOptions({ ...options, outline_width: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Giãn chữ <output>{options.spacing}px</output></span><input type="range" min={-2} max={12} step={0.5} value={options.spacing} onChange={(event) => setOptions({ ...options, spacing: Number(event.target.value) })} /></label>
            </div>
          )}

          {activeTab === "video" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading"><h2>Video</h2><p>Trim và tốc độ dùng chung time-map khi xuất.</p></div>
              <label className="studio-range-field"><span>Tốc độ <output>{options.video_speed?.toFixed(2)}×</output></span><input type="range" min={0.5} max={2} step={0.05} value={options.video_speed} onChange={(event) => setOptions({ ...options, video_speed: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Âm lượng <output>{Math.round((options.volume ?? 1) * 100)}%</output></span><input type="range" min={0} max={1} step={0.01} value={options.volume} onChange={(event) => setOptions({ ...options, volume: Number(event.target.value) })} /></label>
              <div className="studio-field-grid"><label className="studio-field"><span>Trim đầu (s)</span><input type="number" min={0} step={0.001} value={options.trim_start ?? 0} onChange={(event) => setOptions({ ...options, trim_start: Number(event.target.value) })} /></label><label className="studio-field"><span>Trim cuối (s)</span><input type="number" min={0} step={0.001} value={options.trim_end ?? ""} placeholder="Tự động" onChange={(event) => setOptions({ ...options, trim_end: event.target.value ? Number(event.target.value) : null })} /></label></div>
              <label className="studio-field"><span>Tỉ lệ khung</span><select value={options.aspect_ratio} onChange={(event) => setOptions({ ...options, aspect_ratio: event.target.value as SubtitleBurnOptions["aspect_ratio"] })}><option value="original">Gốc</option><option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option></select></label>
              <label className="studio-field"><span>Chuyển động</span><select value={options.animation} onChange={(event) => setOptions({ ...options, animation: event.target.value as SubtitleBurnOptions["animation"] })}><option value="none">Không — timing 1 ms</option><option value="fade">Fade — timing 10 ms</option><option value="rise">Rise — timing 10 ms</option><option value="pan">Pan — timing 10 ms</option><option value="typewriter">Typewriter — timing 10 ms</option></select><small>Hiệu ứng dùng chế độ ASS tương thích 10 ms; chọn “Không” để giữ timestamp 1 ms.</small></label>
            </div>
          )}

          {activeTab === "position" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading"><h2>Vị trí và nền</h2><p>Kéo trực tiếp phụ đề trên video hoặc nhập tọa độ chính xác.</p></div>
              <label className="studio-range-field"><span>Ngang <output>{options.pos_x.toFixed(1)}%</output></span><input type="range" min={0} max={100} step={0.1} value={options.pos_x} onChange={(event) => setOptions({ ...options, position: "custom", pos_x: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Dọc <output>{options.pos_y.toFixed(1)}%</output></span><input type="range" min={0} max={100} step={0.1} value={options.pos_y} onChange={(event) => setOptions({ ...options, position: "custom", pos_y: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Độ mờ nền <output>{Math.round(options.bg_opacity * 100)}%</output></span><input type="range" min={0} max={1} step={0.05} value={options.bg_opacity} disabled={!options.bg_enabled} onChange={(event) => setOptions({ ...options, bg_opacity: Number(event.target.value) })} /></label>
              <button type="button" className="studio-secondary-button" onClick={() => setOptions({ ...options, position: "custom", pos_x: 50, pos_y: 78 })}>Đặt lại vị trí</button>
            </div>
          )}
        </aside>

        {sidebarOpen && <button type="button" className="subtitle-panel-backdrop" aria-label="Đóng bảng công cụ" onClick={() => setSidebarOpen(false)} />}

        <main className="subtitle-studio-main">
          <SubtitleWorkspace
            ref={workspaceRef}
            originalVideoUrl={originalVideoUrl}
            renderedVideoUrl={renderedVideoUrl}
            thumbnailCacheKey={thumbnailCacheKey}
            media={mediaMetadata}
            cues={sortedCues}
            selectedCueId={selectedCueId}
            options={options}
            overlayImage={overlayImage}
            overlayName={overlayName}
            overlayLayout={overlayLayout}
            onDurationChange={handleDurationChange}
            onSelectCue={setSelectedCueId}
            onUpdateCueText={(cueId, text) => handleCueChange(cueId, { text })}
            onCueTimingCommit={handleCueTimingCommit}
            onOptionsChange={setOptions}
            onOverlayLayoutChange={setOverlayLayout}
          />
        </main>
      </div>
    </div>
  );
}
