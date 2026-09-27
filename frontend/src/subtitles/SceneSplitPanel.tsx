import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, Scissors, Square } from "lucide-react";
import { api, type SubtitleSceneJob } from "../api";
import { API_BASE, getAuthHeader } from "../transport/client";
import type { SubtitleDocumentV2, SubtitleSceneExportResult } from "../api/types";
import type { VideoClip } from "./types";

type SceneSplitPanelProps = {
  videoId: string | null;
  durationMs: number;
  videoClips: VideoClip[];
  subtitleDocument: SubtitleDocumentV2 | null;
  onApplySegments: (segments: VideoClip[]) => void;
};

const numberOr = (value: string, fallback: number) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export function SceneSplitPanel({
  videoId,
  durationMs,
  videoClips,
  subtitleDocument,
  onApplySegments,
}: SceneSplitPanelProps) {
  const [threshold, setThreshold] = useState("27");
  const [minSceneLength, setMinSceneLength] = useState("1.5");
  const [targetDuration, setTargetDuration] = useState("45");
  const [minDuration, setMinDuration] = useState("25");
  const [maxDuration, setMaxDuration] = useState("75");
  const [job, setJob] = useState<SubtitleSceneJob | null>(null);
  const [exportJob, setExportJob] = useState<SubtitleSceneJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const exportControllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    controllerRef.current?.abort();
    exportControllerRef.current?.abort();
  }, []);

  const running = job?.state === "queued" || job?.state === "running";
  const exporting = exportJob?.state === "queued" || exportJob?.state === "running";
  const detectionResult = job?.result && "chunks" in job.result ? job.result : null;

  const poll = async (jobId: string, signal: AbortSignal) => {
    const next = await api.subtitleJob(jobId, signal) as SubtitleSceneJob;
    if (signal.aborted) return;
    setJob(next);
    if (next.state === "queued" || next.state === "running") {
      window.setTimeout(() => void poll(jobId, signal), 700);
    }
  };

  const detect = async () => {
    if (!videoId || !durationMs || running) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setError(null);
    try {
      const next = await api.detectSubtitleScenes(videoId, {
        threshold: numberOr(threshold, 27),
        min_scene_len_s: numberOr(minSceneLength, 1.5),
        target_duration_s: numberOr(targetDuration, 45),
        min_duration_s: numberOr(minDuration, 25),
        max_duration_s: numberOr(maxDuration, 75),
      }, controller.signal);
      if (controller.signal.aborted) return;
      setJob(next);
      if (next.state === "queued" || next.state === "running") {
        window.setTimeout(() => void poll(next.id, controller.signal), 500);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "Không thể dò cảnh video.");
      }
    }
  };

  const cancel = async () => {
    if (!job || !running) return;
    try {
      const next = await api.cancelSubtitleJob(job.id, controllerRef.current?.signal);
      setJob(next as SubtitleSceneJob);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không thể hủy dò cảnh.");
    }
  };

  const exportShorts = async () => {
    if (!videoId || exporting || !job?.result || !("chunks" in job.result)) return;
    exportControllerRef.current?.abort();
    const controller = new AbortController();
    exportControllerRef.current = controller;
    setError(null);
    try {
      const next = await api.exportSubtitleScenes(
        videoId,
        job.result.source_fingerprint,
        job.result.chunks,
        subtitleDocument,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setExportJob(next);
      if (next.state === "queued" || next.state === "running") {
        window.setTimeout(() => void pollExport(next.id, controller.signal), 500);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "Không thể xuất Shorts.");
      }
    }
  };

  const pollExport = async (jobId: string, signal: AbortSignal) => {
    const next = await api.subtitleJob(jobId, signal) as SubtitleSceneJob;
    if (signal.aborted) return;
    setExportJob(next);
    if (next.state === "queued" || next.state === "running") {
      window.setTimeout(() => void pollExport(jobId, signal), 700);
    }
  };

  const cancelExport = async () => {
    if (!exportJob || !exporting) return;
    try {
      const next = await api.cancelSubtitleJob(exportJob.id, exportControllerRef.current?.signal);
      setExportJob(next as SubtitleSceneJob);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Không thể hủy xuất Shorts.");
    }
  };

  const downloadFile = async (path: string, filename: string) => {
    const url = path.startsWith("/api/v1/") ? `${API_BASE}${path.slice(7)}` : path;
    const response = await fetch(url, { headers: getAuthHeader() });
    if (!response.ok) throw new Error("Không thể tải file Shorts.");
    const objectUrl = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  };

  const exportResult = exportJob?.state === "succeeded" && exportJob.result && "files" in exportJob.result
    ? exportJob.result as SubtitleSceneExportResult
    : null;

  const applyChunks = () => {
    const chunks = detectionResult?.chunks;
    if (!chunks?.length) return;
    onApplySegments(chunks.map((chunk, index) => ({
      id: `scene-${index + 1}-${chunk.start_ms}`,
      start_ms: chunk.start_ms,
      end_ms: chunk.end_ms,
    })));
  };

  return <section className="studio-scene-panel" aria-labelledby="scene-detect-title">
    <div className="studio-panel-heading">
      <h3 id="scene-detect-title"><Scissors size={16} /> Chia theo cảnh</h3>
      <p>Dò điểm chuyển cảnh rồi dùng các đoạn tự nhiên làm timeline Shorts. Biên đầu và cuối video luôn được giữ.</p>
    </div>
    <div className="studio-field-grid">
      <label className="studio-field"><span>Độ nhạy</span><input type="number" min={1} max={100} step={1} value={threshold} disabled={running} onChange={event => setThreshold(event.target.value)} /></label>
      <label className="studio-field"><span>Cảnh tối thiểu (s)</span><input type="number" min={0.5} max={30} step={0.1} value={minSceneLength} disabled={running} onChange={event => setMinSceneLength(event.target.value)} /></label>
      <label className="studio-field"><span>Chunk mục tiêu (s)</span><input type="number" min={5} max={180} step={1} value={targetDuration} disabled={running} onChange={event => setTargetDuration(event.target.value)} /></label>
      <label className="studio-field"><span>Chunk tối thiểu (s)</span><input type="number" min={1} max={180} step={1} value={minDuration} disabled={running} onChange={event => setMinDuration(event.target.value)} /></label>
      <label className="studio-field"><span>Chunk tối đa (s)</span><input type="number" min={5} max={300} step={1} value={maxDuration} disabled={running} onChange={event => setMaxDuration(event.target.value)} /></label>
    </div>
    <div className="studio-scene-actions">
      <button type="button" className="studio-secondary-button" disabled={!videoId || !durationMs || running} onClick={() => void detect()}>
        {running ? <LoaderCircle className="spin" size={15} /> : <Scissors size={15} />} {running ? "Đang dò cảnh…" : "Dò cảnh video"}
      </button>
      {running && <button type="button" className="studio-secondary-button" onClick={() => void cancel()}><Square size={14} /> Hủy</button>}
      {job?.state === "succeeded" && detectionResult?.chunks.length ? <button type="button" className="studio-primary-button" onClick={applyChunks}>Dùng {detectionResult.chunks.length} chunk</button> : null}
      {job?.state === "succeeded" && detectionResult ? <button type="button" className="studio-secondary-button" disabled={exporting} onClick={() => void exportShorts()}>
        {exporting ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />} {exporting ? "Đang xuất Shorts…" : `Xuất ${detectionResult.chunks.length} Shorts`}
      </button> : null}
      {exporting && <button type="button" className="studio-secondary-button" onClick={() => void cancelExport()}><Square size={14} /> Hủy xuất</button>}
    </div>
    {!videoId && <small>Hãy tải video trước khi dò cảnh.</small>}
    {job && <div className={`subtitle-job-progress state-${job.state}`} role="status">
      <div><span>{job.message}</span><strong>{job.progress}%</strong></div>
      <progress max={100} value={job.progress} />
      {job.state === "succeeded" && detectionResult && <small>{detectionResult.scene_cuts_s.length - 2} điểm cắt · {detectionResult.chunks.length} chunk · {videoClips.length} đoạn đang dùng</small>}
      {job.error && <small role="alert">{job.error}</small>}
    </div>}
    {exportJob && <div className={`subtitle-job-progress state-${exportJob.state}`} role="status">
      <div><span>{exportJob.message}</span><strong>{exportJob.progress}%</strong></div>
      <progress max={100} value={exportJob.progress} />
      {exportJob.error && <small role="alert">{exportJob.error}</small>}
    </div>}
    {exportResult && <div className="studio-scene-files" aria-label="File Shorts đã xuất">
      <div><strong>Đã xuất {exportResult.files.length} Shorts</strong><button type="button" className="studio-inline-link" onClick={() => void downloadFile(exportResult.manifest_url, "shorts-manifest.json")}>Tải manifest</button></div>
      {exportResult.files.map(file => <div key={file.chunk_index}><span>Short {file.chunk_index} · {(file.end_ms - file.start_ms) / 1000}s</span><span className="studio-scene-file-actions"><button type="button" className="studio-inline-link" onClick={() => void downloadFile(file.video_url, `short-${String(file.chunk_index).padStart(2, "0")}.mp4`)}>Video</button>{file.srt_url && <button type="button" className="studio-inline-link" onClick={() => void downloadFile(file.srt_url!, `short-${String(file.chunk_index).padStart(2, "0")}.srt`)}>SRT</button>}{file.json_url && <button type="button" className="studio-inline-link" onClick={() => void downloadFile(file.json_url!, `short-${String(file.chunk_index).padStart(2, "0")}.json`)}>JSON</button>}</span></div>)}
    </div>}
    {error && <p className="subtitle-generation-warn" role="alert">{error}</p>}
  </section>;
}
