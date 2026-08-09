import { Merge, Play, Plus, Scissors, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { TIMING_SOURCE_LABELS } from "./model";
import { formatCompactTimecode, formatTimecode, snapMsToFrame } from "./time";
import { TimecodeInput } from "./TimecodeInput";
import type { FrameTiming, SubtitleCueV2 } from "./types";

const ROW_HEIGHT = 236;
const OVERSCAN_ROWS = 2;

type VirtualSubtitleListProps = {
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
};

export function VirtualSubtitleList({
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
}: VirtualSubtitleListProps) {
  const [scrollTop, setScrollTop] = useState(0);
  const viewportHeight = 472;
  const startIndex = Math.max(
    0,
    Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN_ROWS,
  );
  const endIndex = Math.min(
    cues.length,
    Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + OVERSCAN_ROWS,
  );
  const visibleCues = useMemo(
    () => cues.slice(startIndex, endIndex),
    [cues, endIndex, startIndex],
  );

  return (
    <section className="subtitle-list-section" aria-labelledby="subtitle-list-title">
      <div className="subtitle-list-heading">
        <div>
          <h4 id="subtitle-list-title">Danh sách phụ đề</h4>
          <span>{cues.length} cue · timebase mili-giây</span>
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
      </div>

      <div
        className="subtitle-virtual-list"
        style={{ height: viewportHeight }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <div
          className="subtitle-virtual-list-inner"
          style={{ height: cues.length * ROW_HEIGHT }}
        >
          {visibleCues.map((cue, offset) => {
            const index = startIndex + offset;
            const selected = cue.id === selectedCueId;
            const frameStart = snapMsToFrame(cue.start_ms, frameTiming);
            const frameEnd = snapMsToFrame(cue.end_ms, frameTiming);
            return (
              <article
                key={cue.id}
                className={`subtitle-cue-editor ${selected ? "is-selected" : ""}`}
                style={{ transform: `translateY(${index * ROW_HEIGHT}px)` }}
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
                    <Play size={13} fill="currentColor" />
                    {formatCompactTimecode(cue.start_ms)}
                  </button>
                  <div className="subtitle-cue-badges">
                    <span className={`timing-source-badge source-${cue.timing_source}`}>
                      {TIMING_SOURCE_LABELS[cue.timing_source]}
                    </span>
                    {(cue.needs_review || (cue.confidence ?? 1) < 0.65) && (
                      <span className="timing-source-badge is-warning" title="Độ tin cậy thấp; cần kiểm tra lại">
                        Cần xem
                      </span>
                    )}
                  </div>
                  <div className="subtitle-cue-head-actions">
                    <button
                      type="button"
                      className="studio-icon-button"
                      disabled={cue.end_ms - cue.start_ms < 2 || cue.text.trim().length < 2}
                      onClick={(event) => {
                        event.stopPropagation();
                        onSplit(cue.id);
                      }}
                      aria-label="Tách cue tại playhead"
                      title="Tách tại playhead; nếu playhead nằm ngoài cue thì tách ở giữa"
                    >
                      <Scissors size={15} />
                    </button>
                    <button
                      type="button"
                      className="studio-icon-button"
                      disabled={index >= cues.length - 1}
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

                <div className="subtitle-time-grid">
                  <TimecodeInput
                    key={`start-${cue.id}-${cue.start_ms}`}
                    label="Bắt đầu"
                    valueMs={cue.start_ms}
                    minimumMs={0}
                    maximumMs={Math.max(0, cue.end_ms - 1)}
                    frameTiming={frameTiming}
                    onCommit={(start_ms) => onChange(cue.id, { start_ms })}
                  />
                  <TimecodeInput
                    key={`end-${cue.id}-${cue.end_ms}`}
                    label="Kết thúc"
                    valueMs={cue.end_ms}
                    minimumMs={cue.start_ms + 1}
                    maximumMs={Math.max(cue.start_ms + 1, durationMs || cue.end_ms + 60_000)}
                    frameTiming={frameTiming}
                    onCommit={(end_ms) => onChange(cue.id, { end_ms })}
                  />
                </div>

                <div className="subtitle-cue-actions">
                  <button
                    type="button"
                    className="studio-text-button"
                    onClick={(event) => {
                      event.stopPropagation();
                      onChange(cue.id, { start_ms: frameStart, end_ms: Math.max(frameStart + 1, frameEnd) });
                    }}
                  >
                    Snap theo frame
                  </button>
                  <span>PTS gần nhất: {formatTimecode(frameStart)}</span>
                  {onAlignCue && (
                    <button
                      type="button"
                      className="studio-text-button"
                      disabled={cue.timing_source === "manual"}
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

                <label className="subtitle-copy-field">
                  <span>Nội dung</span>
                  <textarea
                    rows={2}
                    value={cue.text}
                    onFocus={() => onSelect(cue.id)}
                    onChange={(event) => onChange(cue.id, { text: event.target.value })}
                  />
                </label>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
