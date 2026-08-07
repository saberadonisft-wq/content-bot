import { useState, useRef, useEffect } from "react";
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
  Clock,
  Underline,
  Strikethrough,
  List,
  ArrowUpDown,
  Grid,
  X,
  Scissors,
  Sliders,
  Volume2,
  FastForward
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

export function SubtitleStudio() {
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoId, setVideoId] = useState<string | null>(null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState<string | null>(null);
  const [subtitledVideoUrl, setSubtitledVideoUrl] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [duration, setDuration] = useState<number>(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [previewMode, setPreviewMode] = useState<"live" | "rendered">("live");

  const [activeTab, setActiveTab] = useState<"upload" | "text" | "style" | "presets" | "video_edit">("upload");

  const [uploading, setUploading] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [isDragging, setIsDragging] = useState(false);

  const [rawText, setRawText] = useState("");
  const [subtitles, setSubtitles] = useState<SubtitleItem[]>([]);
  const [selectedSubIndex, setSelectedSubIndex] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  
  const [videoSize, setVideoSize] = useState({ width: 0, height: 0 });
  const dragOffset = useRef({ x: 0, y: 0 });

  const [options, setOptions] = useState<SubtitleBurnOptions>({
    font_name: "Arial",
    font_size: 24,
    font_color: "#FFFFFF",
    bold: false,
    italic: false,
    uppercase: false,
    outline_color: "#000000",
    outline_width: 2,
    shadow_color: "#000000",
    shadow_width: 0,
    bg_enabled: false,
    bg_color: "#000000",
    bg_opacity: 0.75,
    spacing: 0,
    pos_x: 50,
    pos_y: 85,
    position: "bottom",
    video_speed: 1.0,
    volume: 1.0,
    fade_in: 0.0,
    fade_out: 0.0,
    aspect_ratio: "16:9",
    bg_fill_type: "blur",
    trim_start: 0,
    trim_end: null,
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

  const handleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (previewMode !== "live") return;
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    
    dragOffset.current = {
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    };
    
    setIsDragging(true);
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging || !playerWrapperRef.current) return;
      const rect = playerWrapperRef.current.getBoundingClientRect();
      
      let x = ((e.clientX - dragOffset.current.x - rect.left) / rect.width) * 100;
      let y = ((e.clientY - dragOffset.current.y - rect.top) / rect.height) * 100;
      
      x = Math.max(0, Math.min(100, x));
      y = Math.max(0, Math.min(100, y));

      setOptions((prev) => ({
        ...prev,
        position: "custom",
        pos_x: Math.round(x * 10) / 10,
        pos_y: Math.round(y * 10) / 10,
      }));
    };

    const handleMouseUp = () => {
      if (isDragging) setIsDragging(false);
    };

    if (isDragging) {
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
    }
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging]);

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

    try {
      const res = await api.uploadSubtitleVideo(file);
      setVideoId(res.video_id);
      setOriginalVideoUrl(`${API_BASE}/subtitles/video/${res.video_id}`);
      setActiveTab("text");
    } catch (err: any) {
      setError(err.message || "Upload video thất bại");
    } finally {
      setUploading(false);
    }
  };

  const handleParseText = async () => {
    if (!rawText.trim()) return;
    setParsing(true);
    setError(null);
    try {
      const res = await api.parseSubtitleText(rawText);
      setSubtitles(res.subtitles);
      if (res.subtitles.length === 0) {
        setError("Không bóc tách được mốc thời gian nào từ văn bản. Hãy kiểm tra lại cấu trúc văn bản dán.");
      }
    } catch (err: any) {
      setError(err.message || "Phân tích phụ đề thất bại");
    } finally {
      setParsing(false);
    }
  };

  const handleSeek = (seconds: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = seconds;
      setCurrentTime(seconds);
      videoRef.current.play().catch(() => {});
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
    setRendering(true);
    setError(null);
    try {
      const res = await api.burnSubtitleVideo(videoId, subtitles, options);
      const url = `${API_BASE}/subtitles/video/${videoId}?type=subtitled&t=${Date.now()}`;
      setSubtitledVideoUrl(url);
      setPreviewMode("rendered");
    } catch (err: any) {
      setError(err.message || "Ghép phụ đề thất bại");
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
          <button className="canva-btn canva-btn-primary" style={{ padding: '8px', background: 'rgba(255,255,255,0.2)', color: 'white' }}>
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
          <button className="canva-btn canva-btn-secondary" style={{ background: 'rgba(0,0,0,0.2)' }}>
            <Sparkles size={16} className="icon-gold" /> Dùng thử với giá 0 đ
          </button>
          <span className="canva-toolbar-divider" style={{ background: 'rgba(255,255,255,0.3)', margin: '0 8px' }}></span>
          {videoId && subtitles.length > 0 && (
            <button 
              className="canva-btn canva-btn-primary" 
              onClick={handleBurnSubtitles} 
              disabled={rendering}
            >
              {rendering ? <LoaderCircle className="spin" size={16} /> : <Film size={16} />}
              <span>{rendering ? "Đang Xuất..." : "Xuất Video"}</span>
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
            className={`canva-icon-tab ${activeTab === "upload" ? "active" : ""}`} 
            onClick={() => setActiveTab("upload")}
          >
            <Upload size={22} />
            <span>Tải lên</span>
          </button>
          <button 
            className={`canva-icon-tab ${activeTab === "text" ? "active" : ""}`} 
            onClick={() => setActiveTab("text")}
          >
            <Type size={22} />
            <span>Văn bản</span>
          </button>
          <button 
            className={`canva-icon-tab ${activeTab === "presets" ? "active" : ""}`} 
            onClick={() => setActiveTab("presets")}
          >
            <LayoutTemplate size={22} />
            <span>Mẫu</span>
          </button>
          <button 
            className={`canva-icon-tab ${activeTab === "style" ? "active" : ""}`} 
            onClick={() => setActiveTab("style")}
          >
            <Palette size={22} />
            <span>Hiệu ứng</span>
          </button>
          <button 
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
                  disabled={parsing || !rawText.trim()}
                  style={{ width: "100%", marginTop: 8 }}
                >
                  {parsing ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                  Bóc Tách Phụ Đề
                </button>
              </div>

              {subtitles.length > 0 && (
                <div className="canva-tool-group" style={{ marginTop: 24 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                    <label className="canva-label" style={{ margin: 0 }}>Danh Sách Phụ Đề</label>
                    <button type="button" className="btn-icon" onClick={handleAddSub} title="Thêm dòng"><Plus size={16}/></button>
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
        </div>

        {/* MAIN CANVAS & TIMELINE */}
        <div className="canva-main-area">
          {/* TOP TOOLBAR AREA */}
          <div className="canva-top-toolbar-area">
            {selectedSub ? (
              <div className="canva-floating-pill-toolbar">
                {/* 1. Duration pill */}
                <div className="canva-pill-item duration-pill">
                  <Clock size={14} />
                  <span>{(selectedSub.end_seconds - selectedSub.start_seconds).toFixed(1)} giây</span>
                </div>

                {/* 2. Font family select */}
                <select 
                  className="canva-pill-select"
                  value={options.font_name} 
                  onChange={(e) => setOptions({ ...options, font_name: e.target.value })}
                >
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
                  <span className="color-letter">A</span>
                  <div className="rainbow-bar" />
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
                  className="canva-pill-btn" 
                  title="Gạch chân"
                >
                  <Underline size={15} />
                </button>

                {/* 8. Strikethrough S */}
                <button 
                  type="button" 
                  className="canva-pill-btn" 
                  title="Gạch ngang"
                >
                  <Strikethrough size={15} />
                </button>

                {/* 9. Uppercase aA */}
                <button 
                  type="button" 
                  className={`canva-pill-btn ${options.uppercase ? "active" : ""}`} 
                  onClick={() => setOptions({ ...options, uppercase: !options.uppercase })}
                  title="Đổi kiểu chữ"
                >
                  aA
                </button>

                {/* 10. Align */}
                <button 
                  type="button" 
                  className="canva-pill-btn" 
                  title="Căn chỉnh"
                >
                  <AlignCenter size={15} />
                </button>

                {/* 11. List */}
                <button 
                  type="button" 
                  className="canva-pill-btn" 
                  title="Danh sách"
                >
                  <List size={15} />
                </button>

                {/* 12. Spacing */}
                <button 
                  type="button" 
                  className="canva-pill-btn" 
                  title="Khoảng cách chữ"
                >
                  <ArrowUpDown size={15} />
                </button>

                {/* 13. Transparency / Grid */}
                <button 
                  type="button" 
                  className="canva-pill-btn" 
                  title="Độ trong suốt"
                >
                  <Grid size={15} />
                </button>

                {/* 14. Hiệu ứng */}
                <button 
                  type="button" 
                  className="canva-pill-btn text-btn" 
                  onClick={() => setActiveTab("style")}
                >
                  Hiệu ứng
                </button>

                {/* 15. Chuyển động */}
                <button 
                  type="button" 
                  className="canva-pill-btn text-btn"
                >
                  Chuyển động
                </button>

                {/* 16. Vị trí */}
                <button 
                  type="button" 
                  className="canva-pill-btn text-btn"
                >
                  Vị trí
                </button>
              </div>
            ) : null}
          </div>

          <div className="canva-canvas">
            {originalVideoUrl ? (
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
                    width: videoSize.width && videoSize.height ? `min(100%, calc(100vh * ${videoSize.width / videoSize.height}))` : "100%",
                    height: videoSize.width && videoSize.height ? `min(100%, calc(100vw * ${videoSize.height / videoSize.width}))` : "100%",
                    maxHeight: "100%",
                    maxWidth: "100%",
                    aspectRatio: videoSize.width && videoSize.height ? `${videoSize.width}/${videoSize.height}` : undefined,
                    boxShadow: "0 8px 24px rgba(0,0,0,0.15)",
                    background: "#000"
                  }}
                >
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
                      setVideoSize({
                        width: e.currentTarget.videoWidth,
                        height: e.currentTarget.videoHeight,
                      });
                    }}
                  />

                  {/* Canva Drag-Drop Interactive Live CSS Subtitle Overlay */}
                  {previewMode === "live" && activeSub && (
                    <div
                      className={`live-sub-overlay canva-box ${isDragging ? "is-dragging" : ""} ${selectedSub && selectedSub === activeSub ? "selected" : ""}`}
                      onMouseDown={(e) => {
                        handleMouseDown(e);
                        const idx = subtitles.indexOf(activeSub);
                        setSelectedSubIndex(idx >= 0 ? idx : 0);
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        const idx = subtitles.indexOf(activeSub);
                        setSelectedSubIndex(idx >= 0 ? idx : 0);
                      }}
                      style={{
                        fontFamily: options.font_name,
                        fontSize: `${Math.max(14, options.font_size)}px`,
                        color: options.font_color,
                        fontWeight: options.bold ? "bold" : "normal",
                        fontStyle: options.italic ? "italic" : "normal",
                        textTransform: options.uppercase ? "uppercase" : "none",
                        letterSpacing: `${options.spacing}px`,
                        left: options.position === "custom" ? `${options.pos_x}%` : undefined,
                        top: options.position === "custom" ? `${options.pos_y}%` : undefined,
                        transform: options.position === "custom" ? "none" : undefined,
                        WebkitPaintOrder: "stroke fill",
                        paintOrder: "stroke fill",
                        WebkitTextStroke: options.outline_width > 0 ? `${options.outline_width}px ${options.outline_color}` : "none",
                        textShadow: options.shadow_width > 0 
                          ? `${options.shadow_width}px ${options.shadow_width}px 4px ${options.shadow_color}` 
                          : (options.outline_width > 0 ? "none" : "0 2px 4px rgba(0,0,0,0.9)"),
                        backgroundColor: options.bg_enabled ? hexToRgba(options.bg_color, options.bg_opacity) : "transparent",
                        padding: options.bg_enabled ? "8px 16px" : "0 8px",
                        borderRadius: "8px",
                      } as any}
                      title="Kéo thả để di chuyển vị trí phụ đề"
                    >
                      <div className="canva-handle handle-tl" />
                      <div className="canva-handle handle-tr" />
                      <div className="canva-handle handle-bl" />
                      <div className="canva-handle handle-br" />
                      {options.uppercase ? activeSub.text.toUpperCase() : activeSub.text}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="canva-player-placeholder">
                <Film size={64} style={{ opacity: 0.5 }} />
                <p>Khu vực Canvas. Hãy tải video lên để bắt đầu.</p>
              </div>
            )}
          </div>

          {/* TIMELINE */}
          <div className="canva-timeline">
            <div className="timeline-controls">
              <span className="time-display">{formatTime(currentTime)}</span>
              <button className="btn-play-circle" onClick={togglePlay} disabled={!originalVideoUrl}>
                {isPlaying ? <Pause size={16} fill="currentColor" /> : <Play size={16} fill="currentColor" style={{ marginLeft: 2 }} />}
              </button>
              <span className="time-display">{formatTime(duration)}</span>
            </div>
            
            <div className="timeline-tracks-area">
              {/* Ruler */}
              <div className="timeline-ruler">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div key={i} className="ruler-mark-group">
                    <span className="ruler-label">| {i * 10} giây</span>
                    <div className="ruler-ticks">
                      <span />
                      <span />
                      <span />
                    </div>
                  </div>
                ))}
              </div>

              {/* Playhead line */}
              <div 
                className="canva-playhead" 
                style={{ left: duration > 0 ? `${(currentTime / duration) * 100}%` : "0%" }}
              >
                <div className="playhead-head">▼</div>
                <div className="playhead-line" />
              </div>
              
              <div className="timeline-tracks-container">
                {/* Track 1: Elements / Subtitles */}
                <div className="canva-track track-elements">
                  {subtitles.length > 0 ? (
                    <div className="subtitles-timeline-row">
                      {subtitles.map((sub, i) => {
                        const left = (sub.start_seconds / Math.max(duration, 1)) * 100;
                        const width = ((sub.end_seconds - sub.start_seconds) / Math.max(duration, 1)) * 100;
                        return (
                          <div 
                            key={i} 
                            className={`sub-block ${activeSub === sub ? "active" : ""} ${selectedSubIndex === i ? "selected" : ""}`}
                            style={{ left: `${left}%`, width: `${width}%` }}
                            onClick={() => {
                              handleSeek(sub.start_seconds);
                              setSelectedSubIndex(i);
                            }}
                            title={sub.text}
                          >
                            {sub.text}
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="track-empty-bar">
                      <Shapes size={14} />
                      <span>Thêm thành phần</span>
                    </div>
                  )}
                </div>

                {/* Track 2: Video / Media */}
                <div className="canva-track track-media">
                  {originalVideoUrl ? (
                    <div className="track-placeholder">
                      <span>🎬 Video Track ({formatTime(duration)})</span>
                    </div>
                  ) : (
                    <div className="track-empty-media">
                      <div className="plus-box">
                        <Plus size={16} />
                      </div>
                      <span>hoặc kéo và thả phương tiện</span>
                    </div>
                  )}
                </div>

                {/* Track 3: Audio */}
                <div className="canva-track track-audio">
                  <div className="track-empty-bar">
                    <Music size={14} />
                    <span>Thêm âm thanh</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
