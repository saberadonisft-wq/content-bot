import { useSubtitleImport } from "./subtitles/useSubtitleImport";
import { SubtitleVersionsPanel } from "./subtitles/SubtitleVersionsPanel";
import { GeminiAdvancedOptions } from "./subtitles/GeminiAdvancedOptions";
import { GeminiChunkProgress } from "./subtitles/GeminiChunkProgress";
import { SubtitlePipelineBar, type SubtitlePipelineStage } from "./subtitles/SubtitlePipelineBar";
import { useRenderedVideoDownload } from "./subtitles/useRenderedVideoDownload";
import type { DesktopSelectedVideo } from "./desktopLiveWall";
import {
AlignCenter,
AlignLeft,
AlignRight,
AudioLines,
Check,
Circle,
Copy,
Download,
EyeOff,
FileText,
Image,
Languages,
LayoutTemplate,
LoaderCircle,
Mic,
Minus,
Moon,
Move,
RectangleHorizontal,
RefreshCw,
ScanText,
Sparkles,
Square,
Trash2,
Type,
Upload,
Video,
X
} from "lucide-react";
import { useCallback,useEffect,useMemo,useRef,useState } from "react";
import {
api,
API_BASE,
type GeminiApiStatus,
type GeminiModel,
type GeminiSubtitleJob,
type MediaMetadata,
type SubtitleBurnOptions,
type SubtitleCueV2,
type SubtitleDocumentV2,
type GeminiSubtitleOptions,
type SubtitleAlignmentOptions,
type SubtitleJob,
type SubtitleRenderJob,
type SubtitleWarning
} from "./api";
import "./canva.css";
import "./subtitle-studio.css";
import { StudioToolbar } from "./subtitles/StudioToolbar";
import {
SubtitleWorkspace,
type SubtitleWorkspaceHandle,
} from "./subtitles/SubtitleWorkspace";
import { VirtualSubtitleList } from "./subtitles/VirtualSubtitleList";
import { SourceSubtitleEditor } from './subtitles/SourceSubtitleEditor';
import { SceneSplitPanel } from './subtitles/SceneSplitPanel';
import { useSourceDocument } from './subtitles/source-document';
import { useExtractionJobs } from './subtitles/useExtractionJobs';
import { useAsrRuntime } from './subtitles/useAsrRuntime';
import { asrReadiness as getAsrReadiness } from './subtitles/asr-readiness';
import {
readSavedDraft,
DEFAULT_EXTRACTION_SETTINGS,
type SavedSubtitleDraft,
} from "./subtitles/draft";
import {
createSubtitleMask,
MAX_SUBTITLE_MASKS,
normalizeSubtitleMask,
} from "./subtitles/masks";
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
import { applyCuePosition } from "./subtitles/position";
import { createRenderOptions,createSubtitleDocument,DEFAULT_OPTIONS,GEMINI_PROMPT_TEMPLATE,STYLE_PRESETS } from "./subtitles/studioConfig";
import { sortCues } from "./subtitles/time";
import {
DEFAULT_FRAME_TIMING,
type OverlayLayout,
type SubtitleMaskEffect,
type SubtitleMaskRegion,
type SubtitleMaskShape,
type ExtractionMode,
type SubtitleOcrRegion,
type VideoClip,
} from "./subtitles/types";
import { SUBTITLE_DRAFT_KEY,useDraftPersistence } from "./subtitles/useDraftPersistence";
import { useJobPolling } from "./subtitles/useJobPolling";
import { useSubtitleHistory } from "./subtitles/useSubtitleHistory";
import {
createInitialVideoClip,
findVideoClipAt,
splitVideoClip,
} from "./subtitles/video-clips";
import { VoicePanel } from "./voiceover/VoicePanel";
import { useVoiceover } from "./voiceover/useVoiceover";
import "./voiceover/voiceover.css";

const documentMetadata = (document: SubtitleDocumentV2): NonNullable<SavedSubtitleDraft['documentMeta']> => {
  const { segments, ...metadata } = document;
  void segments;
  return { ...metadata, revision: document.revision ?? 0, run_id: document.run_id ?? null };
};

type StudioTab = "upload" | "transcript" | "style" | "mask" | "video" | "position" | "voice";

type SubtitleStudioProps = {
  onBack?: () => void;
  onOpenSettings?: () => void;
};



const readLocalDraft = () =>
  readSavedDraft(window.localStorage, SUBTITLE_DRAFT_KEY, DEFAULT_OPTIONS);

const getErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

const formatGeminiError = (error: string | null | undefined) => {
  const raw = (error ?? "").trim();
  const lowered = raw.toLowerCase();
  if (
    lowered.includes("api_key_invalid") ||
    lowered.includes("api key not valid") ||
    lowered.includes("khong hop le") ||
    lowered.includes("từ chối api key") ||
    lowered.includes("http 401")
  ) {
    return lowered.includes("http 401")
      ? raw
      : `HTTP 401 — API key Gemini không hợp lệ hoặc đã bị thu hồi. Chi tiết: ${raw}`;
  }
  if (
    lowered.includes("http 403") ||
    lowered.includes("không có quyền") ||
    lowered.includes("khong co quyen") ||
    lowered.includes("permission_denied")
  ) {
    return lowered.includes("http 403")
      ? raw
      : `HTTP 403 — API key không có quyền truy cập project hoặc model đã chọn. Chi tiết: ${raw}`;
  }
  if (lowered.includes("rate limit") || lowered.includes("quota") || lowered.includes("resource_exhausted") || lowered.includes("429")) {
    return lowered.includes("http 429")
      ? raw
      : `HTTP 429 — Gemini giới hạn tốc độ hoặc quota. Chi tiết: ${raw}`;
  }
  if (lowered.includes("chua cau hinh") || lowered.includes("chưa cấu hình") || lowered.includes("no_api_key")) {
    return "Chưa cấu hình Gemini API key. Vào Settings → Gemini AI → nhập API key từ aistudio.google.com/apikey.";
  }
  return raw.length > 0
    ? raw
    : "Gemini không tạo được phụ đề. Kiểm tra API key và thử lại.";
};

const absoluteApiUrl = (path: string) =>
  path.startsWith("/api/v1/")
    ? `${API_BASE.replace(/\/api\/v1$/, "")}${path}`
    : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;

