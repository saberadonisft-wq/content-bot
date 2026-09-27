import {
  CheckCircle2,
  Languages,
  LoaderCircle,
  ScanText,
  Settings2,
  Sparkles,
  Square,
} from "lucide-react";
import type { ReactNode } from "react";

export type SubtitlePipelineStage =
  | "idle"
  | "extracting"
  | "translating"
  | "ready-to-translate"
  | "complete"
  | "error";

type SubtitlePipelineBarProps = {
  stage: SubtitlePipelineStage;
  progress: number;
  message: string;
  actionLabel: string;
  actionDisabled?: boolean;
  running: boolean;
  onAction: () => void;
  options?: ReactNode;
};

const clampProgress = (value: number) => Math.min(100, Math.max(0, Math.round(value)));

export function SubtitlePipelineBar({
  stage,
  progress,
  message,
  actionLabel,
  actionDisabled = false,
  running,
  onAction,
  options,
}: SubtitlePipelineBarProps) {
  const value = clampProgress(progress);
  const extractState = stage === "extracting" ? "active" : value >= 50 ? "done" : "pending";
  const translateState = stage === "translating"
    ? "active"
    : stage === "complete"
      ? "done"
      : "pending";
  const ActionIcon = running
    ? Square
    : stage === "translating"
      ? Languages
      : stage === "extracting"
        ? ScanText
        : stage === "complete"
          ? CheckCircle2
          : Sparkles;

  return (
    <article className={`subtitle-pipeline-bar is-${stage}`} aria-labelledby="subtitle-pipeline-title">
      <div className="subtitle-pipeline-header">
        <div className="subtitle-pipeline-heading">
          <span className="subtitle-pipeline-icon" aria-hidden="true">
            {running ? <LoaderCircle className="spin" size={18} /> : <ScanText size={18} />}
          </span>
          <div>
            <h3 id="subtitle-pipeline-title">Tìm & dịch phụ đề</h3>
            <p role="status" aria-live="polite">{message}</p>
          </div>
        </div>
        <button
          type="button"
          className={running ? "studio-danger-button subtitle-pipeline-action" : "studio-primary-button subtitle-pipeline-action"}
          disabled={actionDisabled}
          aria-busy={running}
          onClick={onAction}
        >
          <ActionIcon size={16} fill={running ? "currentColor" : undefined} />
          {actionLabel}
        </button>
      </div>

      <div className="subtitle-pipeline-progress">
        <progress max={100} value={value} aria-label="Tiến trình tìm và dịch phụ đề" />
        <span>{value}%</span>
      </div>

      <div className="subtitle-pipeline-stages" aria-label="Các bước xử lý">
        <span data-state={extractState}>
          <ScanText size={14} aria-hidden="true" />
          <span>Tìm phụ đề</span>
        </span>
        <span data-state={translateState}>
          <Languages size={14} aria-hidden="true" />
          <span>Dịch tiếng Việt</span>
        </span>
        <span className="subtitle-pipeline-stage-status">{stage === "error" ? "Cần thử lại" : running ? "Đang xử lý" : value === 100 ? "Hoàn tất" : "Sẵn sàng"}</span>
      </div>

      {options ? (
        <details className="subtitle-pipeline-options">
          <summary><Settings2 size={15} /> Tùy chọn</summary>
          <div>{options}</div>
        </details>
      ) : null}
    </article>
  );
}
