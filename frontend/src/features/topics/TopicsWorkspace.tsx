
import {
  Activity,
  CircleStop,
  Download,
  LoaderCircle,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Settings2,
  Trash2,
  Waypoints,
  WandSparkles,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import {
  Batch,
  InsightBucket,
  InsightSummary,
  Item,
  Keyword,
  Source,
  TrendClusters,
} from "../../api";
import { api } from "../../api";
const XTimelineEmbed = lazy(() => import("../../LiveChannelWall").then(module => ({ default: module.XTimelineEmbed })));

import { fmt, insightEvidence, metric, safeExternalUrl, stateLabel } from "../shared/presentation";
export function TopicsWorkspace({
  selected,
  keywords,
  items,
  summary,
  summaryLoading,
  clusters,
  clustersLoading,
  clustersError,
  runs,
  sources,
  sourceFilter,
  setSourceFilter,
  sessionFilter,
  setSessionFilter,
  languageFilter,
  setLanguageFilter,
  sentimentFilter,
  setSentimentFilter,
  topicFilter,
  setTopicFilter,
  loading,
  running,
  batch,
  canceling,
  onSelect,
  onNew,
  onRun,
  onCancel,
  onExport,
  onRefresh,
  onEdit,
  onDelete,
}: {
  selected: Keyword | null;
  keywords: Keyword[];
  items: Item[];
  summary: InsightSummary | null;
  summaryLoading: boolean;
  clusters: TrendClusters | null;
  clustersLoading: boolean;
  clustersError: string | null;
  runs: Batch[];
  sources: Source[];
  sourceFilter: string;
  setSourceFilter: (value: string) => void;
  sessionFilter: string;
  setSessionFilter: (value: string) => void;
  languageFilter: string;
  setLanguageFilter: (value: string) => void;
  sentimentFilter: string;
  setSentimentFilter: (value: string) => void;
  topicFilter: string;
  setTopicFilter: (value: string) => void;
  loading: boolean;
  running: boolean;
  batch: Batch | null;
  canceling: boolean;
  onSelect: (id: number) => void;
  onNew: () => void;
  onRun: () => void;
  onCancel: () => void;
  onExport: string;
  onRefresh: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [captionTarget, setCaptionTarget] = useState<Item | null>(null);
  const [captionDraft, setCaptionDraft] = useState("");
  const [captionResult, setCaptionResult] = useState<Awaited<ReturnType<typeof api.cleanCaption>> | null>(null);
  const [captionLoading, setCaptionLoading] = useState(false);
  const [captionSaving, setCaptionSaving] = useState(false);
  const [captionError, setCaptionError] = useState("");
  const [captionApplied, setCaptionApplied] = useState(false);

  const openCaptionCleaner = (item: Item) => {
    setCaptionTarget(item);
    setCaptionDraft(
      item.caption_edited ?? [item.title, item.body_snippet].filter(Boolean).join("\n"),
    );
    setCaptionResult(null);
    setCaptionError("");
    setCaptionApplied(false);
  };

  const runCaptionCleaner = async () => {
    if (!captionDraft.trim() || captionLoading) return;
    setCaptionLoading(true);
    setCaptionError("");
    setCaptionApplied(false);
    try {
      setCaptionResult(await api.cleanCaption(captionDraft));
    } catch (cause) {
      setCaptionError(cause instanceof Error ? cause.message : "Không thể làm sạch caption.");
    } finally {
      setCaptionLoading(false);
    }
  };

  const applyCaptionDraft = async () => {
    if (!captionTarget || !captionDraft.trim() || captionSaving) return;
    setCaptionSaving(true);
    setCaptionError("");
    try {
      const original =
        captionTarget.caption_original ??
        [captionTarget.title, captionTarget.body_snippet].filter(Boolean).join("\n");
      await api.applyCaption(captionTarget.id, {
        original_text: original,
        edited_text: captionDraft,
      });
      try {
        await navigator.clipboard.writeText(captionDraft);
      } catch {
        // Saving the product caption does not depend on clipboard permissions.
      }
      setCaptionApplied(true);
      onRefresh();
    } catch {
      setCaptionError("Không thể lưu caption đã chỉnh sửa.");
    } finally {
      setCaptionSaving(false);
    }
  };

  return (
    <>
      <div className="canva-home-header">
        <h1>Chủ đề theo dõi</h1>
        <p className="subtle">
          Chọn một chủ đề để xem bài mới, lịch sử phiên và cấu hình thu thập.
        </p>
      </div>

      <div className="canva-section-title">
        <span>Chủ đề gần đây</span>
        <button type="button" className="canva-see-all" onClick={onNew}>
          <Plus size={16} /> Thêm chủ đề mới
        </button>
      </div>

      <div className="canva-design-grid">
        {keywords.map((keyword) => (
          <div
            key={keyword.id}
            className={`canva-design-card ${selected?.id === keyword.id ? "active" : ""}`}
            onClick={() => onSelect(keyword.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelect(keyword.id);
              }
            }}
            role="button"
            tabIndex={0}
          >
            <div className="canva-design-thumbnail">
              <Activity />
            </div>
            <div className="canva-design-info">
              <h3 className="canva-design-title">{keyword.name}</h3>
              <div className="canva-design-meta">
                <Radar /> {keyword.channels.length} kênh · {keyword.enabled ? `${keyword.interval_minutes} phút` : "thủ công"}
              </div>
            </div>
          </div>
        ))}
      </div>

      {!keywords.length && !loading && (
        <p style={{ color: '#6b7280', marginTop: 16 }}>Chưa có dự án nào. Hãy tạo một dự án mới.</p>
      )}

      {selected && (
        <section className="panel" style={{ marginTop: 40 }}>
          <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between' }}>
            <h2>Chi tiết chủ đề: {selected.name}</h2>
            <div className="toolbar" style={{ display: 'flex', gap: 8 }}>
              <button
                className="button secondary"
                onClick={onEdit}
                aria-label="Chỉnh sửa cấu hình chủ đề"
              >
                <Settings2 size={15} /> Chỉnh sửa
              </button>
              <button className="button secondary" onClick={onRefresh} aria-label="Làm mới dữ liệu">
                <RefreshCw size={15} />
              </button>
              <a className="button secondary" href={onExport} aria-disabled={!selected} aria-label="Xuất dữ liệu CSV">
                <Download size={15} />
              </a>
              <button className="button" onClick={onRun} disabled={running} aria-label="Quét chủ đề">
                {running ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}
                Quét ngay
              </button>
            </div>
          </div>
          <TopicSettingsSummary
            selected={selected}
            onEdit={onEdit}
            onDelete={onDelete}
          />
          <div className="registered-channel-list" aria-label="Kênh đã đăng ký">
            {selected.channels.map((channel) => (
              <div className="registered-channel-entry" key={channel.id ?? channel.url}>
                <a
                  className="registered-channel"
                  href={channel.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span>
                    <strong>{channel.label || channel.source_id || "Kênh"}</strong>
                    <small>{channel.url}</small>
                  </span>
                  <span className={`badge ${channel.last_status ?? ""}`}>
                    {channel.mode === "embed_only"
                      ? "xem trực tiếp"
                      : channel.last_status === "succeeded"
                        ? "đã quét"
                        : "đã lưu"}
                  </span>
                </a>
                {channel.source_id === "x" && channel.mode === "embed_only" && (
                  <Suspense fallback={<div role="status">Đang tải kênh X…</div>}><XTimelineEmbed url={channel.url} /></Suspense>
                )}
              </div>
            ))}
          </div>
          {selected && (
            <div className="analysis-filters" aria-label="Result filters">
              <select
                className="filter"
                value={sessionFilter}
                onChange={(event) => setSessionFilter(event.target.value)}
                aria-label="Lọc theo phiên"
              >
                <option value="">5 phiên gần nhất</option>
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    Phiên #{run.session_number} · {fmt(run.started_at)} · {run.new_item_count} bài mới
                  </option>
                ))}
              </select>
              <select
                className="filter"
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value)}
                aria-label="Filter by source"
              >
                <option value="">Tất cả nguồn</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.label}
                  </option>
                ))}
              </select>
              <select
                className="filter"
                value={languageFilter}
                onChange={(event) => setLanguageFilter(event.target.value)}
                aria-label="Lọc theo ngôn ngữ"
              >
                <option value="">Tất cả ngôn ngữ</option>
                <option value="vi">Tiếng Việt</option>
                <option value="en">Tiếng Anh</option>
                <option value="zh">Tiếng Trung</option>
                <option value="ja">Tiếng Nhật</option>
                <option value="ko">Tiếng Hàn</option>
                <option value="und">Không xác định</option>
              </select>
              <select
                className="filter"
                value={sentimentFilter}
                onChange={(event) => setSentimentFilter(event.target.value)}
                aria-label="Lọc theo cảm xúc"
              >
                <option value="">Tất cả cảm xúc</option>
                <option value="positive">Tích cực</option>
                <option value="negative">Tiêu cực</option>
                <option value="mixed">Hỗn hợp</option>
                <option value="neutral">Trung lập</option>
              </select>
              <select
                className="filter"
                value={topicFilter}
                onChange={(event) => setTopicFilter(event.target.value)}
                aria-label="Lọc theo chủ đề game"
              >
                <option value="">Tất cả chủ đề</option>
                <option value="bugs">Lỗi &amp; sự cố</option>
                <option value="performance">Hiệu năng</option>
                <option value="gameplay">Gameplay</option>
                <option value="updates">Cập nhật &amp; nội dung</option>
                <option value="monetization">Kiếm tiền hóa</option>
                <option value="story">Cốt truyện &amp; lịch sử</option>
                <option value="community">Cộng đồng</option>
              </select>
            </div>
          )}
          {selected && (
            <>
              {/* ── Zone 1: Trạng thái quét ── */}
              {batch && (
                <section className={`zone zone-scan ${batch.state}`} aria-label="Trạng thái quét">
                  <h3 className="zone-title">🔄 Trạng thái quét</h3>
                  <div className="notice">
                    <span>
                      Phiên #{batch.session_number}: <strong>{stateLabel(batch.state)}</strong> ·{" "}
                      {
                        batch.source_runs.filter(
                          (run) => run.state === "succeeded",
                        ).length
                      }
                      /{batch.source_runs.length} nguồn hoàn thành.
                    </span>
                    {["queued", "running"].includes(batch.state) && (
                      <button
                        className="button danger compact-button"
                        onClick={onCancel}
                        disabled={canceling}
                      >
                        {canceling ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : (
                          <CircleStop size={15} />
                        )}
                        {canceling ? "Đang hủy" : "Hủy quét"}
                      </button>
                    )}
                  </div>
                  {!!runs.length && (
                    <ol className="run-list" aria-label="Lịch sử quét gần đây">
                      {runs.slice(0, 3).map((run) => {
                        const ingested = run.source_runs.reduce(
                          (total, sourceRun) => total + sourceRun.ingested_count,
                          0,
                        );
                        const failure = run.source_runs.find(
                          (sourceRun) => sourceRun.error_message,
                        );
                        return (
                          <li key={run.id}>
                            <span className={`badge ${run.state}`}>{stateLabel(run.state)}</span>
                            <span className="mono">{fmt(run.started_at)}</span>
                            <span className="run-summary">
                              Phiên #{run.session_number} · {ingested.toLocaleString("vi-VN")} bài mới · {run.source_runs.length} kênh
                            </span>
                            {failure && (
                              <span className="run-error" role="alert">
                                {failure.channel_label || failure.source_id}: {failure.error_message}
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ol>
                  )}
                </section>
              )}

              {/* ── Zone 2: Tổng quan phân tích ── */}
              <section className="zone zone-analytics" aria-label="Tổng quan phân tích">
                <InsightOverview
                  summary={summary}
                  loading={summaryLoading}
                  sources={sources}
                />
                <StoryClusters
                  data={clusters}
                  loading={clustersLoading}
                  error={clustersError}
                  sources={sources}
                  onRetry={onRefresh}
                />
              </section>

              {/* ── Zone 3: Danh sách bài viết ── */}
              <section className="zone zone-content" aria-label="Danh sách bài viết">
                <h3 className="zone-title">📋 Danh sách bài viết</h3>
                {captionTarget && <section className="caption-cleaner" aria-label="Làm sạch caption">
                  <div className="caption-cleaner-head">
                    <div><strong>Làm sạch caption</strong><span>Giữ nguyên bản gốc, loại liên hệ/rác và cho chỉnh sửa trước khi áp dụng.</span></div>
                    <button type="button" className="button secondary compact-button" onClick={() => setCaptionTarget(null)}>Đóng</button>
                  </div>
                  <label className="caption-cleaner-field"><span>Caption bản nháp</span><textarea value={captionDraft} maxLength={20_000} rows={5} onChange={event => { setCaptionDraft(event.target.value); setCaptionApplied(false); }} /></label>
                  <div className="caption-cleaner-actions"><button type="button" className="button" disabled={!captionDraft.trim() || captionLoading || captionSaving} onClick={() => void runCaptionCleaner()}>{captionLoading ? <LoaderCircle className="spin" size={15} /> : <WandSparkles size={15} />} {captionLoading ? "Đang xử lý…" : "Làm sạch"}</button>{captionResult && <button type="button" className="button secondary" disabled={!captionDraft.trim() || captionSaving} onClick={() => void applyCaptionDraft()}>{captionSaving ? <LoaderCircle className="spin" size={15} /> : null} {captionSaving ? "Đang lưu…" : "Lưu caption"}</button>}</div>
                  {captionResult && <div className="caption-cleaner-result" role="status"><div><span>Bản làm sạch</span><p>{captionResult.cleaned_text || "(trống)"}</p></div><div className="caption-cleaner-meta"><span>Hashtag: {captionResult.hashtags.length ? captionResult.hashtags.map(tag => `#${tag}`).join(" ") : "Không có"}</span><span>Tên file: <code>{captionResult.safe_filename}</code></span></div>{captionApplied && <small>Đã lưu vào bài viết; bản chỉnh sửa cũng đã được chép vào clipboard.</small>}</div>}
                  {captionError && <p className="caption-cleaner-error" role="alert">{captionError}</p>}
                </section>}
                <table>
                  <thead>
                    <tr>
                      <th>Bài viết</th>
                      <th>Nguồn</th>
                      <th>Tương tác</th>
                      <th>Xu hướng</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <a
                            className="item-title"
                            href={safeExternalUrl(item.canonical_url)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {item.title}
                          </a>
                          <div className="mono">
                            {item.author || "Không rõ tác giả"} ·{" "}
                            {fmt(item.published_at)}
                          </div>
                          <button type="button" className="button secondary compact-button caption-cleaner-trigger" onClick={() => openCaptionCleaner(item)} aria-label={`Làm sạch caption: ${item.title}`}><WandSparkles size={13} /> Caption</button>
                          <div
                            className="insight-line"
                            aria-label={`Phân tích: ${item.insights.language.label}, cảm xúc ${item.insights.sentiment.label}${item.insights.topics.length ? `, chủ đề ${item.insights.topics.map((topic) => topic.label).join(", ")}` : ""}`}
                          >
                            <span className="insight-chip language">
                              {item.insights.language.code.toUpperCase()} ·{" "}
                              {Math.round(
                                item.insights.language.confidence * 100,
                              )}
                              %
                            </span>
                            <span
                              className={`insight-chip sentiment ${item.insights.sentiment.label}`}
                            >
                              {item.insights.sentiment.label}
                            </span>
                            {item.insights.topics.slice(0, 3).map((topic) => (
                              <span className="insight-chip" key={topic.id}>
                                {topic.label}
                              </span>
                            ))}
                          </div>
                          {!!insightEvidence(item).length && (
                            <div className="insight-evidence">
                              Tín hiệu: {insightEvidence(item).join(", ")}
                            </div>
                          )}
                        </td>
                        <td>
                          <span className="badge">{item.source_id}</span>
                        </td>
                        <td className="metrics">{metric(item, sources)}</td>
                        <td>
                          <span className="score">
                            {item.trend_score.toFixed(1)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!items.length && !loading && (
                  <Empty onNew={onRun} action="Quét ngay" compact />
                )}
              </section>
            </>
          )}
        </section>
      )}
    </>
  );
}

function StoryClusters({
  data,
  loading,
  error,
  sources,
  onRetry,
}: {
  data: TrendClusters | null;
  loading: boolean;
  error: string | null;
  sources: Source[];
  onRetry: () => void;
}) {
  const sourceLabels = new Map(
    sources.map((source) => [source.id, source.label]),
  );
  return (
    <section
      className="story-clusters"
      aria-labelledby="story-clusters-title"
      aria-busy={loading}
    >
      <div className="story-clusters-head">
        <div>
          <h3 id="story-clusters-title">
            <Waypoints size={15} aria-hidden="true" /> 📰 Nhóm tin tức liên quan
          </h3>
          <p>
            Liên kết chung hoặc tiêu đề tương tự giữa các nguồn khác nhau.
          </p>
        </div>
        {data && (
          <span className="story-cluster-count">
            {data.truncated
              ? `Top ${data.returned_cluster_count.toLocaleString("vi-VN")} of ${data.cluster_count.toLocaleString("vi-VN")}`
              : `${data.cluster_count.toLocaleString("vi-VN")} clusters`} ·{" "}
            {data.returned_clustered_items.toLocaleString("vi-VN")} shown
          </span>
        )}
      </div>
      {loading && !data && (
        <div className="story-skeleton" aria-label="Loading story clusters">
          <span />
          <span />
        </div>
      )}
      {error && (
        <div className="story-error" role="alert">
          <span>Không tải được nhóm tin. {error}</span>
          <button className="button secondary compact-button" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" /> Thử lại
          </button>
        </div>
      )}
      {data && data.clusters.length > 0 && (
        <div className="story-cluster-list">
          {data.clusters.map((cluster) => (
            <details className="story-cluster" key={cluster.id}>
              <summary>
                <span className="story-cluster-label" title={cluster.label}>
                  {cluster.label}
                </span>
                <span className="story-cluster-meta">
                  {cluster.item_count.toLocaleString("vi-VN")} bài ·{" "}
                  {cluster.origin_count.toLocaleString("vi-VN")} nguồn · đỉnh{" "}
                  {cluster.max_trend_score.toFixed(1)}
                </span>
              </summary>
              <div className="story-cluster-body">
                <dl>
                  <div>
                    <dt>Nguồn gốc</dt>
                    <dd>{cluster.item_hosts.join(", ")}</dd>
                  </div>
                  <div>
                    <dt>Nguồn dữ liệu</dt>
                    <dd>
                      {cluster.source_ids
                        .map((id) => sourceLabels.get(id) ?? id)
                        .join(", ")}
                    </dd>
                  </div>
                  <div>
                    <dt>Lý do nhóm</dt>
                    <dd>{cluster.match_reasons.join(" · ")}</dd>
                  </div>
                </dl>
                <ol className="story-members">
                  {cluster.items.slice(0, 6).map((item) => (
                    <li key={item.id}>
                      <a
                        href={safeExternalUrl(item.canonical_url)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {item.title}
                      </a>
                      <span className="mono">
                        {item.item_host} · {item.sentiment} ·{" "}
                        {item.trend_score.toFixed(1)}
                      </span>
                    </li>
                  ))}
                </ol>
                {cluster.items.length > 6 && (
                  <p className="story-more">
                    {cluster.items.length - 6} thành viên khác có sẵn từ API.
                  </p>
                )}
              </div>
            </details>
          ))}
        </div>
      )}
      {data && !data.clusters.length && !error && (
        <p className="story-empty">
          {data.total_items > 0
            ? `Không có nhóm tin lặp lại vượt ngưỡng từ ${data.total_items.toLocaleString("vi-VN")} bài viết đã lọc.`
            : "Chưa có bài viết nào. Quét ít nhất hai nguồn công khai để tìm nhóm tin."}
        </p>
      )}
      {data && (
        <p className="story-caveat">
          Kiểm tra từng thành viên trước khi coi nhóm này là một câu chuyện. Phương pháp:{" "}
          {data.method}.
        </p>
      )}
    </section>
  );
}

function InsightOverview({
  summary,
  loading,
  sources,
}: {
  summary: InsightSummary | null;
  loading: boolean;
  sources: Source[];
}) {
  const sourceLabels = new Map(
    sources.map((source) => [source.id, source.label]),
  );
  return (
    <section
      className="insight-overview"
      aria-labelledby="insight-overview-title"
      aria-busy={loading}
    >
      <div className="insight-overview-head">
        <div>
          <h3 id="insight-overview-title">📈 Tổng quan phân tích</h3>
          <p>
            {loading && !summary
              ? "Đang cập nhật dữ liệu…"
              : `${summary?.total_items.toLocaleString("vi-VN") ?? 0} bài viết công khai`}
          </p>
        </div>
        {summary && (
          <span className="summary-coverage">
            {summary.topic_coverage_count.toLocaleString("vi-VN")} đã gắn chủ đề ·{" "}
            {summary.topic_coverage_percentage.toLocaleString("vi-VN")}%
          </span>
        )}
      </div>
      {summary && summary.total_items > 0 ? (
        <>
          <div className="insight-overview-grid">
            <div className="insight-group">
              <h4>Cảm xúc</h4>
              <SummaryBuckets rows={summary.sentiments} />
            </div>
            <div className="insight-group">
              <h4>Chủ đề game</h4>
              <SummaryBuckets rows={summary.topics.slice(0, 5)} />
            </div>
            <div className="insight-group">
              <h4>Nguồn</h4>
              <SummaryBuckets
                rows={summary.sources.slice(0, 5)}
                labelFor={(row) => sourceLabels.get(row.id) ?? row.label}
              />
            </div>
            <div className="insight-group">
              <h4>Từ khóa nổi bật</h4>
              {summary.top_signals.length ? (
                <ul className="signal-list">
                  {summary.top_signals.slice(0, 6).map((signal) => (
                    <li key={signal.id}>
                      <span>{signal.label}</span>
                      <span className="mono">{signal.count}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="summary-empty">Không phát hiện tín hiệu nào.</p>
              )}
            </div>
          </div>
          {!!summary.top_items.length && (
            <div className="rising-strip">
              <h4>🔥 Bài viết xu hướng cao nhất</h4>
              <ol>
                {summary.top_items.slice(0, 5).map((item) => (
                  <li key={item.id}>
                    <a
                      href={safeExternalUrl(item.canonical_url)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.title}
                    </a>
                    <span className="mono">
                      {sourceLabels.get(item.source_id) ?? item.source_id} ·{" "}
                      {item.sentiment} · {item.trend_score.toFixed(1)}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </>
      ) : (
        !loading && (
          <p className="summary-empty">
            Không có bài viết nào phù hợp với bộ lọc hiện tại.
          </p>
        )
      )}
      {summary && (
        <p className="summary-caveat">
          {summary.caveat} Phương pháp: {summary.method}.
        </p>
      )}
    </section>
  );
}

function SummaryBuckets({
  rows,
  labelFor = (row) => row.label,
}: {
  rows: InsightBucket[];
  labelFor?: (row: InsightBucket) => string;
}) {
  if (!rows.length) {
    return <p className="summary-empty">Không phát hiện danh mục nào.</p>;
  }
  return (
    <ul className="summary-buckets">
      {rows.map((row) => (
        <li key={row.id}>
          <div className="bucket-label">
            <span>{labelFor(row)}</span>
            <span className="mono">
              {row.count.toLocaleString("vi-VN")} ·{" "}
              {row.percentage.toLocaleString("vi-VN")}%
            </span>
          </div>
          <span className="bucket-track" aria-hidden="true">
            <span style={{ width: `${Math.min(row.percentage, 100)}%` }} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function Empty({
  onNew,
  action = "Add a tracked game",
  compact = false,
}: {
  onNew: () => void;
  action?: string;
  compact?: boolean;
}) {
  return (
    <div className={`empty ${compact ? "compact" : ""}`}>
      <Radar size={30} color="var(--cobalt)" />
      <h2>
        {compact ? "Chưa có bài viết phù hợp" : "Bắt đầu với một game"}
      </h2>
      <p>
        {compact
          ? "Bảng dữ liệu sẽ trống cho đến khi nguồn được cấu hình trả về nội dung công khai."
          : "Tạo bộ từ khóa, sau đó chạy quét từ các nguồn đã cấu hình."}
      </p>
      <button className="button" onClick={onNew}>
        <Plus size={15} /> {action}
      </button>
    </div>
  );
}
function TopicSettingsSummary({
  selected,
  onEdit,
  onDelete,
}: {
  selected: Keyword | null;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    selected ? (
      <section className="topic-settings-summary" aria-label="Cấu hình chủ đề">
        <div className="topic-settings-heading">
          <div>
            <span className="eyebrow">Cấu hình chủ đề</span>
            <strong>{selected.enabled ? "Tự động đang bật" : "Đang chạy thủ công"}</strong>
          </div>
          <div className="toolbar">
            <button className="button secondary" onClick={onEdit}>
              <Settings2 size={15} /> Sửa cấu hình
            </button>
            <button className="button danger" onClick={onDelete}>
              <Trash2 size={15} /> Xóa chủ đề
            </button>
          </div>
        </div>
        <dl className="topic-settings-grid">
          <div>
            <dt>Từ khóa bao gồm</dt>
            <dd>{selected.include_terms.join(", ") || selected.name}</dd>
          </div>
          <div>
            <dt>Từ khóa loại trừ</dt>
            <dd>{selected.exclude_terms.join(", ") || "Không có"}</dd>
          </div>
          <div>
            <dt>Giới hạn</dt>
            <dd>{selected.max_items_per_source} bài / kênh</dd>
          </div>
          <div>
            <dt>Chu kỳ</dt>
            <dd>{selected.enabled ? `${selected.interval_minutes} phút` : "Thủ công"}</dd>
          </div>
        </dl>
      </section>
    ) : null
  );
}
