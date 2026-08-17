import { useState } from "react";
import {
  Download,
  ExternalLink,
  Info,
  RefreshCw,
  Sparkles,
  Terminal,
  X,
} from "lucide-react";
import { UpdateCheckResponse } from "./api";

interface UpdateModalProps {
  open: boolean;
  onClose: () => void;
  updateInfo: UpdateCheckResponse | null;
}

export function UpdateModal({ open, onClose, updateInfo }: UpdateModalProps) {
  const [copied, setCopied] = useState(false);

  if (!open || !updateInfo) return null;

  const copyCommand = () => {
    navigator.clipboard.writeText("powershell -ExecutionPolicy Bypass -File .\\scripts\\launcher.ps1 -Action backend");
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="relative w-full max-w-lg bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-800 bg-slate-900/50">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-500/10 border border-indigo-500/20 rounded-xl text-indigo-400">
              <Sparkles className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-white">
                Bản Cập Nhật Mới: v{updateInfo.latest_version}
              </h3>
              <p className="text-xs text-slate-400">
                Phiên bản hiện tại của bạn: <span className="text-slate-300 font-mono">v{updateInfo.current_version}</span>
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="p-6 overflow-y-auto space-y-4 text-sm text-slate-300">
          {updateInfo.mandatory && (
            <div className="flex items-start gap-3 p-3 bg-amber-500/10 border border-amber-500/20 rounded-xl text-amber-300 text-xs">
              <Info className="w-4 h-4 mt-0.5 shrink-0" />
              <div>
                <strong>Bản cập nhật quan trọng:</strong> Phiên bản này chứa các bản vá bảo mật hoặc thay đổi cấu trúc dữ liệu bắt buộc.
              </div>
            </div>
          )}

          {updateInfo.changelog && (
            <div>
              <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">
                Nội dung cập nhật
              </h4>
              <div className="p-3.5 bg-slate-950/60 border border-slate-800 rounded-xl font-mono text-xs whitespace-pre-wrap text-slate-300 max-h-48 overflow-y-auto">
                {updateInfo.changelog}
              </div>
            </div>
          )}

          <div className="p-4 bg-indigo-950/30 border border-indigo-500/20 rounded-xl space-y-2">
            <div className="flex items-center gap-2 text-indigo-300 font-medium text-xs">
              <Terminal className="w-4 h-4" /> Cách cập nhật tự động:
            </div>
            <p className="text-xs text-slate-400">
              Hệ thống sử dụng cơ chế tự động tải và cập nhật mã nguồn trực tiếp từ Cloudflare R2 khi khởi động app. Bạn chỉ cần tắt app và chạy lại qua launcher:
            </p>
            <div className="flex items-center justify-between gap-2 p-2 bg-slate-950/80 rounded-lg border border-slate-800">
              <code className="text-xs text-indigo-300 font-mono select-all">
                .\scripts\launcher.ps1
              </code>
              <button
                type="button"
                onClick={copyCommand}
                className="text-[11px] px-2 py-1 bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-300 rounded border border-indigo-500/30 transition-colors"
              >
                {copied ? "Đã copy!" : "Copy lệnh"}
              </button>
            </div>
          </div>

          {updateInfo.download_url && (
            <div className="text-xs text-slate-500 flex items-center justify-between pt-2">
              <span>Gói cài đặt: Cloudflare R2 ({updateInfo.file_size ? `${(updateInfo.file_size / (1024 * 1024)).toFixed(1)} MB` : "Zip package"})</span>
              <a
                href={updateInfo.download_url}
                target="_blank"
                rel="noreferrer"
                className="text-indigo-400 hover:underline flex items-center gap-1"
              >
                <Download className="w-3 h-3" /> Tải file zip thủ công
              </a>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-3 border-t border-slate-800 bg-slate-900/50">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white text-xs font-semibold rounded-xl transition-colors"
          >
            Đóng
          </button>
        </div>
      </div>
    </div>
  );
}

export function UpdateBanner({
  updateInfo,
  onOpenModal,
  onDismiss,
}: {
  updateInfo: UpdateCheckResponse;
  onOpenModal: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "8px 16px",
        background: "linear-gradient(90deg, rgba(79, 70, 229, 0.95), rgba(124, 58, 237, 0.95))",
        color: "#ffffff",
        fontSize: "13px",
        fontWeight: 500,
        boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
        zIndex: 40,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "2px 8px",
            background: "rgba(255, 255, 255, 0.2)",
            borderRadius: "9999px",
            fontSize: "11px",
            fontWeight: 700,
            letterSpacing: "0.05em",
          }}
        >
          UPDATE
        </span>
        <span>
          Phiên bản mới <strong>v{updateInfo.latest_version}</strong> đã sẵn sàng! (Hiện tại: v{updateInfo.current_version})
        </span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <button
          type="button"
          onClick={onOpenModal}
          style={{
            padding: "4px 12px",
            background: "#ffffff",
            color: "#4f46e5",
            border: "none",
            borderRadius: "6px",
            fontSize: "12px",
            fontWeight: 600,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "4px",
          }}
        >
          <Sparkles size={13} /> Xem chi tiết &amp; Hướng dẫn
        </button>
        {!updateInfo.mandatory && (
          <button
            type="button"
            onClick={onDismiss}
            style={{
              background: "transparent",
              border: "none",
              color: "rgba(255, 255, 255, 0.8)",
              cursor: "pointer",
              padding: "4px",
              display: "flex",
              alignItems: "center",
            }}
            title="Tắt thông báo"
          >
            <X size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
