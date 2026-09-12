import { Download, Film, Home, LoaderCircle, Moon, PanelLeft, Redo2, Square, Sun, Undo2 } from "lucide-react";
import type { SubtitleRenderJob } from "../api";
type Props = {
  onBack: (() => void) | undefined;
  sidebarOpen: boolean;
  setSidebarOpen: (update: (value: boolean) => boolean) => void;
  projectName: string;
  canUndo: boolean;
  canRedo: boolean;
  undo: () => void;
  redo: () => void;
  rendering: boolean;
  renderJob: SubtitleRenderJob | null;
  downloadMessage: string | null;
  renderedVideoUrl: string | null;
  downloadingVideo: boolean;
  handleDownloadVideo: () => Promise<void>;
  videoId: string | null;
  toggleTheme: () => void;
  theme: "dark" | "light";
  canRender: boolean;
  alignmentRunning: boolean;
  handleCancelRender: () => Promise<void>;
  handleRender: () => Promise<void>;
  overlayUploading: boolean;
  overlayNeedsUpload: boolean;
};

export function StudioToolbar({ onBack, sidebarOpen, setSidebarOpen, projectName, canUndo, canRedo, undo, redo, rendering, renderJob, downloadMessage, renderedVideoUrl, downloadingVideo, handleDownloadVideo, videoId, toggleTheme, theme, canRender, alignmentRunning, handleCancelRender, handleRender, overlayUploading, overlayNeedsUpload }: Props) {
  return (
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
        {!rendering && downloadMessage && <span>{downloadMessage}</span>}
      </div>
      <div className="subtitle-studio-header-actions">
        {renderedVideoUrl && (
          window.contentBotDesktop ? (
            <button
              type="button"
              className="studio-header-action is-secondary"
              disabled={downloadingVideo}
              onClick={() => void handleDownloadVideo()}
            >
              {downloadingVideo ? <LoaderCircle className="spin" size={17} /> : <Download size={17} />}
              <span>{downloadingVideo ? "Đang tải…" : "Tải video"}</span>
            </button>
          ) : (
            <a
              className="studio-header-action is-secondary"
              href={renderedVideoUrl}
              download={renderJob?.result?.output_filename ?? `subtitled_${videoId}.mp4`}
            >
              <Download size={17} /> <span>Tải video</span>
            </a>
          )
        )}
        <button
          type="button"
          className="studio-header-icon is-theme-toggle"
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối (Dark Studio)"}
          title={theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối (Dark Studio)"}
        >
          {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        </button>
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
                  ? "Cần video và ít nhất một cue, ảnh phủ hoặc thay đổi cắt video trước khi xuất"
                  : "Xuất video có phụ đề và ảnh phủ"
          }
        >
          {rendering ? <Square size={14} fill="currentColor" /> : <Film size={17} />}
          <span>{rendering ? "Hủy xuất" : "Xuất video"}</span>
        </button>
      </div>
    </header>
  );
}
