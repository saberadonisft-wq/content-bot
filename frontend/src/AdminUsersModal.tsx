import React, { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Ban,
  Check,
  Loader2,
  RefreshCw,
  Shield,
  Trash2,
  Unlock,
  User,
  Users,
  X,
} from "lucide-react";
import { type AuthUser } from "./auth-context";
import { useAuth } from "./useAuth";

interface AdminUsersModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const AdminUsersModal: React.FC<AdminUsersModalProps> = ({
  isOpen,
  onClose,
}) => {
  const { accessToken, authServerUrl, user: currentUser } = useAuth();
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchUsers = useCallback(async () => {
    if (!accessToken) return;
    await Promise.resolve();
    setLoading(true);
    setError(null);
    try {
      const url = filter
        ? `${authServerUrl}/admin/users?status=${filter}`
        : `${authServerUrl}/admin/users`;
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) {
        throw new Error("Không thể tải danh sách người dùng.");
      }
      const data = await res.json();
      setUsers(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Lỗi khi tải dữ liệu.");
    } finally {
      setLoading(false);
    }
  }, [accessToken, authServerUrl, filter]);

  useEffect(() => {
    if (!isOpen) return undefined;
    const timer = window.setTimeout(() => void fetchUsers(), 0);
    return () => window.clearTimeout(timer);
  }, [isOpen, fetchUsers]);

  if (!isOpen) return null;

  const handleApprove = async (userId: string) => {
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}/approve`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) throw new Error("Duyệt tài khoản thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async (userId: string) => {
    if (!confirm("Bạn có chắc chắn muốn từ chối tài khoản này?")) return;
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}/reject`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) throw new Error("Từ chối thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const handleBan = async (userId: string) => {
    if (!confirm("Bạn có chắc chắn muốn khóa tài khoản này?")) return;
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}/ban`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) throw new Error("Khóa tài khoản thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const handleUnban = async (userId: string) => {
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}/unban`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) throw new Error("Mở khóa thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const handleToggleRole = async (userId: string, currentRole: string) => {
    const newRole = currentRole === "admin" ? "user" : "admin";
    if (!confirm(`Đổi quyền thành viên này thành ${newRole.toUpperCase()}?`))
      return;
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}/role`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ role: newRole }),
      });
      if (!res.ok) throw new Error("Cập nhật quyền thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDelete = async (userId: string) => {
    if (!confirm("Hành động này sẽ XÓA VĨNH VIỄN người dùng. Bạn có chắc?"))
      return;
    setActionLoading(userId);
    try {
      const res = await fetch(`${authServerUrl}/admin/users/${userId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!res.ok) throw new Error("Xóa người dùng thất bại.");
      await fetchUsers();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : "Lỗi");
    } finally {
      setActionLoading(null);
    }
  };

  const pendingCount = users.filter((u) => u.status === "pending").length;

  return (
    <div className="admin-modal-overlay" onClick={onClose}>
      <div className="admin-modal" onClick={(e) => e.stopPropagation()}>
        <div className="admin-modal-header">
          <h2 className="admin-modal-title">
            <Users size={22} color="#0f5ea8" />
            Quản lý Tài khoản & Phân quyền
          </h2>
          <button
            type="button"
            className="pending-logout-btn"
            style={{ padding: 6, width: "auto", border: "none" }}
            onClick={onClose}
          >
            <X size={20} />
          </button>
        </div>

        <div
          style={{
            padding: "12px 24px",
            display: "flex",
            gap: 8,
            borderBottom: "1px solid var(--line, #cad7e4)",
            background: "var(--surface, #f5f8fb)",
          }}
        >
          <button
            type="button"
            className={`admin-action-btn ${filter === null ? "approve" : ""}`}
            style={{
              background: filter === null ? "#0f5ea8" : "transparent",
              color: filter === null ? "#fff" : "inherit",
              border: "1px solid var(--line, #cad7e4)",
            }}
            onClick={() => setFilter(null)}
          >
            Tất cả
          </button>
          <button
            type="button"
            className={`admin-action-btn`}
            style={{
              background: filter === "pending" ? "#a55a00" : "transparent",
              color: filter === "pending" ? "#fff" : "inherit",
              border: "1px solid var(--line, #cad7e4)",
            }}
            onClick={() => setFilter("pending")}
          >
            Chờ duyệt {pendingCount > 0 && `(${pendingCount})`}
          </button>
          <button
            type="button"
            className={`admin-action-btn`}
            style={{
              background: filter === "approved" ? "#157a5d" : "transparent",
              color: filter === "approved" ? "#fff" : "inherit",
              border: "1px solid var(--line, #cad7e4)",
            }}
            onClick={() => setFilter("approved")}
          >
            Đã duyệt
          </button>
          <button
            type="button"
            className={`admin-action-btn`}
            style={{
              background: filter === "banned" ? "#b42318" : "transparent",
              color: filter === "banned" ? "#fff" : "inherit",
              border: "1px solid var(--line, #cad7e4)",
            }}
            onClick={() => setFilter("banned")}
          >
            Đã khóa
          </button>

          <button
            type="button"
            className="admin-action-btn"
            style={{
              marginLeft: "auto",
              background: "transparent",
              border: "1px solid var(--line, #cad7e4)",
              color: "inherit",
            }}
            onClick={fetchUsers}
            disabled={loading}
          >
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            Làm mới
          </button>
        </div>

        <div className="admin-modal-body">
          {error && (
            <div className="auth-alert auth-alert-error" style={{ marginBottom: 16 }}>
              <AlertCircle size={18} />
              <div>{error}</div>
            </div>
          )}

          {loading ? (
            <div style={{ textAlign: "center", padding: "40px 0" }}>
              <Loader2 size={32} className="animate-spin" style={{ margin: "0 auto" }} />
              <p style={{ marginTop: 10, color: "#64748b" }}>Đang tải danh sách người dùng...</p>
            </div>
          ) : users.length === 0 ? (
            <div style={{ textAlign: "center", padding: "40px 0", color: "#64748b" }}>
              Không có người dùng nào trong danh mục này.
            </div>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Tài khoản</th>
                  <th>Tên</th>
                  <th>Vai trò</th>
                  <th>Trạng thái</th>
                  <th>Đăng ký qua</th>
                  <th>Hành động</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isCurrent = currentUser?.id === u.id;
                  const isBusy = actionLoading === u.id;

                  return (
                    <tr key={u.id}>
                      <td>
                        <strong style={{ color: "#0f5ea8" }}>{u.email}</strong>
                        {isCurrent && (
                          <span style={{ fontSize: "0.75rem", color: "#64748b", marginLeft: 6 }}>
                            (Bạn)
                          </span>
                        )}
                      </td>
                      <td>{u.display_name}</td>
                      <td>
                        <span className={`role-tag role-tag-${u.role}`}>
                          {u.role.toUpperCase()}
                        </span>
                      </td>
                      <td>
                        <span className={`role-tag role-tag-${u.status}`}>
                          {u.status === "pending"
                            ? "CHỜ DUYỆT"
                            : u.status === "approved"
                            ? "ĐÃ DUYỆT"
                            : u.status === "banned"
                            ? "ĐÃ KHÓA"
                            : "TỪ CHỐI"}
                        </span>
                      </td>
                      <td>
                        {u.auth_provider === "google" ? "Google" : "Email"}
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {u.status === "pending" && (
                            <>
                              <button
                                type="button"
                                className="admin-action-btn approve"
                                onClick={() => handleApprove(u.id)}
                                disabled={isBusy}
                                title="Phê duyệt cho phép dùng app"
                              >
                                <Check size={14} /> Duyệt
                              </button>
                              <button
                                type="button"
                                className="admin-action-btn reject"
                                onClick={() => handleReject(u.id)}
                                disabled={isBusy}
                                title="Từ chối yêu cầu"
                              >
                                <X size={14} /> Từ chối
                              </button>
                            </>
                          )}

                          {u.status === "approved" && !isCurrent && (
                            <button
                              type="button"
                              className="admin-action-btn ban"
                              onClick={() => handleBan(u.id)}
                              disabled={isBusy}
                              title="Khóa tài khoản này"
                            >
                              <Ban size={14} /> Khóa
                            </button>
                          )}

                          {u.status === "banned" && (
                            <button
                              type="button"
                              className="admin-action-btn unban"
                              onClick={() => handleUnban(u.id)}
                              disabled={isBusy}
                              title="Mở khóa tài khoản"
                            >
                              <Unlock size={14} /> Mở khóa
                            </button>
                          )}

                          {!isCurrent && (
                            <button
                              type="button"
                              className="admin-action-btn"
                              style={{
                                background: "var(--surface-sunken, #eaf2fa)",
                                color: "var(--ink, #2e4054)",
                                border: "1px solid var(--line, #cad7e4)",
                              }}
                              onClick={() => handleToggleRole(u.id, u.role)}
                              disabled={isBusy}
                              title="Chuyển đổi vai trò Admin / User"
                            >
                              {u.role === "admin" ? (
                                <>
                                  <User size={13} /> Hạ quyền
                                </>
                              ) : (
                                <>
                                  <Shield size={13} /> Thăng Admin
                                </>
                              )}
                            </button>
                          )}

                          {!isCurrent && (
                            <button
                              type="button"
                              className="admin-action-btn delete"
                              onClick={() => handleDelete(u.id)}
                              disabled={isBusy}
                              title="Xóa vĩnh viễn"
                            >
                              <Trash2 size={13} />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};
