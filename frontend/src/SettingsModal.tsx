import React, { useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Key,
  KeyRound,
  Layers,
  Loader2,
  Lock,
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
}

type TabKey = "overview" | "youtube" | "twitter" | "reddit" | "meta" | "facebook" | "tiktok" | "gemini" | "database" | "security";

export const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose }) => {
  const [activeTab, setActiveTab] = useState<TabKey>("overview");
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [loading, setLoading] = useState(false);
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
    mongodb_uri: "mongodb://localhost:27017",
  });

  const fetchStatus = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getCredentialStatus();
      setStatus(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Không thể tải trạng thái Vault.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchStatus();
      setError(null);
      setSuccessMsg(null);
    }
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
      setSuccessMsg("Đã khóa Vault thành công. Thông tin nhạy cảm đã được xóa khỏi bộ nhớ.");
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

    // Filter non-empty inputs
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

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-md animate-fade-in">
      <div className="relative w-full max-w-4xl max-h-[90vh] flex flex-col bg-slate-900/95 border border-slate-700/80 rounded-2xl shadow-2xl overflow-hidden text-slate-100">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-800 bg-slate-950/60">
          <div className="flex items-center gap-3">
            <div className="flex items-center justify-center w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 shadow-md">
              <KeyRound className="w-5 h-5 text-white" />
            </div>
            <div>
              <h2 className="text-lg font-bold text-white flex items-center gap-2">
                Cài Đặt API Keys & Bảo Mật Cục Bộ
                {status?.is_unlocked ? (
                  <span className="inline-flex items-center gap-1 px-2.5 py-0.5 text-xs font-semibold text-emerald-400 bg-emerald-950/80 border border-emerald-800/80 rounded-full">
                    <ShieldCheck className="w-3.5 h-3.5" /> Vault Đã Mở Khóa
                  </span>
                ) : status?.is_master_password_set ? (
                  <span className="inline-flex items-center gap-1 px-2.5 py-0.5 text-xs font-semibold text-amber-400 bg-amber-950/80 border border-amber-800/80 rounded-full">
                    <Lock className="w-3.5 h-3.5" /> Vault Đang Khóa
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 px-2.5 py-0.5 text-xs font-semibold text-blue-400 bg-blue-950/80 border border-blue-800/80 rounded-full">
                    <ShieldAlert className="w-3.5 h-3.5" /> Chưa Thiết Lập
                  </span>
                )}
              </h2>
              <p className="text-xs text-slate-400">
                Mã hóa AES-256-GCM lưu trên máy local. Không bao giờ tải lên Cloud.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {status?.is_unlocked && (
              <button
                type="button"
                onClick={handleLock}
                disabled={actionLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-amber-300 hover:text-amber-200 bg-amber-950/50 hover:bg-amber-900/60 border border-amber-800/60 rounded-lg transition-colors"
                title="Khóa lại và xóa API keys khỏi RAM"
              >
                <Lock className="w-3.5 h-3.5" /> Khóa Vault
              </button>
            )}
            <button
              type="button"
              onClick={fetchStatus}
              disabled={loading}
              className="p-2 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800 transition-colors"
              title="Làm mới trạng thái"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-2 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Notifications */}
        {error && (
          <div className="flex items-center gap-2 px-6 py-2.5 bg-rose-950/80 border-b border-rose-800 text-rose-300 text-sm animate-fade-in">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {successMsg && (
          <div className="flex items-center gap-2 px-6 py-2.5 bg-emerald-950/80 border-b border-emerald-800 text-emerald-300 text-sm animate-fade-in">
            <Check className="w-4 h-4 flex-shrink-0" />
            <span>{successMsg}</span>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 flex overflow-hidden">
          {/* Sidebar Tabs */}
          <div className="w-56 p-3 border-r border-slate-800 bg-slate-950/40 flex flex-col gap-1 overflow-y-auto">
            <button
              type="button"
              onClick={() => setActiveTab("overview")}
              className={`flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "overview"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <Layers className="w-4 h-4" /> Tổng quan nền tảng
            </button>

            <div className="my-1.5 border-t border-slate-800" />
            <div className="px-3 py-1 text-[10px] font-semibold tracking-wider text-slate-500 uppercase">
              Nền tảng & API
            </div>

            <button
              type="button"
              onClick={() => setActiveTab("youtube")}
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "youtube"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <span className="text-red-500 font-bold text-xs">▶</span> YouTube
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "twitter"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "reddit"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <span className="w-4 h-4 font-bold flex items-center justify-center text-orange-400 text-xs">🔴</span> Reddit
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "meta"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <span className="w-4 h-4 font-bold flex items-center justify-center text-pink-400 text-xs">📸</span> Instagram
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "facebook"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <span className="w-4 h-4 font-bold flex items-center justify-center text-blue-400 text-xs">📘</span> FB Page
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "tiktok"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <span className="w-4 h-4 font-bold flex items-center justify-center text-cyan-400 text-xs">🎵</span> TikTok API
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
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "gemini"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
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

            <button
              type="button"
              onClick={() => setActiveTab("database")}
              className={`flex items-center justify-between px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "database"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <span className="flex items-center gap-2.5">
                <Database className="w-4 h-4 text-emerald-400" /> MongoDB Local
              </span>
              <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
            </button>

            <div className="my-1.5 border-t border-slate-800" />
            <button
              type="button"
              onClick={() => setActiveTab("security")}
              className={`flex items-center gap-2.5 px-3 py-2 text-xs font-medium rounded-lg text-left transition-colors ${
                activeTab === "security"
                  ? "bg-indigo-600 text-white font-semibold shadow-sm"
                  : "text-slate-300 hover:bg-slate-800/80 hover:text-white"
              }`}
            >
              <Shield className="w-4 h-4 text-amber-400" /> Bảo mật Master
            </button>
          </div>

          {/* Main Content Area */}
          <div className="flex-1 p-6 overflow-y-auto">
            {/* Case 1: Master Password NOT Set */}
            {!status?.is_master_password_set ? (
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
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl">
                      <h4 className="text-sm font-semibold text-white mb-2 flex items-center gap-2">
                        <ShieldCheck className="w-4 h-4 text-emerald-400" /> Trạng thái API Keys Cục Bộ
                      </h4>
                      <p className="text-xs text-slate-400 mb-4">
                        Tất cả các API key bên dưới đều được lưu trên máy của bạn và mã hóa AES-256. Click vào từng tab ở cột trái để cập nhật.
                      </p>

                      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                        {[
                          { name: "YouTube API", key: "youtube", icon: "📺" },
                          { name: "X (Twitter)", key: "x_twitter", icon: "𝕏" },
                          { name: "Reddit API", key: "reddit", icon: "🔴" },
                          { name: "Instagram", key: "meta_instagram", icon: "📸" },
                          { name: "Facebook Page", key: "facebook_page", icon: "📘" },
                          { name: "TikTok API", key: "tiktok", icon: "🎵" },
                          { name: "Gemini AI", key: "gemini", icon: "✨" },
                          { name: "MongoDB Local", key: "mongodb", icon: "🍃" },
                        ].map((item) => {
                          const isConfigured = status.platforms[item.key as keyof typeof status.platforms];
                          return (
                            <div
                              key={item.key}
                              className="p-3 bg-slate-900/60 border border-slate-700/60 rounded-lg flex items-center justify-between"
                            >
                              <div className="flex items-center gap-2">
                                <span className="text-base">{item.icon}</span>
                                <span className="text-xs font-medium text-slate-200">{item.name}</span>
                              </div>
                              {isConfigured ? (
                                <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-400">
                                  <CheckCircle2 className="w-3.5 h-3.5" /> Bật
                                </span>
                              ) : (
                                <span className="text-[11px] text-slate-500 font-medium">Chưa có</span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: YOUTUBE */}
                {activeTab === "youtube" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                          <span className="text-red-500 font-bold text-sm">▶</span> Cấu hình YouTube Data API v3
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
                      <div className="flex items-center justify-between">
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
                        <span>🔴</span> Cấu hình Reddit OAuth App
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
                        <span>📸</span> Cấu hình Instagram Graph API
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
                        <span>📘</span> Cấu hình Facebook Page Graph API
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
                        <span>🎵</span> Cấu hình TikTok API
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
                      <div className="flex items-center justify-between">
                        <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                          <Sparkles className="w-4 h-4 text-purple-400" /> Cấu hình Google Gemini AI
                        </h4>
                        {status.configured_keys.gemini_api_key && (
                          <span className="text-xs text-emerald-400 font-mono">
                            Đã lưu: {status.masked_keys.gemini_api_key}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-400">
                        Lấy API Key từ Google AI Studio (aistudio.google.com) để dùng cho tính năng sinh phụ đề AI & insight.
                      </p>
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
                      </div>
                    </div>
                  </div>
                )}

                {/* TAB: DATABASE LOCAL */}
                {activeTab === "database" && (
                  <div className="space-y-4">
                    <div className="p-4 bg-slate-800/40 border border-slate-700/60 rounded-xl space-y-3">
                      <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                        <Database className="w-4 h-4 text-emerald-400" /> MongoDB Local URI
                      </h4>
                      <p className="text-xs text-slate-400">
                        Mặc định Content Bot kết nối tới <code>mongodb://localhost:27017</code> trên máy của bạn để lưu toàn bộ dữ liệu nội dung, bài viết, từ khóa cục bộ.
                      </p>
                      <div>
                        <label className="block text-xs font-semibold text-slate-300 mb-1">
                          MongoDB URI Cục Bộ
                        </label>
                        <input
                          type="text"
                          value={creds.mongodb_uri}
                          onChange={(e) => setCreds({ ...creds, mongodb_uri: e.target.value })}
                          placeholder="mongodb://localhost:27017"
                          className="w-full px-3 py-2 bg-slate-900/90 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 font-mono"
                        />
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
                    <p className="text-xs text-slate-400">
                      💡 Mẹo: Để trống ô nhập nếu muốn giữ nguyên giá trị đã lưu trước đó.
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
