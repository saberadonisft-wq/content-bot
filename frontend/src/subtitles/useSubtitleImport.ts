import { useLayoutEffect, useRef, useState } from "react";
import { parseSubtitleTextV2 } from "../api/subtitles";
import type { SubtitleParseResultV2 } from "./types";

export function useSubtitleImport(options: {
  text: string;
  durationMs: number;
  scope?: string | null;
  snapshot?: unknown;
  onParsed: (result: SubtitleParseResultV2) => void;
  onError: (message: string | null) => void;
  onNotice?: (message: string) => void;
}) {
  const [parsing, setParsing] = useState(false);
  const active = useRef<AbortController | null>(null);
  const latest = useRef(options);
  useLayoutEffect(() => { latest.current = options; });
  useLayoutEffect(() => {
    const reset = window.setTimeout(() => setParsing(false), 0);
    return () => { window.clearTimeout(reset); active.current?.abort(); active.current = null; };
  }, [options.scope]);
  const parse = async () => {
    const text = options.text.trim();
    if (!text) {
      options.onError("Chưa có kết quả Gemini. Dán JSON hoặc SRT/VTT rồi phân tích lại.");
      return;
    }
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const captured = latest.current;
    setParsing(true);
    options.onError(null);
    try {
      const result = await parseSubtitleTextV2(text, options.durationMs || null, controller.signal);
      if (!controller.signal.aborted) {
        const current = latest.current;
        if (current.scope !== captured.scope || current.snapshot !== captured.snapshot || current.text !== captured.text) {
          current.onNotice?.('Phụ đề hoặc nội dung nhập đã thay đổi trong lúc phân tích. Giữ bản đang chỉnh; hãy phân tích lại khi sẵn sàng.');
        } else current.onParsed(result);
      }
    } catch (error) {
      if (!controller.signal.aborted) options.onError(error instanceof Error ? error.message : "Không phân tích được phụ đề. Kiểm tra dữ liệu và thử lại.");
    } finally {
      if (active.current === controller) { active.current = null; setParsing(false); }
    }
  };
  return { parsing, parse };
}
