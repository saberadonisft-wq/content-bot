import { useEffect, useRef, useState } from "react";
import { parseSubtitleTextV2 } from "../api/subtitles";
import type { SubtitleParseResultV2 } from "./types";

export function useSubtitleImport(options: {
  text: string;
  durationMs: number;
  onParsed: (result: SubtitleParseResultV2) => void;
  onError: (message: string | null) => void;
}) {
  const [parsing, setParsing] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const parse = async () => {
    const text = options.text.trim();
    if (!text) {
      options.onError("Chưa có kết quả Gemini. Dán JSON hoặc SRT/VTT rồi phân tích lại.");
      return;
    }
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setParsing(true);
    options.onError(null);
    try {
      const result = await parseSubtitleTextV2(text, options.durationMs || null, controller.signal);
      if (!controller.signal.aborted) options.onParsed(result);
    } catch (error) {
      if (!controller.signal.aborted) options.onError(error instanceof Error ? error.message : "Không phân tích được phụ đề. Kiểm tra dữ liệu và thử lại.");
    } finally {
      if (!controller.signal.aborted) setParsing(false);
    }
  };
  return { parsing, parse };
}
