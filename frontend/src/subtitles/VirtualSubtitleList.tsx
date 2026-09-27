import { LockKeyhole, Magnet, Merge, Play, Plus, Scissors, Trash2, UnlockKeyhole } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { speechEvidenceLabel, TIMING_SOURCE_LABELS } from "./model";
import { formatCompactTimecode, formatTimecode, snapMsToFrame } from "./time";
import { TimecodeInput } from "./TimecodeInput";
import type { FrameTiming, SubtitleCueV2 } from "./types";

const ROW_HEIGHT = 300;
const COMPACT_ROW_HEIGHT = 248;
const ROW_GAP = 8;
const OVERSCAN_ROWS = 2;

type VirtualSubtitleListProps = {
  title?: string;
  compact?: boolean;
  hideHeading?: boolean;
  respectLocks?: boolean;
  cues: readonly SubtitleCueV2[];
  selectedCueId: string | null;
  durationMs: number;
  frameTiming: FrameTiming;
  onAdd: () => void;
  onSelect: (cueId: string) => void;
  onSeek: (milliseconds: number) => void;
  onDelete: (cueId: string) => void;
  onSplit: (cueId: string) => void;
  onMergeNext: (cueId: string) => void;
  onChange: (
    cueId: string,
    patch: Partial<Pick<SubtitleCueV2, "start_ms" | "end_ms" | "text">>,
  ) => void;
  onAlignCue?: (cueId: string) => void;
  onToggleLock?: (cueId: string) => void;
};

