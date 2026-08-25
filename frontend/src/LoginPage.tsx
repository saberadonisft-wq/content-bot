import React, { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Lock,
  Mail,
  ShieldCheck,
  User,
  Loader2,
} from "lucide-react";
import { useAuth } from "./useAuth";

export const LoginPage: React.FC = () => {
  const { login, register, startGoogleLogin, error: contextError } = useAuth();
  const [tab, setTab] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    setSuccessMsg(null);
    setLoading(true);

    try {
      if (tab === "login") {
        await login(email, password);
      } else {
        if (!displayName.trim()) {
          setLocalError("Vui lòng nhập tên hiển thị.");
          setLoading(false);
          return;
        }
        const msg = await register(email, password, displayName);
        setSuccessMsg(msg);
        // Switch to login tab after registration
        setTab("login");
        setPassword("");
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Đã có lỗi xảy ra.";
      setLocalError(msg);
    } finally {
      setLoading(false);
    }
  };

  const displayError = localError || contextError;

  return (
    <div className="auth-container">
      <div className="auth-card">
        <div className="auth-header">
          <div className="auth-logo-badge">
            <ShieldCheck size={28} />
          </div>
          <h1 className="auth-title">Content Bot</h1>
          <p className="auth-subtitle">
            Hệ thống Social Listening & Video Subtitle Studio
          </p>
        </div>

        <div className="auth-tabs">
          <button
            type="button"
            className={`auth-tab-btn ${tab === "login" ? "active" : ""}`}
            onClick={() => {
              setTab("login");
              setLocalError(null);
              setSuccessMsg(null);
            }}
          >
            Đăng nhập
          </button>
          <button
            type="button"
            className={`auth-tab-btn ${tab === "register" ? "active" : ""}`}
            onClick={() => {
              setTab("register");
              setLocalError(null);
              setSuccessMsg(null);
            }}
          >
            Đăng ký
          </button>
        </div>

        {displayError && (
          <div className="auth-alert auth-alert-error">
            <AlertCircle size={18} style={{ flexShrink: 0, marginTop: 1 }} />
            <div>{displayError}</div>
          </div>
        )}

        {successMsg && (
          <div className="auth-alert auth-alert-success">
            <CheckCircle2 size={18} style={{ flexShrink: 0, marginTop: 1 }} />
            <div>{successMsg}</div>
          </div>
        )}

        <form className="auth-form" onSubmit={handleSubmit}>
          {tab === "register" && (
            <div className="auth-field">
              <label className="auth-label">Tên hiển thị</label>
              <div className="auth-input-wrapper">
                <span className="auth-input-icon">
                  <User size={16} />
                </span>
                <input
                  type="text"
                  className="auth-input"
                  placeholder="Ví dụ: Nguyễn Văn A"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  required
                />
              </div>
            </div>
          )}

          <div className="auth-field">
            <label className="auth-label">Địa chỉ Email</label>
            <div className="auth-input-wrapper">
              <span className="auth-input-icon">
                <Mail size={16} />
              </span>
              <input
                type="email"
                className="auth-input"
                placeholder="name@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
          </div>

          <div className="auth-field">
            <label className="auth-label">Mật khẩu</label>
            <div className="auth-input-wrapper">
              <span className="auth-input-icon">
                <Lock size={16} />
              </span>
              <input
                type="password"
                className="auth-input"
                placeholder={
                  tab === "register"
                    ? "Tối thiểu 8 ký tự"
                    : "Nhập mật khẩu của bạn"
                }
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={tab === "register" ? 8 : undefined}
              />
            </div>
          </div>

          <button
            type="submit"
            className="auth-submit-btn"
            disabled={loading}
          >
            {loading ? (
              <>
                <Loader2 size={18} className="animate-spin" />
                Đang xử lý...
              </>
            ) : tab === "login" ? (
              "Đăng nhập vào hệ thống"
            ) : (
              "Tạo tài khoản mới"
            )}
          </button>
        </form>

        <div className="auth-divider">
          <span className="auth-divider-text">hoặc</span>
        </div>

        <button
          type="button"
          className="auth-google-btn"
          onClick={startGoogleLogin}
        >
          <svg width="18" height="18" viewBox="0 0 24 24">
            <path
              fill="#4285F4"
              d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
            />
            <path
              fill="#34A853"
              d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
            />
            <path
              fill="#FBBC05"
              d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"
            />
            <path
              fill="#EA4335"
              d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"
            />
          </svg>
          Đăng nhập bằng tài khoản Google
        </button>
      </div>
    </div>
  );
};
