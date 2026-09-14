import { API_BASE } from "../transport/client";
import type { VideoDownloadJob, VideoLibraryItem } from "../api";

export function extractVideoLinks(text: string): string[] {
  const matches = text.match(/https?:\/\/[^\s<>"\u3000]+/gi) ?? [];
  return [...new Set(matches.map(value => value.replace(/[.,;!?)\]}>，。；！）】》]+$/u, "")))];
}

export function videoMatches(video: VideoLibraryItem, query: string, filter: string, platform: string) {
  return (filter === "all" || (filter === "downloaded" ? video.downloaded : filter === "original" ? video.type === "original" && !video.downloaded : video.type === filter))
    && (!platform || video.platform === platform)
    && `${video.title ?? ""} ${video.filename} ${video.platform ?? ""} ${video.source_url ?? ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
}

export function mediaUrl(value: string): string {
  if (/^https?:\/\//i.test(value)) return value;
  if (value.startsWith("/api/v1/")) return `${API_BASE}${value.slice(7)}`;
  return "";
}

export function formatBytes(bytes: number) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

export function durationLabel(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  return total >= 3600
    ? `${Math.floor(total / 3600)}:${String(Math.floor(total / 60) % 60).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`
    : `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function downloadLabel(job: VideoDownloadJob) {
  if (job.state === "paused") return job.phase === "needs_cookies" ? "Chờ cookies để tiếp tục" : "Đã giữ lượt tải";
  if (job.state === "failed") return "Tải thất bại";
  if (job.state === "canceled") return "Đã hủy";
  if (job.state === "succeeded") return "Đã lưu vào thư viện";
  if (job.state === "queued") return "Đang chờ tải";
  if (job.phase === "merging") return "Đang ghép hình và âm thanh";
  if (job.phase === "extracting") return "Đang lấy thông tin video";
  if (job.phase === "resuming") return "Đang tiếp tục lượt tải";
  return "Đang tải video";
}
