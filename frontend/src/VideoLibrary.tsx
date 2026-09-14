import { useEffect, useRef, useState } from "react";
import { Check, Download, FileKey, Film, Link2, LoaderCircle, RefreshCw, Search, Trash2, X } from "lucide-react";
import { api, type VideoDownloadJob, type VideoDownloadQuality, type VideoLibraryItem } from "./api";
import { getAuthHeader } from "./transport/client";
import { downloadLabel, durationLabel, extractVideoLinks, formatBytes, mediaUrl, videoMatches } from "./video-library/model";
import { VideoCard } from "./video-library/VideoCard";
import { VideoPreview } from "./video-library/VideoPreview";
import "./video-library.css";

const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Có lỗi xảy ra. Vui lòng thử lại.";
const videoKey = (video: VideoLibraryItem) => `${video.type}:${video.id}`;

export function VideoLibrary({ embedded = false }: { embedded?: boolean }) {
  const [videos, setVideos] = useState<VideoLibraryItem[]>([]);
  const [jobs, setJobs] = useState<VideoDownloadJob[]>([]);
  const [links, setLinks] = useState("");
  const [quality, setQuality] = useState<VideoDownloadQuality>("1080");
  const [autoDownload, setAutoDownload] = useState(true);
  const [cookies, setCookies] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const submissionLock = useRef(false);
  const [formError, setFormError] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [platform, setPlatform] = useState("");
  const [expandedJobs, setExpandedJobs] = useState(false);
  const [preview, setPreview] = useState<VideoLibraryItem | null>(null);
  const [actionIds, setActionIds] = useState<string[]>([]);
  const [visibleCount, setVisibleCount] = useState(24);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [deleteNotice, setDeleteNotice] = useState<{ text: string; failed: boolean } | null>(null);
  const deleteLock = useRef(false);
  const libraryRevision = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let signature = "";
    const poll = async () => {
      let active = false;
      try {
        const nextJobs = await api.videoDownloads();
        if (disposed) return;
        active = nextJobs.some(job => ["queued", "running"].includes(job.state));
        setJobs(nextJobs);
        const nextSignature = nextJobs.filter(job => job.state === "succeeded").map(job => job.id).sort().join(",");
        if (signature !== nextSignature || signature === "") {
          const revision = libraryRevision.current;
          const nextVideos = await api.videos();
          if (disposed) return;
          if (!deleteLock.current && revision === libraryRevision.current) setVideos(nextVideos);
          signature = nextSignature || "empty";
        }
        setError("");
      } catch (error) {
        if (!disposed) setError(errorMessage(error));
      } finally {
        if (!disposed) {
          setLoading(false);
          timer = setTimeout(() => void poll(), active ? 1500 : 8000);
        }
      }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [refresh]);

  const submit = async (text: string, selectedQuality = quality, clearInput = true) => {
    if (submissionLock.current) return;
    const urls = extractVideoLinks(text);
    if (!urls.length || urls.length > 20) {
      setFormError("Hãy dán từ 1 đến 20 link video http/https, mỗi link một dòng.");
      return;
    }
    submissionLock.current = true;
    setSubmitting(true);
    setFormError("");
    setMessage("");
    try {
      if (cookies && cookies.size > 1_000_000) throw new Error("File cookies tối đa 1 MB.");
      const records = await api.downloadVideos(urls, selectedQuality, cookies ? await cookies.text() : undefined);
      setJobs(current => [...records, ...current.filter(job => !records.some(record => record.id === job.id))]);
      const completed = records.filter(job => job.state === "succeeded").length;
      setMessage(completed === records.length ? "Video này đã có trong thư viện." : `Đã nhận ${records.length} link. Tiến độ tải hiển thị bên dưới.`);
      if (clearInput) setLinks("");
      setRefresh(value => value + 1);
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      submissionLock.current = false;
      setSubmitting(false);
    }
  };

  const cancel = async (job: VideoDownloadJob) => {
    setActionIds(current => [...current, job.id]);
    try {
      const canceled = await api.cancelVideoDownload(job.id);
      setJobs(current => current.map(item => item.id === canceled.id ? canceled : item));
    } catch (error) { setError(errorMessage(error)); }
    finally { setActionIds(current => current.filter(id => id !== job.id)); }
  };
  const retry = async (job: VideoDownloadJob) => {
    if (submissionLock.current) return;
    submissionLock.current = true;
    setSubmitting(true);
    setFormError("");
    try {
      if (cookies && cookies.size > 1_000_000) throw new Error("File cookies tối đa 1 MB.");
      const resumed = await api.retryVideoDownload(job.id, cookies ? await cookies.text() : undefined);
      setJobs(current => current.map(item => item.id === resumed.id ? resumed : item));
      setRefresh(value => value + 1);
    } catch (error) { setFormError(errorMessage(error)); }
    finally { submissionLock.current = false; setSubmitting(false); }
  };
  const removeVideos = async (targets: VideoLibraryItem[]) => {
    if (!targets.length || deleteLock.current) return;
    const prompt = targets.length === 1
      ? `Xóa “${targets[0].title || targets[0].filename}” khỏi thư viện?`
      : `Xóa ${targets.length} video đã chọn khỏi thư viện? Các file video trên máy đã chọn sẽ bị xóa. Không thể hoàn tác.`;
    if (!window.confirm(prompt)) return;
    deleteLock.current = true;
    libraryRevision.current += 1;
    setDeleting(true);
    setDeleteNotice(null);
    let deletedCount = 0;
    const failures: string[] = [];
    try {
      // Bound requests so selecting a large library doesn't flood the backend.
      for (let offset = 0; offset < targets.length; offset += 4) {
        const batch = targets.slice(offset, offset + 4);
        const results = await Promise.allSettled(batch.map(video => api.deleteVideo(video.id, video.type)));
        const deleted = new Set<string>();
        results.forEach((result, index) => {
          if (result.status === "fulfilled") deleted.add(videoKey(batch[index]));
          else failures.push(errorMessage(result.reason));
        });
        deletedCount += deleted.size;
        setVideos(current => current.filter(video => !deleted.has(videoKey(video))));
        setSelectedKeys(current => new Set([...current].filter(key => !deleted.has(key))));
        setDeleteNotice({ text: `Đang xóa… ${offset + batch.length}/${targets.length}`, failed: false });
      }
      setDeleteNotice({ failed: failures.length > 0, text: failures.length
        ? `Đã xóa ${deletedCount}/${targets.length} video. ${failures.length} video chưa xóa được; bạn có thể thử lại. ${failures[0]}`
        : `Đã xóa ${deletedCount} video.` });
    } finally {
      deleteLock.current = false;
      libraryRevision.current += 1;
      setDeleting(false);
      setRefresh(value => value + 1);
    }
  };
  const remove = (video: VideoLibraryItem) => removeVideos([video]);
  const save = async (video: VideoLibraryItem) => {
    if (video.type === "scraped") { await submit(video.video_url, quality, false); return; }
    try {
      const url = mediaUrl(video.video_url);
      const safeTitle = [...(video.title || "")].map(char => char.charCodeAt(0) < 32 || '<>:"/\\|?*'.includes(char) ? "_" : char).join("").slice(0, 150);
      const filename = safeTitle ? `${safeTitle}.${video.filename.split(".").pop()}` : video.filename;
      if (window.contentBotDesktop) {
        const result = await window.contentBotDesktop.downloadFile({ url, filename, accessToken: localStorage.getItem("content_bot_access_token") ?? undefined });
        if (result.state === "interrupted") throw new Error("Lưu video bị gián đoạn. Vui lòng thử lại.");
        return;
      }
      const response = await fetch(url, { headers: getAuthHeader() });
      if (!response.ok) throw new Error("Không lưu được video. Kiểm tra kết nối và thử lại.");
      const blobUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = blobUrl;
      anchor.download = filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
    } catch (error) { setError(errorMessage(error)); }
  };
  const openPreview = (video: VideoLibraryItem) => {
    if (video.type === "scraped") { window.open(mediaUrl(video.video_url), "_blank", "noopener,noreferrer"); return; }
    setPreview(video);
  };

  const filtered = videos.filter(video => videoMatches(video, query, filter, platform));
  const selectedVideos = filtered.filter(video => selectedKeys.has(videoKey(video)));
  const allSelected = filtered.length > 0 && selectedVideos.length === filtered.length;
  const toggleVideo = (video: VideoLibraryItem) => setSelectedKeys(current => {
    const next = new Set(current);
    const key = videoKey(video);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const platforms = [...new Set(videos.map(video => video.platform).filter((value): value is string => !!value))].sort();
  const activeJobs = jobs.filter(job => ["queued", "running"].includes(job.state));
  const recentJobs = jobs.filter(job => !["queued", "running"].includes(job.state));
  const visibleJobs = [...activeJobs, ...(expandedJobs ? recentJobs : recentJobs.slice(0, Math.max(0, 3 - activeJobs.length)))];
  const localVideos = videos.filter(video => video.type !== "scraped");

  return <div className="video-library">
    {!embedded && <header className="vl-heading"><h1>Tải & quản lý video</h1><p>Dán link, tải video về máy và lưu vào thư viện của bạn.</p></header>}
    <form className="vl-import" onSubmit={event => { event.preventDefault(); void submit(links); }}
      onDragOver={event => { if (event.dataTransfer.types.includes("text/plain") || event.dataTransfer.types.includes("text/uri-list")) event.preventDefault(); }}
      onDrop={event => {
        const text = event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain");
        if (!text) return;
        event.preventDefault(); setLinks(text);
        if (autoDownload && !submitting) void submit(text);
      }}>
      <div className="vl-import-head"><Link2 size={22} aria-hidden="true" /><div><h2>Tải video từ link</h2><p>Bilibili, YouTube, TikTok và các nền tảng được bộ tải hỗ trợ.</p></div></div>
      <label htmlFor="vl-links">Link video <span className="vl-muted">· mỗi link một dòng</span></label>
      <div className="vl-input-row">
        <textarea id="vl-links" rows={2} value={links} placeholder="https://www.bilibili.com/video/BV…" disabled={submitting}
          aria-invalid={!!formError} aria-describedby={formError ? "vl-form-error" : "vl-link-help"}
          onChange={event => { setLinks(event.target.value); setFormError(""); setMessage(""); }}
          onPaste={event => {
            if (!autoDownload) return;
            const text = event.clipboardData.getData("text");
            if (!extractVideoLinks(text).length) return;
            event.preventDefault();
            const input = event.currentTarget;
            const merged = links.slice(0, input.selectionStart) + text + links.slice(input.selectionEnd);
            setLinks(merged); void submit(merged);
          }} />
        <button className="vl-button vl-primary vl-submit" type="submit" disabled={submitting || !links.trim()}>
          {submitting ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />}{submitting ? "Đang thêm…" : "Tải video"}
        </button>
      </div>
      <div className="vl-import-options">
        <label className="vl-checkbox"><input type="checkbox" checked={autoDownload} onChange={event => setAutoDownload(event.target.checked)} /> Tự tải khi dán link</label>
        <label className="vl-quality" htmlFor="vl-quality">Chất lượng <select id="vl-quality" value={quality} onChange={event => setQuality(event.target.value as VideoDownloadQuality)} disabled={submitting}>
          <option value="1080">Tối đa 1080p</option><option value="720">Tối đa 720p</option><option value="480">Tối đa 480p</option><option value="best">Cao nhất có thể</option>
        </select></label>
      </div>
      <p className="vl-help" id="vl-link-help">Nhận cả link rút gọn b23.tv và nội dung chia sẻ có kèm link. Mỗi link tải một video; chất lượng tùy nguồn và tài khoản.</p>
      <details className="vl-cookies"><summary><FileKey size={15} /> Video cần đăng nhập?</summary>
        <div><p>Chọn file cookies định dạng Netscape (.txt) xuất từ tài khoản của bạn trên nền tảng đó. Bản tạm chỉ dùng cho lượt tải này và được xóa khi xử lý xong.</p>
          <input ref={fileInput} aria-label="File cookies đăng nhập" type="file" accept=".txt" disabled={submitting} onChange={event => { setCookies(event.target.files?.[0] ?? null); setFormError(""); }} />
          {cookies && <button className="vl-button vl-secondary" type="button" onClick={() => { setCookies(null); if (fileInput.current) fileInput.current.value = ""; }}>Bỏ file cookies</button>}
        </div>
      </details>
      {formError && <p id="vl-form-error" className="vl-error" role="alert">{formError}</p>}
      {message && <p className="vl-success" role="status"><Check size={16} /> {message}</p>}
    </form>

    {jobs.length > 0 && <section className="vl-queue" aria-label="Hàng đợi tải video">
      <div className="vl-section-head"><h2>{activeJobs.length ? `Đang tải · ${activeJobs.length}` : "Lượt tải gần đây"}</h2>
        {recentJobs.length > 0 && <button className="vl-button vl-text" type="button" onClick={() => setExpandedJobs(value => !value)}>{expandedJobs ? "Thu gọn" : "Xem lịch sử"}</button>}
      </div>
      <p className="vl-help">Chuyển tab vẫn tiếp tục tải. Khi ứng dụng mở lại, lượt đang tải tự tiếp tục từ file dở nếu nguồn hỗ trợ; lượt dùng cookies cần chọn lại file đăng nhập.</p>
      <ul className="vl-jobs">{visibleJobs.map(job => <li key={job.id} className={`vl-job vl-job-${job.state}`}>
        <div className="vl-job-icon">{job.state === "succeeded" ? <Check size={20} /> : job.state === "running" ? <LoaderCircle className="spin" size={20} /> : <Download size={20} />}</div>
        <div className="vl-job-body"><div className="vl-job-title"><strong title={job.title || job.url}>{job.title || job.url}</strong><span className="vl-platform">{job.platform}</span></div>
          <div className="vl-job-status"><span>{downloadLabel(job)}</span>
            {job.state === "running" && job.phase === "downloading" && <span>{job.progress != null && `${job.progress}% · `}{formatBytes(job.downloaded_bytes)}{job.speed != null && ` · ${formatBytes(job.speed)}/s`}{job.eta != null && ` · còn ${durationLabel(job.eta)}`}</span>}
          </div>
          {job.state === "running" && <progress max={100} value={job.phase === "downloading" && job.progress != null ? job.progress : undefined} aria-label={`Tiến độ: ${job.title || job.platform}`} />}
          {job.error && <p className="vl-error">{job.error}</p>}
          {["failed", "paused"].includes(job.state) && <div className="vl-recovery-actions">
            <a className="vl-button vl-text" href={mediaUrl(job.url)} target="_blank" rel="noreferrer">Mở video gốc</a>
            <button className="vl-button vl-text" type="button" disabled={submitting} onClick={() => {
              const details = fileInput.current?.closest("details");
              if (details) details.open = true;
              fileInput.current?.scrollIntoView({ block: "center" });
              fileInput.current?.click();
            }}>Chọn cookies</button>
          </div>}
        </div>
        {["queued", "running"].includes(job.state) ? <button className="vl-button vl-icon" type="button" aria-label={`Hủy tải ${job.title || job.platform}`} disabled={actionIds.includes(job.id)} onClick={() => void cancel(job)}><X size={18} /></button>
          : ["failed", "canceled", "paused"].includes(job.state) ? <button className="vl-button vl-secondary" type="button" disabled={submitting} onClick={() => void retry(job)}><RefreshCw size={14} /> {job.state === "paused" ? "Tiếp tục" : "Thử lại"}</button> : null}
      </li>)}</ul>
    </section>}

    <section className="vl-library" aria-label="Thư viện video">
      <div className="vl-section-head"><div><h2>Thư viện video <span className="vl-count">{videos.length}</span></h2><p className="vl-help">{localVideos.length} video trên máy · {formatBytes(localVideos.reduce((sum, video) => sum + video.size_bytes, 0))}</p></div>
        <button className="vl-button vl-secondary" type="button" onClick={() => setRefresh(value => value + 1)} aria-label="Làm mới thư viện"><RefreshCw size={15} /> Làm mới</button>
      </div>
      <div className="vl-filters">
        <label className="vl-search"><Search size={17} /><input aria-label="Tìm video" placeholder="Tìm theo tên hoặc link…" value={query} disabled={deleting} onChange={event => { setQuery(event.target.value); setVisibleCount(24); setSelectedKeys(new Set()); }} /></label>
        <select aria-label="Lọc loại video" value={filter} disabled={deleting} onChange={event => { setFilter(event.target.value); setVisibleCount(24); setSelectedKeys(new Set()); }}>
          <option value="all">Tất cả video</option><option value="downloaded">Đã tải từ link</option><option value="original">Video tải lên</option><option value="subtitled">Đã ghép phụ đề</option><option value="scraped">Bài đăng đã quét</option>
        </select>
        <select aria-label="Lọc nền tảng" value={platform} disabled={deleting} onChange={event => { setPlatform(event.target.value); setVisibleCount(24); setSelectedKeys(new Set()); }}>
          <option value="">Tất cả nền tảng</option>{platforms.map(name => <option key={name} value={name}>{name}</option>)}
        </select>
      </div>
      {!loading && filtered.length > 0 && <div className="vl-selection-toolbar">
        <label className="vl-checkbox"><input type="checkbox" checked={allSelected} disabled={deleting}
          ref={element => { if (element) element.indeterminate = selectedVideos.length > 0 && !allSelected; }}
          onChange={() => setSelectedKeys(allSelected ? new Set() : new Set(filtered.map(videoKey)))} />
          Chọn tất cả ({filtered.length})
        </label>
        <span className="vl-help">{selectedVideos.length ? `Đã chọn ${selectedVideos.length} video` : "Chọn video để xóa nhiều mục"}{filtered.length > visibleCount ? " · gồm cả video chưa hiển thị" : ""}</span>
        <div className="vl-selection-actions">
          {selectedVideos.length > 0 && <button className="vl-button vl-text" type="button" disabled={deleting} onClick={() => setSelectedKeys(new Set())}>Bỏ chọn</button>}
          <button className="vl-button vl-danger" type="button" disabled={deleting || !selectedVideos.length} onClick={() => void removeVideos(selectedVideos)}>
            {deleting ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{deleting ? "Đang xóa…" : `Xóa đã chọn (${selectedVideos.length})`}
          </button>
        </div>
      </div>}
      {deleteNotice && <p className={deleteNotice.failed ? "vl-error" : "vl-success"} role={deleteNotice.failed ? "alert" : "status"}>{deleteNotice.text}</p>}
      {error && <div className="vl-error" role="alert">{error} <button className="vl-button vl-text" type="button" onClick={() => setRefresh(value => value + 1)}>Thử lại</button></div>}
      {loading ? <div className="vl-empty" role="status"><LoaderCircle className="spin" size={28} /> Đang tải thư viện…</div>
        : filtered.length ? <><div className="vl-grid">{filtered.slice(0, visibleCount).map(video => <VideoCard key={videoKey(video)} video={video} selected={selectedKeys.has(videoKey(video))} selectionDisabled={deleting} onToggleSelect={() => toggleVideo(video)} onDelete={remove} onDownload={save} onPreview={openPreview} />)}</div>
          {filtered.length > visibleCount && <button className="vl-button vl-secondary vl-load-more" type="button" onClick={() => setVisibleCount(count => count + 24)}>Xem thêm video</button>}</>
        : !error && <div className="vl-empty"><Film size={36} /><h3>{videos.length ? "Không tìm thấy video" : "Thư viện đang trống"}</h3><p>{videos.length ? "Thử từ khóa khác hoặc thay đổi bộ lọc." : "Dán link Bilibili hoặc một video khác ở trên để bắt đầu."}</p></div>}
    </section>
    {preview && <VideoPreview video={preview} onClose={() => setPreview(null)} />}
  </div>;
}
