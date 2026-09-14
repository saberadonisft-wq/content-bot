import { useEffect, useRef, useState } from "react";
import { LoaderCircle, X } from "lucide-react";
import type { VideoLibraryItem } from "../api";
import { getAuthHeader } from "../transport/client";
import { mediaUrl } from "./model";

export function VideoPreview({ video, onClose }: { video: VideoLibraryItem; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [url, setUrl] = useState(() => getAuthHeader().Authorization ? "" : mediaUrl(video.video_url));
  const [error, setError] = useState("");
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    const controller = new AbortController();
    let objectUrl = "";
    const headers = getAuthHeader();
    if (headers.Authorization) void fetch(mediaUrl(video.video_url), { headers, signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error("Không mở được video. Hãy đăng nhập lại hoặc thử lại."); return response.blob(); })
      .then(blob => { if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); } })
      .catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : "Không mở được video."); });
    return () => { controller.abort(); element?.close(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [video.video_url]);
  return <dialog className="vl-preview" ref={dialog} onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }} aria-labelledby="vl-preview-title">
    <div className="vl-preview-head"><h2 id="vl-preview-title">{video.title || video.filename}</h2><button autoFocus className="vl-button vl-icon" type="button" aria-label="Đóng video" onClick={onClose}><X size={20} /></button></div>
    {error ? <p className="vl-error" role="alert">{error}</p> : url ? <video src={url} controls autoPlay onError={() => setError("Trình duyệt chưa phát được định dạng này. Bạn có thể lưu video ra máy để xem.")} /> : <div className="vl-empty"><LoaderCircle className="spin" /> Đang mở video…</div>}
  </dialog>;
}
