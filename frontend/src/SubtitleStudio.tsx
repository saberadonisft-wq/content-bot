import { useState, useRef, useEffect, type CSSProperties } from "react";
import {
  Upload,
  Copy,
  Check,
  Play,
  Pause,
  Film,
  Sparkles,
  Download,
  Trash2,
  Plus,
  LoaderCircle,
  Video,
  Type,
  Palette,
  LayoutTemplate,
  Home,
  AlignLeft,
  AlignCenter,
  AlignRight,
  Music,
  Shapes,
  Underline,
  Strikethrough,
  List,
  ArrowUpDown,
  Grid,
  X,
  Scissors,
  Volume2,
  FastForward,
  Paintbrush,
  Maximize2,
  HelpCircle,
  Subtitles,
} from "lucide-react";
import {
  API_BASE,
  api,
  SubtitleItem,
  SubtitleBurnOptions,
} from "./api";

const GEMINI_PROMPT_TEMPLATE = `Hãy nghe/xem video này và tạo phụ đề chính xác bằng tiếng Việt. 
Yêu cầu định dạng đầu ra chuẩn từng dòng để máy tính phân tích tự động:

Định dạng mẫu cho từng câu (không chèn thêm các ký tự trang trí khác):
[MM:SS - MM:SS] Nội dung phụ đề

Ví dụ:
[00:00 - 00:03] Tại sao nhẫn cưới lại phải đeo ở ngón áp út này?
[00:03 - 00:05] Bởi vì mạch máu ở ngón áp út nối thẳng đến trái tim.
[00:06 - 00:10] Hai người sau khi kết hôn, trái tim của họ sẽ kết nối lại với nhau.

Quy tắc:
1. Mốc thời gian theo định dạng Phút:Giây (VD: 00:05 - 00:10 hoặc 01:15 - 01:20).
2. Không thêm văn bản giải thích lời nói đầu hay lời kết, chỉ trả về đúng danh sách phụ đề theo định dạng trên.`;

const hexToRgba = (hex: string, alpha: number) => {
  let c = hex.replace("#", "");
  if (c.length === 3) c = c.split("").map((x) => x + x).join("");
  const num = parseInt(c, 16) || 0;
  return `rgba(${(num >> 16) & 255}, ${(num >> 8) & 255}, ${num & 255}, ${alpha})`;
};

const getErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

type SubtitleStudioProps = {
  onBack?: () => void;
};

