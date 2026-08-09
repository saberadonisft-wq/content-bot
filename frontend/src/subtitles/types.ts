export type SubtitleTimingSource =
  | "manual"
  | "gemini_estimate"
  | "asr_word"
  | "forced_alignment"
  | "imported_srt"
  | "imported_vtt";

export type SubtitleWordV2 = {
  id: string;
  text: string;
  start_ms: number;
  end_ms: number;
  confidence?: number | null;
};

export type SubtitleCueV2 = {
  id: string;
  start_ms: number;
  end_ms: number;
  speech_start_ms?: number | null;
  speech_end_ms?: number | null;
  text: string;
  secondary_text?: string | null;
  words?: SubtitleWordV2[] | null;
  timing_source: SubtitleTimingSource;
  timing_precision_ms: number;
  confidence?: number | null;
  needs_review: boolean;
  revision: number;
};

export type SubtitleDocumentV2 = {
  schema_version: 2;
  language: string;
  timebase: "milliseconds";
  timing_source: SubtitleTimingSource;
  timing_precision_ms: number;
  segments: SubtitleCueV2[];
};

export type SubtitleWarning = {
  code: string;
  message: string;
  cue_id?: string | null;
  related_cue_id?: string | null;
  delta_ms?: number | null;
};

export type SubtitleParseResultV2 = {
  document: SubtitleDocumentV2;
  warnings: SubtitleWarning[];
  srt: string;
  count: number;
};

export type SubtitleTimeMap = {
  trim_start_ms: number;
  trim_end_ms?: number | null;
  video_speed: string;
};

export type PreviewMode = "live" | "rendered";

export type VideoDimensions = {
  width: number;
  height: number;
};

export type OverlayLayout = {
  x: number;
  y: number;
  width: number;
};

export type MediaMetadata = {
  fingerprint: string;
  file_size_bytes: number;
  duration_ms: number;
  source_start_ms: number;
  time_base_numerator: number;
  time_base_denominator: number;
  frame_rate_numerator: number;
  frame_rate_denominator: number;
  average_fps: number;
  frame_count: number;
  is_vfr: boolean;
  frame_pts_ms: number[];
  frame_index_source: "packet_pts" | "average_fps";
  width: number;
  height: number;
  rotation: number;
  video_codec: string;
  has_audio: boolean;
  audio_codec?: string | null;
  audio_sample_rate?: number | null;
  audio_channels?: number | null;
  audio_hash?: string | null;
};

export type FrameTiming = Pick<
  MediaMetadata,
  | "frame_rate_numerator"
  | "frame_rate_denominator"
  | "is_vfr"
  | "frame_pts_ms"
>;

export const DEFAULT_FRAME_TIMING: FrameTiming = {
  frame_rate_numerator: 30,
  frame_rate_denominator: 1,
  is_vfr: false,
  frame_pts_ms: [],
};
