
import {
LoaderCircle,
Play,
Radar,
RefreshCw,
Waypoints
} from "lucide-react";
import { useState } from "react";
import {
Batch,
Keyword
} from "../../api";

import { channelDisplayName,channelErrorDetail,channelNeedsPaidAccess,fmt } from "../shared/presentation";

export function RegisteredChannels({
  selected,
  batch,
  running,
  pendingChannelId,
  onRunChannel,
  onRunChannels,
}: {
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingChannelId: string | null;
  onRunChannel: (channelId: string) => void;
  onRunChannels: (channelIds: string[]) => void;
}) {
  const activeBatch =
    batch && selected && batch.keyword_id === selected.id &&
      ["queued", "running"].includes(batch.state)
      ? batch
      : null;
  const scannableIds = (selected?.channels ?? [])
    .filter((channel) => {
      const mode = channel.mode ?? "manual";
      return Boolean(channel.id) && !["embed_only", "manual", "setup_required"].includes(mode);
    })
    .map((channel) => channel.id as string);
  const [selectedChannelIds, setSelectedChannelIds] = useState<string[]>(scannableIds);
  const currentSelectedIds = selectedChannelIds.filter((id) => scannableIds.includes(id));
  const allSelected = scannableIds.length > 0 &&
    scannableIds.every((id) => currentSelectedIds.includes(id));
  const toggleChannel = (channelId: string) => {
    setSelectedChannelIds((current) =>
      current.includes(channelId)
        ? current.filter((id) => id !== channelId)
        : [...current, channelId],
    );
  };
  const toggleAll = () => {
    setSelectedChannelIds(allSelected ? [] : scannableIds);
  };

  if (!selected) {
    return (
      <div className="empty compact">
        <Radar size={30} color="var(--cobalt)" />
        <h2>Chưa chọn chủ đề</h2>
        <p>Chọn một chủ đề ở mục Chủ đề để xem và quét các kênh đã đăng ký.</p>
      </div>
    );
  }
  if (!selected.channels.length) {
    return (
      <div className="empty compact">
        <Waypoints size={30} color="var(--cobalt)" />
        <h2>Chủ đề này chưa có kênh</h2>
        <p>Mở trang Chủ đề và chỉnh sửa cấu hình để thêm link kênh cần theo dõi.</p>
      </div>
    );
  }
  return (
    <div>
      <div className="registered-channel-selection" aria-label="Chọn kênh để quét">
        <label className="channel-select-all">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={toggleAll}
            disabled={!scannableIds.length || Boolean(activeBatch) || running}
          />
          <span>Chọn tất cả</span>
        </label>
        <span className="channel-selection-count">
          {currentSelectedIds.length}/{scannableIds.length} kênh có thể quét
        </span>
        <button
          className="button"
          type="button"
          onClick={() => onRunChannels(currentSelectedIds)}
          disabled={!currentSelectedIds.length || Boolean(activeBatch) || running}
        >
          <Play size={15} /> Quét kênh đã chọn
        </button>
      </div>
      {!scannableIds.length && (
        <p className="channel-selection-hint">
          Các kênh hiện tại chỉ xem hoặc cần cấu hình nên chưa thể quét tự động.
        </p>
      )}
      <div className="registered-channel-grid">
        {selected.channels.map((channel) => {
          const channelRun = batch?.source_runs.find(
            (run) => run.channel_id === channel.id,
          );
          const cannotScan = ["embed_only", "manual", "setup_required"].includes(
            channel.mode ?? "manual",
          );
          const needsPaidAccess = channelNeedsPaidAccess(channel);
          const canSelect = Boolean(channel.id) && !cannotScan;
          const isSelected = Boolean(channel.id && currentSelectedIds.includes(channel.id));
          return (
            <article className="registered-channel-card" key={channel.id ?? channel.url}>
              <div className="registered-channel-card-head">
                <label className="channel-select-checkbox">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => channel.id && toggleChannel(channel.id)}
                    disabled={!canSelect || Boolean(activeBatch) || running}
                    aria-label={`Chọn kênh ${channelDisplayName(channel)}`}
                  />
                </label>
                <div style={{ minWidth: 0 }}>
                  <span className="eyebrow">{channel.source_id ?? "web"}</span>
                  <h2 title={channel.url} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{channelDisplayName(channel)}</h2>
                </div>
                <span className={`badge ${channel.last_status ?? ""}`}>
                  {channel.mode === "embed_only"
                    ? "Chỉ xem"
                    : channel.last_status === "succeeded"
                      ? "Đã quét"
                      : channel.last_status === "failed"
                        ? "Có lỗi"
                        : "Đã lưu"}
                </span>
              </div>
              <a href={channel.url} target="_blank" rel="noreferrer" className="channel-url" style={{ fontSize: 12, opacity: 0.7, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "block" }}>
                {channel.normalized_url || channel.url}
              </a>
              <dl className="channel-meta-grid">
                <div>
                  <dt>Quét gần nhất</dt>
                  <dd>{fmt(channel.last_scanned_at)}</dd>
                </div>
                <div>
                  <dt>Bài mới gần nhất</dt>
                  <dd>{channelRun?.ingested_count ?? 0}</dd>
                </div>
              </dl>
              {channel.last_error && channel.mode !== "embed_only" && (
                <p className="channel-error">{channelErrorDetail(channel)}</p>
              )}
              <div className="toolbar registered-channel-actions">
                <a className="button secondary" href={channel.url} target="_blank" rel="noreferrer">
                  Mở kênh
                </a>
                {!cannotScan && channel.id && (
                  <button
                    className="button"
                    type="button"
                    onClick={() => onRunChannel(channel.id as string)}
                    disabled={Boolean(activeBatch) || running}
                  >
                    {pendingChannelId === channel.id ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : needsPaidAccess ? (
                      <RefreshCw size={15} />
                    ) : (
                      <Play size={15} />
                    )}
                    {pendingChannelId === channel.id
                      ? "Đang khởi động"
                      : needsPaidAccess
                        ? "Thử lại X API"
                        : "Quét kênh"}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

