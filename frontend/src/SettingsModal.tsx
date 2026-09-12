import React, { useEffect, useState } from "react";
import "./settings-modal.css";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ClipboardList,
  Camera,
  Eye,
  EyeOff,
  Globe2,
  Key,
  KeyRound,
  Layers,
  Lightbulb,
  Loader2,
  Lock,
  MessageCircle,
  Music2,
  RefreshCw,
  Save,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Unlock,
  Video,
  X,
} from "lucide-react";
import { api, CredentialStatus } from "./api";

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  initialTab?: TabKey;
}

type TabKey = "overview" | "youtube" | "twitter" | "reddit" | "meta" | "facebook" | "tiktok" | "gemini" | "security";

const TAB_COPY: Record<TabKey, { title: string; description: string }> = {
  overview: {
    title: "Tổng quan API",
    description: "Kiểm tra nguồn credential và trạng thái kết nối của từng nền tảng.",
  },
  youtube: {
    title: "YouTube API",
    description: "Quản lý khóa truy cập YouTube Data API v3.",
  },
  twitter: {
    title: "X (Twitter)",
    description: "Cấu hình Bearer Token cho X API v2.",
  },
  reddit: {
    title: "Reddit API",
    description: "Quản lý thông tin OAuth của Reddit script app.",
  },
  meta: {
    title: "Instagram",
    description: "Kết nối tài khoản chuyên nghiệp qua Meta Graph API.",
  },
  facebook: {
    title: "Facebook Page",
    description: "Thiết lập quyền truy cập và định danh Facebook Page.",
  },
  tiktok: {
    title: "TikTok API",
    description: "Quản lý Client Key và Client Secret của TikTok.",
  },
  gemini: {
    title: "Gemini AI",
    description: "Cấu hình model đa phương thức dùng để tạo phụ đề video.",
  },
  security: {
    title: "Bảo mật Vault",
    description: "Đổi Master Password và kiểm soát trạng thái khóa cục bộ.",
  },
};

