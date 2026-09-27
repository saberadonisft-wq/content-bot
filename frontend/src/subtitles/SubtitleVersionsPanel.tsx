import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronLeft, ChevronRight, History, LoaderCircle, Save, X } from "lucide-react";
import { api } from "../api";
import type { SubtitleVersion } from "../api/subtitles";
import type { SubtitleDocumentV2 } from "./types";
import "./subtitle-versions.css";

const formatDate = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Chưa rõ thời gian" : new Intl.DateTimeFormat("vi-VN", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
};
const formatTime = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
const versionDocument = (version: SubtitleVersion): SubtitleDocumentV2 => {
  const document = version.document;
  return { ...document,
    ...(version.media_fingerprint ? { media_fingerprint: version.media_fingerprint } : {}),
    ...(!document.document_role && ['ocr', 'asr', 'translation'].includes(version.source)
      ? { document_role: version.source === 'translation' ? 'translation' as const : 'source' as const } : {}) };
};

export function SubtitleVersionsPanel({ videoId, document, sourceDocument, reloadKey, onDocument }: {
  videoId: string; document: SubtitleDocumentV2; reloadKey?: string;
  sourceDocument?: SubtitleDocumentV2 | null;
  onDocument: (document: SubtitleDocumentV2) => void;
}) {
  const [versions, setVersions] = useState<Omit<SubtitleVersion, "document">[]>([]);
  const [selected, setSelected] = useState<SubtitleVersion | null>(null);
  const [message, setMessage] = useState("");
  const [operation, setOperation] = useState<string | null>(null);
  const busy = operation !== null;
  const [loading, setLoading] = useState(true);
  const [messageTone, setMessageTone] = useState("success");
  const [reload, setReload] = useState(0);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [unreadableCount, setUnreadableCount] = useState(0);
  const [cuePage, setCuePage] = useState(0);
  const current = useRef(document);
  const activeVideo = useRef({ id: videoId });
  const currentSource = useRef(sourceDocument);
  const alive = useRef(true);
  useEffect(() => { current.current = document; }, [document]);
  useEffect(() => { activeVideo.current = { id: videoId }; }, [videoId]);
  useEffect(() => { currentSource.current = sourceDocument; }, [sourceDocument]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    void api.listSubtitleVersions(videoId, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) { setVersions(result.versions); setTotal(result.total); setUnreadableCount(result.unreadable_count ?? 0); setLoading(false); }
    }).catch(error => { if (!controller.signal.aborted) { setMessage(String(error)); setMessageTone("error"); setLoading(false); } });
    return () => controller.abort();
  }, [videoId, offset, reload, reloadKey]);
  async function choose(id: string) {
    const scope = activeVideo.current;
    const valid = () => alive.current && activeVideo.current === scope;
    setOperation(id); setMessage("");
    try { const result = await api.getSubtitleVersion(videoId, id); if (valid()) {
      setSelected({ ...result, document: versionDocument(result) }); setCuePage(0);
    } }
    catch (error) { if (valid()) { setMessage(String(error)); setMessageTone("error"); } }
    finally { if (valid()) setOperation(null); }
  }
  async function save(useSelected = false) {
    const scope = activeVideo.current;
    const valid = () => alive.current && activeVideo.current === scope;
    setOperation(useSelected ? "apply" : "save"); setMessage(""); setMessageTone("success");
    const isSource = useSelected && selected?.document.document_role === 'source';
    const before = isSource ? currentSource.current : current.current;
    try {
      const saved = before ? await api.saveSubtitleVersion(videoId, before, useSelected ? "Trước khi đổi phiên bản" : "Bản đang chỉnh", undefined, useSelected) : null;
      if (!valid()) return;
      setReload(value => value + 1);
      if (useSelected && selected) {
        // Revalidate the source at the apply boundary, even if the comparison
        // was opened before the underlying video file changed.
        const refreshed = await api.getSubtitleVersion(videoId, selected.id);
        if (!valid()) return;
        if ((isSource ? currentSource.current : current.current) !== before) { setMessageTone("error"); setMessage("Phụ đề vừa thay đổi; giữ bản đang chỉnh. Hãy chọn sử dụng lại khi sẵn sàng."); return; }
        onDocument(versionDocument(refreshed));
      } else if (before && current.current === before && saved?.document?.media_fingerprint
          && saved.document.media_fingerprint !== before.media_fingerprint) {
        onDocument({ ...before, media_fingerprint: saved.document.media_fingerprint });
      }
      setMessage(useSelected ? "Đã chọn phiên bản. Bản trước đó đã được lưu." : "Đã lưu phiên bản.");
    } catch (error) { if (valid()) { setMessage(String(error)); setMessageTone("error"); } }
    finally { if (valid()) setOperation(null); }
  }
  const start = cuePage * 10;
  return <details className="subtitle-versions-panel">
    <summary><History size={17} aria-hidden="true" /><span>Phiên bản phụ đề</span><span className="version-count">{total}</span><ChevronDown className="version-chevron" size={16} aria-hidden="true" /></summary>
    <div className="versions-body">
      <p className="versions-hint">Lưu lại các lần chỉnh sửa, so sánh và chọn bản muốn dùng.</p>
      <button className="version-save" type="button" disabled={busy} onClick={() => void save()}>
        {operation === "save" ? <LoaderCircle className="version-spinner" size={16} /> : <Save size={16} />}
        {operation === "save" ? "Đang lưu…" : "Lưu bản đang chỉnh"}
      </button>
      {loading && <p className="versions-empty" role="status">Đang tải phiên bản…</p>}
      {!loading && !versions.length && unreadableCount === 0 && <p className="versions-empty">Chưa có bản lưu. Lưu bản đang chỉnh để có thể quay lại sau.</p>}
      {unreadableCount > 0 && <p className="version-feedback is-error" role="alert">Không đọc được {unreadableCount} bản lưu trên trang này. Các bản còn lại vẫn sử dụng được.</p>}
      <ul className="version-list" aria-label="Các phiên bản đã lưu">
        {versions.map(version => <li key={version.id}>
          <button className="version-item" type="button" disabled={busy || version.media_binding === 'mismatch'} aria-pressed={selected?.id === version.id} onClick={() => void choose(version.id)}>
            <span className="version-item-copy">
              <strong title={version.name}>{version.name}</strong>
              {version.media_binding === 'mismatch' && <span>Thuộc video nguồn khác</span>}
              <span className="version-item-meta"><time dateTime={version.created_at}>{formatDate(version.created_at)}</time><span>{version.cue_count} câu</span></span>
            </span>
            {operation === version.id ? <LoaderCircle size={16} className="version-spinner" /> : selected?.id === version.id ? <Check size={16} /> : <ChevronRight size={16} />}
          </button>
        </li>)}
      </ul>
      {total > 20 && <nav className="version-pagination" aria-label="Trang phiên bản">
        <button type="button" aria-label="Trang phiên bản trước" disabled={busy || loading || offset === 0} onClick={() => { setLoading(true); setOffset(Math.max(0, offset - 20)); }}><ChevronLeft size={16} /></button>
        <span>{offset + 1}–{Math.min(offset + 20, total)} / {total} bản</span>
        <button type="button" aria-label="Trang phiên bản sau" disabled={busy || loading || offset + 20 >= total} onClick={() => { setLoading(true); setOffset(offset + 20); }}><ChevronRight size={16} /></button>
      </nav>}
      {selected && <section className="version-comparison" aria-label="So sánh phiên bản">
        <div className="version-comparison-heading"><h4>So sánh phiên bản</h4><button type="button" aria-label="Đóng so sánh" disabled={busy} onClick={() => setSelected(null)}><X size={16} /></button></div>
        <p className="versions-hint">{selected.name}</p>
        {selected.media_binding === 'unverified' && <p className="versions-hint">Bản cũ chưa có thông tin xác minh video nguồn. Hãy đối chiếu nội dung và thời gian trước khi sử dụng.</p>}
        <div className="version-compare-columns">
          {[{ title: selected.document.document_role === 'source' ? 'Nguồn đang chỉnh' : 'Đang chỉnh', cues: selected.document.document_role === 'source' ? sourceDocument?.segments ?? [] : document.segments }, { title: "Bản đã lưu", cues: selected.document.segments }].map(column => <div key={column.title}>
            <div className="version-compare-label"><strong>{column.title}</strong><span>{column.cues.length} câu</span></div>
            <div className="version-compare-cues">
              {column.cues.slice(start, start + 10).map(c => <p key={c.id}><span>{formatTime(c.start_ms)}–{formatTime(c.end_ms)}</span>{c.text}</p>)}
              {!column.cues.slice(start, start + 10).length && <p className="versions-hint">Không có câu phụ đề.</p>}
            </div>
          </div>)}
        </div>
        {Math.max(document.segments.length, selected.cue_count) > 10 && <nav className="version-pagination" aria-label="Trang câu phụ đề">
          <button type="button" aria-label="10 câu trước" disabled={!cuePage} onClick={() => setCuePage(cuePage - 1)}><ChevronLeft size={16} /></button>
          <span>Trang {cuePage + 1}</span>
          <button type="button" aria-label="10 câu sau" disabled={start + 10 >= Math.max(document.segments.length, selected.cue_count)} onClick={() => setCuePage(cuePage + 1)}><ChevronRight size={16} /></button>
        </nav>}
        <button className="version-apply" type="button" disabled={busy} onClick={() => void save(true)}>{operation === "apply" ? <LoaderCircle className="version-spinner" size={16} /> : <Check size={16} />}{operation === "apply" ? "Đang áp dụng…" : "Sử dụng bản này"}</button>
        <p className="versions-hint">Bản đang chỉnh sẽ được lưu trước khi đổi.</p>
      </section>}
      {message && <p className={`version-feedback is-${messageTone}`} role={messageTone === "error" ? "alert" : "status"}>{message}</p>}
    </div>
  </details>;
}
