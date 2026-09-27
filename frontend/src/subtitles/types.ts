export type SubtitleTimingSource =
  | "manual"
  | "gemini_estimate"
  | "asr_word"
  | "forced_alignment"
  | "imported_srt"
  | "imported_vtt"
  | "ocr"
  | "asr";

export type SubtitleOcrRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ExtractionMode = "ocr" | "asr" | "gemini";

export type AlignmentMethod = 'asr_observed' | 'ctc_aligned' | 'energy_estimated' | 'interpolated' | 'manual';
export type SpeechEvidence = {
  method: AlignmentMethod; audio_identity: string | null; transcript_sha256: string;
  start_ms: number; end_ms: number; algorithm: string; transcript_complete: boolean;
};
export type SubtitleWordV2 = {
  id: string;
  text: string;
  start_ms: number;
  end_ms: number;
  confidence?: number | null;
  alignment_method?: AlignmentMethod | null;
};

export type SubtitlePosition = { x: number; y: number };

export type SubtitleCueV2 = {
  layout?: SubtitlePosition | null;
  id: string;
  start_ms: number;
  end_ms: number;
  speech_start_ms?: number | null;
  speech_end_ms?: number | null;
  speech_evidence?: SpeechEvidence | null;
  text: string;
  secondary_text?: string | null;
  source_text?: string | null;
  source_language?: string | null;
  content_source?: "audio" | "screen" | "mixed" | "unknown";
  origin_chunk_id?: string | null;
  origin_model?: string | null;
  locked?: boolean;
  words?: SubtitleWordV2[] | null;
  timing_source: SubtitleTimingSource;
  timing_precision_ms: number;
  confidence?: number | null;
  needs_review: boolean;
  revision: number;
};

export type SubtitleDocumentV2 = {
  schema_version: 2;
  media_fingerprint?: string | null;
  document_role?: 'source' | 'translation' | null;
  source_revision?: number | null;
  source_run_id?: string | null;
  translation_models?: string[];
  revision?: number;
  run_id?: string | null;
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
  start_ms?: number;
  end_ms?: number;
  cue_ids?: string[];
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

export type VideoClip = {
  id: string;
  start_ms: number;
  end_ms: number;
};

export type VideoDimensions = {
  width: number;
  height: number;
};

export type OverlayLayout = {
  x: number;
  y: number;
  width: number;
};

export type SubtitleMaskShape = "rectangle" | "rounded" | "ellipse" | "band";

export type SubtitleMaskEffect = "blur" | "pixelate" | "solid" | "darken";

export type SubtitleMaskRegion = {
  id: string;
  shape: SubtitleMaskShape;
  effect: SubtitleMaskEffect;
  x: number;
  y: number;
  width: number;
  height: number;
  strength: number;
  opacity: number;
  feather: number;
  cornerRadius: number;
  color: string;
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
