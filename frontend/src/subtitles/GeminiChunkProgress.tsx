import type { GeminiChunkStatus, GeminiSubtitleJob } from "../api";
import "./gemini-chunk-progress.css";

const phaseLabels: Record<string, string> = {
  queued: "Đang chờ",
  preparing_video: "Chuẩn bị video",
  uploading_video: "Tải lên Gemini",
  gemini_analyzing: "Đang dịch",
  gemini_checking: "Đang đối chiếu video",
  gemini_repairing: "Đang sửa lại",
  quality_failed: "Chưa khớp · tự sửa",
  quota_wait: "Chờ lượt API",
  retrying: "Đang thử lại",
  retry_wait: "Chờ tự thử lại",
};

function chunkStatus(chunk: GeminiChunkStatus, job: GeminiSubtitleJob) {
  if (chunk.state === "completed") return { tone: "complete", label: "Hoàn tất", value: 100 };
  if (chunk.state === "failed") return { tone: "failed", label: "Lỗi", value: undefined };
  if (job.cancel_requested || job.state === "canceled") {
    return { tone: "stopped", label: job.state === "canceled" ? "Đã hủy" : "Đang dừng", value: undefined };
  }
  if (job.state === "failed" && job.phase !== "interrupted") {
    return { tone: "stopped", label: "Chưa hoàn tất", value: undefined };
  }
  if (job.phase === "interrupted" || job.phase === "resuming") {
    return { tone: "waiting", label: "Chờ tiếp tục", value: undefined };
  }
  if (chunk.state === "queued") return { tone: "queued", label: phaseLabels.queued, value: 0 };
  const waiting = ["quota_wait", "retrying", "retry_wait", "quality_failed"].includes(chunk.state);
  return { tone: waiting ? "waiting" : "active", label: phaseLabels[chunk.state] ?? "Đang xử lý", value: undefined };
}

export function GeminiChunkProgress({ job }: { job: GeminiSubtitleJob }) {
  const details = job.details;
  if (!details?.chunks?.length) return null;

  return (
    <details className="gemini-chunk-details">
      <summary>Chi tiết {details.completed ?? 0}/{details.total ?? details.chunks.length} đoạn</summary>
      <ul className="gemini-chunk-list" aria-label="Trạng thái từng đoạn phụ đề" tabIndex={0}>
        {details.chunks.map(chunk => {
          const status = chunkStatus(chunk, job);
          const stopped = status.tone === "failed" || status.tone === "stopped";
          return (
            <li key={chunk.chunk_id} className={`gemini-chunk-row is-${status.tone}`}>
              <div className="gemini-chunk-heading">
                <strong>{chunk.chunk_id.replace(/^chunk-0*/, "Đoạn ")}</strong>
                <span className="gemini-chunk-status">{status.label}</span>
              </div>
              <div
                className="gemini-chunk-track"
                role={stopped ? "img" : "progressbar"}
                aria-label={`${chunk.chunk_id}: ${status.label}`}
                aria-valuemin={stopped ? undefined : 0}
                aria-valuemax={stopped ? undefined : 100}
                aria-valuenow={status.value}
                aria-valuetext={stopped ? undefined : status.label}
              ><span /></div>
              <small className="gemini-chunk-meta">
                {chunk.key_name || "Chưa giao key"}{chunk.model ? ` · ${chunk.model}` : ""}
              </small>
              {(status.tone === "failed" || status.tone === "waiting") && chunk.message && (
                <p className="gemini-chunk-message">{chunk.message}</p>
              )}
              {status.tone === "active" && !!chunk.quality_errors?.length && (
                <p className="gemini-chunk-message" role="status">
                  Kết quả trước chưa đạt · đang tự sửa lần {chunk.repair_attempt}: {chunk.quality_errors[0]}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