export function SubtitleStudio({ onBack }: SubtitleStudioProps) {
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoId, setVideoId] = useState<string | null>(null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState<string | null>(null);
  const [subtitledVideoUrl, setSubtitledVideoUrl] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [duration, setDuration] = useState<number>(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [previewMode, setPreviewMode] = useState<"live" | "rendered">("live");

  const [activeTab, setActiveTab] = useState<"upload" | "text" | "style" | "presets" | "video_edit" | "position" | "animate">("text");

  const [uploading, setUploading] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [isDragging, setIsDragging] = useState(false);

  const [showSpacingPopover, setShowSpacingPopover] = useState(false);
  const [zoomLevel, setZoomLevel] = useState(42);
  const [copiedStyleBuffer, setCopiedStyleBuffer] = useState<Partial<SubtitleBurnOptions> | null>(null);
  const [isEditingInline, setIsEditingInline] = useState(false);

  const [rawText, setRawText] = useState("");
  const [subtitles, setSubtitles] = useState<SubtitleItem[]>([]);
  const [selectedSubIndex, setSelectedSubIndex] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [renderElapsedSec, setRenderElapsedSec] = useState(0);

  useEffect(() => {
    if (!rendering) return undefined;
    const interval = window.setInterval(() => {
      setRenderElapsedSec((prev) => prev + 1);
    }, 1000);
    return () => window.clearInterval(interval);
  }, [rendering]);


  const [videoSize, setVideoSize] = useState({ width: 0, height: 0 });
  const [videoThumbnails, setVideoThumbnails] = useState<string[]>([]);
  const [overlayImage, setOverlayImage] = useState<string | null>(null);
  const [overlayName, setOverlayName] = useState<string>("");

  const extractVideoThumbnails = (videoUrl: string, totalDur: number) => {
    if (!totalDur || totalDur <= 0) return;
    const v = document.createElement("video");
    v.src = videoUrl;
    v.crossOrigin = "anonymous";
    v.onloadeddata = async () => {
      const thumbs: string[] = [];
      const count = 8;
      const step = totalDur / count;
      const canvas = document.createElement("canvas");
      canvas.width = 120;
      canvas.height = 68;
      const ctx = canvas.getContext("2d");

      for (let i = 0; i < count; i++) {
        v.currentTime = Math.min(totalDur - 0.1, i * step);
        await new Promise((resolve) => {
          v.onseeked = resolve;
        });
        if (ctx) {
          ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
          thumbs.push(canvas.toDataURL("image/jpeg", 0.7));
        }
      }
      setVideoThumbnails(thumbs);
    };
  };

  const [options, setOptions] = useState<SubtitleBurnOptions>({
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
    pos_y: 50,
    position: "custom",
    video_speed: 1.0,
    volume: 1.0,
    fade_in: 0.0,
    fade_out: 0.0,
    aspect_ratio: "16:9",
    bg_fill_type: "blur",
    trim_start: 0,
    trim_end: null,
    animation: "none",
  });

  const videoRef = useRef<HTMLVideoElement>(null);
  const playerWrapperRef = useRef<HTMLDivElement>(null);

  const activeSub =
    subtitles.find(
      (s) => s.start_seconds <= currentTime && currentTime <= s.end_seconds
    ) || (subtitles.length > 0 ? subtitles[0] : null);

  const selectedSub = selectedSubIndex !== null && subtitles[selectedSubIndex]
    ? subtitles[selectedSubIndex]
    : null;

  const formatSrtTime = (totalSeconds: number): string => {
    const s = Math.max(0, totalSeconds);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = Math.floor(s % 60);
    const millis = Math.floor((s % 1) * 1000);
    return `${hrs.toString().padStart(2, "0")}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")},${millis.toString().padStart(3, "0")}`;
  };

  const dragStartRef = useRef<{
    startX: number;
    startY: number;
    mouseX: number;
    mouseY: number;
  }>({ startX: 0, startY: 0, mouseX: 0, mouseY: 0 });

  const [isResizingText, setIsResizingText] = useState(false);
  const resizeStartRef = useRef<{ startSize: number; mouseY: number }>({ startSize: 38, mouseY: 0 });

  const [isScrubbing, setIsScrubbing] = useState(false);
  const timelineTracksAreaRef = useRef<HTMLDivElement>(null);

  const [pillDragState, setPillDragState] = useState<{
    index: number;
    mode: "move" | "trim_start" | "trim_end";
    startMouseX: number;
    origStartSec: number;
    origEndSec: number;
  } | null>(null);

  const handleStartDrag = (e: React.MouseEvent) => {
    if (previewMode !== "live" || !playerWrapperRef.current || isEditingInline) return;
    e.preventDefault();
    e.stopPropagation();

    dragStartRef.current = {
      startX: options.pos_x,
      startY: options.pos_y,
      mouseX: e.clientX,
      mouseY: e.clientY,
    };

    setIsDragging(true);
  };

  const handleStartResizeText = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    resizeStartRef.current = {
      startSize: options.font_size,
      mouseY: e.clientY,
    };
    setIsResizingText(true);
  };

  const handleStartScrubbing = (e: React.MouseEvent) => {
    if (!timelineTracksAreaRef.current) return;
    const rect = timelineTracksAreaRef.current.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const totalDur = duration || 40;
    const newTime = Math.max(0, Math.min(totalDur, (clickX / rect.width) * totalDur));
    handleSeek(newTime);
    setIsScrubbing(true);
  };

  const handleStartPillDrag = (e: React.MouseEvent, index: number, mode: "move" | "trim_start" | "trim_end") => {
    e.preventDefault();
    e.stopPropagation();
    setSelectedSubIndex(index);
    const sub = subtitles[index];
    if (!sub) return;
    setPillDragState({
      index,
      mode,
      startMouseX: e.clientX,
      origStartSec: sub.start_seconds,
      origEndSec: sub.end_seconds,
    });
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      // 1. Drag subtitle position on Canvas
      if (isDragging && playerWrapperRef.current) {
        const rect = playerWrapperRef.current.getBoundingClientRect();
        if (rect.width && rect.height) {
          const deltaXPercent = ((e.clientX - dragStartRef.current.mouseX) / rect.width) * 100;
          const deltaYPercent = ((e.clientY - dragStartRef.current.mouseY) / rect.height) * 100;

          let newX = dragStartRef.current.startX + deltaXPercent;
          let newY = dragStartRef.current.startY + deltaYPercent;

          // Magnetic snapping at center (50%)
          if (Math.abs(newX - 50) < 1.8) newX = 50;
          if (Math.abs(newY - 50) < 1.8) newY = 50;

          newX = Math.max(0, Math.min(100, newX));
          newY = Math.max(0, Math.min(100, newY));

          setOptions((prev) => ({
            ...prev,
            position: "custom",
            pos_x: Math.round(newX * 10) / 10,
            pos_y: Math.round(newY * 10) / 10,
          }));
        }
      }

      // 2. Resize subtitle font size via corner handles
      if (isResizingText) {
        const deltaY = resizeStartRef.current.mouseY - e.clientY;
        const newFontSize = Math.max(14, Math.min(96, Math.round(resizeStartRef.current.startSize + deltaY * 0.3)));
        setOptions((prev) => ({ ...prev, font_size: newFontSize }));
      }

      // 3. Scrub timeline playhead
      if (isScrubbing && timelineTracksAreaRef.current) {
        const rect = timelineTracksAreaRef.current.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const totalDur = duration || 40;
        const newTime = Math.max(0, Math.min(totalDur, (clickX / rect.width) * totalDur));
        if (videoRef.current) {
          videoRef.current.currentTime = newTime;
        }
        setCurrentTime(newTime);
      }

      // 4. Move / Trim subtitle pills on timeline
      if (pillDragState && timelineTracksAreaRef.current) {
        const rect = timelineTracksAreaRef.current.getBoundingClientRect();
        const totalDur = duration || 40;
        const deltaSec = ((e.clientX - pillDragState.startMouseX) / rect.width) * totalDur;
        const { index, mode, origStartSec, origEndSec } = pillDragState;

        setSubtitles((prev) => {
          return prev.map((item, idx) => {
            if (idx !== index) return item;

            let newStart = item.start_seconds;
            let newEnd = item.end_seconds;
            const durationSec = origEndSec - origStartSec;

            if (mode === "move") {
              newStart = Math.max(0, Math.min(totalDur - durationSec, origStartSec + deltaSec));
              newEnd = newStart + durationSec;
            } else if (mode === "trim_start") {
              newStart = Math.max(0, Math.min(item.end_seconds - 0.3, origStartSec + deltaSec));
            } else if (mode === "trim_end") {
              newEnd = Math.max(item.start_seconds + 0.3, Math.min(totalDur, origEndSec + deltaSec));
            }

            newStart = Math.round(newStart * 10) / 10;
            newEnd = Math.round(newEnd * 10) / 10;

            return {
              ...item,
              start_seconds: newStart,
              end_seconds: newEnd,
              start_time: formatSrtTime(newStart),
              end_time: formatSrtTime(newEnd),
            };
          });
        });
      }
    };

    const handleMouseUp = () => {
      if (isDragging) setIsDragging(false);
      if (isResizingText) setIsResizingText(false);
      if (isScrubbing) setIsScrubbing(false);
      if (pillDragState) setPillDragState(null);
    };

    if (isDragging || isResizingText || isScrubbing || pillDragState) {
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
    }
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, isResizingText, isScrubbing, pillDragState, duration]);

  const applyPreset = (type: string) => {
    if (type === "tiktok") {
      setOptions((prev) => ({
        ...prev,
        font_name: "Impact",
        font_size: 28,
        font_color: "#FFE600",
        bold: true,
        uppercase: true,
        outline_color: "#000000",
        outline_width: 3,
        bg_enabled: false,
        shadow_width: 0,
      }));
    } else if (type === "neon") {
      setOptions((prev) => ({
        ...prev,
        font_name: "Montserrat",
        font_size: 26,
        font_color: "#00FFFF",
        bold: true,
        uppercase: false,
        outline_color: "#FF0055",
        outline_width: 2,
        shadow_color: "#FF0055",
        shadow_width: 4,
        bg_enabled: false,
      }));
    } else if (type === "box") {
      setOptions((prev) => ({
        ...prev,
        font_name: "Roboto",
        font_size: 24,
        font_color: "#FFFFFF",
        bold: true,
        uppercase: false,
        outline_width: 0,
        bg_enabled: true,
        bg_color: "#000000",
        bg_opacity: 0.85,
        shadow_width: 0,
      }));
    } else if (type === "minimal") {
      setOptions((prev) => ({
        ...prev,
        font_name: "Segoe UI",
        font_size: 22,
        font_color: "#FFFFFF",
        bold: false,
        uppercase: false,
        outline_color: "#000000",
        outline_width: 1,
        bg_enabled: false,
        shadow_color: "#000000",
        shadow_width: 2,
      }));
    }
  };

  const handleCopyPrompt = () => {
    navigator.clipboard.writeText(GEMINI_PROMPT_TEMPLATE);
    setCopiedPrompt(true);
    setTimeout(() => setCopiedPrompt(false), 2000);
  };

  const handleFileChange = async (file: File) => {
    setVideoFile(file);
    setError(null);
    setUploading(true);
    setSubtitledVideoUrl(null);
    setPreviewMode("live");
    setSubtitles([]);
    setRawText("");
    setSelectedSubIndex(null);

    try {
      const res = await api.uploadSubtitleVideo(file);
      setVideoId(res.video_id);
      setOriginalVideoUrl(`${API_BASE}/subtitles/video/${res.video_id}`);
      setActiveTab("text");
    } catch (err: unknown) {
      setError(getErrorMessage(err, "Upload video thất bại"));
    } finally {
      setUploading(false);
    }
  };

  const handleParseText = async () => {
    const text = rawText.trim();
    if (!text) {
      setError("Hãy dán nội dung phụ đề trước khi bấm Bóc Tách Phụ Đề.");
      return;
    }
    setParsing(true);
    setError(null);
    try {
      const res = await api.parseSubtitleText(text);
      setSubtitles(res.subtitles);
      if (res.subtitles.length === 0) {
        setError("Không bóc tách được mốc thời gian nào từ văn bản. Hãy kiểm tra lại cấu trúc văn bản dán.");
      }
    } catch (err: unknown) {
      setError(getErrorMessage(err, "Phân tích phụ đề thất bại"));
    } finally {
      setParsing(false);
    }
  };

  const handleSeek = (seconds: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = seconds;
      setCurrentTime(seconds);
      videoRef.current.play().catch(() => { });
    }
  };

  const togglePlay = () => {
    if (videoRef.current) {
      if (videoRef.current.paused) {
        videoRef.current.play();
      } else {
        videoRef.current.pause();
      }
    }
  };

  const handleUpdateSubText = (index: number, newText: string) => {
    setSubtitles((prev) =>
      prev.map((item, i) => (i === index ? { ...item, text: newText } : item))
    );
  };

  const handleDeleteSub = (index: number) => {
    setSubtitles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleAddSub = () => {
    setSubtitles((prev) => [
      ...prev,
      {
        start_time: "00:00:00,000",
        end_time: "00:00:03,000",
        start_seconds: 0,
        end_seconds: 3,
        text: "Dòng phụ đề mới",
      },
    ]);
  };

  const handleBurnSubtitles = async () => {
    if (!videoId || subtitles.length === 0) return;
    setRenderElapsedSec(0);
    setRendering(true);
    setError(null);
    try {
      await api.burnSubtitleVideo(videoId, subtitles, options);
      const url = `${API_BASE}/subtitles/video/${videoId}?type=subtitled&t=${Date.now()}`;
      setSubtitledVideoUrl(url);
      setPreviewMode("rendered");
    } catch (err: unknown) {
      setError(getErrorMessage(err, "Ghép phụ đề thất bại"));
    } finally {
      setRendering(false);
    }
  };

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  };

  return (
    <div className="canva-layout">
      {/* ── HEADER ── */}
      <header className="canva-header">
        <div className="canva-brand">
          <button type="button" aria-label="Về trang chủ" onClick={onBack} className="canva-btn canva-btn-primary" style={{ padding: '8px', background: 'rgba(255,255,255,0.2)', color: 'white' }}>
            <Home size={20} />
          </button>
          <span style={{ margin: '0 12px' }}>Tệp</span>
          <span style={{ margin: '0 12px' }}>Đổi cỡ</span>
          <span style={{ margin: '0 12px' }}>Sửa</span>
        </div>
        <div style={{ position: 'absolute', left: '50%', transform: 'translateX(-50%)', fontWeight: 600, fontSize: 16 }}>
          Thiết kế không tên - Phụ Đề Video
        </div>
        <div className="canva-header-actions">
          <button type="button" className="canva-btn canva-btn-secondary" style={{ background: 'rgba(0,0,0,0.2)' }}>
            <Sparkles size={16} className="icon-gold" /> Dùng thử với giá 0 đ
          </button>
          <span className="canva-toolbar-divider" style={{ background: 'rgba(255,255,255,0.3)', margin: '0 8px' }}></span>
          {rendering && (
            <span style={{ fontSize: 13, marginRight: 16, color: '#ffeb3b', display: 'flex', alignItems: 'center' }}>
              Đang xử lý video (có thể mất vài phút)...
            </span>
          )}
          {videoId && subtitles.length > 0 && (
            <button
              className="canva-btn canva-btn-primary"
              onClick={handleBurnSubtitles}
              disabled={rendering}
            >
              {rendering ? <LoaderCircle className="spin" size={16} /> : <Film size={16} />}
              <span>{rendering ? `Đang Xuất... (${formatTime(renderElapsedSec)})` : "Xuất Video"}</span>
            </button>
          )}
          {subtitledVideoUrl && (
            <a
              href={subtitledVideoUrl}
              download={`subtitled_${videoId}.mp4`}
              className="canva-btn canva-btn-secondary"
            >
              <Download size={16} /> Chia sẻ
            </a>
          )}
        </div>
      </header>

      {/* ── BODY ── */}
      <div className="canva-body">
        {/* LEFT ICONS */}
        <div className="canva-sidebar-icons">
          <button
            type="button"
            aria-label="Tải lên"
            className={`canva-icon-tab ${activeTab === "upload" ? "active" : ""}`}
            onClick={() => setActiveTab("upload")}
          >
            <Upload size={22} />
            <span>Tải lên</span>
          </button>
          <button
            type="button"
            aria-label="Văn bản"
            className={`canva-icon-tab ${activeTab === "text" ? "active" : ""}`}
            onClick={() => setActiveTab("text")}
          >
            <Type size={22} />
            <span>Văn bản</span>
          </button>
          <button
            type="button"
            aria-label="Mẫu"
            className={`canva-icon-tab ${activeTab === "presets" ? "active" : ""}`}
            onClick={() => setActiveTab("presets")}
          >
            <LayoutTemplate size={22} />
            <span>Mẫu</span>
          </button>
          <button
            type="button"
            aria-label="Hiệu ứng"
            className={`canva-icon-tab ${activeTab === "style" ? "active" : ""}`}
            onClick={() => setActiveTab("style")}
          >
            <Palette size={22} />
            <span>Hiệu ứng</span>
          </button>
          <button
            type="button"
            aria-label="Biên tập video"
            className={`canva-icon-tab ${activeTab === "video_edit" ? "active" : ""}`}
            onClick={() => setActiveTab("video_edit")}
          >
            <Scissors size={22} />
            <span>Biên tập</span>
          </button>
        </div>

        {/* SIDEBAR CONTENT PANEL */}
        <div className="canva-sidebar-panel">
          {error && <div className="studio-error" style={{ marginBottom: 16 }}>{error}</div>}

          {/* TAB: UPLOAD */}
          {activeTab === "upload" && (
            <div className="canva-panel-content">
              <h3>Tải video lên</h3>
              <p className="canva-text-muted">Chọn video từ máy tính của bạn để bắt đầu chỉnh sửa phụ đề.</p>

              <div className="upload-box" style={{ marginTop: 16 }}>
                <input
                  type="file"
                  accept="video/*"
                  id="video-upload"
                  onChange={(e) => e.target.files?.[0] && handleFileChange(e.target.files[0])}
                  style={{ display: "none" }}
                />
                <label htmlFor="video-upload" className="upload-label">
                  {uploading ? (
                    <div className="loading-state">
                      <LoaderCircle className="spin" size={32} />
                      <span>Đang tải...</span>
                    </div>
                  ) : videoFile ? (
                    <div className="file-info">
                      <Video size={32} />
                      <div>
                        <strong>{videoFile.name}</strong>
                        <p>{(videoFile.size / (1024 * 1024)).toFixed(2)} MB</p>
                      </div>
                    </div>
                  ) : (
                    <div className="upload-placeholder">
                      <Upload size={32} />
                      <span>Tải lên nội dung</span>
                    </div>
                  )}
                </label>
              </div>

              <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid #e5e7eb" }}>
                <h4 style={{ margin: "0 0 4px 0", fontSize: 15, fontWeight: 700 }}>Tải logo / hình ảnh đè</h4>
                <p className="canva-text-muted">Chọn tệp ảnh logo (PNG/JPG) để chèn đè lên video nếu muốn.</p>

                <div className="upload-box" style={{ marginTop: 12 }}>
                  <input
                    type="file"
                    accept="image/*"
                    id="logo-upload"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) {
                        const url = URL.createObjectURL(file);
                        setOverlayImage(url);
                        setOverlayName(file.name);
                      }
                    }}
                    style={{ display: "none" }}
                  />
                  <label htmlFor="logo-upload" className="upload-label">
                    {overlayImage ? (
                      <div className="file-info" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <img src={overlayImage} alt="Logo" style={{ width: 28, height: 28, objectFit: "contain", borderRadius: 4 }} />
                          <strong style={{ fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 140 }}>{overlayName}</strong>
                        </div>
                        <button
                          type="button"
                          className="canva-btn canva-btn-outline"
                          style={{ padding: "2px 8px", fontSize: 12, color: "#ef4444", borderColor: "#fca5a5" }}
                          onClick={(evt) => {
                            evt.preventDefault();
                            setOverlayImage(null);
                            setOverlayName("");
                          }}
                        >
                          Xóa
                        </button>
                      </div>
                    ) : (
                      <div className="upload-placeholder">
                        <Upload size={24} />
                        <span>Tải ảnh logo đè lên</span>
                      </div>
                    )}
                  </label>
                </div>
              </div>
            </div>
          )}

          {/* TAB: TEXT (Gemini & Subtitle List) */}
          {activeTab === "text" && (
            <div className="canva-panel-content">
              <h3>Tạo phụ đề (Gemini)</h3>

              <div className="canva-tool-group">
                <label className="canva-label">1. Copy Prompt</label>
                <button
                  type="button"
                  className={`canva-btn ${copiedPrompt ? "canva-btn-success" : "canva-btn-outline"}`}
                  onClick={handleCopyPrompt}
                  style={{ width: "100%" }}
                >
                  {copiedPrompt ? <Check size={16} /> : <Copy size={16} />}
                  {copiedPrompt ? "Đã Sao Chép!" : "Sao Chép Prompt"}
                </button>
              </div>

              <div className="canva-tool-group">
                <label className="canva-label">2. Dán Kết Quả</label>
                <textarea
                  className="canva-textarea"
                  rows={4}
                  placeholder="Dán văn bản phụ đề từ Gemini ở đây..."
                  value={rawText}
                  onChange={(e) => setRawText(e.target.value)}
                />
                  <button
                    type="button"
                    className="canva-btn canva-btn-primary"
                    onClick={handleParseText}
                    disabled={parsing}
                    aria-busy={parsing}
                    style={{ width: "100%", marginTop: 8 }}
                >
                  {parsing ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                  {parsing ? "Đang bóc tách..." : "Bóc Tách Phụ Đề"}
                </button>
              </div>

              {subtitles.length > 0 && (
                <div className="canva-tool-group" style={{ marginTop: 24 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                    <label className="canva-label" style={{ margin: 0 }}>Danh Sách Phụ Đề</label>
                    <button type="button" className="btn-icon" onClick={handleAddSub} title="Thêm dòng"><Plus size={16} /></button>
                  </div>
                  <div className="subtitle-list" style={{ maxHeight: '400px' }}>
                    {subtitles.map((sub, idx) => (
                      <div
                        key={idx}
                        className={`sub-item-row ${selectedSubIndex === idx ? "selected" : ""}`}
                        style={{
                          flexDirection: 'column',
                          alignItems: 'flex-start',
                          border: selectedSubIndex === idx ? "2px solid #7d2ae8" : "1px solid #e5e7eb",
                          borderRadius: 8,
                          padding: 8,
                          marginBottom: 8,
                          background: selectedSubIndex === idx ? "#f5f3ff" : "white"
                        }}
                        onClick={() => {
                          handleSeek(sub.start_seconds);
                          setSelectedSubIndex(idx);
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%' }}>
                          <button
                            type="button"
                            className="btn-play-seek"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleSeek(sub.start_seconds);
                              setSelectedSubIndex(idx);
                            }}
                          >
                            <Play size={12} /> {sub.start_time.slice(3, 8)}
                          </button>
                          <button type="button" className="btn-icon btn-delete" onClick={(e) => { e.stopPropagation(); handleDeleteSub(idx); }}>
                            <Trash2 size={14} />
                          </button>
                        </div>
                        <textarea
                          className="canva-textarea"
                          rows={2}
                          value={sub.text}
                          onFocus={() => setSelectedSubIndex(idx)}
                          onChange={(e) => handleUpdateSubText(idx, e.target.value)}
                          style={{ width: "100%", marginTop: 4, minHeight: 40 }}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* TAB: PRESETS */}
          {activeTab === "presets" && (
            <div className="canva-panel-content">
              <h3>Mẫu Phụ Đề</h3>
              <p className="canva-text-muted">Áp dụng nhanh các phong cách phổ biến.</p>

              <div className="canva-presets-grid">
                <button className="preset-card tiktok" onClick={() => applyPreset("tiktok")}>
                  <span>🔥 TikTok</span>
                </button>
                <button className="preset-card neon" onClick={() => applyPreset("neon")}>
                  <span>✨ Neon</span>
                </button>
                <button className="preset-card box" onClick={() => applyPreset("box")}>
                  <span>⬛ Hộp Đen</span>
                </button>
                <button className="preset-card minimal" onClick={() => applyPreset("minimal")}>
                  <span>⚪ Tối Giản</span>
                </button>
              </div>
            </div>
          )}

          {/* TAB: STYLE (EFFECTS PANEL - MATCHING CANVA EXACTLY) */}
          {activeTab === "style" && (
            <div className="canva-panel-content" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, borderBottom: '1px solid #e5e7eb', paddingBottom: 12 }}>
                <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: '#111827' }}>Hiệu ứng</h3>
                <button type="button" className="canva-toolbar-btn canva-toolbar-btn-icon" onClick={() => setActiveTab("text")} title="Đóng">
                  <X size={18} />
                </button>
              </div>

              {/* 1. Phong cách (Standard Text Effects) */}
              <div className="canva-effects-section">
                <div className="canva-effects-grid">
                  {/* Không có */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 0, outline_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ textShadow: 'none', WebkitTextStroke: 'none' }}>Ag</div>
                    <span className="effect-card-label">Không có</span>
                  </button>

                  {/* Đổ bóng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 5, shadow_color: '#000000', outline_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ textShadow: '3px 3px 6px rgba(0,0,0,0.4)' }}>Ag</div>
                    <span className="effect-card-label">Đổ bóng</span>
                  </button>

                  {/* Phát sáng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 10, shadow_color: '#a855f7', outline_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ textShadow: '0 0 12px #a855f7' }}>Ag</div>
                    <span className="effect-card-label">Phát sáng</span>
                  </button>

                  {/* Lặp bóng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 6, shadow_color: '#c084fc', outline_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ textShadow: '4px 4px 0 #c084fc' }}>Ag</div>
                    <span className="effect-card-label">Lặp bóng</span>
                  </button>

                  {/* Viền */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, outline_width: 2, outline_color: '#7d2ae8', shadow_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ color: 'white', WebkitTextStroke: '1.5px #7d2ae8' }}>Ag</div>
                    <span className="effect-card-label">Viền</span>
                  </button>

                  {/* Nền */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, bg_enabled: true, bg_color: '#d8b4fe', bg_opacity: 0.8, outline_width: 0, shadow_width: 0 })}
                  >
                    <div className="effect-card-preview" style={{ background: '#d8b4fe', color: '#6b21a8', borderRadius: 8 }}>Ag</div>
                    <span className="effect-card-label">Nền</span>
                  </button>

                  {/* Bóng rỗng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, outline_width: 2, outline_color: '#7d2ae8', shadow_width: 4, shadow_color: '#e9d5ff', bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ color: 'transparent', WebkitTextStroke: '1.5px #7d2ae8', textShadow: '3px 3px 0 #e9d5ff' }}>Ag</div>
                    <span className="effect-card-label">Bóng rỗng</span>
                  </button>

                  {/* Rỗng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, outline_width: 2, outline_color: '#7d2ae8', shadow_width: 0, bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ color: 'transparent', WebkitTextStroke: '1.5px #7d2ae8' }}>Ag</div>
                    <span className="effect-card-label">Rỗng</span>
                  </button>

                  {/* Neon */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 12, shadow_color: '#f472b6', font_color: '#ffffff', outline_width: 1, outline_color: '#ec4899', bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ color: '#f472b6', textShadow: '0 0 10px #ec4899, 0 0 20px #ec4899' }}>Ag</div>
                    <span className="effect-card-label">Neon</span>
                  </button>

                  {/* Nhiễu */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, shadow_width: 4, shadow_color: '#06b6d4', outline_width: 2, outline_color: '#ec4899', bg_enabled: false })}
                  >
                    <div className="effect-card-preview" style={{ color: '#7d2ae8', textShadow: '-2px 0 #06b6d4, 2px 0 #ec4899' }}>Ag</div>
                    <span className="effect-card-label">Nhiễu</span>
                  </button>
                </div>
              </div>

              {/* 2. Nâng cao (Advanced Presets) */}
              <div className="canva-effects-section">
                <div className="canva-effects-section-title">Nâng cao</div>
                <div className="canva-effects-grid">
                  {/* Đèn neon */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#f472b6', shadow_width: 12, shadow_color: '#ec4899' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#f472b6', textShadow: '0 0 12px #ec4899' }}>AG</div>
                    <span className="effect-card-label">Đèn neon</span>
                  </button>

                  {/* Nhiễu TV */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#38bdf8', shadow_width: 6, shadow_color: '#c084fc', outline_width: 2, outline_color: '#f43f5e' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#38bdf8', textShadow: '-2px 2px #f43f5e' }}>Ag</div>
                    <span className="effect-card-label">Nhiễu TV</span>
                  </button>

                  {/* Thập niên 70 */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#fbbf24', shadow_width: 5, shadow_color: '#b45309', outline_width: 1, outline_color: '#78350f' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#fbbf24', textShadow: '3px 3px 0 #b45309' }}>Ag</div>
                    <span className="effect-card-label">Thập niên 70</span>
                  </button>

                  {/* Khoa học viễn tưởng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#4ade80', shadow_width: 10, shadow_color: '#22c55e', outline_width: 1, outline_color: '#15803d' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#4ade80', textShadow: '0 0 8px #22c55e', fontFamily: 'monospace' }}>AG</div>
                    <span className="effect-card-label">Khoa học viễn tưởng</span>
                  </button>

                  {/* In lụa */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#f472b6', shadow_width: 6, shadow_color: '#db2777', outline_width: 1, outline_color: '#9d174d' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#f472b6', textShadow: '4px 4px 0 #db2777' }}>AG</div>
                    <span className="effect-card-label">In lụa</span>
                  </button>

                  {/* Phương Tây */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#f97316', shadow_width: 4, shadow_color: '#9a3412', outline_width: 2, outline_color: '#7c2d12' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#f97316', fontFamily: 'serif', fontStyle: 'italic' }}>Ag</div>
                    <span className="effect-card-label">Phương Tây</span>
                  </button>

                  {/* Graffiti */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#10b981', shadow_width: 6, shadow_color: '#047857', outline_width: 3, outline_color: '#064e3b' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#10b981', WebkitTextStroke: '2px #064e3b', fontWeight: 900 }}>AG</div>
                    <span className="effect-card-label">Graffiti</span>
                  </button>

                  {/* Bong bóng */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#06b6d4', shadow_width: 5, shadow_color: '#0e7490', outline_width: 2, outline_color: '#164e63' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#06b6d4', WebkitTextStroke: '1.5px #164e63', borderRadius: '50%' }}>Ag</div>
                    <span className="effect-card-label">Bong bóng</span>
                  </button>

                  {/* Thể dục nhịp điệu */}
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, font_color: '#e879f9', shadow_width: 10, shadow_color: '#c084fc', outline_width: 1, outline_color: '#a855f7' })}
                  >
                    <div className="effect-card-preview" style={{ color: '#e879f9', fontStyle: 'italic', textShadow: '0 0 10px #c084fc' }}>Ag</div>
                    <span className="effect-card-label">Thể dục nhịp điệu</span>
                  </button>
                </div>
              </div>

              {/* 3. Hình dạng (Shape Effects) */}
              <div className="canva-effects-section">
                <div className="canva-effects-section-title">Hình dạng</div>
                <div className="canva-effects-grid" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                  <button
                    type="button"
                    className="canva-effect-card"
                    onClick={() => setOptions({ ...options, spacing: options.spacing === 2 ? 0 : 2 })}
                  >
                    <div className="effect-card-preview" style={{ color: '#7d2ae8', fontSize: 18 }}>
                      <span style={{ display: 'inline-block', transform: 'rotate(-10deg)' }}>A</span>
                      <span style={{ display: 'inline-block', transform: 'rotate(-5deg)' }}>B</span>
                      <span style={{ display: 'inline-block', transform: 'rotate(5deg)' }}>C</span>
                      <span style={{ display: 'inline-block', transform: 'rotate(10deg)' }}>D</span>
                    </div>
                    <span className="effect-card-label">Uốn cong</span>
                  </button>
                </div>
              </div>

              {/* 4. Fine-tuning sliders */}
              <div style={{ marginTop: 24, paddingTop: 16, borderTop: '1px solid #e5e7eb' }}>
                <div className="canva-tool-group">
                  <label className="canva-label">Khoảng cách chữ: {options.spacing}</label>
                  <input type="range" className="canva-slider" min={-5} max={15} value={options.spacing} onChange={(e) => setOptions({ ...options, spacing: Number(e.target.value) })} />
                </div>

                <div className="canva-tool-group">
                  <label className="canva-label">Màu viền & Độ dày ({options.outline_width}px)</label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input type="color" className="canva-color-picker" value={options.outline_color} onChange={(e) => setOptions({ ...options, outline_color: e.target.value })} />
                    <input type="range" className="canva-slider" style={{ flex: 1 }} min={0} max={10} value={options.outline_width} onChange={(e) => setOptions({ ...options, outline_width: Number(e.target.value) })} />
                  </div>
                </div>

                <div className="canva-tool-group">
                  <label className="canva-label">Màu bóng đổ & Độ dày ({options.shadow_width}px)</label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input type="color" className="canva-color-picker" value={options.shadow_color} onChange={(e) => setOptions({ ...options, shadow_color: e.target.value })} />
                    <input type="range" className="canva-slider" style={{ flex: 1 }} min={0} max={10} value={options.shadow_width} onChange={(e) => setOptions({ ...options, shadow_width: Number(e.target.value) })} />
                  </div>
                </div>

                <div className="canva-tool-group">
                  <label className="checkbox-label" style={{ fontWeight: 'bold', display: 'flex', alignItems: 'center', gap: 8 }}>
                    <input type="checkbox" checked={options.bg_enabled} onChange={(e) => setOptions({ ...options, bg_enabled: e.target.checked })} />
                    Bật khung nền
                  </label>
                  {options.bg_enabled && (
                    <div style={{ padding: 12, background: '#f3f4f6', borderRadius: 8, marginTop: 8 }}>
                      <div className="canva-tool-group">
                        <label className="canva-label">Màu nền</label>
                        <input type="color" className="canva-color-picker" value={options.bg_color} onChange={(e) => setOptions({ ...options, bg_color: e.target.value })} />
                      </div>
                      <div className="canva-tool-group" style={{ marginBottom: 0 }}>
                        <label className="canva-label">Độ mờ: {Math.round(options.bg_opacity * 100)}%</label>
                        <input type="range" className="canva-slider" min={0.1} max={1.0} step={0.05} value={options.bg_opacity} onChange={(e) => setOptions({ ...options, bg_opacity: Number(e.target.value) })} />
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* TAB: VIDEO EDIT (Cắt xén, Tốc độ, Âm thanh, Nền, Tỷ lệ) */}
          {activeTab === "video_edit" && (
            <div className="canva-panel-content" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, borderBottom: '1px solid #e5e7eb', paddingBottom: 12 }}>
                <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: '#111827' }}>Biên Tập Video</h3>
                <button type="button" className="canva-toolbar-btn canva-toolbar-btn-icon" onClick={() => setActiveTab("text")} title="Đóng">
                  <X size={18} />
                </button>
              </div>

              {/* 1. Tốc độ (Speed) */}
              <div className="canva-tool-group">
                <label className="canva-label" style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700 }}>
                  <FastForward size={16} />
                  Tốc độ phát: {options.video_speed || 1.0}x
                </label>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 6, marginTop: 8 }}>
                  {[0.5, 1.0, 1.25, 1.5, 2.0].map((s) => (
                    <button
                      key={s}
                      type="button"
                      className={`canva-btn ${(options.video_speed || 1.0) === s ? "canva-btn-primary" : "canva-btn-outline"}`}
                      style={{ padding: '6px 0', fontSize: 12, fontWeight: 600 }}
                      onClick={() => setOptions({ ...options, video_speed: s })}
                    >
                      {s}x
                    </button>
                  ))}
                </div>
              </div>

              {/* 2. Âm thanh (Audio) */}
              <div className="canva-tool-group" style={{ marginTop: 20 }}>
                <label className="canva-label" style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700 }}>
                  <Volume2 size={16} />
                  Âm lượng: {Math.round((options.volume || 1.0) * 100)}%
                </label>
                <input
                  type="range"
                  className="canva-slider"
                  min={0.0}
                  max={2.0}
                  step={0.1}
                  value={options.volume || 1.0}
                  onChange={(e) => setOptions({ ...options, volume: Number(e.target.value) })}
                />

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>Tăng âm đầu (Fade-in): {(options.fade_in || 0).toFixed(1)}s</label>
                    <input
                      type="range"
                      className="canva-slider"
                      min={0.0}
                      max={3.0}
                      step={0.5}
                      value={options.fade_in || 0}
                      onChange={(e) => setOptions({ ...options, fade_in: Number(e.target.value) })}
                    />
                  </div>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>Giảm âm cuối (Fade-out): {(options.fade_out || 0).toFixed(1)}s</label>
                    <input
                      type="range"
                      className="canva-slider"
                      min={0.0}
                      max={3.0}
                      step={0.5}
                      value={options.fade_out || 0}
                      onChange={(e) => setOptions({ ...options, fade_out: Number(e.target.value) })}
                    />
                  </div>
                </div>
              </div>

              {/* 3. Tỷ lệ & Nền (Aspect Ratio & Background Fill) */}
              <div className="canva-tool-group" style={{ marginTop: 20 }}>
                <label className="canva-label" style={{ fontWeight: 700 }}>Tỷ lệ khung hình & Nền</label>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 8 }}>
                  <button
                    type="button"
                    className={`canva-btn ${(options.aspect_ratio || "16:9") === "16:9" ? "canva-btn-primary" : "canva-btn-outline"}`}
                    style={{ fontSize: 12, padding: '8px 4px' }}
                    onClick={() => setOptions({ ...options, aspect_ratio: "16:9" })}
                  >
                    16:9 (Ngang)
                  </button>
                  <button
                    type="button"
                    className={`canva-btn ${(options.aspect_ratio || "16:9") === "9:16" ? "canva-btn-primary" : "canva-btn-outline"}`}
                    style={{ fontSize: 12, padding: '8px 4px' }}
                    onClick={() => setOptions({ ...options, aspect_ratio: "9:16" })}
                  >
                    9:16 (TikTok)
                  </button>
                  <button
                    type="button"
                    className={`canva-btn ${(options.aspect_ratio || "16:9") === "1:1" ? "canva-btn-primary" : "canva-btn-outline"}`}
                    style={{ fontSize: 12, padding: '8px 4px' }}
                    onClick={() => setOptions({ ...options, aspect_ratio: "1:1" })}
                  >
                    1:1 (Vuông)
                  </button>
                </div>

                {(options.aspect_ratio === "9:16" || options.aspect_ratio === "1:1") && (
                  <div style={{ marginTop: 12, padding: 12, background: '#f8fafc', borderRadius: 8, border: '1px solid #e2e8f0' }}>
                    <label className="canva-label" style={{ fontSize: 12, fontWeight: 600 }}>Kiểu phủ nền (Background Fill)</label>
                    <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                      <button
                        type="button"
                        className={`canva-btn ${(options.bg_fill_type || "blur") === "blur" ? "canva-btn-primary" : "canva-btn-outline"}`}
                        style={{ flex: 1, fontSize: 12 }}
                        onClick={() => setOptions({ ...options, bg_fill_type: "blur" })}
                      >
                        ✨ Mờ nền (Blur)
                      </button>
                      <button
                        type="button"
                        className={`canva-btn ${(options.bg_fill_type || "blur") === "black" ? "canva-btn-primary" : "canva-btn-outline"}`}
                        style={{ flex: 1, fontSize: 12 }}
                        onClick={() => setOptions({ ...options, bg_fill_type: "black" })}
                      >
                        ⬛ Khung đen
                      </button>
                    </div>
                  </div>
                )}
              </div>

              {/* 4. Cắt xén Video (Video Trimming) */}
              <div className="canva-tool-group" style={{ marginTop: 20 }}>
                <label className="canva-label" style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700 }}>
                  <Scissors size={16} />
                  Cắt xén Video (Trim)
                </label>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 8 }}>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>Thời điểm bắt đầu (s)</label>
                    <input
                      type="number"
                      className="canva-toolbar-input"
                      style={{ width: '100%' }}
                      min={0}
                      max={duration || 100}
                      step={0.5}
                      value={options.trim_start || 0}
                      onChange={(e) => setOptions({ ...options, trim_start: Number(e.target.value) })}
                    />
                  </div>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>Thời điểm kết thúc (s)</label>
                    <input
                      type="number"
                      className="canva-toolbar-input"
                      style={{ width: '100%' }}
                      min={options.trim_start || 0}
                      max={duration || 100}
                      step={0.5}
                      placeholder={duration ? duration.toFixed(1) : "Hết video"}
                      value={options.trim_end !== null && options.trim_end !== undefined ? options.trim_end : ""}
                      onChange={(e) => setOptions({ ...options, trim_end: e.target.value ? Number(e.target.value) : null })}
                    />
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TAB: POSITION (Vị trí) */}
          {activeTab === "position" && (
            <div className="canva-panel-content" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, borderBottom: '1px solid #e5e7eb', paddingBottom: 12 }}>
                <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: '#111827' }}>Vị trí</h3>
                <button type="button" className="canva-toolbar-btn canva-toolbar-btn-icon" onClick={() => setActiveTab("text")} title="Đóng">
                  <X size={18} />
                </button>
              </div>

              <div className="canva-tool-group">
                <label className="canva-label" style={{ fontWeight: 700 }}>Căn chỉnh vị trí trên trang</label>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginTop: 8 }}>
                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: 10, pos_y: options.pos_y })}
                  >
                    Trái
                  </button>
                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: 50, pos_y: options.pos_y })}
                  >
                    Giữa (Ngang)
                  </button>
                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: 90, pos_y: options.pos_y })}
                  >
                    Phải
                  </button>

                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: options.pos_x, pos_y: 15 })}
                  >
                    Trên
                  </button>
                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: options.pos_x, pos_y: 50 })}
                  >
                    Giữa (Dọc)
                  </button>
                  <button
                    type="button"
                    className="canva-btn canva-btn-outline"
                    style={{ fontSize: 12 }}
                    onClick={() => setOptions({ ...options, position: "custom", pos_x: options.pos_x, pos_y: 85 })}
                  >
                    Dưới
                  </button>
                </div>
              </div>

              <div className="canva-tool-group" style={{ marginTop: 24 }}>
                <label className="canva-label" style={{ fontWeight: 700 }}>Tọa độ tùy chỉnh (%)</label>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>X: {options.pos_x}%</label>
                    <input
                      type="range"
                      className="canva-slider"
                      min={0}
                      max={100}
                      value={options.pos_x}
                      onChange={(e) => setOptions({ ...options, position: "custom", pos_x: Number(e.target.value) })}
                    />
                  </div>
                  <div>
                    <label className="canva-label" style={{ fontSize: 12 }}>Y: {options.pos_y}%</label>
                    <input
                      type="range"
                      className="canva-slider"
                      min={0}
                      max={100}
                      value={options.pos_y}
                      onChange={(e) => setOptions({ ...options, position: "custom", pos_y: Number(e.target.value) })}
                    />
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* TAB: ANIMATE (Chuyển động) */}
          {activeTab === "animate" && (
            <div className="canva-panel-content" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, borderBottom: '1px solid #e5e7eb', paddingBottom: 12 }}>
                <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: '#111827' }}>Chuyển động phụ đề</h3>
                <button type="button" className="canva-toolbar-btn canva-toolbar-btn-icon" onClick={() => setActiveTab("text")} title="Đóng">
                  <X size={18} />
                </button>
              </div>

              <div className="canva-effects-grid" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
                {[
                  { id: "none", label: "Không có", icon: "✨" },
                  { id: "fade", label: "Mờ dần", icon: "🌫️" },
                  { id: "rise", label: "Trồi lên", icon: "⬆️" },
                  { id: "pan", label: "Gạt sang", icon: "➡️" },
                  { id: "typewriter", label: "Máy đánh chữ", icon: "⌨️" },
                ].map((anim) => (
                  <button
                    key={anim.id}
                    type="button"
                    className={`canva-effect-card ${options.animation === anim.id ? "active" : ""}`}
                    onClick={() => setOptions({ ...options, animation: anim.id as SubtitleBurnOptions["animation"] })}
                  >
                    <div className="effect-card-preview" style={{ fontSize: 24 }}>
                      {anim.icon}
                    </div>
                    <span className="effect-card-label">{anim.label}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* MAIN CANVAS & TIMELINE */}
        <div className="canva-main-area">
          {/* TOP TOOLBAR AREA */}
          <div className="canva-top-toolbar-area">
            {selectedSub ? (
              <div className="canva-floating-pill-toolbar" style={{ position: "relative" }}>
                {/* 1. Subtitle / Caption Label Pill */}
                <button
                  type="button"
                  className={`canva-pill-item ${activeTab === "text" ? "active" : ""}`}
                  onClick={() => setActiveTab("text")}
                  title="Chú thích phụ đề"
                  style={{ background: "#f0e7fe", color: "#7d2ae8", border: "1px solid #c084fc", fontWeight: 600 }}
                >
                  <Subtitles size={15} />
                  <span>Chú thích</span>
                </button>

                {/* 2. Font family select */}
                <select
                  className="canva-pill-select"
                  value={options.font_name}
                  onChange={(e) => setOptions({ ...options, font_name: e.target.value })}
                >
                  <option value="Arimo">Arimo</option>
                  <option value="Arial">Arial</option>
                  <option value="Roboto">Roboto</option>
                  <option value="Montserrat">Montserrat</option>
                  <option value="Impact">Impact</option>
                  <option value="Trebuchet MS">Trebuchet MS</option>
                  <option value="Comic Sans MS">Comic Sans MS</option>
                  <option value="Times New Roman">Times New Roman</option>
                </select>

                {/* 3. Font size capsule */}
                <div className="canva-pill-capsule">
                  <button className="canva-pill-btn-sm" onClick={() => setOptions({ ...options, font_size: Math.max(14, options.font_size - 2) })}>-</button>
                  <span className="canva-pill-size-num">{options.font_size}</span>
                  <button className="canva-pill-btn-sm" onClick={() => setOptions({ ...options, font_size: Math.min(72, options.font_size + 2) })}>+</button>
                </div>

                {/* 4. Text color with Rainbow bar */}
                <div className="canva-pill-color-btn" title="Màu chữ">
                  <span className="color-letter" style={{ color: options.font_color !== "#FFFFFF" ? options.font_color : "#0f172a" }}>A</span>
                  <div className="rainbow-bar" style={{ background: options.font_color !== "#FFFFFF" ? options.font_color : undefined }} />
                  <input
                    type="color"
                    className="hidden-color-input"
                    value={options.font_color}
                    onChange={(e) => setOptions({ ...options, font_color: e.target.value })}
                  />
                </div>

                {/* 5. Bold B */}
                <button
                  type="button"
                  className={`canva-pill-btn ${options.bold ? "active" : ""}`}
                  onClick={() => setOptions({ ...options, bold: !options.bold })}
                  title="In đậm"
                >
                  <strong>B</strong>
                </button>

                {/* 6. Italic I */}
                <button
                  type="button"
                  className={`canva-pill-btn ${options.italic ? "active" : ""}`}
                  onClick={() => setOptions({ ...options, italic: !options.italic })}
                  title="In nghiêng"
                >
                  <em>I</em>
                </button>

                {/* 7. Underline U */}
                <button
                  type="button"
                  className={`canva-pill-btn ${options.underline ? "active" : ""}`}
                  onClick={() => setOptions({ ...options, underline: !options.underline })}
                  title="Gạch chân"
                >
                  <Underline size={15} />
                </button>

                {/* 8. Strikethrough S */}
                <button
                  type="button"
                  className={`canva-pill-btn ${options.strikethrough ? "active" : ""}`}
                  onClick={() => setOptions({ ...options, strikethrough: !options.strikethrough })}
                  title="Gạch ngang"
                >
                  <Strikethrough size={15} />
                </button>

                {/* 9. Uppercase aA */}
                <button
                  type="button"
                  className={`canva-pill-btn ${options.uppercase ? "active" : ""}`}
                  onClick={() => setOptions({ ...options, uppercase: !options.uppercase })}
                  title="Đổi kiểu chữ hoa/thường"
                >
                  aA
                </button>

                {/* 10. Align Toggle */}
                <button
                  type="button"
                  className="canva-pill-btn"
                  title="Căn chỉnh dòng"
                  onClick={() => {
                    const nextAlign = options.alignment_type === "center" ? "left" : options.alignment_type === "left" ? "right" : "center";
                    setOptions({ ...options, alignment_type: nextAlign });
                  }}
                >
                  {options.alignment_type === "left" ? <AlignLeft size={15} /> : options.alignment_type === "right" ? <AlignRight size={15} /> : <AlignCenter size={15} />}
                </button>

                {/* 11. List */}
                <button
                  type="button"
                  className="canva-pill-btn"
                  title="Danh sách"
                >
                  <List size={15} />
                </button>

                {/* 12. Spacing Button with Popover */}
                <button
                  type="button"
                  className={`canva-pill-btn ${showSpacingPopover ? "active" : ""}`}
                  title="Khoảng cách chữ & dòng"
                  onClick={() => setShowSpacingPopover(!showSpacingPopover)}
                >
                  <ArrowUpDown size={15} />
                </button>

                {/* Spacing Popover Floating Container */}
                {showSpacingPopover && (
                  <div className="canva-popover-menu">
                    <div className="canva-popover-row">
                      <label className="canva-label" style={{ fontSize: 12 }}>Khoảng cách chữ: {options.spacing}px</label>
                      <input
                        type="range"
                        className="canva-slider"
                        min={-5}
                        max={15}
                        value={options.spacing}
                        onChange={(e) => setOptions({ ...options, spacing: Number(e.target.value) })}
                      />
                    </div>
                    <div className="canva-popover-row">
                      <label className="canva-label" style={{ fontSize: 12 }}>Khoảng cách dòng: {(options.line_spacing || 1.2).toFixed(1)}</label>
                      <input
                        type="range"
                        className="canva-slider"
                        min={0.8}
                        max={2.5}
                        step={0.1}
                        value={options.line_spacing || 1.2}
                        onChange={(e) => setOptions({ ...options, line_spacing: Number(e.target.value) })}
                      />
                    </div>
                  </div>
                )}

                {/* 13. Format Painter */}
                <button
                  type="button"
                  className={`canva-pill-btn ${copiedStyleBuffer ? "active" : ""}`}
                  title="Sao chép định dạng"
                  onClick={() => {
                    if (!copiedStyleBuffer) {
                      setCopiedStyleBuffer({ ...options });
                    } else {
                      setOptions({ ...options, ...copiedStyleBuffer });
                    }
                  }}
                >
                  <Paintbrush size={15} />
                </button>

                {/* 14. Hiệu ứng */}
                <button
                  type="button"
                  className={`canva-pill-btn text-btn ${activeTab === "style" ? "active" : ""}`}
                  style={activeTab === "style" ? { background: "#f0e7fe", color: "#7d2ae8", fontWeight: 700 } : undefined}
                  onClick={() => setActiveTab("style")}
                >
                  Hiệu ứng
                </button>

                {/* 15. Chuyển động */}
                <button
                  type="button"
                  className={`canva-pill-btn text-btn ${activeTab === "animate" ? "active" : ""}`}
                  style={activeTab === "animate" ? { background: "#f0e7fe", color: "#7d2ae8", fontWeight: 700 } : undefined}
                  onClick={() => setActiveTab("animate")}
                >
                  Chuyển động
                </button>

                {/* 16. Vị trí */}
                <button
                  type="button"
                  className={`canva-pill-btn text-btn ${activeTab === "position" ? "active" : ""}`}
                  style={activeTab === "position" ? { background: "#f0e7fe", color: "#7d2ae8", fontWeight: 700 } : undefined}
                  onClick={() => setActiveTab("position")}
                >
                  Vị trí
                </button>
              </div>
            ) : null}
          </div>

          <div className="canva-canvas">
            <div
              className="player-wrapper"
              style={{
                display: "flex",
                justifyContent: "center",
                alignItems: "center",
                width: "100%",
                height: "100%",
                padding: "20px"
              }}
            >
              <div
                className="video-aspect-container"
                ref={playerWrapperRef}
                style={{
                  position: "relative",
                  width: videoSize.width && videoSize.height ? `min(100%, calc(100vh * ${videoSize.width / videoSize.height}))` : "800px",
                  height: videoSize.width && videoSize.height ? `min(100%, calc(100vw * ${videoSize.height / videoSize.width}))` : "450px",
                  maxHeight: "100%",
                  maxWidth: "100%",
                  aspectRatio: videoSize.width && videoSize.height ? `${videoSize.width}/${videoSize.height}` : "16/9",
                  boxShadow: "0 8px 24px rgba(0,0,0,0.15)",
                  background: originalVideoUrl ? "#000" : "#1e293b",
                  overflow: "hidden"
                }}
              >
                {originalVideoUrl ? (
                  <video
                    ref={videoRef}
                    src={previewMode === "rendered" && subtitledVideoUrl ? subtitledVideoUrl : originalVideoUrl}
                    style={{ width: "100%", height: "100%", display: "block" }}
                    onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)}
                    onDurationChange={(e) => setDuration(e.currentTarget.duration)}
                    onPlay={() => setIsPlaying(true)}
                    onPause={() => setIsPlaying(false)}
                    onEnded={() => setIsPlaying(false)}
                    onClick={togglePlay}
                    onLoadedMetadata={(e) => {
                      const dur = e.currentTarget.duration || 40;
                      setVideoSize({
                        width: e.currentTarget.videoWidth,
                        height: e.currentTarget.videoHeight,
                      });
                      if (originalVideoUrl) {
                        extractVideoThumbnails(originalVideoUrl, dur);
                      }
                    }}
                  />
                ) : (
                  <div
                    style={{
                      width: "100%",
                      height: "100%",
                      backgroundColor: "#ffffff",
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      position: "relative"
                    }}
                  />
                )}

                {/* Magenta Alignment Guidelines */}
                {(isDragging || Math.abs(options.pos_x - 50) < 1.5) && (
                  <div className="canva-snap-line-v" style={{ left: `${options.pos_x}%` }} />
                )}
                {(isDragging || Math.abs(options.pos_y - 50) < 1.5) && (
                  <div className="canva-snap-line-h" style={{ top: `${options.pos_y}%` }} />
                )}

                {/* User-uploaded Logo Overlay Image */}
                {overlayImage && (
                  <div
                    style={{
                      position: "absolute",
                      top: 16,
                      left: 16,
                      zIndex: 40,
                      pointerEvents: "none",
                    }}
                  >
                    <img src={overlayImage} alt="Logo overlay" style={{ maxHeight: 40, maxWidth: 160, objectFit: "contain" }} />
                  </div>
                )}

                {/* Canva Drag-Drop Interactive Live Subtitle Overlay */}
                {previewMode === "live" && activeSub && (
                  <div
                    className={`live-sub-overlay canva-box ${selectedSub && selectedSub === activeSub ? "selected" : ""}`}
                    role="button"
                    tabIndex={0}
                    aria-label="Chọn lớp phụ đề đang hiển thị"
                    onClick={(e) => {
                      e.stopPropagation();
                      const idx = subtitles.indexOf(activeSub);
                      setSelectedSubIndex(idx >= 0 ? idx : 0);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        const idx = subtitles.indexOf(activeSub);
                        setSelectedSubIndex(idx >= 0 ? idx : 0);
                      }
                    }}
                    style={{
                      fontFamily: options.font_name,
                      fontSize: `${Math.max(14, options.font_size)}px`,
                      color: options.font_color,
                      fontWeight: options.bold ? "bold" : "normal",
                      fontStyle: options.italic ? "italic" : "normal",
                      textDecoration: `${options.underline ? "underline" : ""} ${options.strikethrough ? "line-through" : ""}`.trim() || "none",
                      textAlign: options.alignment_type || "center",
                      lineHeight: options.line_spacing || 1.2,
                      textTransform: options.uppercase ? "uppercase" : "none",
                      letterSpacing: `${options.spacing}px`,
                      left: `${options.pos_x}%`,
                      top: `${options.pos_y}%`,
                      transform: "translate(-50%, -50%)",
                      WebkitPaintOrder: "stroke fill",
                      paintOrder: "stroke fill",
                      WebkitTextStroke: options.outline_width > 0 ? `${options.outline_width}px ${options.outline_color}` : "none",
                      textShadow: options.shadow_width > 0
                        ? `${options.shadow_width}px ${options.shadow_width}px 4px ${options.shadow_color}`
                        : (options.outline_width > 0 ? "none" : "0 2px 4px rgba(0,0,0,0.9)"),
                      backgroundColor: options.bg_enabled ? hexToRgba(options.bg_color, options.bg_opacity) : "transparent",
                      padding: options.bg_enabled ? "8px 16px" : "4px 12px",
                      borderRadius: "8px",
                      maxWidth: "85%",
                    } as CSSProperties}
                  >
                    {/* Bounding box drag border area */}
                    <div
                      className="canva-border-drag-area"
                      onMouseDown={handleStartDrag}
                      title="Bấm và kéo đường viền để di chuyển vị trí phụ đề"
                    />

                    {/* Corner Handles */}
                    <div className="canva-handle handle-tl" onMouseDown={handleStartResizeText} title="Kéo để thay đổi cỡ chữ" />
                    <div className="canva-handle handle-tr" onMouseDown={handleStartResizeText} title="Kéo để thay đổi cỡ chữ" />
                    <div className="canva-handle handle-bl" onMouseDown={handleStartResizeText} title="Kéo để thay đổi cỡ chữ" />
                    <div className="canva-handle handle-br" onMouseDown={handleStartResizeText} title="Kéo để thay đổi cỡ chữ" />

                    {/* Drag Move handle (+) */}
                    <div
                      className={`canva-move-handle ${isDragging ? "is-dragging" : ""}`}
                      onMouseDown={handleStartDrag}
                      title="Tóm vào đây để di chuyển phụ đề"
                    >
                      +
                    </div>

                    {/* Subtitle text area (Bấm và kéo để di chuyển, nhấp kép để sửa chữ) */}
                    <div
                      className="sub-text-content"
                      contentEditable={isEditingInline}
                      suppressContentEditableWarning
                      onMouseDown={(e) => {
                        if (!isEditingInline) {
                          handleStartDrag(e);
                        } else {
                          e.stopPropagation();
                        }
                      }}
                      onDoubleClick={(e) => {
                        e.stopPropagation();
                        setIsEditingInline(true);
                      }}
                      onBlur={(e) => {
                        setIsEditingInline(false);
                        const updatedText = e.currentTarget.innerText.trim();
                        if (updatedText && activeSub && selectedSubIndex !== null) {
                          handleUpdateSubText(selectedSubIndex, updatedText);
                        }
                      }}
                      style={{
                        outline: isEditingInline ? "1px dashed #7d2ae8" : "none",
                        backgroundColor: isEditingInline ? "rgba(255,255,255,0.2)" : undefined,
                        cursor: isEditingInline ? "text" : "move",
                      }}
                    >
                      {options.uppercase ? activeSub.text.toUpperCase() : activeSub.text}
                    </div>

                    {activeSub.secondary_text && (
                      <span className="sub-secondary-line">
                        {activeSub.secondary_text}
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* TIMELINE CENTER PLAYBACK BAR */}
          <div className="canva-timeline-center-controls">
            <span className="time-display">{formatTime(currentTime)}</span>
            <button className="btn-play-circle-lg" onClick={togglePlay} disabled={!originalVideoUrl} title={isPlaying ? "Tạm dừng" : "Phát"}>
              {isPlaying ? <Pause size={16} fill="currentColor" /> : <Play size={16} fill="currentColor" style={{ marginLeft: 2 }} />}
            </button>
            <span className="time-display">{formatTime(duration || 40)}</span>
          </div>

          {/* TIMELINE MULTI-TRACK AREA */}
          <div className="canva-timeline">
            <div className="timeline-tracks-area">
              <div className="timeline-body">
                {/* Left Track Control Rail */}
                <div className="timeline-track-controls">
                  {!originalVideoUrl ? (
                    <>
                      <button className="btn-track-toggle" title="Thành phần">
                        <Shapes size={16} />
                      </button>
                      <button className="btn-track-toggle" title="Phương tiện">
                        <Film size={16} />
                      </button>
                      <button className="btn-track-toggle" title="Âm thanh">
                        <Music size={16} />
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="btn-track-toggle active" title="Phụ đề">
                        <Subtitles size={16} />
                        <div className="btn-track-badge" />
                      </button>
                      {overlayImage && (
                        <button className="btn-track-toggle active" title="Ảnh Logo đè">
                          <Shapes size={16} />
                          <div className="btn-track-badge" />
                        </button>
                      )}
                      <button className="btn-track-toggle" title="Video Clip">
                        <Film size={16} />
                      </button>
                    </>
                  )}
                </div>

                <div className="timeline-scale" ref={timelineTracksAreaRef} onMouseDown={handleStartScrubbing}>
                  {/* Time Ruler */}
                  <div className="timeline-ruler">
                    {Array.from({ length: 7 }).map((_, i) => (
                      <div key={i} className="ruler-mark-group">
                        <span className="ruler-label">{i === 6 ? "1:00" : `${i * 10} giây`}</span>
                        <div className="ruler-ticks">
                          <span />
                          <span />
                          <span />
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="timeline-tracks-container">
                    {/* Canva Playhead Line */}
                    <div
                      className="canva-playhead"
                      style={{ left: (duration || 40) > 0 ? `${(currentTime / (duration || 40)) * 100}%` : "0%" }}
                    >
                      <div className="playhead-head">▼</div>
                      <div className="playhead-line" />
                    </div>

                    <div className="timeline-track-lanes">
                  {!originalVideoUrl ? (
                    <>
                      {/* Empty State Track 1: Thêm thành phần */}
                      <div className="canva-track track-empty-placeholder" style={{ height: 32 }}>
                        <Shapes size={14} />
                        <span>Thêm thành phần</span>
                      </div>
                      {/* Empty State Track 2: Kéo và thả phương tiện */}
                      <div className="canva-track track-empty-placeholder" style={{ height: 64, backgroundColor: "#f3f4f6" }}>
                        <div className="empty-add-btn">
                          <Plus size={16} />
                        </div>
                        <span>hoặc kéo và thả phương tiện</span>
                      </div>
                      {/* Empty State Track 3: Thêm âm thanh */}
                      <div className="canva-track track-empty-placeholder" style={{ height: 32 }}>
                        <Music size={14} />
                        <span>Thêm âm thanh</span>
                      </div>
                    </>
                  ) : (
                    <>
                      {/* Track 1: Subtitles (Canva Bright Green Capsules) */}
                      <div className="canva-track track-elements">
                        {subtitles.length > 0 ? (
                          <div className="subtitles-timeline-row">
                            {subtitles.map((sub, i) => {
                              const totalSec = duration || 40;
                              const left = (sub.start_seconds / Math.max(totalSec, 1)) * 100;
                              const width = Math.max(1, ((sub.end_seconds - sub.start_seconds) / Math.max(totalSec, 1)) * 100);
                              const isAct = activeSub === sub;
                              const isSel = selectedSubIndex === i;
                              return (
                                <div
                                  key={i}
                                  className={`sub-block-pill ${isAct ? "active" : ""} ${isSel ? "selected" : ""}`}
                                  style={{ left: `${left}%`, width: `${width}%` }}
                                  onMouseDown={(e) => handleStartPillDrag(e, i, "move")}
                                  onClick={() => {
                                    handleSeek(sub.start_seconds);
                                    setSelectedSubIndex(i);
                                  }}
                                  title={`${sub.text} (${sub.start_seconds.toFixed(1)}s - ${sub.end_seconds.toFixed(1)}s)`}
                                >
                                  <div
                                    className="pill-handle-l"
                                    onMouseDown={(e) => handleStartPillDrag(e, i, "trim_start")}
                                    title="Kéo để chỉnh mốc thời gian bắt đầu"
                                  />
                                  <div className="sub-track-indicator-top" />
                                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{sub.text}</span>
                                  <div
                                    className="pill-handle-r"
                                    onMouseDown={(e) => handleStartPillDrag(e, i, "trim_end")}
                                    title="Kéo để chỉnh mốc thời gian kết thúc"
                                  />
                                </div>
                              );
                            })}
                          </div>
                        ) : (
                          <div className="track-empty-bar">
                            <Shapes size={14} />
                            <span>Thêm phụ đề</span>
                          </div>
                        )}
                      </div>

                      {/* Track 2: Overlay Logo Track (Only rendered if user uploaded a logo image!) */}
                      {overlayImage && (
                        <div className="canva-track track-elements">
                          <div className="subtitles-timeline-row">
                            <div
                              className="watermark-block-pill"
                              style={{ left: "0%", width: "100%" }}
                              title={overlayName || "Logo Overlay"}
                            >
                              <img src={overlayImage} alt="Logo" style={{ width: 16, height: 16, objectFit: "contain", borderRadius: 2 }} />
                              <span>{overlayName || "Logo Overlay"}</span>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* Track 3: Video Clips Filmstrip with Real Extracted Thumbnails */}
                      <div className="canva-track track-media" style={{ height: 48 }}>
                        <div className="video-thumbnails-strip">
                          {videoThumbnails.length > 0 ? (
                            videoThumbnails.map((thumb, idx) => (
                              <div key={idx} className="video-thumb-clip">
                                <img src={thumb} alt={`Clip frame ${idx}`} />
                              </div>
                            ))
                          ) : (
                            <div className="track-empty-bar">
                              <Film size={14} />
                              <span>Đang tải video...</span>
                            </div>
                          )}
                          <div className="plus-box" style={{ borderRadius: 0, height: "100%", width: 36, cursor: "pointer" }}>
                            <Plus size={16} />
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* BOTTOM STATUS BAR (Matching Canva exactly) */}
          <div className="canva-status-bar">
            <div className="canva-status-left">
              <span>Trang 1 / 1</span>
            </div>

            <div className="canva-status-right">
              {/* Zoom Controls */}
              <div className="canva-zoom-control">
                <button className="canva-status-btn" onClick={() => setZoomLevel(Math.max(10, zoomLevel - 5))}>-</button>
                <input
                  type="range"
                  className="canva-zoom-slider"
                  min={10}
                  max={200}
                  value={zoomLevel}
                  onChange={(e) => setZoomLevel(Number(e.target.value))}
                />
                <button className="canva-status-btn" onClick={() => setZoomLevel(Math.min(200, zoomLevel + 5))}>+</button>
                <span style={{ minWidth: 32, textAlign: "right" }}>{zoomLevel}%</span>
              </div>

              {/* Trang grid button */}
              <button className="canva-status-btn">
                <Grid size={14} />
                <span>Trang</span>
                <span style={{ opacity: 0.7, marginLeft: 4 }}>{formatTime(currentTime)} / {formatTime(duration || 40)}</span>
              </button>

              {/* Fullscreen button */}
              <button className="canva-status-btn" title="Toàn màn hình">
                <Maximize2 size={14} />
              </button>

              {/* Help button */}
              <button className="canva-status-btn" title="Trợ giúp">
                <HelpCircle size={14} />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
