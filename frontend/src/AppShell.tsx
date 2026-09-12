import { Activity,Database,Film,LogOut,PanelLeftClose,PanelLeftOpen,Plus,Radar,Settings2,Users } from "lucide-react";
import type { ReactNode } from "react";
import type { AuthUser } from "./auth-context";
export type View = "topics" | "library" | "subtitles";
type Props = {
  view: View;
  setView: (view: View) => void;
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (value: boolean) => void;
  onNewTopic: () => void;
  onOpenAdmin: () => void;
  onOpenSettings: () => void;
  user: AuthUser | null;
  isAdmin: boolean;
  logout: () => void;
  children: ReactNode;
};
export function AppShell({ view, setView, sidebarCollapsed, setSidebarCollapsed, onNewTopic, onOpenAdmin, onOpenSettings, user, isAdmin, logout, children }: Props) {
  return (
    <div className={`canva-home-app ${view === "subtitles" ? "editor-mode" : "home-mode"} ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      {!sidebarCollapsed && <aside id="primary-sidebar" className="canva-home-sidebar">
        <div className="canva-home-sidebar-header">
          <span className="canva-home-brand">
            <Radar size={24} color="#7c3aed" />
            <span>Content Bot</span>
          </span>
          <button
            className="canva-home-sidebar-toggle"
            type="button"
            aria-controls="primary-sidebar"
            aria-expanded="true"
            aria-label="Ẩn thanh điều hướng"
            title="Ẩn thanh điều hướng"
            onClick={() => setSidebarCollapsed(true)}
          >
            <PanelLeftClose size={19} />
          </button>
        </div>
          
          <button 
            className="canva-home-create-btn"
            onClick={() => {
              setView("topics");
              onNewTopic();
            }}
          >
            <Plus size={18} /> Tạo chủ đề mới
          </button>

          <nav className="canva-home-nav">
            <button
              className={`canva-home-nav-item ${view === "topics" ? "active" : ""}`}
              onClick={() => setView("topics")}
            >
              <Activity size={20} /> Chủ đề
            </button>
            <button
              className={`canva-home-nav-item ${view === "library" ? "active" : ""}`}
              onClick={() => setView("library")}
            >
              <Database size={20} /> Nguồn &amp; Video
            </button>
            <button
              className={`canva-home-nav-item ${view === "subtitles" ? "active" : ""}`}
              onClick={() => setView("subtitles")}
            >
              <Film size={20} /> Phụ đề Video
            </button>
          </nav>

        <div className="canva-home-sidebar-footer" style={{ flexDirection: "column", gap: 10, alignItems: "stretch", padding: "14px 12px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {user?.avatar_url ? (
              <img
                src={user.avatar_url}
                alt="Avatar"
                style={{ width: 34, height: 34, borderRadius: "50%", objectFit: "cover" }}
              />
            ) : (
              <div className="canva-home-profile" style={{ width: 34, height: 34, fontSize: 13, background: "#0f5ea8", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: "50%" }}>
                {(user?.display_name || user?.email || "U").charAt(0).toUpperCase()}
              </div>
            )}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {user?.display_name || user?.email?.split("@")[0]}
              </div>
              <div style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4 }}>
                <span className={`role-tag role-tag-${user?.role || "user"}`}>
                  {(user?.role || "user").toUpperCase()}
                </span>
                <span style={{ color: "#6b7280", fontSize: 10 }}>{user?.auth_provider}</span>
              </div>
            </div>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <button
              type="button"
              className="button secondary"
              style={{ flex: 1, minWidth: 80, fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
              onClick={() => onOpenSettings()}
              title="Quản lý Master Password & API Keys cục bộ"
            >
              <Settings2 size={13} color="#7c3aed" /> Cài đặt API
            </button>
            {isAdmin && (
              <button
                type="button"
                className="button secondary"
                style={{ flex: 1, minWidth: 80, fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
                onClick={() => onOpenAdmin()}
                title="Quản lý người dùng & phê duyệt"
              >
                <Users size={13} color="#0f5ea8" /> Quản lý User
              </button>
            )}
            <button
              type="button"
              className="button secondary"
              style={{ fontSize: 11, padding: "5px 8px", display: "flex", alignItems: "center", justifyContent: "center", gap: 4 }}
              onClick={logout}
              title="Đăng xuất khỏi hệ thống"
            >
              <LogOut size={13} color="#b42318" /> Đăng xuất
            </button>
          </div>
        </div>
      </aside>}

      {sidebarCollapsed && (
        <button
          className="canva-home-sidebar-show"
          type="button"
          aria-controls="primary-sidebar"
          aria-expanded="false"
          aria-label="Hiện thanh điều hướng"
          title="Hiện thanh điều hướng"
          onClick={() => setSidebarCollapsed(false)}
        >
          <PanelLeftOpen size={20} />
        </button>
      )}
      
      {children}
    </div>
  );
}