export function SubtitleStudio({ onBack, onOpenSettings }: SubtitleStudioProps) {
  const [initialDraft] = useState(readLocalDraft);
  const workspaceRef = useRef<SubtitleWorkspaceHandle>(null);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const metadataControllerRef = useRef<AbortController | null>(null);
  const generationControllerRef = useRef<AbortController | null>(null);
  const alignmentControllerRef = useRef<AbortController | null>(null);
  const renderControllerRef = useRef<AbortController | null>(null);
  const overlayUploadControllerRef = useRef<AbortController | null>(null);
  const overlayObjectUrlRef = useRef<string | null>(null);
  const [activeTab, setActiveTab] = useState<StudioTab>("transcript");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [videoFile, setVideoFile] = useState<Pick<File, "name" | "size" | "lastModified"> | null>(null);
  const [pickingVideo, setPickingVideo] = useState(false);
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
    document: subtitleDocument,
    applyDocument,
    commit: commitCues,
    reset: resetCues,
    undo,
    redo,
    canUndo,
    canRedo,
  } = useSubtitleHistory({ ...createSubtitleDocument(initialDraft?.cues ?? []), ...initialDraft?.documentMeta,
    source_revision: initialDraft?.documentMeta?.source_revision ?? initialDraft?.translatedSourceRevision ?? null });
  const [selectedCueId, setSelectedCueId] = useState<string | null>(
    initialDraft?.selectedCueId ?? null,
  );
  const [allSubtitlePositions, setAllSubtitlePositions] = useState(false);
  const [activePositionCueId, setActivePositionCueId] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<SubtitleWarning[]>([]);
  const [activeGeminiJobId, setActiveGeminiJobId] = useState<string | null>(
    initialDraft?.activeGeminiJobId ?? initialDraft?.lastGeminiJobId ?? null,
  );
  const [generationJob, setGenerationJob] = useState<GeminiSubtitleJob | null>(null);
  const generationPollErrorRef = useRef<string | null>(null);
  const [geminiStatus, setGeminiStatus] = useState<GeminiApiStatus | null>(null);
  const [geminiModels, setGeminiModels] = useState<GeminiModel[]>([]);
  const [geminiModelsError, setGeminiModelsError] = useState<string | null>(null);
  const [geminiModelsLoading, setGeminiModelsLoading] = useState(false);
  const [geminiModelsReload, setGeminiModelsReload] = useState(0);
  const [geminiModel, setGeminiModel] = useState("");
  const [geminiBilingual, setGeminiBilingual] = useState(true);
  const [geminiAdvanced, setGeminiAdvanced] = useState<GeminiSubtitleOptions>({ alignment_mode: "off", chunk_policy: { target_ms: 120000, max_chunk_ms: 600000, min_pause_ms: 800, context_ms: 2000 } });
  const [alignmentEngine, setAlignmentEngine] = useState<SubtitleAlignmentOptions["engine"]>("auto");
  const [alignmentScope, setAlignmentScope] = useState("review");
  const [alignmentStart, setAlignmentStart] = useState(0);
  const [alignmentEnd, setAlignmentEnd] = useState(120);
  const [activeAlignmentJobId, setActiveAlignmentJobId] = useState<string | null>(
    initialDraft?.activeAlignmentJobId ?? null,
  );
  const [alignmentJob, setAlignmentJob] = useState<SubtitleJob | null>(null);
  const [activeRenderJobId, setActiveRenderJobId] = useState<string | null>(
    initialDraft?.activeRenderJobId ?? null,
  );
  const [renderJob, setRenderJob] = useState<SubtitleRenderJob | null>(null);
  const initialExtraction = initialDraft?.extractionSettings ?? DEFAULT_EXTRACTION_SETTINGS;
  const [extractionMode, setExtractionMode] = useState<ExtractionMode>(initialExtraction.mode);
  const [ocrRegion, setOcrRegion] = useState<SubtitleOcrRegion>(initialExtraction.region);
  const [ocrLanguage, setOcrLanguage] = useState(initialExtraction.ocrLanguage);
  const [ocrSampleFps, setOcrSampleFps] = useState(initialExtraction.sampleFps);
  const [ocrAutoProbe, setOcrAutoProbe] = useState(initialExtraction.autoProbe ?? false);
  const [asrLanguage, setAsrLanguage] = useState(initialExtraction.asrLanguage);
  const [asrModel, setAsrModel] = useState(initialExtraction.asrModel);
  const [asrDevice, setAsrDevice] = useState<'auto' | 'cpu' | 'cuda'>(initialExtraction.asrDevice ?? 'auto');
  const [asrComputeType, setAsrComputeType] = useState(initialExtraction.asrComputeType ?? 'auto');
  const asrRuntime = useAsrRuntime(extractionMode === 'asr');
  const asrReadiness = getAsrReadiness(asrRuntime.status, asrModel, asrDevice, asrComputeType);
  const asrCanRun = asrReadiness.canRun;
  const asrComputeTypes = (asrDevice === 'auto' ? asrReadiness.device : asrRuntime.status?.devices.find(device => device.id === asrDevice))?.compute_types ?? [];
  const [translateBilingual, setTranslateBilingual] = useState(initialExtraction.bilingual);
  const [translateModel, setTranslateModel] = useState(initialExtraction.model);
  const source = useSourceDocument(initialDraft?.sourceDocument ?? null);
  const sourceDocumentRef = useRef(source.document);
  useEffect(() => { sourceDocumentRef.current = source.document; }, [source.document]);
  const translatedSourceRevision = subtitleDocument.source_revision ?? null;
  const [sourceSaving, setSourceSaving] = useState(false);
  const [sourceVersionReload, setSourceVersionReload] = useState(0);
  const [isExportingSourceSrt, setIsExportingSourceSrt] = useState<boolean>(false);
  const [options, setOptions] = useState<SubtitleBurnOptions>(
    initialDraft?.options ?? DEFAULT_OPTIONS,
  );
  const [liveAssTrack, setLiveAssTrack] = useState<{
    videoId: string;
    content: string;
  } | null>(null);
  const liveAssContent =
    videoId && cues.length > 0 && liveAssTrack?.videoId === videoId ? liveAssTrack.content : null;
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
  const [subtitleMasks, setSubtitleMasks] = useState<SubtitleMaskRegion[]>(
    initialDraft?.subtitleMasks ?? [],
  );
  const [videoClips, setVideoClips] = useState<VideoClip[]>(() =>
    initialDraft?.videoClips.length
      ? initialDraft.videoClips
      : createInitialVideoClip(initialDraft?.mediaDurationMs ?? 0),
  );
  const [selectedVideoClipId, setSelectedVideoClipId] = useState<string | null>(
    initialDraft?.videoClips[0]?.id ?? (initialDraft?.mediaDurationMs ? "video-1" : null),
  );
  const [lastDeletedVideoClip, setLastDeletedVideoClip] =
    useState<VideoClip | null>(null);
  const [selectedMaskId, setSelectedMaskId] = useState<string | null>(
    initialDraft?.subtitleMasks[0]?.id ?? null,
  );
  const [uploading, setUploading] = useState(false);
  const [overlayUploading, setOverlayUploading] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const sortedCues = useMemo(() => sortCues(cues), [cues]);
  const documentMeta = useMemo(() => documentMetadata(subtitleDocument), [subtitleDocument]);
  const voice = useVoiceover(videoId, mediaMetadata?.fingerprint, sortedCues);
  const extractionSettings = useMemo(() => ({ mode: extractionMode, region: ocrRegion, ocrLanguage,
    sampleFps: ocrSampleFps, autoProbe: ocrAutoProbe, asrLanguage, asrModel, asrDevice, asrComputeType, bilingual: translateBilingual, model: translateModel }),
    [extractionMode, ocrRegion, ocrLanguage, ocrSampleFps, ocrAutoProbe, asrLanguage, asrModel, asrDevice, asrComputeType, translateBilingual, translateModel]);
  const extraction = useExtractionJobs({ videoId, document: subtitleDocument, source: source.document,
    configuration: JSON.stringify(extractionSettings), initial: initialDraft?.pendingExtraction,
    onError: setError, onNotice: setNotice,
    onSource: (document, result) => {
      source.dispatch({ type: 'replace', document });
      if (result.region) setOcrRegion(result.region);
      setWarnings(result.warnings ?? []);
      setNotice('Đã trích xuất nguồn. Kiểm tra nội dung rồi chọn dịch.');
    },
    onTranslation: document => {
      applyDocument(document);
      setSelectedCueId(document.segments[0]?.id ?? null);
      setNotice('Đã cập nhật bản dịch từ phụ đề nguồn.');
    },
  });
  const extractionJob = extraction.job;
  const extractionRunning = extraction.running;
  const videoIdRef = useRef(videoId);
  useEffect(() => { videoIdRef.current = videoId; }, [videoId]);
  const alignmentSnapshotRef = useRef<typeof subtitleDocument | null>(null);
  const generationSnapshotRef = useRef<typeof subtitleDocument | null>(null);
  const renderOptions = useMemo(
    () => createRenderOptions(options, videoClips),
    [options, videoClips],
  );
  const frameTiming = mediaMetadata ?? DEFAULT_FRAME_TIMING;
  const thumbnailCacheKey = mediaMetadata?.fingerprint ?? (
    videoFile && videoId
      ? `${videoId}:${videoFile.size}:${videoFile.lastModified}`
      : videoId
  );
  const overlayNeedsUpload = Boolean(overlayImage && !overlayId && !overlayUploading);
  const overlayReady = !overlayImage || Boolean(overlayId && !overlayUploading);
  const hasVideoEdits = Boolean(
    mediaDurationMs > 0 && (
      videoClips.length !== 1 ||
      videoClips[0]?.start_ms !== 0 ||
      videoClips[0]?.end_ms !== mediaDurationMs
    ),
  );
  const hasRenderContent =
    sortedCues.length > 0 || Boolean(overlayId) || subtitleMasks.length > 0 || hasVideoEdits || Boolean(voice.document?.clips.length);
  const selectedMask = subtitleMasks.find((mask) => mask.id === selectedMaskId) ?? null;
  const canRender = Boolean(
    videoId &&
      renderOptions.video_segments.length > 0 &&
      overlayReady &&
      hasRenderContent &&
      sortedCues.every(
        (cue) => cue.text.trim() && cue.start_ms >= 0 && cue.end_ms > cue.start_ms,
      ),
  );


  useEffect(() => {
    if (!videoId || subtitleDocument.segments.length === 0) {
      return undefined;
    }

    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      void api
        .previewSubtitleDocument(
          videoId,
          subtitleDocument,
          renderOptions,
          controller.signal,
        )
        .then((result) => {
          if (!controller.signal.aborted) setLiveAssTrack({ videoId, content: result.ass });
        })
        .catch((previewError: unknown) => {
          if (
            !controller.signal.aborted &&
            !(previewError instanceof DOMException && previewError.name === "AbortError")
          ) {
            setLiveAssTrack(null);
          }
        });
    }, 120);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [renderOptions, subtitleDocument, videoId]);
  const alignmentRunning =
    alignmentJob?.state === "queued" || alignmentJob?.state === "running";
  const canAlign = Boolean(
    videoId &&
      mediaMetadata?.has_audio &&
      sortedCues.some((cue) => cue.timing_source !== "manual") &&
      !activeAlignmentJobId &&
      !alignmentRunning,
  );
  const generationRunning = Boolean(activeGeminiJobId) ||
    (generationJob?.phase === "interrupted" && !generationJob.cancel_requested) ||
    generationJob?.state === "queued" ||
    generationJob?.state === "running";
  const canGenerate = Boolean(
    videoId &&
      mediaMetadata &&
      geminiStatus?.authenticated &&
      (geminiModel || geminiStatus?.model) &&
      !generationRunning,
  );
  const rendering = Boolean(activeRenderJobId) ||
    renderJob?.state === "queued" ||
    renderJob?.state === "running";

  useEffect(
    () => () => {
      uploadControllerRef.current?.abort();
      metadataControllerRef.current?.abort();
      generationControllerRef.current?.abort();
      alignmentControllerRef.current?.abort();
      renderControllerRef.current?.abort();
      overlayUploadControllerRef.current?.abort();
      if (overlayObjectUrlRef.current) URL.revokeObjectURL(overlayObjectUrlRef.current);
    },
    [],
  );

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const controller = new AbortController();
    const pollStatus = async () => {
      try {
        const status = await api.geminiApiStatus(controller.signal);
        if (stopped) return;
        setGeminiStatus(status);
        if (!status.authenticated) {
          timer = window.setTimeout(() => void pollStatus(), 5000);
        }
      } catch {
        if (!stopped && !controller.signal.aborted) {
          setGeminiStatus({ installed: false, authenticated: false });
          timer = window.setTimeout(() => void pollStatus(), 5000);
        }
      }
    };
    void pollStatus();
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (!geminiStatus?.authenticated) return undefined;
    let stopped = false;
    const controller = new AbortController();
    const loadModels = async () => {
      setGeminiModelsLoading(true);
      try {
        const response = await api.geminiModels(controller.signal);
        if (stopped) return;
        setGeminiModels(response.models);
        setGeminiModelsError(null);
        setGeminiModel((current) => {
          if (current && response.models.some((model) => model.id === current)) {
            return current;
          }
          return response.models.find((model) => model.id === response.selected_model)?.id
            ?? response.models[0]?.id
            ?? response.selected_model;
        });
      } catch (modelsError: unknown) {
        if (!stopped && !controller.signal.aborted) {
          setGeminiModelsError(
            getErrorMessage(modelsError, "Không tải được danh sách model Gemini."),
          );
          setGeminiModel((current) => current || geminiStatus.model || "");
        }
      } finally {
        if (!stopped) setGeminiModelsLoading(false);
      }
    };
    void loadModels();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [geminiModelsReload, geminiStatus?.authenticated, geminiStatus?.model]);

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
        setVideoClips((current) => {
          if (current.length) return current;
          return createInitialVideoClip(media.duration_ms);
        });
        setSelectedVideoClipId((current) => current ?? "video-1");
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

  useJobPolling({
    jobId: activeAlignmentJobId,
    fetchJob: api.subtitleJob,
    interval: 500, retryDelay: 1200,
    onJob: (job) => {
        setAlignmentJob(job);
        if (job.state === "succeeded") {
          const result = job.result;
          if (result) {
            const nextCues = sortCues(result.document.segments);
            if (subtitleDocument === alignmentSnapshotRef.current) {
              applyDocument(result.document);
            } else {
              setNotice("Đã lưu kết quả căn trong Phiên bản phụ đề. Bản đang chỉnh được giữ vì đã thay đổi hoặc phiên làm việc được khôi phục.");
            }
            setWarnings(result.warnings ?? []);
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
    },
    onError: (pollError) => {
        setError(
          getErrorMessage(
            pollError,
            "Mất kết nối khi đọc tiến độ alignment; đang thử lại.",
          ),
        );
    },
  });

  useJobPolling({
    jobId: activeGeminiJobId ?? (generationJob?.phase === "interrupted" && !generationJob.cancel_requested ? generationJob.id : null),
    fetchJob: api.geminiSubtitleJob,
    interval: 1000, retryDelay: 2000,
    isTerminal: job => ["succeeded", "failed", "canceled"].includes(job.state) &&
      !(job.phase === "interrupted" && !job.cancel_requested),
    onJob: (job) => {
        setGenerationJob(job);
        if (job.phase === "interrupted" && !job.cancel_requested) {
          setError(current => current === job.error ? null : current);
          return;
        }
        const interruptedError = generationJob?.phase === "interrupted" ? generationJob.error : null;
        if (interruptedError) setError(current => current === interruptedError ? null : current);
        const connectionError = generationPollErrorRef.current;
        if (connectionError) {
          setError(current => current === connectionError ? null : current);
          generationPollErrorRef.current = null;
        }
        if (job.state === "succeeded") {
          if (job.result) {
            const nextCues = sortCues(job.result.document.segments);
            if (cues.length === 0 && subtitleDocument === generationSnapshotRef.current) {
              applyDocument(job.result.document);
              setRawText(job.result.srt);
              setWarnings(job.result.warnings ?? []);
              setSelectedCueId(nextCues[0]?.id ?? null);
            }
          }
          setActiveGeminiJobId(null);
          return;
        }
        if (job.state === "failed" || job.state === "canceled") {
          if (job.state === "failed") {
            setError(formatGeminiError(job.error));
            void api.geminiApiStatus().then(setGeminiStatus).catch(() => undefined);
          }
          setActiveGeminiJobId(null);
          return;
        }
    },
    onError: (pollError) => {
        const message = getErrorMessage(
          pollError,
          "Mất kết nối khi đọc tiến độ Gemini API; đang thử lại.",
        );
        generationPollErrorRef.current = message;
        setError(message);
    },
  });

  useJobPolling({
    jobId: activeRenderJobId,
    fetchJob: api.subtitleRenderJob,
    interval: 500, retryDelay: 1200,
    onJob: (job) => {
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
    },
    onError: (pollError) => {
        setError(
          getErrorMessage(
            pollError,
            "Mất kết nối khi đọc tiến độ render; đang thử lại.",
          ),
        );
    },
  });

  const savedDraft = useMemo<SavedSubtitleDraft>(() => ({
        version: 2,
        videoId,
        projectName,
        mediaDurationMs,
        rawText,
        cues,
        documentMeta,
        sourceDocument: source.document, translatedSourceRevision, extractionSettings, pendingExtraction: extraction.pending,
        selectedCueId,
        activeGeminiJobId,
        lastGeminiJobId: generationJob?.id ?? (initialDraft?.videoId === videoId ? initialDraft?.lastGeminiJobId : null) ?? null,
        activeAlignmentJobId,
        activeRenderJobId,
        options,
        overlayId,
        overlayName,
        overlayLayout,
        subtitleMasks,
        videoClips,
      }), [
    activeGeminiJobId,
    generationJob?.id,
    initialDraft?.lastGeminiJobId,
    initialDraft?.videoId,
    activeAlignmentJobId,
    activeRenderJobId,
    cues,
    documentMeta, source.document, translatedSourceRevision, extractionSettings, extraction.pending,
    mediaDurationMs,
    options,
    overlayId,
    overlayLayout,
    overlayName,
    projectName,
    rawText,
    selectedCueId,
    subtitleMasks,
    videoClips,
    videoId,
  ]);
  useDraftPersistence(savedDraft);

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

  const handleFileChange = async (file: File | DesktopSelectedVideo) => {
    if (voice.document) {
      try { await voice.save(); }
      catch (saveError) {
        setError(getErrorMessage(saveError, "Chưa lưu được lời đọc. Hãy lưu lại trước khi đổi video."));
        return;
      }
    }
    extraction.reset();
    source.dispatch({ type: 'reset', document: null });
    setSourceSaving(false);
    uploadControllerRef.current?.abort();
    const controller = new AbortController();
    uploadControllerRef.current = controller;
    setVideoFile(file);
    setVideoId(null);
    setLiveAssTrack(null);
    setProjectName(file.name);
    setUploading(true);
    setError(null);
    setRenderedVideoUrl(null);
    setMediaMetadata(null);
    setMediaDurationMs(0);
    alignmentControllerRef.current?.abort();
    generationControllerRef.current?.abort();
    renderControllerRef.current?.abort();
    setActiveAlignmentJobId(null);
    setAlignmentJob(null);
    setActiveGeminiJobId(null);
    setGenerationJob(null);
    setActiveRenderJobId(null);
    setRenderJob(null);
    resetCues();
    setVideoClips([]);
    setSelectedVideoClipId(null);
    setLastDeletedVideoClip(null);
    setSelectedCueId(null);
    setSubtitleMasks([]);
    setSelectedMaskId(null);
    setWarnings([]);
    try {
      const result = "upload" in file ? file.upload : await api.uploadSubtitleVideo(file, controller.signal);
      if (controller.signal.aborted) return;
      setVideoId(result.video_id);
      setMediaMetadata(result.media);
      setMediaDurationMs(result.media.duration_ms);
      const initialClips = createInitialVideoClip(result.media.duration_ms);
      setVideoClips(initialClips);
      setSelectedVideoClipId(initialClips[0]?.id ?? null);
      setOriginalVideoUrl(`${API_BASE}/subtitles/video/${result.video_id}`);
      setActiveTab("transcript");
    } catch (uploadError: unknown) {
      if (controller.signal.aborted) return;
      setError(getErrorMessage(uploadError, "Không tải được video. Kiểm tra định dạng và thử lại."));
    } finally {
      if (!controller.signal.aborted) setUploading(false);
    }
  };

  const handleChooseProjectVideo = async () => {
    const choose = window.contentBotDesktop?.pickProjectVideo;
    if (!choose || pickingVideo || uploading) return;
    setPickingVideo(true);
    setError(null);
    try {
      const selected = await choose({ apiBase: API_BASE, accessToken: localStorage.getItem("content_bot_access_token") ?? undefined });
      if (selected) await handleFileChange(selected);
    } catch (error) {
      setError(getErrorMessage(error, "Không mở được thư mục video dự án."));
    } finally { setPickingVideo(false); }
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
      const prompt = GEMINI_PROMPT_TEMPLATE.replaceAll(
        "{{VIDEO_DURATION_MS}}",
        mediaDurationMs > 0 ? String(Math.round(mediaDurationMs)) : "[ĐIỀN THỜI LƯỢNG VIDEO BẰNG MILLISECOND]",
      );
      await navigator.clipboard.writeText(prompt);
      setCopiedPrompt(true);
      window.setTimeout(() => setCopiedPrompt(false), 2500);
    } catch {
      setError("Không sao chép được prompt. Hãy cấp quyền clipboard rồi thử lại.");
    }
  };

  const { parsing, parse: handleParse } = useSubtitleImport({
    text: rawText, durationMs: mediaDurationMs, scope: videoId, snapshot: subtitleDocument,
    onError: setError, onNotice: setNotice,
    onParsed: (result) => {
      const nextCues = sortCues(result.document.segments);
      applyDocument(result.document);
      setWarnings(result.warnings ?? []);
      setSelectedCueId(nextCues[0]?.id ?? null);
      if (nextCues.length === 0) {
        setError("Không tìm thấy cue hợp lệ. Kiểm tra JSON hoặc mốc thời gian đầu vào.");
      }
    },
  });

  const handleGenerateWithGemini = async () => {
    if (!videoId || !mediaMetadata) {
      setError("Hãy tải video trước khi chạy Gemini.");
      return;
    }
    generationControllerRef.current?.abort();
    const controller = new AbortController();
    generationControllerRef.current = controller;
    generationSnapshotRef.current = subtitleDocument;
    setError(null);
    try {
      const job = await api.generateSubtitlesWithGemini(
        videoId,
        {
          ...geminiAdvanced,
          bilingual: geminiBilingual,
          model: geminiModel || geminiStatus?.model || undefined,
        },
        controller.signal,
        subtitleDocument,
        cues.length > 0,
      );
      if (controller.signal.aborted) return;
      setGenerationJob(job);
      setActiveGeminiJobId(job.id);
    } catch (generationError: unknown) {
      if (!controller.signal.aborted) {
        setError(formatGeminiError(getErrorMessage(generationError, "Không khởi chạy được Gemini API.")));
        void api.geminiApiStatus().then(setGeminiStatus).catch(() => undefined);
      }
    }
  };

  const handleCancelGeneration = async () => {
    if (!generationJob || !generationRunning) return;
    try {
      const job = await api.cancelGeminiSubtitleJob(generationJob.id);
      setGenerationJob(job);
    } catch (cancelError: unknown) {
      setError(getErrorMessage(cancelError, "Không gửi được yêu cầu hủy job Gemini."));
    }
  };

  const preserveBeforeExtraction = async (signal: AbortSignal) => {
    if (!videoId) return;
    if (source.document) await api.saveSubtitleVersion(videoId, source.document, 'Nguồn trước khi chạy tác vụ', signal, true);
    if (signal.aborted) return;
    if (subtitleDocument.segments.length) await api.saveSubtitleVersion(videoId, subtitleDocument, 'Phụ đề trước khi chạy tác vụ', signal, true);
  };
  const handleExtractOcr = async () => {
    if (!videoId || !mediaMetadata) return;
    await extraction.start('ocr', async signal => {
      await preserveBeforeExtraction(signal);
      signal.throwIfAborted();
      return api.extractSubtitlesOcr(videoId, ocrRegion,
        { source_language: ocrLanguage, sample_fps: ocrSampleFps, auto_probe: ocrAutoProbe }, signal);
    });
  };
  const handleExtractAsr = async () => {
    if (!videoId || !mediaMetadata) return;
    if (!mediaMetadata.has_audio) { setError('Video không có âm thanh. Hãy dùng OCR hoặc Gemini.'); return; }
    await extraction.start('asr', async signal => {
      await preserveBeforeExtraction(signal);
      signal.throwIfAborted();
      return api.extractSubtitlesAsr(videoId, { source_language: asrLanguage, model: asrModel, device: asrDevice, compute_type: asrComputeType }, signal);
    });
  };
  const handleTranslateGemini = async () => {
    if (!videoId || !source.document?.segments.length) return;
    const sourceDocument = source.document;
    await extraction.start('translation', async signal => {
      await preserveBeforeExtraction(signal);
      signal.throwIfAborted();
      return api.translateSubtitlesWithGemini(videoId, sourceDocument,
        { target_language: 'vi', bilingual: translateBilingual,
          model: translateModel || geminiModel || geminiStatus?.model || undefined }, null, signal);
    });
  };
  const handleCancelExtraction = extraction.cancel;
  const handleSaveSource = async () => {
    if (!videoId || !source.document || sourceSaving) return;
    const savedVideo = videoId;
    const before = source.document;
    setSourceSaving(true);
    try {
      const saved = await api.saveSubtitleVersion(videoId, before, `Nguồn đã chỉnh · bản ${before.revision ?? 0}`);
      if (videoIdRef.current === savedVideo) {
        if (sourceDocumentRef.current === before && saved.document?.media_fingerprint
            && saved.document.media_fingerprint !== before.media_fingerprint) {
          source.dispatch({ type: 'replace', document: { ...before, media_fingerprint: saved.document.media_fingerprint } });
        }
        setSourceVersionReload(n => n + 1); setNotice('Đã lưu bản nguồn.');
      }
    } catch (error) { if (videoIdRef.current === savedVideo) setError(String(error)); }
    finally { if (videoIdRef.current === savedVideo) setSourceSaving(false); }
  };

  const handleExportSourceSrt = async () => {
    if (!source.document?.segments.length) {
      setError("Chưa có phụ đề để xuất file SRT nguồn.");
      return;
    }
    setIsExportingSourceSrt(true);
    setError(null);
    try {
      const res = await api.exportSourceSrt(source.document, videoId || undefined);
      const blob = new Blob([res.srt], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const baseName = videoFile?.name ? videoFile.name.replace(/\.[^/.]+$/, "") : (projectName || "subtitles");
      a.href = url;
      a.download = `${baseName}_source.srt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (exportErr: unknown) {
      setError(getErrorMessage(exportErr, "Không thể xuất file SRT nguồn."));
    } finally {
      setIsExportingSourceSrt(false);
    }
  };

  const handleResumeGeneration = async () => {
    if (!generationJob || generationRunning) return;
    generationControllerRef.current?.abort();
    const controller = new AbortController();
    generationControllerRef.current = controller;
    generationSnapshotRef.current = null;
    try {
      const job = await api.resumeGeminiSubtitleJob(generationJob.id, controller.signal);
      if (!controller.signal.aborted) {
        setGenerationJob(job);
        setActiveGeminiJobId(job.id);
      }
    } catch (resumeError: unknown) {
      if (!controller.signal.aborted) setError(getErrorMessage(resumeError, "Không thể tiếp tục tác vụ Gemini."));
    }
  };

  const hasSourceCues = Boolean(source.document?.segments.length);
  const translationReady = Boolean(
    hasSourceCues &&
      translatedSourceRevision !== null &&
      translatedSourceRevision === (source.document?.revision ?? 0) &&
      (subtitleDocument.source_run_id == null || subtitleDocument.source_run_id === source.document?.run_id),
  );
  const pipelineRunning = extractionRunning || generationRunning;
  const pipelineActionKind: "gemini" | "translation" | "extract" = extractionMode === "gemini"
    ? "gemini"
    : hasSourceCues
      ? "translation"
      : "extract";
  const generationResumeAvailable = pipelineActionKind === "gemini" &&
    !generationRunning &&
    Boolean(generationJob && (generationJob.state === "failed" || generationJob.state === "canceled") && generationJob.details?.resume_available);
  const pipelineFailed = !pipelineRunning && Boolean(
    (pipelineActionKind === "gemini" && generationJob?.state === "failed") ||
      (pipelineActionKind !== "gemini" && extractionJob?.state === "failed" &&
        (pipelineActionKind === "translation"
          ? extractionJob.kind === "translation"
          : extractionJob.kind === (extractionMode === "asr" ? "asr" : "ocr"))),
  );
  const pipelineStage: SubtitlePipelineStage = pipelineFailed
    ? "error"
    : pipelineRunning
      ? generationRunning ? "extracting" : extractionJob?.kind === "translation" ? "translating" : "extracting"
      : translationReady
        ? "complete"
        : hasSourceCues
          ? "ready-to-translate"
          : "idle";
  const pipelineProgress = pipelineRunning
    ? generationRunning
      ? Math.round((generationJob?.progress ?? 0) * 0.5)
      : extractionJob?.kind === "translation"
        ? 50 + Math.round((extractionJob.progress ?? 0) * 0.5)
        : Math.round((extractionJob?.progress ?? 0) * 0.5)
    : translationReady
      ? 100
      : hasSourceCues
        ? 50
        : 0;
  const pipelineMessage = pipelineFailed
    ? generationJob?.error || extractionJob?.error || "Tác vụ chưa hoàn tất. Hãy thử lại."
    : pipelineRunning
      ? extraction.submitting
        ? "Đang chuẩn bị tác vụ…"
        : generationRunning
          ? generationJob?.message || "Gemini đang tìm phụ đề…"
          : extractionJob?.message || "Đang tìm phụ đề…"
      : translationReady
        ? `Đã tìm và dịch ${cues.length} đoạn.`
        : hasSourceCues
          ? `Đã tìm thấy ${source.document?.segments.length ?? 0} đoạn · sẵn sàng dịch.`
          : !videoId
            ? "Tải video trước để bắt đầu."
            : !mediaMetadata
              ? "Đang đọc thông tin video…"
              : "Chọn một cách tìm phụ đề rồi bắt đầu từ thanh này.";
  const pipelineActionLabel = pipelineRunning
    ? "Dừng"
    : pipelineActionKind === "gemini"
      ? generationResumeAvailable
        ? "Tiếp tục Gemini"
        : cues.length ? "Tạo lại với Gemini" : "Tìm bằng Gemini"
      : pipelineActionKind === "translation"
        ? extractionJob?.kind === "translation" && extractionJob.details?.resume_available
          ? "Tiếp tục dịch"
          : translationReady ? "Dịch lại" : "Dịch tiếng Việt"
        : extractionMode === "asr"
          ? cues.length ? "Nghe lại & tìm" : "Nghe & tìm phụ đề"
          : cues.length ? "Quét lại & tìm" : "Quét & tìm phụ đề";
  const pipelineActionDisabled = pipelineRunning
    ? false
    : !videoId ||
      (pipelineActionKind === "gemini" && !canGenerate) ||
      (pipelineActionKind === "translation" && !hasSourceCues) ||
      (pipelineActionKind === "extract" && (!mediaMetadata || (extractionMode === "asr" && (!asrCanRun || !mediaMetadata.has_audio))));
  const handleSubtitlePipelineAction = () => {
    if (pipelineRunning) {
      if (generationRunning) void handleCancelGeneration();
      else void handleCancelExtraction();
      return;
    }
    if (pipelineActionKind === "gemini") {
      if (generationResumeAvailable) void handleResumeGeneration();
      else void handleGenerateWithGemini();
    }
    else if (pipelineActionKind === "translation") void handleTranslateGemini();
    else if (extractionMode === "asr") void handleExtractAsr();
    else void handleExtractOcr();
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
        const document = subtitleDocument;
        alignmentSnapshotRef.current = document;
        const selectedIds = cueIds ?? (alignmentScope === "all" ? undefined : sortedCues.filter(cue =>
          alignmentScope === "selected" ? cue.id === selectedCueId : alignmentScope === "range"
            ? cue.start_ms >= alignmentStart * 1000 && cue.end_ms <= alignmentEnd * 1000
            : cue.needs_review).map(cue => cue.id));
        if (selectedIds?.length === 0) { setError("Không có cue trong phạm vi căn đã chọn."); return; }
        const job = await api.alignSubtitleDocument(
          videoId,
          document,
          { engine: alignmentEngine },
          selectedIds,
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
    [mediaMetadata, sortedCues, videoId, subtitleDocument, alignmentScope, alignmentStart, alignmentEnd, selectedCueId, alignmentEngine],
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

  const positionCue = cues.find((cue) => cue.id === selectedCueId)
    ?? cues.find((cue) => cue.id === activePositionCueId);
  const editPosition = positionCue?.layout ?? { x: options.pos_x, y: options.pos_y };

  const handleCuePositionChange = (cueId: string, position: { x: number; y: number }) => {
    commitCues((current) => applyCuePosition(current, cueId, position, allSubtitlePositions));
  };
  const toggleAllSubtitlePositions = () => {
    if (!allSubtitlePositions) {
      const currentMs = workspaceRef.current?.getCurrentMs() ?? 0;
      const visible = cues.find((cue) => cue.start_ms <= currentMs && currentMs < cue.end_ms) ?? positionCue;
      if (!visible) return;
      const position = visible.layout ?? { x: options.pos_x, y: options.pos_y };
      commitCues((current) => applyCuePosition(current, visible.id, position, true));
      setSelectedCueId(visible.id);
    }
    setAllSubtitlePositions((current) => !current);
  };

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
    if (!videoId) {
      setError("Hãy tải video trước khi xuất video.");
      return;
    }
    if (!canRender) {
      setError(
        overlayNeedsUpload
          ? "Hãy chờ ảnh phủ tải xong hoặc chọn lại ảnh phủ trước khi xuất."
          : "Cần ít nhất một cue phụ đề, ảnh phủ hoặc thay đổi cắt video để xuất.",
      );
      return;
    }
    renderControllerRef.current?.abort();
    const controller = new AbortController();
    renderControllerRef.current = controller;
    setError(null);
    try {
      const job = await api.renderSubtitleDocument(
        videoId,
        subtitleDocument,
        renderOptions,
        overlayId ? { overlay_id: overlayId, ...overlayLayout } : null,
        subtitleMasks,
        controller.signal,
        voice.document?.mix.enabled ? await voice.save() : null,
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

  const { downloading: downloadingVideo, message: downloadMessage, download: handleDownloadVideo } =
    useRenderedVideoDownload(renderedVideoUrl, renderJob?.result?.output_filename, setError);

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

  const addSubtitleMask = () => {
    if (subtitleMasks.length >= MAX_SUBTITLE_MASKS) return;
    const mask = createSubtitleMask();
    setSubtitleMasks((current) => [...current, mask]);
    setSelectedMaskId(mask.id);
    setSelectedCueId(null);
    setActiveTab("mask");
    setSidebarOpen(true);
  };

  const updateSubtitleMask = (
    maskId: string,
    patch: Partial<SubtitleMaskRegion>,
  ) => {
    setSubtitleMasks((current) => current.map((mask) =>
      mask.id === maskId
        ? normalizeSubtitleMask({ ...mask, ...patch, id: mask.id })
        : mask,
    ));
  };

  const deleteSubtitleMask = (maskId: string) => {
    const next = subtitleMasks.filter((mask) => mask.id !== maskId);
    setSubtitleMasks(next);
    if (selectedMaskId === maskId) setSelectedMaskId(next[0]?.id ?? null);
  };

  const splitVideoAtPlayhead = useCallback(() => {
    const playheadMs = workspaceRef.current?.getCurrentMs() ?? 0;
    const clip = findVideoClipAt(videoClips, playheadMs);
    if (!clip) {
      setError("Đặt con trỏ bên trong một đoạn video đang giữ rồi thử tách lại.");
      return;
    }
    const next = splitVideoClip(videoClips, clip.id, playheadMs);
    if (next.length === videoClips.length) {
      setError("Con trỏ đang quá sát mép clip; hãy di chuyển vào bên trong đoạn rồi tách.");
      return;
    }
    setVideoClips(next);
    const rightClip = next.find((item) => item.start_ms === Math.round(playheadMs));
    setSelectedVideoClipId(rightClip?.id ?? clip.id);
    setLastDeletedVideoClip(null);
    setRenderedVideoUrl(null);
    setError(null);
  }, [setSelectedVideoClipId, videoClips]);

  const deleteSelectedVideoClip = useCallback(() => {
    const selectedIndex = videoClips.findIndex((clip) => clip.id === selectedVideoClipId);
    if (selectedIndex < 0) {
      setError("Chọn một đoạn video trên timeline trước khi xóa.");
      return;
    }
    if (videoClips.length === 1) {
      setError("Không thể xóa đoạn video cuối cùng của dự án.");
      return;
    }
    const deleted = videoClips[selectedIndex];
    const next = videoClips.filter((clip) => clip.id !== deleted.id);
    const nextSelection = next[Math.min(selectedIndex, next.length - 1)] ?? null;
    setVideoClips(next);
    setSelectedVideoClipId(nextSelection?.id ?? null);
    setLastDeletedVideoClip(deleted);
    setRenderedVideoUrl(null);
    workspaceRef.current?.seekTo(nextSelection?.start_ms ?? 0);
    setError(null);
  }, [selectedVideoClipId, setSelectedVideoClipId, videoClips]);

  const restoreDeletedVideoClip = useCallback(() => {
    if (!lastDeletedVideoClip) return;
    setVideoClips((current) => [...current, lastDeletedVideoClip].sort(
      (left, right) => left.start_ms - right.start_ms,
    ));
    setSelectedVideoClipId(lastDeletedVideoClip.id);
    setLastDeletedVideoClip(null);
    setRenderedVideoUrl(null);
  }, [lastDeletedVideoClip, setSelectedVideoClipId]);

  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try {
      return (localStorage.getItem('subtitle_studio_theme') as 'dark' | 'light') || 'light';
    } catch {
      return 'light';
    }
  });

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next = current === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem('subtitle_studio_theme', next);
      } catch {
        // The selected theme still applies when browser storage is unavailable.
      }
      return next;
    });
  }, []);

  const tabs: { id: StudioTab; label: string; icon: typeof Upload }[] = [
    { id: "upload", label: "Tải lên", icon: Upload },
    { id: "transcript", label: "Phụ đề", icon: FileText },
    { id: "voice", label: "Giọng đọc", icon: AudioLines },
    { id: "style", label: "Kiểu chữ", icon: Type },
    { id: "mask", label: "Che chữ cũ", icon: EyeOff },
    { id: "video", label: "Video", icon: Video },
    { id: "position", label: "Bố cục", icon: Move },
  ];

  return (
    <div className={`subtitle-studio-shell theme-${theme}`}>
      <StudioToolbar
        onBack={onBack}
        sidebarOpen={sidebarOpen}
        setSidebarOpen={setSidebarOpen}
        projectName={projectName}
        canUndo={canUndo}
        canRedo={canRedo}
        undo={undo}
        redo={redo}
        rendering={rendering}
        renderJob={renderJob}
        downloadMessage={downloadMessage}
        renderedVideoUrl={renderedVideoUrl}
        downloadingVideo={downloadingVideo}
        handleDownloadVideo={handleDownloadVideo}
        videoId={videoId}
        toggleTheme={toggleTheme}
        theme={theme}
        canRender={canRender}
        alignmentRunning={alignmentRunning}
        handleCancelRender={handleCancelRender}
        handleRender={handleRender}
        overlayUploading={overlayUploading}
        overlayNeedsUpload={overlayNeedsUpload}
      />

      {(error || notice) && (
        <div className={`subtitle-studio-error${error ? "" : " is-notice"}`} role={error ? "alert" : "status"}>
          <span>{error || notice}</span>
          <button type="button" onClick={() => { setError(null); setNotice(null); }} aria-label="Đóng thông báo"><X size={16} /></button>
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
          {activeTab === "voice" && <VoicePanel key={videoId ?? 'no-video'} voice={voice} exportTimeline={{ duration_ms: mediaDurationMs,
            trim_start_ms: renderOptions.trim_start_ms, trim_end_ms: renderOptions.trim_end_ms,
            video_speed: renderOptions.video_speed, video_segments: renderOptions.video_segments }} />}
          {activeTab === "upload" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading">
                <h2>Tệp dự án</h2>
                <p>Video gốc và ảnh phủ được tải một lần, sau đó tái sử dụng trong preview.</p>
              </div>
              <label className={`studio-file-drop ${uploading || pickingVideo ? "is-loading" : ""}`}>
                <input
                  type="file"
                  accept=".mp4,.webm,.mkv"
                  disabled={uploading || pickingVideo}
                  onClick={(event) => {
                    if (window.contentBotDesktop?.pickProjectVideo) {
                      event.preventDefault();
                      void handleChooseProjectVideo();
                    }
                  }}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void handleFileChange(file);
                  }}
                />
                {uploading || pickingVideo ? <LoaderCircle className="spin" size={22} /> : <Upload size={22} />}
                <strong>{pickingVideo ? "Đang chọn video" : uploading ? "Đang tải video" : videoFile?.name ?? "Chọn video"}</strong>
                <span>MP4, WebM hoặc MKV</span>
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
                <h2>Phụ đề</h2>
                <p>Tìm phụ đề nguồn, kiểm tra nhanh rồi dịch sang tiếng Việt trong cùng một luồng.</p>
              </div>

              <SubtitlePipelineBar
                stage={pipelineStage}
                progress={pipelineProgress}
                message={pipelineMessage}
                actionLabel={pipelineActionLabel}
                actionDisabled={pipelineActionDisabled}
                running={pipelineRunning}
                onAction={handleSubtitlePipelineAction}
              />

              <details className="subtitle-pipeline-advanced">
                <summary>Tùy chọn tìm phụ đề</summary>
                <p className="subtitle-pipeline-advanced-note">Thanh trên là luồng nhanh. Mở phần này khi cần đổi phương thức hoặc chạy lại riêng OCR, ASR hay Gemini.</p>
              <div className="subtitle-mode-tabs" role="tablist" aria-label="Phương thức trích xuất phụ đề">
                <button
                  type="button"
                  role="tab"
                  aria-selected={extractionMode === "ocr"}
                  className={`subtitle-mode-tab ${extractionMode === "ocr" ? "is-active" : ""}`}
                  onClick={() => setExtractionMode("ocr")}
                >
                  <ScanText size={15} />
                  <span>OCR (Trên hình)</span>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={extractionMode === "asr"}
                  className={`subtitle-mode-tab ${extractionMode === "asr" ? "is-active" : ""}`}
                  onClick={() => setExtractionMode("asr")}
                >
                  <Mic size={15} />
                  <span>ASR (Lời thoại)</span>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={extractionMode === "gemini"}
                  className={`subtitle-mode-tab ${extractionMode === "gemini" ? "is-active" : ""}`}
                  onClick={() => setExtractionMode("gemini")}
                >
                  <Sparkles size={15} />
                  <span>Gemini Video</span>
                </button>
              </div>

              {extractionMode === "ocr" && (
                <section className="subtitle-generation-panel" aria-labelledby="ocr-extraction-title">
                  <div className="subtitle-generation-heading">
                    <div>
                      <strong id="ocr-extraction-title"><ScanText size={17} /> RapidOCR (Phụ đề trên hình)</strong>
                      <span>Nhận diện phụ đề cứng từ khung hình video. Kéo khung xanh lá trên video để chọn vùng.</span>
                    </div>
                    {extractionRunning && extractionJob?.kind === "ocr" ? (
                      <button
                        type="button"
                        className="studio-icon-button is-danger"
                        aria-label="Hủy trích xuất OCR"
                        title="Hủy tác vụ OCR đang chạy"
                        onClick={() => void handleCancelExtraction()}
                      >
                        <Square size={14} fill="currentColor" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="studio-primary-button subtitle-generation-start"
                        disabled={!videoId || extractionRunning}
                        onClick={() => void handleExtractOcr()}
                      >
                        <ScanText size={17} />
                        {cues.length ? "Trích xuất lại OCR" : "Trích xuất OCR"}
                      </button>
                    )}
                  </div>

                  <div className="subtitle-generation-options is-ocr">
                    <div>
                      <label htmlFor="ocr-lang-select">Ngôn ngữ nguồn</label>
                      <select
                        id="ocr-lang-select"
                        value={ocrLanguage}
                        disabled={extractionRunning}
                        onChange={(e) => setOcrLanguage(e.target.value)}
                      >
                        <option value="zh">Tiếng Trung (zh)</option>
                        <option value="en">Tiếng Anh (en)</option>
                        <option value="ja">Tiếng Nhật (ja)</option>
                        <option value="ko">Tiếng Hàn (ko)</option>
                        <option value="vi">Tiếng Việt (vi)</option>
                      </select>
                    </div>
                    <div>
                      <label htmlFor="ocr-fps-select">Tần suất quét (FPS)</label>
                      <select
                        id="ocr-fps-select"
                        value={ocrSampleFps}
                        disabled={extractionRunning}
                        onChange={(e) => setOcrSampleFps(Number(e.target.value))}
                      >
                        <option value="2">2 fps (Nhanh)</option>
                        <option value="3">3 fps (Mặc định)</option>
                        <option value="4">4 fps (Chính xác cao)</option>
                      </select>
                    </div>
                  </div>

                  <div className="subtitle-ocr-presets">
                    <label className="studio-toggle-row"><input type="checkbox" checked={ocrAutoProbe}
                      disabled={extractionRunning} onChange={event => setOcrAutoProbe(event.target.checked)} />
                      Tự dò vùng trước khi trích xuất
                    </label>
                    <small>Vùng OCR ({ocrRegion.x.toFixed(0)}%, {ocrRegion.y.toFixed(0)}%, {ocrRegion.width.toFixed(0)}% × {ocrRegion.height.toFixed(0)}%):</small>
                    <div className="subtitle-ocr-preset-buttons">
                      <button
                        type="button"
                        className="studio-secondary-button is-compact"
                        disabled={extractionRunning}
                        title="Quét phụ đề ở đáy màn hình"
                        onClick={() => setOcrRegion({ x: 10, y: 72, width: 80, height: 18 })}
                      >
                        Đáy
                      </button>
                      <button
                        type="button"
                        className="studio-secondary-button is-compact"
                        disabled={extractionRunning}
                        title="Quét phụ đề ở đỉnh màn hình"
                        onClick={() => setOcrRegion({ x: 10, y: 8, width: 80, height: 18 })}
                      >
                        Đỉnh
                      </button>
                      <button
                        type="button"
                        className="studio-secondary-button is-compact"
                        disabled={extractionRunning}
                        title="Quét chữ trên toàn màn hình"
                        onClick={() => setOcrRegion({ x: 5, y: 5, width: 90, height: 90 })}
                      >
                        Toàn màn hình
                      </button>
                    </div>
                  </div>

                  {!videoId && <small>Hãy tải video trước khi chạy OCR.</small>}

                  {extractionJob && extractionJob.kind === "ocr" && (
                    <div className={`subtitle-job-progress state-${extractionJob.state}`} aria-live="polite">
                      <div>
                        <span>{extractionJob.message}</span>
                        <strong>{extractionJob.progress}%</strong>
                      </div>
                      <progress max={100} value={extractionJob.progress} />
                      {extractionJob.state === "succeeded" && extractionJob.result && (
                        <small>
                          Đã trích xuất {extractionJob.result.segment_count} phụ đề nguồn.
                        </small>
                      )}
                    </div>
                  )}
                </section>
              )}

              {extractionMode === "asr" && (
                <section className="subtitle-generation-panel" aria-labelledby="asr-extraction-title">
                  <div className="subtitle-generation-heading">
                    <div>
                      <strong id="asr-extraction-title"><Mic size={17} /> Faster-Whisper (Nhận diện lời thoại)</strong>
                      <span>Trích xuất phụ đề từ âm thanh bằng AI Whisper cục bộ trên máy.</span>
                    </div>
                    {extractionRunning && extractionJob?.kind === "asr" ? (
                      <button
                        type="button"
                        className="studio-icon-button is-danger"
                        aria-label="Hủy trích xuất ASR"
                        title="Hủy tác vụ ASR đang chạy"
                        onClick={() => void handleCancelExtraction()}
                      >
                        <Square size={14} fill="currentColor" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="studio-primary-button subtitle-generation-start"
                        disabled={!videoId || !asrCanRun || Boolean(mediaMetadata && !mediaMetadata.has_audio) || extractionRunning}
                        onClick={() => void handleExtractAsr()}
                      >
                        <Mic size={17} />
                        {cues.length ? "Nhận diện lại ASR" : "Nghe lời thoại (ASR)"}
                      </button>
                    )}
                  </div>

                  <div className="subtitle-generation-options is-asr">
                    <div>
                      <label htmlFor="asr-lang-select">Ngôn ngữ nói</label>
                      <select
                        id="asr-lang-select"
                        value={asrLanguage}
                        disabled={extractionRunning}
                        onChange={(e) => setAsrLanguage(e.target.value)}
                      >
                        <option value="zh">Tiếng Trung (zh)</option>
                        <option value="en">Tiếng Anh (en)</option>
                        <option value="ja">Tiếng Nhật (ja)</option>
                        <option value="ko">Tiếng Hàn (ko)</option>
                        <option value="vi">Tiếng Việt (vi)</option>
                        <option value="auto">Tự động phát hiện</option>
                      </select>
                    </div>
                    <div>
                      <label htmlFor="asr-model-select">Model Whisper</label>
                      <select
                        id="asr-model-select"
                        value={asrModel}
                        disabled={extractionRunning}
                        onChange={(e) => setAsrModel(e.target.value)}
                      >
                        {(asrRuntime.status?.models.length ? asrRuntime.status.models : [{id: asrModel, state: 'missing'}]).map(model =>
                          <option key={model.id} value={model.id}>{model.id} · {model.state === 'ready' ? 'Đã cài' : 'Chưa cài đầy đủ'}</option>)}
                      </select>
                    </div>
                  </div>

                  <div className="subtitle-generation-options">
                    <label className="studio-field"><span>Thiết bị</span><select value={asrDevice} disabled={extractionRunning}
                      onChange={event => { setAsrDevice(event.target.value as 'auto' | 'cpu' | 'cuda'); setAsrComputeType('auto'); }}>
                      <option value="auto">Tự động</option>
                      {asrRuntime.status?.devices.map(device => <option key={device.id} value={device.id}>{device.id === 'cuda' ? 'GPU NVIDIA' : 'CPU'}</option>)}
                    </select></label>
                    <label className="studio-field"><span>Kiểu tính toán</span><select value={asrComputeType}
                      disabled={extractionRunning} onChange={event => setAsrComputeType(event.target.value)}>
                      <option value="auto">Tự động</option>
                      {asrComputeTypes.map(type => <option key={type} value={type}>{type}</option>)}
                    </select></label>
                  </div>
                  <div role="status">
                    {asrRuntime.loading ? 'Đang kiểm tra model và thiết bị…' : asrRuntime.error || asrRuntime.status?.message ||
                      asrReadiness.message}
                    <button type="button" className="studio-text-button" disabled={asrRuntime.loading || extractionRunning}
                      onClick={asrRuntime.refresh}>Kiểm tra lại</button>
                  </div>
                  {mediaMetadata && !mediaMetadata.has_audio && (
                    <small role="alert" className="subtitle-generation-warn">
                      ⚠️ Video này không có âm thanh; vui lòng chọn OCR (Trên hình) hoặc Gemini.
                    </small>
                  )}
                  {!videoId && <small>Hãy tải video trước khi chạy ASR.</small>}

                  {extractionJob && extractionJob.kind === "asr" && (
                    <div className={`subtitle-job-progress state-${extractionJob.state}`} aria-live="polite">
                      <div>
                        <span>{extractionJob.message}</span>
                        <strong>{extractionJob.progress}%</strong>
                      </div>
                      <progress max={100} value={extractionJob.progress} />
                      {extractionJob.state === "succeeded" && extractionJob.result && (
                        <small>
                          Đã nhận diện {extractionJob.result.segment_count} câu thoại · {extractionJob.result.detected_language} · {extractionJob.result.device} ({extractionJob.result.compute_type}).
                        </small>
                      )}
                    </div>
                  )}
                </section>
              )}

              {extractionMode === "gemini" && (
                <section className="subtitle-generation-panel" aria-labelledby="gemini-generation-title">
                <div className="subtitle-generation-heading">
                  <div>
                    <strong id="gemini-generation-title"><Sparkles size={17} /> Gemini AI</strong>
                    <span>Gửi video lên Gemini qua REST API, không cần cài thêm phần mềm.</span>
                  </div>
                  {generationRunning ? (
                    <button
                      type="button"
                      className="studio-icon-button is-danger"
                      aria-label="Hủy tạo phụ đề bằng Gemini"
                      title="Hủy tác vụ Gemini đang chạy"
                      onClick={() => void handleCancelGeneration()}
                    >
                      <Square size={14} fill="currentColor" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="studio-primary-button subtitle-generation-start"
                      disabled={!canGenerate}
                      onClick={() => void handleGenerateWithGemini()}
                    >
                      <Sparkles size={17} />
                      {cues.length ? "Tạo lại phụ đề" : "Tạo phụ đề"}
                    </button>
                  )}
                </div>
                <div className="subtitle-generation-options is-gemini">
                  <label className="subtitle-generation-checkbox">
                    <input
                      type="checkbox"
                      checked={geminiBilingual}
                      disabled={generationRunning}
                      onChange={(event) => setGeminiBilingual(event.target.checked)}
                    />
                    <span>Giữ câu gốc làm dòng phụ</span>
                  </label>
                  <div className="subtitle-generation-model">
                    <label htmlFor="gemini-model-select">Model Gemini</label>
                    <div className="subtitle-generation-model-controls">
                      <select
                        id="gemini-model-select"
                        value={geminiModel || geminiStatus?.model || ""}
                        disabled={generationRunning || geminiModelsLoading || !geminiModels.length}
                        onChange={(event) => setGeminiModel(event.target.value)}
                      >
                        {!geminiModels.length && (
                          <option value={geminiModel || geminiStatus?.model || ""}>
                            {geminiModelsLoading ? "Đang tải model…" : "Chưa có model khả dụng"}
                          </option>
                        )}
                        {geminiModels.map((model) => (
                          <option key={model.id} value={model.id}>
                            {model.display_name || model.id}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        className="subtitle-generation-model-reload"
                        disabled={geminiModelsLoading || generationRunning}
                        onClick={() => setGeminiModelsReload((value) => value + 1)}
                        aria-label="Làm mới danh sách model Gemini"
                        title="Làm mới danh sách model Gemini"
                      >
                        <RefreshCw className={geminiModelsLoading ? "spin" : undefined} size={16} />
                      </button>
                    </div>
                    {geminiModelsError ? (
                      <small role="alert" className="subtitle-generation-warn">
                        {geminiModelsError} Hãy thử “Làm mới” hoặc kiểm tra API key.
                      </small>
                    ) : (
                      <small className="subtitle-generation-model-help">
                        Model dùng cho lần tạo này: <code>{geminiModel || geminiStatus?.model}</code>.
                        Có thể đổi trước mỗi lần chạy.
                      </small>
                    )}
                  </div>
                </div>
                <GeminiAdvancedOptions value={geminiAdvanced} disabled={generationRunning} onChange={setGeminiAdvanced} />
                {!videoId && <small>Hãy tải video trước để bật Gemini.</small>}
                {geminiStatus && !geminiStatus.authenticated && (
                  <small role="alert" className="subtitle-generation-warn">
                    Chưa có Gemini API key.{" "}
                    <button
                      type="button"
                      className="studio-inline-link"
                      onClick={onOpenSettings}
                    >
                      Vào Settings → Gemini AI
                    </button>{" "}
                    để nhập key từ{" "}
                    <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
                      aistudio.google.com/apikey
                    </a>.
                  </small>
                )}
                {geminiStatus?.authenticated && (
                  <small className="subtitle-generation-ready">
                    Đã cấu hình Gemini API key
                    {geminiModel || geminiStatus.model
                      ? ` · ${geminiModel || geminiStatus.model}`
                      : ""}
                  </small>
                )}
                {videoId && mediaMetadata && !mediaMetadata.has_audio && (
                  <small>Video không có audio; Gemini vẫn có thể đọc chữ trên hình.</small>
                )}
                {generationJob && (
                  <div
                    className={`subtitle-job-progress state-${generationJob.state}`}
                    aria-live="polite"
                  >
                    <div>
                      <span>{generationJob.message}</span>
                      <strong>{generationJob.progress}%</strong>
                    </div>
                    <progress max={100} value={generationJob.progress} />
                    {generationRunning && !generationJob.cancel_requested && (
                      <small>Nếu API khởi động lại, tác vụ sẽ tự tiếp tục các đoạn còn thiếu.</small>
                    )}
                    {!generationRunning && (generationJob.state === "failed" || generationJob.state === "canceled") && <button type="button" className="studio-secondary-button" onClick={async () => {
                      generationControllerRef.current?.abort();
                      const controller = new AbortController();
                      generationControllerRef.current = controller;
                      generationSnapshotRef.current = null;
                      try { const job = await api.resumeGeminiSubtitleJob(generationJob.id, controller.signal); if (!controller.signal.aborted) { setGenerationJob(job); setActiveGeminiJobId(job.id); } }
                      catch (error) { if (!controller.signal.aborted) setError(getErrorMessage(error, "Không thể tiếp tục tác vụ.")); }
                    }}>Tiếp tục các đoạn còn thiếu</button>}
                    <GeminiChunkProgress job={generationJob} />
                    {generationJob.cancel_requested && generationRunning && (
                      <small>Đang dừng tiến trình Gemini an toàn.</small>
                    )}
                    {generationJob.state === "succeeded" && generationJob.result && (
                      <small>
                        {generationJob.result.version_id && "Đã lưu bản mới trong Phiên bản phụ đề. "}
                        {generationJob.result.segment_count} cue · {generationJob.result.chunk_count} đoạn
                        {generationJob.result.actual_models?.length ? ` · Model thực tế: ${generationJob.result.actual_models.join(", ")}` : ""}
                        {generationJob.result.processing_seconds
                          ? ` · ${Math.round(generationJob.result.processing_seconds)} giây xử lý`
                          : ""}
                      </small>
                    )}
                  </div>
                )}
              </section>
              )}

              </details>

              {source.document && <SourceSubtitleEditor key={`source-${videoId}`} source={source}
                durationMs={mediaDurationMs} frameTiming={frameTiming}
                onSeek={ms => workspaceRef.current?.seekTo(ms)} onSave={() => void handleSaveSource()} saving={sourceSaving} />}
              {!source.document && cues.length > 0 && <button type="button" className="studio-secondary-button"
                onClick={() => source.dispatch({ type: 'replace', document: subtitleDocument })}>
                Dùng phụ đề hiện có làm nguồn dịch
              </button>}
              {source.document && translatedSourceRevision !== null &&
                (translatedSourceRevision !== (source.document.revision ?? 0) ||
                  subtitleDocument.source_run_id != null && subtitleDocument.source_run_id !== source.document.run_id) &&
                <p role="status">Nguồn đã thay đổi. Bản dịch hiện tại cần cập nhật.</p>}
              {!!source.document?.segments.length && (
                <details className="subtitle-pipeline-translation-details">
                  <summary>Thiết lập dịch & xuất SRT</summary>
                <section className="subtitle-translation-card" aria-labelledby="gemini-translation-title">
                  <div className="subtitle-generation-heading">
                    <div>
                      <strong id="gemini-translation-title"><Languages size={17} /> Dịch sang tiếng Việt (Gemini AI)</strong>
                      <span>Dịch bản nguồn đã chỉnh, giữ mốc thời gian và ID của từng đoạn.</span>
                    </div>
                    {extractionRunning && extractionJob?.kind === "translation" ? (
                      <button
                        type="button"
                        className="studio-icon-button is-danger"
                        aria-label="Hủy dịch Gemini"
                        title="Hủy dịch đang chạy"
                        onClick={() => void handleCancelExtraction()}
                      >
                        <Square size={14} fill="currentColor" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="studio-primary-button subtitle-generation-start"
                        disabled={extractionRunning || !videoId}
                        onClick={() => void handleTranslateGemini()}
                      >
                        <Languages size={17} />
                        {extractionJob?.kind === 'translation' && extractionJob.details?.resume_available ? 'Tiếp tục dịch' : 'Dịch sang tiếng Việt'}
                      </button>
                    )}
                  </div>

                  {extractionJob?.kind === 'translation' && extractionJob.state === 'failed' && extractionJob.details?.resume_available &&
                    <p role="status">Các nhóm đã dịch được giữ lại. Tiếp tục sẽ dùng cache còn khớp với bản nguồn hiện tại.</p>}
                  {documentMeta.translation_models?.length ? <p>Model đã dùng: {documentMeta.translation_models.join(', ')}</p> : null}
                  <div className="subtitle-generation-options is-translate">
                    <label className="subtitle-generation-checkbox">
                      <input
                        type="checkbox"
                        checked={translateBilingual}
                        disabled={extractionRunning}
                        onChange={(e) => setTranslateBilingual(e.target.checked)}
                      />
                      <span>Song ngữ (giữ câu gốc làm dòng phụ)</span>
                    </label>
                    <div className="subtitle-generation-model">
                      <label htmlFor="translate-model-select">Model Gemini</label>
                      <select
                        id="translate-model-select"
                        value={translateModel || geminiModel || geminiStatus?.model || ""}
                        disabled={extractionRunning || geminiModelsLoading || !geminiModels.length}
                        onChange={(e) => setTranslateModel(e.target.value)}
                      >
                        {geminiModels.map((model) => (
                          <option key={model.id} value={model.id}>
                            {model.display_name || model.id}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <div className="subtitle-translation-actions">
                    <button
                      type="button"
                      className="studio-secondary-button"
                      disabled={isExportingSourceSrt}
                      onClick={() => void handleExportSourceSrt()}
                      title="Tải về file phụ đề SRT chứa văn bản gốc đã trích xuất"
                    >
                      <Download size={15} />
                      {isExportingSourceSrt ? "Đang xuất SRT nguồn…" : "Xuất SRT nguồn"}
                    </button>
                  </div>

                  {extractionJob && extractionJob.kind === "translation" && (
                    <div className={`subtitle-job-progress state-${extractionJob.state}`} aria-live="polite">
                      <div>
                        <span>{extractionJob.message}</span>
                        <strong>{extractionJob.progress}%</strong>
                      </div>
                      <progress max={100} value={extractionJob.progress} />
                      {extractionJob.details && extractionJob.details.translated_count != null && (
                        <small>
                          Đã dịch {extractionJob.details.translated_count} / {extractionJob.details.total_cues || cues.length} đoạn.
                        </small>
                      )}
                    </div>
                  )}
                </section>
                </details>
              )}
              {videoId && <SubtitleVersionsPanel key={`versions-${videoId}`} videoId={videoId} document={subtitleDocument} sourceDocument={source.document} reloadKey={`${generationJob?.result?.version_id ?? ""}:${alignmentJob?.result?.version_id ?? ""}:${extraction.versionReload}:${sourceVersionReload}`}
                onDocument={document => { if (document.document_role === 'source') source.dispatch({ type: 'replace', document }); else applyDocument(document); }} />}
              <details className="subtitle-generation-advanced subtitle-manual-import">
                <summary>Nhập phụ đề thủ công</summary>
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
              </details>
              {sortedCues.length > 0 && (
                <div className="subtitle-alignment-panel">
                  <div className="subtitle-alignment-heading">
                    <div>
                      <strong>Căn timing theo audio</strong>
                      <span>
                        {mediaMetadata?.has_audio
                          ? "Căn theo lời gốc; giữ cue khóa và timing chưa đủ bằng chứng."
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
                        Căn lại thời gian
                      </button>
                    )}
                  </div>
                  <label>Phạm vi căn <select value={alignmentScope} disabled={alignmentRunning} onChange={e => setAlignmentScope(e.target.value)}>
                    <option value="review">Cue cần kiểm tra</option><option value="selected">Cue đang chọn</option><option value="range">Khoảng thời gian</option><option value="all">Tất cả</option>
                  </select></label>
                  {alignmentScope === "range" && <div><label>Từ giây <input type="number" min={0} value={alignmentStart} onChange={e => setAlignmentStart(Number(e.target.value))} /></label><label>Đến giây <input type="number" min={alignmentStart} value={alignmentEnd} onChange={e => setAlignmentEnd(Number(e.target.value))} /></label></div>}
                  <label>Phương pháp căn <select value={alignmentEngine} disabled={alignmentRunning} onChange={e => setAlignmentEngine(e.target.value as SubtitleAlignmentOptions["engine"])}>
                    <option value="auto">Theo thiết lập máy</option><option value="faster_whisper">ASR theo lời gốc</option><option value="energy">Biên năng lượng audio</option>
                  </select></label>
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
                      <li key={`${warning.code}-${warning.cue_id ?? index}`}>{warning.message}
                        {(warning.start_ms != null || warning.cue_id || warning.cue_ids?.length) && <button type="button" className="studio-inline-link" onClick={() => {
                          const cue = sortedCues.find(cue => cue.id === (warning.cue_id ?? warning.cue_ids?.[0]));
                          workspaceRef.current?.seekTo(warning.start_ms ?? cue?.start_ms ?? 0);
                          if (cue) setSelectedCueId(cue.id);
                        }}>Xem đoạn này</button>}
                      </li>
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
                  onToggleLock={cueId => commitCues(current => current.map(cue => cue.id === cueId ? { ...cue, locked: !cue.locked, revision: cue.revision + 1 } : cue))}
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

          {activeTab === "mask" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading">
                <h2>Che phụ đề cũ</h2>
                <p>Tạo vùng che theo phần trăm khung video. Mặc định làm mờ và áp dụng suốt video.</p>
              </div>
              <div className="studio-mask-toolbar">
                <button
                  type="button"
                  className="studio-primary-button"
                  disabled={!videoId || subtitleMasks.length >= MAX_SUBTITLE_MASKS}
                  onClick={addSubtitleMask}
                >
                  <EyeOff size={17} /> Thêm vùng che
                </button>
              </div>
              {!videoId && <p className="studio-mask-hint">Hãy tải video trước để tạo vùng che.</p>}
              {videoId && subtitleMasks.length === 0 && (
                <p className="studio-mask-hint">Nhấn “Thêm vùng che”, sau đó kéo vùng trên video tới đúng phụ đề nước ngoài.</p>
              )}
              {subtitleMasks.length > 0 && (
                <label className="studio-mask-selector">
                  <select
                    value={selectedMaskId ?? ""}
                    aria-label="Chọn vùng che để chỉnh sửa"
                    onChange={(event) => {
                      setSelectedMaskId(event.target.value);
                      setSelectedCueId(null);
                    }}
                  >
                    {subtitleMasks.map((mask, index) => (
                      <option key={mask.id} value={mask.id}>Vùng che {index + 1}</option>
                    ))}
                  </select>
                  <span>{subtitleMasks.length}/{MAX_SUBTITLE_MASKS}</span>
                </label>
              )}
              {selectedMask && (
                <>
                  <div className="studio-mask-group">
                    <span>Hình dạng</span>
                    <div className="studio-mask-choice-grid">
                      {([
                        ["rectangle", "Chữ nhật", Square],
                        ["rounded", "Bo góc", RectangleHorizontal],
                        ["ellipse", "Elip", Circle],
                        ["band", "Dải ngang", Minus],
                      ] as const).map(([shape, label, Icon]) => (
                        <button
                          key={shape}
                          type="button"
                          className={selectedMask.shape === shape ? "is-active" : ""}
                          aria-pressed={selectedMask.shape === shape}
                          onClick={() => updateSubtitleMask(selectedMask.id, { shape: shape as SubtitleMaskShape })}
                        >
                          <Icon size={17} /> {label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="studio-mask-group">
                    <span>Hiệu ứng</span>
                    <div className="studio-mask-choice-grid">
                      {([
                        ["blur", "Làm mờ", EyeOff],
                        ["pixelate", "Pixel", LayoutTemplate],
                        ["solid", "Màu đặc", Square],
                        ["darken", "Làm tối", Moon],
                      ] as const).map(([effect, label, Icon]) => (
                        <button
                          key={effect}
                          type="button"
                          className={selectedMask.effect === effect ? "is-active" : ""}
                          aria-pressed={selectedMask.effect === effect}
                          onClick={() => updateSubtitleMask(selectedMask.id, { effect: effect as SubtitleMaskEffect })}
                        >
                          <Icon size={17} /> {label}
                        </button>
                      ))}
                    </div>
                  </div>
                  {(selectedMask.effect === "blur" || selectedMask.effect === "pixelate") && (
                    <label className="studio-range-field">
                      <span>{selectedMask.effect === "blur" ? "Độ mờ" : "Cỡ pixel"} <output>{selectedMask.strength.toFixed(0)}</output></span>
                      <input type="range" min={1} max={40} step={1} value={selectedMask.strength} onChange={(event) => updateSubtitleMask(selectedMask.id, { strength: Number(event.target.value) })} />
                    </label>
                  )}
                  {(selectedMask.effect === "solid" || selectedMask.effect === "darken") && (
                    <label className="studio-range-field">
                      <span>Độ phủ <output>{Math.round(selectedMask.opacity * 100)}%</output></span>
                      <input type="range" min={0.05} max={1} step={0.05} value={selectedMask.opacity} onChange={(event) => updateSubtitleMask(selectedMask.id, { opacity: Number(event.target.value) })} />
                    </label>
                  )}
                  <label className="studio-range-field">
                    <span>Mềm viền <output>{selectedMask.feather.toFixed(0)}</output></span>
                    <input type="range" min={0} max={20} step={1} value={selectedMask.feather} onChange={(event) => updateSubtitleMask(selectedMask.id, { feather: Number(event.target.value) })} />
                  </label>
                  {selectedMask.shape === "rounded" && (
                    <label className="studio-range-field">
                      <span>Bo góc <output>{selectedMask.cornerRadius.toFixed(0)}%</output></span>
                      <input type="range" min={0} max={50} step={1} value={selectedMask.cornerRadius} onChange={(event) => updateSubtitleMask(selectedMask.id, { cornerRadius: Number(event.target.value) })} />
                    </label>
                  )}
                  {selectedMask.effect === "solid" && (
                    <label className="studio-field">
                      <span>Màu che</span>
                      <input type="color" value={selectedMask.color} onChange={(event) => updateSubtitleMask(selectedMask.id, { color: event.target.value })} />
                    </label>
                  )}
                  <div className="studio-field-grid">
                    <label className="studio-range-field">
                      <span>Rộng <output>{selectedMask.width.toFixed(1)}%</output></span>
                      <input type="range" min={4} max={100} step={0.1} disabled={selectedMask.shape === "band"} value={selectedMask.width} onChange={(event) => updateSubtitleMask(selectedMask.id, { width: Number(event.target.value) })} />
                    </label>
                    <label className="studio-range-field">
                      <span>Cao <output>{selectedMask.height.toFixed(1)}%</output></span>
                      <input type="range" min={3} max={100} step={0.1} value={selectedMask.height} onChange={(event) => updateSubtitleMask(selectedMask.id, { height: Number(event.target.value) })} />
                    </label>
                  </div>
                  <p className="studio-mask-hint">Kéo vùng để di chuyển, kéo bốn góc để đổi kích thước. Giữ Shift + phím mũi tên để dịch nhanh.</p>
                  <button
                    type="button"
                    className="studio-secondary-button studio-mask-delete"
                    onClick={() => deleteSubtitleMask(selectedMask.id)}
                  >
                    <Trash2 size={16} /> Xóa vùng che
                  </button>
                </>
              )}
            </div>
          )}

          {activeTab === "video" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading"><h2>Video</h2><p>Trim và tốc độ dùng chung time-map khi xuất.</p></div>
              <label className="studio-range-field"><span>Tốc độ <output>{options.video_speed?.toFixed(2)}×</output></span><input type="range" min={0.5} max={2} step={0.05} value={options.video_speed} onChange={(event) => setOptions({ ...options, video_speed: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Âm lượng <output>{Math.round((options.volume ?? 1) * 100)}%</output></span><input type="range" min={0} max={1} step={0.01} value={options.volume} onChange={(event) => setOptions({ ...options, volume: Number(event.target.value) })} /></label>
              <div className="studio-field-grid"><label className="studio-field"><span>Trim đầu (s)</span><input type="number" min={0} step={0.001} value={options.trim_start ?? 0} onChange={(event) => setOptions({ ...options, trim_start: Number(event.target.value) })} /></label><label className="studio-field"><span>Trim cuối (s)</span><input type="number" min={0} step={0.001} value={options.trim_end ?? ""} placeholder="Tự động" onChange={(event) => setOptions({ ...options, trim_end: event.target.value ? Number(event.target.value) : null })} /></label></div>
              <label className="studio-field"><span>Tỉ lệ khung</span><select value={options.aspect_ratio} onChange={(event) => setOptions({ ...options, aspect_ratio: event.target.value as SubtitleBurnOptions["aspect_ratio"] })}><option value="original">Gốc</option><option value="16:9">16:9</option><option value="9:16">9:16</option><option value="1:1">1:1</option></select></label>
              <label className="studio-field"><span>Chuyển động</span><select value={options.animation} onChange={(event) => setOptions({ ...options, animation: event.target.value as SubtitleBurnOptions["animation"] })}><option value="none">Không — timing 1 ms</option><option value="fade">Fade — timing 10 ms</option><option value="rise">Rise — timing 10 ms</option><option value="pan">Pan — timing 10 ms</option><option value="typewriter">Typewriter — timing 10 ms</option></select><small>Preview và video xuất cùng dùng libass; chế độ không hiệu ứng vẫn giữ timestamp 1 ms.</small></label>
              <SceneSplitPanel
                videoId={videoId}
                durationMs={mediaDurationMs}
                videoClips={videoClips}
                subtitleDocument={subtitleDocument}
                onApplySegments={(segments) => {
                  setVideoClips(segments);
                  setSelectedVideoClipId(segments[0]?.id ?? null);
                  setLastDeletedVideoClip(null);
                  setRenderedVideoUrl(null);
                  setNotice(`Đã áp dụng ${segments.length} chunk theo cảnh vào timeline.`);
                }}
              />
            </div>
          )}

          {activeTab === "position" && (
            <div className="studio-panel-section">
              <div className="studio-panel-heading"><h2>Vị trí và nền</h2><p>Kéo trực tiếp phụ đề trên video hoặc nhập tọa độ chính xác.</p></div>
              <label className="subtitle-generation-checkbox">
                <input type="checkbox" checked={allSubtitlePositions} disabled={!allSubtitlePositions && !positionCue && !activePositionCueId} onChange={toggleAllSubtitlePositions} />
                Chọn tất cả phụ đề
              </label>
              <small>{allSubtitlePositions
                ? "Chỉnh vị trí sẽ áp dụng cho tất cả phụ đề."
                : "Chọn một đoạn hoặc kéo phụ đề đang hiện để chỉnh vị trí riêng."}</small>
              <label className="studio-range-field"><span>Ngang <output>{editPosition.x.toFixed(1)}%</output></span><input type="range" min={0} max={100} step={0.1} value={editPosition.x} disabled={!positionCue} onChange={(event) => positionCue && handleCuePositionChange(positionCue.id, { ...editPosition, x: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Dọc <output>{editPosition.y.toFixed(1)}%</output></span><input type="range" min={0} max={100} step={0.1} value={editPosition.y} disabled={!positionCue} onChange={(event) => positionCue && handleCuePositionChange(positionCue.id, { ...editPosition, y: Number(event.target.value) })} /></label>
              <label className="studio-range-field"><span>Độ mờ nền <output>{Math.round(options.bg_opacity * 100)}%</output></span><input type="range" min={0} max={1} step={0.05} value={options.bg_opacity} disabled={!options.bg_enabled} onChange={(event) => setOptions({ ...options, bg_opacity: Number(event.target.value) })} /></label>
              <button type="button" className="studio-secondary-button" disabled={!positionCue} onClick={() => positionCue && handleCuePositionChange(positionCue.id, { x: 50, y: 78 })}>Đặt lại vị trí</button>
            </div>
          )}
        </aside>

        {sidebarOpen && <button type="button" className="subtitle-panel-backdrop" aria-label="Đóng bảng công cụ" onClick={() => setSidebarOpen(false)} />}

        <main className="subtitle-studio-main">
          <SubtitleWorkspace
            ref={workspaceRef}
            voice={voice}
            onOpenVoice={() => { setActiveTab("voice"); setSidebarOpen(true); }}
            originalVideoUrl={originalVideoUrl}
            renderedVideoUrl={renderedVideoUrl}
            thumbnailCacheKey={thumbnailCacheKey}
            media={mediaMetadata}
            liveAssContent={liveAssContent}
            cues={sortedCues}
            selectedCueId={selectedCueId}
            options={options}
            overlayImage={overlayImage}
            overlayName={overlayName}
            overlayLayout={overlayLayout}
            subtitleMasks={subtitleMasks}
            selectedMaskId={selectedMaskId}
            ocrRegion={ocrRegion}
            onOcrRegionChange={setOcrRegion}
            showOcrBox={extractionMode === "ocr" && activeTab === "transcript"}
            ocrDisabled={extractionRunning}
            videoClips={videoClips}
            selectedVideoClipId={selectedVideoClipId}
            canRestoreVideoClip={Boolean(lastDeletedVideoClip)}
            onDurationChange={handleDurationChange}
            onSelectCue={cueId => {
              setSelectedCueId(cueId);
              const clip = voice.document?.clips.find(c => cueId && c.source_cue_ids.includes(cueId));
              voice.setSelectedId(clip?.id ?? null);
            }}
            onUpdateCueText={(cueId, text) => handleCueChange(cueId, { text })}
            onCueTimingCommit={handleCueTimingCommit}
            onOptionsChange={setOptions}
            onCuePositionChange={handleCuePositionChange}
            onActiveCueChange={setActivePositionCueId}
            onOverlayLayoutChange={setOverlayLayout}
            onSelectMask={(maskId) => {
              setSelectedMaskId(maskId);
              if (maskId) {
                setSelectedCueId(null);
                setActiveTab("mask");
                setSidebarOpen(true);
              }
            }}
            onMaskChange={updateSubtitleMask}
            onMaskDelete={deleteSubtitleMask}
            onSelectVideoClip={setSelectedVideoClipId}
            onSplitVideo={splitVideoAtPlayhead}
            onDeleteVideoClip={deleteSelectedVideoClip}
            onRestoreVideoClip={restoreDeletedVideoClip}
          />
        </main>
      </div>
    </div>
  );
}
