import { useEffect, useState } from "react";
import { Download, Film, LoaderCircle, Trash2, Video, Eye, Heart, MessageCircle } from "lucide-react";
import { API_BASE, api, VideoLibraryItem } from "./api";

export function VideoLibrary() {
  const [videos, setVideos] = useState<VideoLibraryItem[]>([]);
  const [filterType, setFilterType] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadVideos = async () => {
    setLoading(true);
    try {
      const data = await api.videos();
      setVideos(data);
      setError(null);
    } catch (err: any) {
      setError(err.message || "Không thể tải danh sách video");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadVideos();
  }, []);

  const formatSize = (bytes: number) => {
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  };
  
  const formatNumber = (num: number) => {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + "M";
    if (num >= 1000) return (num / 1000).toFixed(1) + "K";
    return num.toString();
  };

  const handleDelete = async (id: string, type: string) => {
    if (!confirm("Bạn có chắc chắn muốn xoá video này không?")) return;
    try {
      await api.deleteVideo(id, type);
      setVideos(videos.filter(v => !(v.id === id && v.type === type)));
    } catch (err: any) {
      alert("Xoá video thất bại: " + err.message);
    }
  };

  const resolveUrl = (url: string) => {
    if (!url) return "";
    if (url.startsWith("http://") || url.startsWith("https://")) {
      return url;
    }
    return `${API_BASE}${url.replace('/api/v1', '')}`;
  };

  return (
    <>
      <div className="canva-home-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <h1>Quản lý Video</h1>
          <p className="canva-text-muted" style={{ fontSize: 15, marginTop: 8 }}>
            Quản lý các video bạn đã tải lên, video ghép phụ đề và video từ các bài đăng quét được.
          </p>
        </div>
        
        <div style={{ paddingBottom: 8 }}>
          <select 
            value={filterType} 
            onChange={(e) => setFilterType(e.target.value)}
            style={{ 
              padding: "8px 16px", 
              borderRadius: 8, 
              border: "1px solid #d1d5db",
              backgroundColor: "white",
              fontSize: 14,
              outline: "none",
              cursor: "pointer"
            }}
          >
            <option value="all">Tất cả video</option>
            <option value="scraped">Bài đăng quét được</option>
            <option value="subtitled">Đã ghép phụ đề</option>
            <option value="original">Video gốc (Tải lên)</option>
          </select>
        </div>
      </div>

      {loading ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 200, color: "#6b7280" }}>
          <LoaderCircle className="spin" size={32} />
          <span style={{ marginLeft: 12 }}>Đang tải video...</span>
        </div>
      ) : error ? (
        <div style={{ padding: 24, background: "#fef2f2", color: "#b91c1c", borderRadius: 8 }}>
          {error}
        </div>
      ) : videos.length === 0 ? (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: 200, color: "#9ca3af" }}>
          <Film size={48} style={{ opacity: 0.5, marginBottom: 16 }} />
          <p>Chưa có video nào.</p>
        </div>
      ) : (
        <div className="canva-design-grid">
          {videos.filter(v => filterType === "all" || v.type === filterType).map((video) => (
            <div key={`${video.type}-${video.id}`} className="canva-design-card">
              <div className="canva-design-thumbnail" style={{ padding: 0, overflow: "hidden", background: "#000" }}>
                <img 
                  src={resolveUrl(video.thumbnail_url)} 
                  alt={video.filename} 
                  style={{ width: "100%", height: "100%", objectFit: "cover" }}
                  onError={(e) => {
                    e.currentTarget.style.display = 'none';
                    e.currentTarget.parentElement!.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;"><svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-video" style="opacity:0.5;color:white;"><path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/></svg></div>';
                  }}
                />
                {video.type === "subtitled" && (
                  <div style={{ position: "absolute", top: 8, right: 8, background: "#7c3aed", color: "white", fontSize: 11, padding: "4px 8px", borderRadius: 4, fontWeight: "bold" }}>
                    Đã có phụ đề
                  </div>
                )}
                {video.type === "scraped" && (
                  <div style={{ position: "absolute", top: 8, right: 8, background: "#3b82f6", color: "white", fontSize: 11, padding: "4px 8px", borderRadius: 4, fontWeight: "bold" }}>
                    Bài đăng
                  </div>
                )}
              </div>
              <div className="canva-design-info" style={{ display: 'flex', flexDirection: 'column' }}>
                <h3 className="canva-design-title" title={video.filename}>{video.filename}</h3>
                <div className="canva-design-meta" style={{ marginBottom: 12 }}>
                  {video.type === "scraped" ? (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 12px' }}>
                      <span style={{ display: 'flex', alignItems: 'center' }}><Video size={14} style={{ marginRight: 4 }}/> {new Date(video.created_at).toLocaleDateString("vi-VN")}</span>
                      {video.metrics?.view_count !== undefined && <span style={{ display: 'flex', alignItems: 'center' }}><Eye size={14} style={{ marginRight: 4 }}/> {formatNumber(video.metrics.view_count)}</span>}
                      {video.metrics?.like_count !== undefined && <span style={{ display: 'flex', alignItems: 'center' }}><Heart size={14} style={{ marginRight: 4 }}/> {formatNumber(video.metrics.like_count)}</span>}
                      {video.metrics?.comment_count !== undefined && <span style={{ display: 'flex', alignItems: 'center' }}><MessageCircle size={14} style={{ marginRight: 4 }}/> {formatNumber(video.metrics.comment_count)}</span>}
                    </div>
                  ) : (
                    <span><Video size={14} /> {formatSize(video.size_bytes)} · {new Date(video.created_at).toLocaleDateString("vi-VN")}</span>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 'auto' }}>
                  <a 
                    href={resolveUrl(video.video_url)} 
                    target="_blank" 
                    rel="noreferrer"
                    className="canva-btn canva-btn-primary" 
                    style={{ flex: 1, padding: '6px 12px', fontSize: 13, background: '#7c3aed', color: 'white' }}
                  >
                    Xem Video
                  </a>
                  {video.type !== "scraped" && (
                  <a 
                    href={resolveUrl(video.video_url)} 
                    download={video.filename}
                    className="canva-btn canva-btn-outline" 
                    style={{ padding: '6px 12px', fontSize: 13 }}
                  >
                    <Download size={14} />
                  </a>
                  )}
                  <button 
                    onClick={() => handleDelete(video.id, video.type)}
                    className="canva-btn canva-btn-outline" 
                    style={{ padding: '6px 12px', fontSize: 13, color: '#ef4444', borderColor: '#fca5a5' }}
                    title="Xóa video"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
