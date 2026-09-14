import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { GeminiReview, GeminiReviewJob, GeminiReviewScope } from "../api/subtitles";
import type { SubtitleDocumentV2 } from "./types";

type Props = { videoId: string; document: SubtitleDocumentV2; durationMs: number; selectedCueId: string | null;
  initialModel: string; onDocument: (document: SubtitleDocumentV2) => void; onSeek: (ms: number) => void };
const inputStyle = { background: "var(--color-paper, #fff)", color: "inherit", border: "1px solid var(--color-rule, #cad7e4)", padding: "6px", borderRadius: "5px", maxWidth: "100%" };

export function GeminiReviewPanel({ videoId, document, durationMs, selectedCueId, initialModel, onDocument, onSeek }: Props) {
  const [model, setModel] = useState(initialModel);
  const [review, setReview] = useState<GeminiReview | null>(null);
  const [job, setJob] = useState<GeminiReviewJob | null>(null);
  const [scope, setScope] = useState<GeminiReviewScope["mode"]>("all");
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(durationMs / 1000);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [canUndo, setCanUndo] = useState(false);
  const [page, setPage] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[] | null>(null);
  const [selectionPage, setSelectionPage] = useState(0);
  const documentIds = new Set(document.segments.map(c => c.id));
  const chosenIds = (selectedIds ?? (selectedCueId ? [selectedCueId] : [])).filter(id => documentIds.has(id));
  const current = useRef(document);
  const mounted = useRef(true);
  useEffect(() => { current.current = document; }, [document]);
  useEffect(() => () => { mounted.current = false; }, []);
  const storageKey = `gemini-review:${videoId}`;
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    try {
      const saved = localStorage.getItem(storageKey);
      if (!saved) return;
      const { reviewId, jobId } = JSON.parse(saved) as { reviewId: string; jobId: string };
      void Promise.all([api.getGeminiReview(reviewId, controller.signal), api.getGeminiReviewJob(jobId, controller.signal)]).then(([record, existingJob]) => {
        if (controller.signal.aborted) return;
        setReview(record); setJob(existingJob); setCanUndo(record.can_undo ?? false);
      }).catch(error => { if (!controller.signal.aborted) setMessage(String(error)); });
    } catch { /* Storage may be unavailable; the review panel remains usable. */ }
    return () => controller.abort();
  }, [storageKey]);
  const running = job?.state === "queued" || job?.state === "running";
  const activeJobId = running ? job.id : null;
  const activeReviewId = review?.id ?? null;
  useEffect(() => {
    if (!activeJobId || !activeReviewId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let lastReviewRefresh = 0;
    async function poll() {
      try {
        const next = await api.getGeminiReviewJob(activeJobId!, controller.signal);
        if (controller.signal.aborted) return;
        if (next.result) setReview(next.result);
        else if (next.state !== "running" || Date.now() - lastReviewRefresh >= 5000) {
          // Checkpoints and warnings remain available while running or after a failure.
          const latest = await api.getGeminiReview(activeReviewId!, controller.signal);
          if (controller.signal.aborted) return;
          setReview(latest);
          lastReviewRefresh = Date.now();
        }
        setJob(next);
        if (next.state === "failed") setMessage(next.error || "Review thất bại; có thể tiếp tục từ checkpoint.");
        if (next.state === "running" || next.state === "queued") timer = setTimeout(() => void poll(), 1000);
      } catch (error) {
        if (!controller.signal.aborted) { setMessage(String(error)); timer = setTimeout(() => void poll(), 2000); }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [activeJobId, activeReviewId]);
  async function startReview() {
    setBusy(true); setMessage("");
    try {
      const requested: GeminiReviewScope = scope === "selected" ? { mode: scope, cue_ids: chosenIds } : scope === "range" ? { mode: scope, start_ms: Math.round(start * 1000), end_ms: Math.round(end * 1000) } : { mode: scope };
      const result = await api.createGeminiReview(videoId, current.current, { ...requested, combined: true }, model || undefined);
      if (!mounted.current) return;
      setReview(result.review); setJob(result.job); setCanUndo(false); setPage(0);
      localStorage.setItem(storageKey, JSON.stringify({ reviewId: result.review.id, jobId: result.job.id }));
    } catch (error) { if (mounted.current) setMessage(String(error)); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function apply(ids: string[], skip = false) {
    if (!review) return;
    setBusy(true); setMessage("");
    const before = current.current;
    try {
      const result = await api.applyGeminiReview(review.id, before, ids, skip);
      if (!mounted.current) return;
      setReview(result.review); setCanUndo(result.can_undo);
      if (current.current !== before) {
        if (result.applied_ids.length) {
          const restored = await api.undoGeminiReview(review.id, result.document);
          if (mounted.current) { setReview(restored.review); setCanUndo(restored.can_undo); }
        }
        setMessage("Phụ đề vừa thay đổi trong lúc áp dụng; đã giữ bản bạn đang chỉnh. Đề xuất chưa được đưa vào timeline, hãy kiểm tra lại.");
        return;
      }
      if (result.applied_ids.length) onDocument(result.document);
      setMessage(`Đã áp dụng ${result.applied_ids.length} đề xuất. Mục xung đột giữ nguyên để kiểm tra lại.`);
    } catch (error) { if (mounted.current) setMessage(String(error)); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function undo() {
    if (!review) return;
    setBusy(true);
    const before = current.current;
    try {
      const result = await api.undoGeminiReview(review.id, before);
      if (!mounted.current) return;
      if (current.current !== before) { setMessage("Phụ đề vừa thay đổi; giữ bản đang chỉnh."); return; }
      onDocument(result.document); setReview(result.review); setCanUndo(result.can_undo);
    } catch (error) { if (mounted.current) setMessage(String(error)); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function cancelOrResume() {
    if (!review || !job) return;
    setBusy(true);
    try {
      if (running) await api.cancelGeminiSubtitleJob(job.id);
      else {
        const next = await api.resumeGeminiReview(review.id);
        if (!mounted.current) return;
        setJob(next); localStorage.setItem(storageKey, JSON.stringify({ reviewId: review.id, jobId: next.id }));
      }
    } catch (error) { if (mounted.current) setMessage(String(error)); }
    finally { if (mounted.current) setBusy(false); }
  }
  const pending = review?.proposals.filter(p => p.state === "pending" && !p.locked) ?? [];
  const pages = Math.max(1, Math.ceil((review?.proposals.length ?? 0) / 10));
  return <section className="subtitle-review-panel">
    <h3>Kiểm tra sửa chữa bằng AI</h3>
    <p>Mỗi phụ đề có thể là một câu hoặc một vế câu; được tách ở dấu phẩy khi bám sát phụ đề gốc. Quét và tách phụ đề có từ hai câu trở lên hoặc quá dài, khoanh vùng mọi đoạn chồng thời gian và khoảng trống trên timeline trước khi gửi Gemini. AI đối chiếu video để tách câu, sửa thời gian và bổ sung lời thiếu; giữ khoảng im lặng hợp lệ, trả đề xuất để bạn duyệt trước khi áp dụng.</p>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
      {<label>Phạm vi <select style={inputStyle} value={scope} onChange={e => setScope(e.target.value as GeminiReviewScope["mode"])}><option value="all">Toàn video</option><option value="range">Khoảng thời gian</option><option value="selected">Chọn nhóm cue</option></select></label>}
      <label>Model kiểm tra <input style={inputStyle} value={model} onChange={event => setModel(event.target.value)} /></label>
      {scope === "range" && <><label>Từ giây <input type="number" min={0} max={durationMs / 1000} step="0.1" value={start} style={inputStyle} onChange={e => setStart(Number(e.target.value))} /></label><label>Đến giây <input type="number" min={0} max={durationMs / 1000} step="0.1" value={end} style={inputStyle} onChange={e => setEnd(Number(e.target.value))} /></label></>}
      <button type="button" disabled={busy || running || (scope === "selected" && !chosenIds.length)} onClick={() => void startReview()}>Sửa bằng AI</button>
      {job && (running || job.state === "failed" || job.state === "canceled") && <button type="button" disabled={busy} onClick={() => void cancelOrResume()}>{running ? "Hủy kiểm tra" : "Tiếp tục kiểm tra"}</button>}
    </div>
    {scope === "selected" && <fieldset disabled={busy || running}><legend>{chosenIds.length} cue được chọn</legend>
      <button type="button" onClick={() => setSelectedIds(selectedCueId ? [selectedCueId] : [])}>Dùng cue đang chọn</button>
      {document.segments.slice(selectionPage * 10, selectionPage * 10 + 10).map(c => <label key={c.id} style={{ display: "flex", gap: 8 }}>
        <input type="checkbox" checked={chosenIds.includes(c.id)} onChange={e => setSelectedIds(e.target.checked ? [...new Set([...chosenIds, c.id])] : chosenIds.filter(id => id !== c.id))} />
        <span>{(c.start_ms / 1000).toFixed(1)}s · {c.text.slice(0, 90)}</span>
      </label>)}
      <button type="button" disabled={!selectionPage} onClick={() => setSelectionPage(selectionPage - 1)}>Cue trước</button>
      <button type="button" disabled={(selectionPage + 1) * 10 >= document.segments.length} onClick={() => setSelectionPage(selectionPage + 1)}>Cue sau</button>
    </fieldset>}
    {job && <div role="status">{job.message} · {job.progress}%{running && <progress max={100} value={job.progress} />}</div>}
    <p role="status">{message}</p>
    {review?.warnings?.map((warning, index) => <p key={`${warning.code}-${index}`} role="status">{warning.message}</p>)}
    {review && <><div style={{ display: "flex", gap: 12 }}><button type="button" disabled={busy || running || pending.length === 0} onClick={() => void apply(pending.map(p => p.id))}>Áp dụng tất cả hợp lệ ({pending.length})</button><button type="button" disabled={busy || !canUndo} onClick={() => void undo()}>Hoàn tác lần áp dụng</button></div>
      {review.state === "succeeded" && review.proposals.length === 0 && <p>AI không đề xuất sửa trong phạm vi này.</p>}
      {review.proposals.slice(page * 10, (page + 1) * 10).map(proposal => <article key={proposal.id} style={{ borderTop: "1px solid #536477", paddingTop: 12 }}>
        <button type="button" onClick={() => onSeek(proposal.start_ms)}>Xem {(proposal.start_ms / 1000).toFixed(2)}–{(proposal.end_ms / 1000).toFixed(2)}s</button>
        <span> · {({ missing: "Thiếu nội dung", unsupported: "Không có bằng chứng", translation: "Bản dịch", terminology: "Tên/thuật ngữ", duplicate: "Trùng câu", truncated: "Câu bị cắt", timing: "Thời gian", readability: "Khó đọc" } as Record<string, string>)[proposal.issue] || proposal.issue} · {({ pending: "Chờ duyệt", applied: "Đã áp dụng", skipped: "Đã bỏ qua", conflict: "Xung đột — cần kiểm tra lại" })[proposal.state]}{proposal.locked ? " · Cue đã khóa" : ""}</span>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))", gap: 12 }}><div><strong>Trước</strong>{proposal.before.map(c => <p key={c.id}>{c.text}<br /><small>{c.start_ms / 1000}–{c.end_ms / 1000}s</small></p>)}</div><div><strong>Sau</strong>{proposal.after.map(c => <p key={c.id}>{c.text}<br /><small>{c.start_ms / 1000}–{c.end_ms / 1000}s</small></p>)}</div></div>
        <p>{proposal.reason}</p><p><small>Bằng chứng: {proposal.evidence} · Mức chắc chắn do AI tự đánh giá: {proposal.certainty}</small></p>
        {proposal.state === "pending" && <div style={{ display: "flex", gap: 12 }}><button type="button" disabled={busy || running || proposal.locked} onClick={() => void apply([proposal.id])}>Áp dụng</button><button type="button" disabled={busy || running} onClick={() => void apply([proposal.id], true)}>Bỏ qua</button></div>}
      </article>)}
      <div><button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>Trước</button> {page + 1}/{pages} <button type="button" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Sau</button></div></>}
  </section>;
}
