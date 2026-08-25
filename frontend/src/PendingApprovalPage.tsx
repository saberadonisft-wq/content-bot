import React, { useEffect, useState } from "react";
import {
  Clock,
  LogOut,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { useAuth } from "./useAuth";

export const PendingApprovalPage: React.FC = () => {
  const { user, checkStatus, logout, isBanned } = useAuth();
  const [checking, setChecking] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  // Auto-poll status every 15 seconds when tab is active
  useEffect(() => {
    const interval = setInterval(async () => {
      if (document.visibilityState === "visible") {
        await checkStatus();
      }
    }, 15000);

    return () => clearInterval(interval);
  }, [checkStatus]);

  const handleManualCheck = async () => {
    setChecking(true);
    setFeedback(null);
    try {
      const updated = await checkStatus();
      if (updated?.status === "approved") {
        setFeedback("Tài khoản đã được phê duyệt! Đang chuyển hướng...");
      } else if (updated?.status === "rejected") {
        setFeedback("Tài khoản của bạn đã bị từ chối phê duyệt.");
      } else if (updated?.status === "banned") {
        setFeedback("Tài khoản của bạn đã bị khóa.");
      } else {
        setFeedback("Tài khoản vẫn đang trong hàng chờ duyệt.");
      }
    } catch {
      setFeedback("Không thể kết nối đến máy chủ xác thực.");
    } finally {
      setChecking(false);
    }
  };

  const isRejected = user?.status === "rejected";

  return (
    <div className="auth-container">
      <div className="auth-card pending-card">
        <div className="pending-badge-icon">
          {isBanned || isRejected ? (
            <ShieldAlert size={36} color="#ef4444" />
          ) : (
            <Clock size={36} />
          )}
        </div>

        <h1 className="auth-title">
          {isBanned
            ? "Tài khoản bị khóa"
            : isRejected
            ? "Tài khoản bị từ chối"
            : "Chờ phê duyệt tài khoản"}
        </h1>

        <p className="auth-subtitle">
          {isBanned
            ? "Tài khoản này đã bị khóa bởi quản trị viên hệ thống."
            : isRejected
            ? "Yêu cầu đăng ký tài khoản của bạn không được quản trị viên chấp thuận."
            : "Tài khoản của bạn đã được tạo thành công nhưng cần quản trị viên phê duyệt trước khi có thể sử dụng Content Bot."}
        </p>

        {user && (
          <div
            style={{
              margin: "20px 0",
              padding: "12px 16px",
              background: "rgba(255, 255, 255, 0.04)",
              borderRadius: "10px",
              border: "1px solid rgba(255, 255, 255, 0.08)",
              textAlign: "left",
              fontSize: "0.85rem",
              lineHeight: 1.6,
            }}
          >
            <div style={{ color: "#94a3b8" }}>
              Tài khoản:{" "}
              <strong style={{ color: "#f8fafc" }}>{user.email}</strong>
            </div>
            <div style={{ color: "#94a3b8" }}>
              Tên hiển thị:{" "}
              <strong style={{ color: "#f8fafc" }}>{user.display_name}</strong>
            </div>
            <div style={{ color: "#94a3b8" }}>
              Trạng thái:{" "}
              <span
                className={`role-tag role-tag-${user.status}`}
                style={{ marginLeft: 4 }}
              >
                {user.status.toUpperCase()}
              </span>
            </div>
          </div>
        )}

        {feedback && (
          <div
            className={`auth-alert ${
              feedback.includes("phê duyệt!")
                ? "auth-alert-success"
                : "auth-alert-warning"
            }`}
          >
            <div>{feedback}</div>
          </div>
        )}

        <div className="pending-actions">
          {!isBanned && !isRejected && (
            <button
              type="button"
              className="pending-refresh-btn"
              onClick={handleManualCheck}
              disabled={checking}
            >
              <RefreshCw
                size={16}
                className={checking ? "animate-spin" : ""}
              />
              {checking ? "Đang kiểm tra..." : "Kiểm tra trạng thái duyệt"}
            </button>
          )}

          <button
            type="button"
            className="pending-logout-btn"
            onClick={logout}
          >
            <LogOut size={16} style={{ verticalAlign: "middle", marginRight: 6 }} />
            Đăng xuất / Đổi tài khoản
          </button>
        </div>
      </div>
    </div>
  );
};
