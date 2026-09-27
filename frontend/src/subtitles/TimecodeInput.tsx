import { useId, useState, type KeyboardEvent } from "react";
import { formatTimecode, keyboardTargetMs, parseTimecode } from "./time";
import type { FrameTiming } from "./types";

type TimecodeInputProps = {
  disabled?: boolean;
  showKeyboardHint?: boolean;
  label: string;
  valueMs: number;
  minimumMs?: number;
  maximumMs?: number;
  frameTiming: FrameTiming;
  onCommit: (valueMs: number) => void;
};

export function TimecodeInput({
  disabled,
  showKeyboardHint = true,
  label,
  valueMs,
  minimumMs = 0,
  maximumMs = Number.MAX_SAFE_INTEGER,
  frameTiming,
  onCommit,
}: TimecodeInputProps) {
  const inputId = useId();
  const errorId = `${inputId}-error`;
  const [draft, setDraft] = useState(() => formatTimecode(valueMs));
  const [error, setError] = useState<string | null>(null);

  const commitDraft = () => {
    const parsed = parseTimecode(draft);
    if (parsed === null) {
      setError("Dùng định dạng HH:MM:SS.mmm.");
      return;
    }
    if (parsed < minimumMs || parsed > maximumMs) {
      setError(
        `Mốc phải nằm trong ${formatTimecode(minimumMs)}–${formatTimecode(maximumMs)}.`,
      );
      return;
    }
    setError(null);
    setDraft(formatTimecode(parsed));
    if (parsed !== valueMs) onCommit(parsed);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitDraft();
      event.currentTarget.blur();
      return;
    }
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const parsed = parseTimecode(draft) ?? valueMs;
    const direction = event.key === "ArrowUp" ? 1 : -1;
    const next = Math.min(
      maximumMs,
      Math.max(
        minimumMs,
        keyboardTargetMs(parsed, direction, event.nativeEvent, frameTiming),
      ),
    );
    setError(null);
    setDraft(formatTimecode(next));
    onCommit(next);
  };

  return (
    <div className="subtitle-time-field">
      <label htmlFor={inputId}>{label}</label>
      <input
        disabled={disabled}
        id={inputId}
        className="subtitle-time-input"
        type="text"
        inputMode="numeric"
        value={draft}
        spellCheck={false}
        title="↑↓: 1 ms · Shift: 10 ms · Ctrl: 100 ms · Alt: 1 khung hình"
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commitDraft}
        onKeyDown={handleKeyDown}
      />
      {(error || showKeyboardHint) && <span id={errorId} className="subtitle-field-helper"
        title={error ?? undefined} role={error ? "alert" : undefined}>
        {error ?? "↑↓ 1 ms · Shift 10 · Ctrl 100 · Alt 1 frame"}
      </span>}
    </div>
  );
}
