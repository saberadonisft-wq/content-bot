import { useEffect, useState } from "react";
import { Download, ExternalLink, Film, LoaderCircle, Play, Sparkles, Trash2 } from "lucide-react";
import type { VideoLibraryItem } from "../api";
import { getAuthHeader } from "../transport/client";
import { durationLabel, formatBytes, mediaUrl } from "./model";

export function VideoCard({ video, selected, selectionDisabled, onToggleSelect, onDelete, onDownload, onSelectThumbnail, onPreview }: {
  video: VideoLibraryItem;
  selected: boolean;
  selectionDisabled: boolean;
  onToggleSelect: () => void;
  onDelete: (video: VideoLibraryItem) => Promise<void>;
  onDownload: (video: VideoLibraryItem) => Promise<void>;
  onSelectThumbnail: (video: VideoLibraryItem) => Promise<void>;
  onPreview: (video: VideoLibraryItem) => void;
}) {
  const [thumbnail, setThumbnail] = useState("");
  const [busy, setBusy] = useState(false);
  const title = video.title || video.filename;
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    const url = mediaUrl(video.thumbnail_url);
    if (video.type === "scraped") return;
    if (url) void fetch(url, { headers: getAuthHeader(), signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error(); return response.blob(); })
      .then(blob => { if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setThumbnail(objectUrl); } })
      .catch(() => {});
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [video.thumbnail_url, video.type]);
  const image = video.type === "scraped" ? mediaUrl(video.thumbnail_url) : thumbnail;
  const [imageFailed, setImageFailed] = useState(false);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true);
    try { await operation(); } finally { setBusy(false); }
  };
  return <article className="vl-card" aria-busy={busy} data-selected={selected}>
    <label className="vl-card-select"><input type="checkbox" checked={selected} disabled={selectionDisabled || busy} onChange={onToggleSelect} aria-label={`Chọn ${title}`} /></label>
    <button className="vl-thumbnail" type="button" onClick={() => onPreview(video)} aria-label={`Xem ${title}`}>
      {image && !imageFailed ? <img src={image} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setImageFailed(true)} /> : <Film size={36} aria-hidden="true" />}
      <span className="vl-play"><Play size={20} fill="currentColor" /></span>
      <span className="vl-video-badge">{video.downloaded ? video.platform : video.type === "subtitled" ? "Đã ghép phụ đề" : video.type === "scraped" ? "Bài đăng" : "Tải lên"}</span>
      {video.duration != null && <span className="vl-duration">{durationLabel(video.duration)}</span>}
    </button>
    <div className="vl-card-info">
      <h3 title={title}>{title}</h3>
      <p className="vl-meta">{video.size_bytes > 0 && <span>{formatBytes(video.size_bytes)}</span>}<span>{new Date(video.created_at).toLocaleDateString("vi-VN")}</span>
        {video.type === "scraped" && video.metrics?.view_count != null && <span>{video.metrics.view_count.toLocaleString("vi-VN")} lượt xem</span>}
      </p>
      <div className="vl-card-actions">
        <button className="vl-button vl-secondary" type="button" onClick={() => onPreview(video)}><Play size={15} /> Xem video</button>
        <button className="vl-button vl-icon" type="button" disabled={busy} onClick={() => void act(() => onDownload(video))} aria-label={video.type === "scraped" ? `Tải về thư viện: ${title}` : `Lưu ra máy: ${title}`} title={video.type === "scraped" ? "Tải về thư viện" : "Lưu ra máy"}>
          {busy ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
        </button>
        {video.type === "original" && <button className="vl-button vl-icon" type="button" disabled={busy} onClick={() => void act(() => onSelectThumbnail(video))} aria-label={`Chọn thumbnail: ${title}`} title="Chọn thumbnail tự động"><Sparkles size={16} /></button>}
        {video.source_url && <a className="vl-button vl-icon" href={mediaUrl(video.source_url)} target="_blank" rel="noreferrer" aria-label={`Mở nguồn: ${title}`} title="Mở link gốc"><ExternalLink size={16} /></a>}
        <button className="vl-button vl-icon vl-danger" type="button" disabled={busy || selectionDisabled} onClick={() => void act(() => onDelete(video))} aria-label={`Xóa ${title}`} title="Xóa video"><Trash2 size={16} /></button>
      </div>
    </div>
  </article>;
}