export const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose, initialTab = "overview" }) => {
  const [activeTab, setActiveTab] = useState<TabKey>(initialTab);
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [loading, setLoading] = useState(isOpen);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Master password forms
  const [masterPassword, setMasterPassword] = useState("");
  const [confirmMasterPassword, setConfirmMasterPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  // Change password form
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmNewPassword, setConfirmNewPassword] = useState("");

  // Credentials form values
  const [creds, setCreds] = useState<Record<string, string>>({
    youtube_api_key: "",
    x_bearer_token: "",
    reddit_client_id: "",
    reddit_client_secret: "",
    reddit_user_agent: "ContentBot/0.1 (local research tool)",
    meta_access_token: "",
    instagram_professional_user_id: "",
    facebook_page_access_token: "",
    facebook_page_id: "",
    facebook_page_username: "",
    tiktok_client_key: "",
    tiktok_client_secret: "",
    tiktok_redirect_uri: "",
    gemini_api_key: "",
  });

  const fetchStatus = async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getCredentialStatus(signal);
      setStatus(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Không thể tải trạng thái Vault.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!isOpen) return undefined;
    const controller = new AbortController();
    void api
      .getCredentialStatus(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setStatus(data);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : "Không thể tải trạng thái Vault.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [isOpen]);

  const handleSetupPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!masterPassword || masterPassword.length < 6) {
      setError("Master password phải có ít nhất 6 ký tự.");
      return;
    }
    if (masterPassword !== confirmMasterPassword) {
      setError("Mật khẩu xác nhận không khớp.");
      return;
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await api.setupMasterPassword(masterPassword);
      setStatus(res.status);
      setSuccessMsg("Đã thiết lập Master Password và mở khóa Vault thành công!");
      setMasterPassword("");
      setConfirmMasterPassword("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi thiết lập Master Password.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleUnlock = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!masterPassword) {
      setError("Vui lòng nhập Master Password.");
      return;
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await api.unlockCredentials(masterPassword);
      setStatus(res.status);
      setSuccessMsg("Mở khóa Vault thành công!");
      setMasterPassword("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Mật khẩu không đúng.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleLock = async () => {
    setActionLoading(true);
    setError(null);
    try {
      const res = await api.lockCredentials();
      setStatus(res.status);
      setSuccessMsg("Đã khóa Vault. Credential từ Vault đã được xóa khỏi RAM; .env vẫn có thể tiếp tục được dùng.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi khóa Vault.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!oldPassword) {
      setError("Vui lòng nhập mật khẩu hiện tại.");
      return;
    }
    if (!newPassword || newPassword.length < 6) {
      setError("Mật khẩu mới phải có ít nhất 6 ký tự.");
      return;
    }
    if (newPassword !== confirmNewPassword) {
      setError("Mật khẩu mới xác nhận không khớp.");
      return;
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await api.changeMasterPassword(oldPassword, newPassword);
      setStatus(res.status);
      setSuccessMsg("Đã đổi Master Password thành công!");
      setOldPassword("");
      setNewPassword("");
      setConfirmNewPassword("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi đổi mật khẩu.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleSaveCredentials = async (e: React.FormEvent) => {
    e.preventDefault();
    setActionLoading(true);
    setError(null);
    setSuccessMsg(null);

    // Empty fields retain the existing value; use the remove action to delete one.
    const payload: Record<string, string> = {};
    for (const [k, v] of Object.entries(creds)) {
      if (v.trim() !== "") {
        payload[k] = v.trim();
      }
    }

    try {
      const res = await api.updateCredentials(payload);
      setStatus(res.status);
      setSuccessMsg("Đã lưu API Credentials vào Vault cục bộ (AES-256) thành công!");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi lưu credentials.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleRemoveCredential = async (key: string) => {
    if (!status?.is_unlocked || !status.vault_configured_keys[key]) return;
    if (!window.confirm(`Xóa credential ${key} khỏi Vault?`)) return;
    setActionLoading(true);
    setError(null);
    try {
      const res = await api.updateCredentials({ [key]: "" });
      setStatus(res.status);
      setCreds((current) => ({ ...current, [key]: "" }));
      setSuccessMsg(`Đã xóa ${key} khỏi Vault. Nếu .env có giá trị, ứng dụng sẽ dùng giá trị đó.`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi xóa credential.");
    } finally {
      setActionLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="cb-settings-overlay animate-fade-in">
      <div className="cb-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-modal-title">
        {/* Header */}
        <div className="cb-settings-header">
          <div className="cb-settings-brand">
            <div className="cb-settings-brand-mark" aria-hidden="true">
              <KeyRound className="w-5 h-5" />
            </div>
            <div className="cb-settings-heading-copy">
              <h2 id="settings-modal-title" className="cb-settings-title">
                API Keys & Bảo mật
                {status?.is_unlocked ? (
                  <span className="cb-settings-status is-success">
                    <ShieldCheck className="w-3.5 h-3.5" /> Vault Đã Mở Khóa
                  </span>
                ) : status?.is_master_password_set ? (
                  <span className="cb-settings-status is-warning">
                    <Lock className="w-3.5 h-3.5" /> Vault Đang Khóa
                  </span>
                ) : (
                  <span className="cb-settings-status is-neutral">
                    <ShieldAlert className="w-3.5 h-3.5" /> Chưa Thiết Lập
                  </span>
                )}
              </h2>
              <p className="cb-settings-subtitle">
                Credential được mã hóa AES-256-GCM và chỉ lưu trên máy này.
              </p>
            </div>
          </div>

          <div className="cb-settings-actions">
            {status?.is_unlocked && (
              <button
                type="button"
                onClick={handleLock}
                disabled={actionLoading}
                className="cb-settings-lock-button"
                title="Khóa lại và xóa API keys khỏi RAM"
              >
                <Lock className="w-3.5 h-3.5" /> Khóa Vault
              </button>
            )}
            <button
              type="button"
              onClick={() => void fetchStatus()}
              disabled={loading}
              className="cb-settings-icon-button"
              title="Làm mới trạng thái"
              aria-label="Làm mới trạng thái"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="cb-settings-icon-button"
              aria-label="Đóng cài đặt"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Notifications */}
        {error && (
          <div
            className="cb-settings-notice is-error animate-fade-in"
            role="alert"
          >
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span className="flex-1">{error}</span>
            <button
              type="button"
              onClick={() => void fetchStatus()}
              disabled={loading}
              className="cb-settings-notice-action"
            >
              Thử lại
            </button>
          </div>
        )}
        {successMsg && (
          <div className="cb-settings-notice is-success animate-fade-in" role="status">
            <Check className="w-4 h-4 flex-shrink-0" />
            <span>{successMsg}</span>
          </div>
        )}

        {/* Body */}
        <div className="cb-settings-body">
          {/* Sidebar Tabs */}
          <nav className="cb-settings-sidebar" aria-label="Nhóm cài đặt">
            <button
              type="button"
              onClick={() => setActiveTab("overview")}
              className={`cb-settings-tab ${activeTab === "overview" ? "is-active" : ""}`}
              aria-current={activeTab === "overview" ? "page" : undefined}
            >
              <Layers className="w-4 h-4" /> Tổng quan nền tảng
            </button>

            <div className="cb-settings-sidebar-rule" />
            <div className="cb-settings-sidebar-label">
              Nền tảng & API
            </div>

            <button
              type="button"
              onClick={() => setActiveTab("youtube")}
              className={`cb-settings-tab ${activeTab === "youtube" ? "is-active" : ""}`}
              aria-current={activeTab === "youtube" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <Video className="w-4 h-4" /> YouTube
              </span>
              {status?.platforms.youtube ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("twitter")}
              className={`cb-settings-tab ${activeTab === "twitter" ? "is-active" : ""}`}
              aria-current={activeTab === "twitter" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <span className="w-4 h-4 font-bold flex items-center justify-center text-xs">𝕏</span> X (Twitter)
              </span>
              {status?.platforms.x_twitter ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("reddit")}
              className={`cb-settings-tab ${activeTab === "reddit" ? "is-active" : ""}`}
              aria-current={activeTab === "reddit" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <MessageCircle className="w-4 h-4" /> Reddit
              </span>
              {status?.platforms.reddit ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("meta")}
              className={`cb-settings-tab ${activeTab === "meta" ? "is-active" : ""}`}
              aria-current={activeTab === "meta" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <Camera className="w-4 h-4" /> Instagram
              </span>
              {status?.platforms.meta_instagram ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("facebook")}
              className={`cb-settings-tab ${activeTab === "facebook" ? "is-active" : ""}`}
              aria-current={activeTab === "facebook" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <Globe2 className="w-4 h-4" /> FB Page
              </span>
              {status?.platforms.facebook_page ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("tiktok")}
              className={`cb-settings-tab ${activeTab === "tiktok" ? "is-active" : ""}`}
              aria-current={activeTab === "tiktok" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <Music2 className="w-4 h-4" /> TikTok API
              </span>
              {status?.platforms.tiktok ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("gemini")}
              className={`cb-settings-tab ${activeTab === "gemini" ? "is-active" : ""}`}
              aria-current={activeTab === "gemini" ? "page" : undefined}
            >
              <span className="flex items-center gap-2.5">
                <Sparkles className="w-4 h-4 text-purple-400" /> Gemini AI
              </span>
              {status?.platforms.gemini ? (
                <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              ) : (
                <span className="w-2 h-2 rounded-full bg-slate-600"></span>
              )}
            </button>

            <div className="cb-settings-sidebar-rule" />
            <button
              type="button"
              onClick={() => setActiveTab("security")}
              className={`cb-settings-tab ${activeTab === "security" ? "is-active" : ""}`}
              aria-current={activeTab === "security" ? "page" : undefined}
            >
              <Shield className="w-4 h-4 text-amber-400" /> Bảo mật Master
            </button>
          </nav>

          {/* Main Content Area */}
          <div className="cb-settings-content">
            <header className="cb-settings-section-head">
              <h3>{TAB_COPY[activeTab].title}</h3>
              <p>{TAB_COPY[activeTab].description}</p>
            </header>
            {/* Status is required before showing a credential form. */}
            {status === null ? (
              <div className="max-w-lg mx-auto py-12">
                <div className="p-6 bg-slate-800/60 border border-slate-700/80 rounded-2xl text-center space-y-3">
                  {loading ? (
                    <Loader2 className="w-8 h-8 mx-auto animate-spin text-indigo-400" />
                  ) : (
                    <AlertCircle className="w-8 h-8 mx-auto text-rose-400" />
                  )}
                  <h3 className="text-base font-bold text-white">
                    {loading ? "Đang tải trạng thái Vault…" : "Chưa tải được trạng thái Vault"}
                  </h3>
                  <p className="text-xs text-slate-400">
                    {loading
                      ? "Đang kiểm tra cấu hình Gemini và các credential cục bộ."
                      : "Hãy kiểm tra backend đang chạy, sau đó nhấn Thử lại ở thông báo phía trên."}
                  </p>
                </div>
              </div>
            ) : !status.is_master_password_set ? (
              <div className="max-w-lg mx-auto py-8">
                <div className="p-6 bg-slate-800/60 border border-slate-700/80 rounded-2xl shadow-xl text-center space-y-4">
                  <div className="inline-flex p-3 rounded-full bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">
                    <ShieldCheck className="w-8 h-8" />
                  </div>
                  <div>
                    <h3 className="text-base font-bold text-white">Thiết Lập Master Password Cho Vault Cục Bộ</h3>
                    <p className="text-xs text-slate-400 mt-1">
                      Để bảo vệ các API keys (YouTube, X, Reddit...) trên máy của bạn, hãy tạo một Master Password.
                      Dữ liệu sẽ được mã hóa chuẩn quân đội AES-256-GCM.
                    </p>
                  </div>

                  <form onSubmit={handleSetupPassword} className="space-y-3.5 text-left pt-2">
                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">
                        Master Password (tối thiểu 6 ký tự)
                      </label>
                      <div className="relative">
                        <input
                          type={showPassword ? "text" : "password"}
                          value={masterPassword}
                          onChange={(e) => setMasterPassword(e.target.value)}
                          placeholder="Nhập Master Password..."
                          required
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-3 top-2.5 text-slate-400 hover:text-slate-200"
                        >
                          {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                        </button>
                      </div>
                    </div>

                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">
                        Xác nhận Master Password
                      </label>
                      <input
                        type={showPassword ? "text" : "password"}
                        value={confirmMasterPassword}
                        onChange={(e) => setConfirmMasterPassword(e.target.value)}
                        placeholder="Nhập lại Master Password..."
                        required
                        className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                      />
                    </div>

                    <button
                      type="submit"
                      disabled={actionLoading}
                      className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white text-sm font-semibold rounded-lg shadow-md transition-all disabled:opacity-50"
                    >
                      {actionLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Key className="w-4 h-4" />}
                      Tạo Vault & Mở Khóa
                    </button>
                  </form>
                </div>
              </div>
            ) : !status.is_unlocked ? (
              /* Case 2: Master Password Set, but LOCKED */
              <div className="max-w-lg mx-auto py-8">
                <div className="p-6 bg-slate-800/60 border border-slate-700/80 rounded-2xl shadow-xl text-center space-y-4">
                  <div className="inline-flex p-3 rounded-full bg-amber-500/20 text-amber-400 border border-amber-500/30">
                    <Lock className="w-8 h-8" />
                  </div>
                  <div>
                    <h3 className="text-base font-bold text-white">Vault Đang Được Khóa An Toàn</h3>
                    <p className="text-xs text-slate-400 mt-1">
                      Nhập Master Password để giải mã và quản lý API keys của bạn.
                    </p>
                  </div>

                  <form onSubmit={handleUnlock} className="space-y-3.5 text-left pt-2">
                    <div>
                      <label className="block text-xs font-semibold text-slate-300 mb-1">
                        Master Password
                      </label>
                      <div className="relative">
                        <input
                          type={showPassword ? "text" : "password"}
                          value={masterPassword}
                          onChange={(e) => setMasterPassword(e.target.value)}
                          placeholder="Nhập Master Password..."
                          required
                          autoFocus
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-3 top-2.5 text-slate-400 hover:text-slate-200"
                        >
                          {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                        </button>
                      </div>
                    </div>

                    <button
                      type="submit"
                      disabled={actionLoading}
                      className="w-full flex items-center justify-center gap-2 py-2.5 px-4 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white text-sm font-semibold rounded-lg shadow-md transition-all disabled:opacity-50"
                    >
                      {actionLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Unlock className="w-4 h-4" />}
                      Mở Khóa Vault
                    </button>
                  </form>
                </div>
              </div>
            ) : (
              /* Case 3: UNLOCKED — Active Settings Form */
              <form onSubmit={handleSaveCredentials} className="space-y-6">
                {/* TAB: OVERVIEW */}
                {activeTab === "overview" && (
                  <div className="space-y-4">
                    <div className="cb-settings-overview-card p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl">
                      <h4 className="text-sm font-semibold text-white mb-2 flex items-center gap-2">
                        <ShieldCheck className="w-4 h-4 text-emerald-400" /> Trạng thái API Keys Cục Bộ
                      </h4>
                      <p className="text-xs text-slate-400 mb-4">
                        Tất cả các API key bên dưới đều được lưu trên máy của bạn và mã hóa AES-256. Click vào từng tab ở cột trái để cập nhật.
                      </p>

                      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                        {[
                           { name: "YouTube API", key: "youtube", credentialKey: "youtube_api_key", icon: <Video className="w-4 h-4" /> },
                           { name: "X (Twitter)", key: "x_twitter", credentialKey: "x_bearer_token", icon: <span className="font-bold">𝕏</span> },
                           { name: "Reddit API", key: "reddit", credentialKey: "reddit_client_id", icon: <MessageCircle className="w-4 h-4" /> },
                           { name: "Instagram", key: "meta_instagram", credentialKey: "meta_access_token", icon: <Camera className="w-4 h-4" /> },
                           { name: "Facebook Page", key: "facebook_page", credentialKey: "facebook_page_access_token", icon: <Globe2 className="w-4 h-4" /> },
                           { name: "TikTok API", key: "tiktok", credentialKey: "tiktok_client_key", icon: <Music2 className="w-4 h-4" /> },
                           { name: "Gemini AI", key: "gemini", credentialKey: "gemini_api_key", icon: <Sparkles className="w-4 h-4" /> },
                        ].map((item) => {
                          const isConfigured = status.platforms[item.key as keyof typeof status.platforms];
                          return (
                            <div
                              key={item.key}
                              className="cb-settings-platform-row p-3 bg-slate-900/60 border border-slate-700/60 rounded-lg flex items-center justify-between"
                            >
                              <div className="flex items-center gap-2">
                                <span className="cb-settings-platform-icon">{item.icon}</span>
                                <span className="text-xs font-medium text-slate-200">{item.name}</span>
                              </div>
                              {isConfigured ? (
                                <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-400">
                                  <CheckCircle2 className="w-3.5 h-3.5" />
                                  {status.credential_sources[item.credentialKey] === "vault" ? "Vault" : ".env"}
                                </span>
                              ) : (
                                <span className="text-[11px] text-slate-500 font-medium">Chưa có</span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      <div className="mt-4 border-t border-slate-700/60 pt-4 space-y-2">
                        <p className="text-xs font-semibold text-slate-300">Nguồn và thao tác credential</p>
                        {Object.entries(status.configured_keys).map(([key, isConfigured]) => (
                          <div key={key} className="cb-settings-credential-row flex items-center justify-between gap-3 rounded-lg bg-slate-900/50 px-3 py-2">
                            <div className="min-w-0">
                              <p className="truncate text-xs text-slate-200">{key}</p>
                              <p className="text-[11px] text-slate-500">
                                {isConfigured
                                  ? status.credential_sources[key] === "vault"
                                    ? status.masked_keys[key] || "Vault"
                                    : ".env"
                                  : "Chưa cấu hình"}
                              </p>
                            </div>
                            <button
                              type="button"
                              onClick={() => void handleRemoveCredential(key)}
                              disabled={actionLoading || !status.vault_configured_keys[key]}
                              className="shrink-0 rounded-md p-1.5 text-slate-500 hover:bg-rose-950/60 hover:text-rose-300 disabled:cursor-not-allowed disabled:opacity-30"
                              title="Xóa credential khỏi Vault"
                              aria-label={`Xóa ${key} khỏi Vault`}
                            >
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: YOUTUBE */}
                {activeTab === "youtube" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <div className="cb-settings-card-head flex items-center justify-between">
                        <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                          <Video className="w-4 h-4" /> Cấu hình YouTube Data API v3
                        </h4>
                        {status.configured_keys.youtube_api_key && (
                          <span className="text-xs text-emerald-400 font-mono">
                            Đã lưu: {status.masked_keys.youtube_api_key}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-400">
                        Lấy API Key từ Google Cloud Console (bật dịch vụ <i>YouTube Data API v3</i>).
                      </p>
                      <div>
                        <label className="block text-xs font-semibold text-slate-300 mb-1">
                          YouTube API Key
                        </label>
                        <input
                          type="password"
                          value={creds.youtube_api_key}
                          onChange={(e) => setCreds({ ...creds, youtube_api_key: e.target.value })}
                          placeholder={status.masked_keys.youtube_api_key || "AIzaSy..."}
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                        />
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: X (TWITTER) */}
                {activeTab === "twitter" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <div className="cb-settings-card-head flex items-center justify-between">
                        <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                          <span className="font-bold text-sm">𝕏</span> Cấu hình X (Twitter) API v2
                        </h4>
                        {status.configured_keys.x_bearer_token && (
                          <span className="text-xs text-emerald-400 font-mono">
                            Đã lưu: {status.masked_keys.x_bearer_token}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-400">
                        Lấy Bearer Token từ X Developer Portal (Project & App Settings).
                      </p>
                      <div>
                        <label className="block text-xs font-semibold text-slate-300 mb-1">
                          X Bearer Token
                        </label>
                        <input
                          type="password"
                          value={creds.x_bearer_token}
                          onChange={(e) => setCreds({ ...creds, x_bearer_token: e.target.value })}
                          placeholder={status.masked_keys.x_bearer_token || "AAAA..."}
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                        />
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: REDDIT */}
                {activeTab === "reddit" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <MessageCircle className="w-4 h-4" /> Cấu hình Reddit OAuth App
                      </h4>
                      <p className="text-xs text-slate-400">
                        Tạo app dạng <i>script</i> tại reddit.com/prefs/apps để lấy Client ID và Secret.
                      </p>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Reddit Client ID
                          </label>
                          <input
                            type="text"
                            value={creds.reddit_client_id}
                            onChange={(e) => setCreds({ ...creds, reddit_client_id: e.target.value })}
                            placeholder={status.masked_keys.reddit_client_id || "Client ID..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Reddit Client Secret
                          </label>
                          <input
                            type="password"
                            value={creds.reddit_client_secret}
                            onChange={(e) => setCreds({ ...creds, reddit_client_secret: e.target.value })}
                            placeholder={status.masked_keys.reddit_client_secret || "Secret..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: INSTAGRAM / META */}
                {activeTab === "meta" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <Camera className="w-4 h-4" /> Cấu hình Instagram Graph API
                      </h4>
                      <div className="space-y-3">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Meta User Access Token
                          </label>
                          <input
                            type="password"
                            value={creds.meta_access_token}
                            onChange={(e) => setCreds({ ...creds, meta_access_token: e.target.value })}
                            placeholder={status.masked_keys.meta_access_token || "EAA..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Instagram Professional User ID
                          </label>
                          <input
                            type="text"
                            value={creds.instagram_professional_user_id}
                            onChange={(e) => setCreds({ ...creds, instagram_professional_user_id: e.target.value })}
                            placeholder={status.masked_keys.instagram_professional_user_id || "178414..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: FACEBOOK */}
                {activeTab === "facebook" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <Globe2 className="w-4 h-4" /> Cấu hình Facebook Page Graph API
                      </h4>
                      <div className="space-y-3">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Facebook Page Access Token
                          </label>
                          <input
                            type="password"
                            value={creds.facebook_page_access_token}
                            onChange={(e) => setCreds({ ...creds, facebook_page_access_token: e.target.value })}
                            placeholder={status.masked_keys.facebook_page_access_token || "EAA..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                          <div>
                            <label className="block text-xs font-semibold text-slate-300 mb-1">
                              Facebook Page ID
                            </label>
                            <input
                              type="text"
                              value={creds.facebook_page_id}
                              onChange={(e) => setCreds({ ...creds, facebook_page_id: e.target.value })}
                              placeholder={status.masked_keys.facebook_page_id || "100234..."}
                              className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                            />
                          </div>
                          <div>
                            <label className="block text-xs font-semibold text-slate-300 mb-1">
                              Facebook Page Username
                            </label>
                            <input
                              type="text"
                              value={creds.facebook_page_username}
                              onChange={(e) => setCreds({ ...creds, facebook_page_username: e.target.value })}
                              placeholder="my_fanpage"
                              className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                            />
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: TIKTOK */}
                {activeTab === "tiktok" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <Music2 className="w-4 h-4" /> Cấu hình TikTok API
                      </h4>
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            TikTok Client Key
                          </label>
                          <input
                            type="text"
                            value={creds.tiktok_client_key}
                            onChange={(e) => setCreds({ ...creds, tiktok_client_key: e.target.value })}
                            placeholder={status.masked_keys.tiktok_client_key || "aw..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            TikTok Client Secret
                          </label>
                          <input
                            type="password"
                            value={creds.tiktok_client_secret}
                            onChange={(e) => setCreds({ ...creds, tiktok_client_secret: e.target.value })}
                            placeholder={status.masked_keys.tiktok_client_secret || "Secret..."}
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: GEMINI AI */}
                {activeTab === "gemini" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <div className="cb-settings-card-head flex items-center justify-between">
                        <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                          <Sparkles className="w-4 h-4 text-purple-400" /> Google Gemini AI — Tạo phụ đề tự động
                        </h4>
                        {status.configured_keys.gemini_api_key && (
                          <span className="text-xs text-emerald-400 font-mono">
                            Đã lưu: {status.masked_keys.gemini_api_key}
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-slate-400 space-y-1">
                        <p>
                          Dùng <strong className="text-slate-200">model Gemini đã chọn trong Subtitle Studio</strong> để xem video và tạo phụ đề tiếng Việt — <strong className="text-emerald-400">không cần cài thêm phần mềm</strong>.
                        </p>
                        <p>
                          Hạn mức phụ thuộc project và model. Ứng dụng sẽ hiển thị riêng lỗi quota, dịch vụ, timeout và kết nối.
                        </p>
                      </div>
                      <div className="text-xs text-slate-300 space-y-1 bg-slate-900/50 rounded-lg p-3">
                        <p className="font-semibold text-slate-200 mb-1 flex items-center gap-2">
                          <ClipboardList className="w-4 h-4" /> Cách lấy API key miễn phí:
                        </p>
                        <ol className="list-decimal list-inside space-y-0.5 text-slate-400">
                          <li>Vào <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer" className="text-indigo-400 hover:text-indigo-300 underline">aistudio.google.com/apikey</a></li>
                          <li>Nhấn <strong className="text-slate-300">Create API key</strong> → chọn project</li>
                          <li>Copy key dán vào ô bên dưới → nhấn Lưu</li>
                        </ol>
                      </div>
                      <div>
                        <label className="block text-xs font-semibold text-slate-300 mb-1">
                          Gemini API Key
                        </label>
                        <input
                          type="password"
                          value={creds.gemini_api_key}
                          onChange={(e) => setCreds({ ...creds, gemini_api_key: e.target.value })}
                          placeholder={status.masked_keys.gemini_api_key || "AIzaSy..."}
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                        />
                        <p className="text-xs text-slate-500 mt-1">
                          Key được lưu mã hóa trong Credential Vault trên máy bạn, không gửi đi đâu.
                        </p>
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: SECURITY / CHANGE PASSWORD */}
                {activeTab === "security" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <Key className="w-4 h-4 text-amber-400" /> Đổi Master Password Vault
                      </h4>
                      <form onSubmit={handleChangePassword} className="space-y-3">
                        <div>
                          <label className="block text-xs font-semibold text-slate-300 mb-1">
                            Mật khẩu hiện tại
                          </label>
                          <input
                            type="password"
                            value={oldPassword}
                            onChange={(e) => setOldPassword(e.target.value)}
                            required
                            className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                          />
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                          <div>
                            <label className="block text-xs font-semibold text-slate-300 mb-1">
                              Mật khẩu mới
                            </label>
                            <input
                              type="password"
                              value={newPassword}
                              onChange={(e) => setNewPassword(e.target.value)}
                              required
                              className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                            />
                          </div>
                          <div>
                            <label className="block text-xs font-semibold text-slate-300 mb-1">
                              Xác nhận mật khẩu mới
                            </label>
                            <input
                              type="password"
                              value={confirmNewPassword}
                              onChange={(e) => setConfirmNewPassword(e.target.value)}
                              required
                              className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500"
                            />
                          </div>
                        </div>
                        <button
                          type="submit"
                          disabled={actionLoading}
                          className="px-4 py-2 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded-lg shadow-md transition-colors"
                        >
                          Cập nhật Master Password
                        </button>
                      </form>
                    </div>
                  </div>
                )}

                {/* Save button footer if not on security tab */}
                {activeTab !== "security" && (
                  <div className="flex items-center justify-between pt-4 border-t border-slate-800">
                    <p className="text-xs text-slate-400 flex items-center gap-2">
                      <Lightbulb className="w-4 h-4" /> Mẹo: Để trống ô nhập nếu muốn giữ nguyên giá trị đã lưu trước đó.
                    </p>
                    <button
                      type="submit"
                      disabled={actionLoading}
                      className="flex items-center gap-2 px-5 py-2.5 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white text-sm font-semibold rounded-xl shadow-lg transition-all disabled:opacity-50"
                    >
                      {actionLoading ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Save className="w-4 h-4" />
                      )}
                      Lưu Vào Vault Cục Bộ
                    </button>
                  </div>
                )}
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