export function VirtualSubtitleList({
  title = "Danh sách phụ đề",
  compact = false,
  hideHeading = false,
  respectLocks = false,
  cues,
  selectedCueId,
  durationMs,
  frameTiming,
  onAdd,
  onSelect,
  onSeek,
  onDelete,
  onSplit,
  onMergeNext,
  onChange,
  onAlignCue,
  onToggleLock,
}: VirtualSubtitleListProps) {
  const titleId = useId();
  const [scrollTop, setScrollTop] = useState(0);
  const rowHeight = compact ? COMPACT_ROW_HEIGHT : ROW_HEIGHT;
  const viewportHeight = Math.min(472, Math.max(rowHeight, cues.length * rowHeight));
  const startIndex = Math.max(
    0,
    Math.min(cues.length - 1, Math.floor(scrollTop / rowHeight)) - OVERSCAN_ROWS,
  );
  const endIndex = Math.min(
    cues.length,
    Math.max(startIndex, Math.ceil((scrollTop + viewportHeight) / rowHeight)) + OVERSCAN_ROWS,
  );
  const visibleCues = useMemo(
    () => cues.slice(startIndex, endIndex),
    [cues, endIndex, startIndex],
  );

  return (
    <section className={`subtitle-list-section${compact ? " is-compact" : ""}`}
      aria-labelledby={hideHeading ? undefined : titleId}
      aria-label={hideHeading ? title : undefined}>
      {!hideHeading && <div className="subtitle-list-heading">
        <div>
          <h4 id={titleId}>{title}</h4>
          <span>{cues.length} đoạn</span>
        </div>
        <button
          type="button"
          className="studio-icon-button"
          onClick={onAdd}
          aria-label="Thêm cue phụ đề"
          title="Thêm cue"
        >
          <Plus size={17} />
        </button>
      </div>}

      {cues.length === 0 ? <p className="subtitle-list-empty">Chưa có phụ đề. Nhấn + để thêm đoạn.</p> : <div
        className="subtitle-virtual-list"
        style={{ height: viewportHeight }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <div
          className="subtitle-virtual-list-inner"
          style={{ height: cues.length * rowHeight }}
        >
          {visibleCues.map((cue, offset) => {
            const index = startIndex + offset;
            const selected = cue.id === selectedCueId;
            const frameStart = snapMsToFrame(cue.start_ms, frameTiming);
            const frameEnd = snapMsToFrame(cue.end_ms, frameTiming);
            const copyField = (
              <label className="subtitle-copy-field">
                <span>Nội dung đoạn {index + 1}</span>
                <textarea rows={2} value={cue.text}
                  disabled={respectLocks && cue.locked}
                  onFocus={() => onSelect(cue.id)}
                  onChange={(event) => onChange(cue.id, { text: event.target.value })}
                />
              </label>
            );
            return (
              <article
                key={cue.id}
                className={`subtitle-cue-editor ${selected ? "is-selected" : ""}`}
                style={{
                  height: rowHeight - ROW_GAP,
                  transform: `translateY(${index * rowHeight}px)`,
                }}
                onClick={() => onSelect(cue.id)}
              >
                <div className="subtitle-cue-editor-head">
                  <button
                    type="button"
                    className="subtitle-seek-button"
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(cue.id);
                      onSeek(cue.start_ms);
                    }}
                    aria-label={`Tua đến ${formatTimecode(cue.start_ms)}`}
                  >
                    {compact && <span className="subtitle-cue-number" aria-hidden="true">{index + 1}</span>}
                    <Play size={13} fill="currentColor" />
                    {formatCompactTimecode(cue.start_ms)}
                  </button>
                  {!compact && <div className="subtitle-cue-badges">
                    <span className={`timing-source-badge source-${cue.timing_source}`}>
                      {speechEvidenceLabel(cue) || TIMING_SOURCE_LABELS[cue.timing_source]}
                    </span>
                    {(cue.needs_review || (cue.confidence ?? 1) < 0.65) && (
                      <span className="timing-source-badge is-warning" title="Độ tin cậy thấp; cần kiểm tra lại">
                        Cần xem
                      </span>
                    )}
                  </div>}
                  <div className="subtitle-cue-head-actions">
                    <button
                      type="button"
                      className="studio-icon-button"
                      disabled={(respectLocks && cue.locked) || cue.end_ms - cue.start_ms < 2 || cue.text.trim().length < 2}
                      onClick={(event) => {
                        event.stopPropagation();
                        onSplit(cue.id);
                      }}
                      aria-label={respectLocks ? "Tách đoạn nguồn ở giữa" : "Tách cue tại playhead"}
                      title={respectLocks ? "Tách đoạn nguồn ở giữa" : "Tách tại playhead; nếu playhead nằm ngoài cue thì tách ở giữa"}
                    >
                      <Scissors size={15} />
                    </button>
                    <button
                      type="button"
                      className="studio-icon-button"
                      disabled={(respectLocks && (cue.locked || cues[index + 1]?.locked)) || index >= cues.length - 1}
                      onClick={(event) => {
                        event.stopPropagation();
                        onMergeNext(cue.id);
                      }}
                      aria-label="Gộp cue kế tiếp"
                      title="Gộp với cue kế tiếp"
                    >
                      <Merge size={15} />
                    </button>
                    <button
                      type="button"
                      className="studio-icon-button is-danger"
                      disabled={respectLocks && cue.locked}
                      onClick={(event) => {
                        event.stopPropagation();
                        onDelete(cue.id);
                      }}
                      aria-label="Xóa cue phụ đề"
                      title="Xóa cue"
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>

                {compact && copyField}
                <div className="subtitle-time-grid">
                  <TimecodeInput
                    showKeyboardHint={!compact}
                    key={`start-${cue.id}-${cue.start_ms}`}
                    label="Bắt đầu"
                    disabled={respectLocks && cue.locked}
                    valueMs={cue.start_ms}
                    minimumMs={0}
                    maximumMs={Math.max(0, cue.end_ms - 1)}
                    frameTiming={frameTiming}
                    onCommit={(start_ms) => onChange(cue.id, { start_ms })}
                  />
                  <TimecodeInput
                    showKeyboardHint={!compact}
                    key={`end-${cue.id}-${cue.end_ms}`}
                    label="Kết thúc"
                    disabled={respectLocks && cue.locked}
                    valueMs={cue.end_ms}
                    minimumMs={cue.start_ms + 1}
                    maximumMs={Math.max(cue.start_ms + 1, durationMs || cue.end_ms + 60_000)}
                    frameTiming={frameTiming}
                    onCommit={(end_ms) => onChange(cue.id, { end_ms })}
                  />
                </div>

                <div className="subtitle-cue-actions">
                  {onToggleLock && <button type="button" className="studio-text-button" aria-pressed={!!cue.locked}
                    title={cue.locked ? 'Mở khóa đoạn' : 'Khóa đoạn'}
                    onClick={event => { event.stopPropagation(); onToggleLock(cue.id); }}>
                    {compact && (cue.locked ? <LockKeyhole size={13} /> : <UnlockKeyhole size={13} />)}
                    {cue.locked ? 'Mở khóa' : 'Khóa đoạn'}
                  </button>}
                  <button
                    type="button"
                    className="studio-text-button"
                    disabled={respectLocks && cue.locked}
                    title={`Căn theo khung hình · Mốc gần nhất: ${formatTimecode(frameStart)}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onChange(cue.id, { start_ms: frameStart, end_ms: Math.max(frameStart + 1, frameEnd) });
                    }}
                  >
                    {compact && <Magnet size={13} />}
                    {compact ? 'Căn khung' : 'Snap theo frame'}
                  </button>
                  {!compact && <span>PTS gần nhất: {formatTimecode(frameStart)}</span>}
                  {compact && (cue.needs_review || (cue.confidence ?? 1) < 0.65) && (
                    <span className="timing-source-badge is-warning" title="Độ tin cậy thấp; cần kiểm tra lại">Cần xem</span>
                  )}
                  {onAlignCue && (
                    <button
                      type="button"
                      className="studio-text-button"
                      disabled={cue.locked || cue.timing_source === "manual"}
                      title={
                        cue.timing_source === "manual"
                          ? "Cue đã chỉnh tay đang được khóa"
                          : "Căn lại cue này theo audio"
                      }
                      onClick={(event) => {
                        event.stopPropagation();
                        onAlignCue(cue.id);
                      }}
                    >
                      {cue.timing_source === "manual" ? "Đã khóa thủ công" : "Căn cue này"}
                    </button>
                  )}
                </div>

                {!compact && copyField}
              </article>
            );
          })}
        </div>
      </div>}
    </section>
  );
}
