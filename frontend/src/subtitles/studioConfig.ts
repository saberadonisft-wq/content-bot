import {
  type SubtitleBurnOptions,
  type SubtitleCueV2,
  type SubtitleDocumentV2,
  type SubtitleRenderOptionsV2
} from "../api";
import {
  type VideoClip
} from "../subtitles/types";

export const GEMINI_PROMPT_TEMPLATE = `Bạn là biên tập viên phụ đề chuyên nghiệp cho video hội thoại. Hãy xem và nghe TOÀN BỘ video, xác định lời thoại theo audio và dịch sang tiếng Việt. Kết quả sẽ được đưa qua một bước forced alignment riêng, vì vậy bạn phải trung thực về timing và không được bịa độ chính xác.

GIỚI HẠN VIDEO — BẮT BUỘC
- Thời điểm kết thúc chính xác là VIDEO_END_MS={{VIDEO_DURATION_MS}} ms.
- Mọi cue phải thỏa \`0 <= start_ms < end_ms <= {{VIDEO_DURATION_MS}}\`. Cue bắt đầu tại hoặc sau VIDEO_END_MS là không hợp lệ và phải bị loại bỏ.
- Khi video kết thúc, dừng ngay. Nếu câu hoặc cảnh bị cắt, không hoàn thành phần còn lại, không suy diễn cảnh tiếp theo và không dùng kiến thức về phim/video gốc để viết tiếp.
- Khoảng dài không có lời thoại/phụ đề là hợp lệ. Không tạo cue để lấp timeline hoặc đạt một số lượng cue tối thiểu.

MỤC TIÊU VÀ THỨ TỰ ƯU TIÊN
1. Xác định đúng đoạn hội thoại thực sự được nói, không tóm tắt và không tự thêm lời.
2. Xác định ngôn ngữ gốc, người nói, lượt thoại và ngữ cảnh hình ảnh.
3. Chép nguyên văn lời gốc nếu nghe/đọc đủ rõ.
4. Dịch tự nhiên, đúng ý và đúng sắc thái sang tiếng Việt.
5. Ước lượng mốc thời gian theo âm thanh thực tế; mốc cuối cùng sẽ do audio alignment hiệu chỉnh.

QUY TẮC NGUỒN VÀ BẢN DỊCH
- \`text\` luôn là phụ đề tiếng Việt dùng để hiển thị.
- Nếu nhận diện được lời gốc, có thể đặt nguyên văn vào \`secondary_text\` để lưu làm dữ liệu đối chiếu/alignment; trường này là metadata nội bộ và không được hiển thị trên video.
- Nếu video có phụ đề gốc hiển thị trên hình, phụ đề gốc là nguồn transcript và mốc căn chính. Bám sát từng dòng, thứ tự, điểm bắt đầu và điểm kết thúc nhìn thấy của phụ đề gốc.
- Mỗi cue tiếng Việt phải dùng đúng thời gian của cue phụ đề gốc tương ứng nhưng chỉ hiển thị bản dịch tiếng Việt. Không dồn nhiều cue gốc thành một cue dịch và không tự tách một cue gốc thành nhiều cue dịch nếu không có bằng chứng rõ ràng.
- Khi có phụ đề gốc, giữ nguyên ranh giới cue gốc ngay cả khi câu dịch dài/ngắn khác nhau; bản dịch phải theo đúng cue đó, không theo độ dài chữ tiếng Việt.
- Nếu phụ đề gốc và audio lệch nhau, ghi nhận mốc theo phần xuất hiện thực tế trên hình, đánh dấu \`needs_review: true\` và giảm \`confidence\`; không tự làm tất cả cue liền nhau.
- Giữ nguyên tên riêng, chức danh, đại từ, quan hệ nhân vật và thuật ngữ nhất quán.
- Không đoán chữ bị che, bị nuốt âm hoặc bị tiếng ồn che. Dùng “[không rõ]” đúng tại vị trí không chắc chắn.
- Không dùng phụ đề/chữ trên hình làm bằng chứng duy nhất nếu nó không khớp với audio.
- Không tạo cue cho nhạc, hiệu ứng âm thanh hoặc chữ trên màn hình nếu không phải lời thoại.

QUY TẮC TIMING — RẤT QUAN TRỌNG
- Tất cả mốc tính từ đầu video, dùng integer milliseconds.
- Giới hạn cứng: \`0 <= start_ms < end_ms <= {{VIDEO_DURATION_MS}}\`; cue cuối cùng cũng không được vượt VIDEO_END_MS.
- Nếu có phụ đề gốc hiển thị trên hình, \`start_ms\`/\`end_ms\` phải bám theo thời điểm cue gốc xuất hiện để bản dịch xuất hiện đồng thời; nếu không có phụ đề gốc, dùng âm đầu tiên và sau âm cuối cùng của lời thoại.
- Dựa vào waveform/audio và khoảng im lặng, không dựa máy móc vào dấu phẩy, dấu chấm hay độ dài bản dịch.
- Nếu giữa hai câu có im lặng, bắt buộc để khoảng trống: \`next.start_ms > previous.end_ms\`. Không kéo cue chạm nhau chỉ để lấp timeline.
- Không dùng khoảng trống để che việc không nghe rõ; khi không chắc phải đặt \`needs_review: true\`.
- Không để cue chồng lấn nếu không có hai người thực sự nói đồng thời.
- \`timing_precision_ms\` mô tả độ tin cậy của Gemini: dùng 1000 nếu chỉ nhìn/nghe được gần từng giây; dùng 100 nếu xác định được gần 0,1 giây. Không đặt 10 hoặc 1 chỉ vì trường dữ liệu cho phép; 10 ms sẽ do bộ căn audio tạo ra.
- Không làm tròn tất cả cue thành các mốc đều kết thúc đúng giây hoặc nối liên tục.

QUY TẮC CHIA CUE
- Một cue là một lượt nói hoặc một ý tự nhiên, thường dài 1–6 giây.
- Tách khi đổi người nói, có khoảng nghỉ rõ, đổi ý hoặc câu quá dài.
- Không quá 84 ký tự tiếng Việt mỗi cue; ưu tiên tách tại khoảng nghỉ tự nhiên, không cắt giữa một cụm từ.
- Không tạo cue cực ngắn chỉ vì một tiếng động hoặc một từ không chắc chắn.
- Giữ thứ tự thời gian và ID tăng dần: s0001, s0002, …
- \`confidence\` phản ánh mức chắc chắn của cả lời thoại và timing, không được mặc định tất cả là 0.95.

ĐỊNH DẠNG ĐẦU RA
Chỉ trả về MỘT JSON hợp lệ, không Markdown, không code fence, không giải thích ngoài JSON:
{
  "schema_version": 2,
  "language": "vi",
  "timebase": "milliseconds",
  "timing_source": "gemini_estimate",
  "timing_precision_ms": 1000,
  "segments": [
    {
      "id": "s0001",
      "start_ms": 0,
      "end_ms": 2450,
      "text": "Bản dịch tiếng Việt.",
      "secondary_text": "Lời thoại nguyên văn nếu xác định được.",
      "confidence": 0.82,
      "needs_review": false
    }
  ]
}

TỰ KIỂM TRA TRƯỚC KHI TRẢ KẾT QUẢ
- JSON parse được và chỉ có một object JSON ở cấp cao nhất.
- schema_version = 2, language = “vi”, timebase = “milliseconds”.
- ID không trùng, đúng thứ tự, không có segment rỗng.
- start_ms/end_ms là integer, 0 <= start_ms < end_ms.
- \`max(end_ms) <= {{VIDEO_DURATION_MS}}\`. Nếu vi phạm, xóa cue nằm hoàn toàn ngoài video và cắt cue giao với điểm kết thúc về đúng VIDEO_END_MS rồi kiểm tra lại.
- Không có cue chồng lấn ngoài trường hợp hai người thực sự nói đè nhau.
- Khoảng im lặng thật được giữ nguyên, không nối các cue thành một dải liên tục.
- Không bịa timestamp 10 ms, không bịa lời thoại và không có văn bản nào ngoài JSON.`;

export const DEFAULT_OPTIONS: SubtitleBurnOptions = {
  font_name: "Arimo",
  font_size: 38,
  font_color: "#FFFFFF",
  bold: true,
  italic: false,
  underline: false,
  strikethrough: false,
  uppercase: false,
  alignment_type: "center",
  outline_color: "#000000",
  outline_width: 2,
  shadow_color: "#000000",
  shadow_width: 2,
  bg_enabled: false,
  bg_color: "#000000",
  bg_opacity: 0.75,
  spacing: 0,
  line_spacing: 1.2,
  pos_x: 50,
  pos_y: 78,
  position: "custom",
  video_speed: 1,
  volume: 1,
  fade_in: 0,
  fade_out: 0,
  aspect_ratio: "original",
  bg_fill_type: "blur",
  trim_start: 0,
  trim_end: null,
  animation: "none",
};

export const createSubtitleDocument = (
  cues: readonly SubtitleCueV2[],
): SubtitleDocumentV2 => {
  const timingSources = new Set(cues.map((cue) => cue.timing_source));
  return {
    schema_version: 2,
    language: "vi",
    timebase: "milliseconds",
    timing_source:
      timingSources.size === 1 ? cues[0]?.timing_source ?? "manual" : "manual",
    timing_precision_ms: Math.max(1, ...cues.map((cue) => cue.timing_precision_ms)),
    segments: [...cues],
  };
};

export const createRenderOptions = (
  options: SubtitleBurnOptions,
  videoClips: readonly VideoClip[],
): SubtitleRenderOptionsV2 => {
  const animation = options.animation ?? "none";
  const trimStartMs = Math.max(0, Math.round((options.trim_start ?? 0) * 1000));
  const trimEndMs = options.trim_end == null ? null : Math.round(options.trim_end * 1000);
  const renderClips = videoClips
    .map((clip) => ({
      ...clip,
      start_ms: Math.max(clip.start_ms, trimStartMs),
      end_ms: Math.min(clip.end_ms, trimEndMs ?? clip.end_ms),
    }))
    .filter((clip) => clip.end_ms > clip.start_ms);
  return {
    render_mode: animation === "none" ? "precision" : "effects",
    profile: "fast",
    encoder: "auto",
    font_name: options.font_name,
    font_size: options.font_size,
    font_color: options.font_color,
    bold: options.bold,
    italic: options.italic,
    underline: options.underline ?? false,
    strikethrough: options.strikethrough ?? false,
    uppercase: options.uppercase,
    alignment_type: options.alignment_type ?? "center",
    outline_color: options.outline_color,
    outline_width: options.outline_width,
    shadow_color: options.shadow_color,
    shadow_width: options.shadow_width,
    bg_enabled: options.bg_enabled,
    bg_color: options.bg_color,
    bg_opacity: options.bg_opacity,
    spacing: options.spacing,
    line_spacing: options.line_spacing ?? 1.2,
    pos_x: options.pos_x,
    pos_y: options.pos_y,
    position: options.position,
    video_speed: options.video_speed ?? 1,
    volume: options.volume ?? 1,
    aspect_ratio: options.aspect_ratio ?? "original",
    bg_fill_type: options.bg_fill_type ?? "blur",
    trim_start_ms: trimStartMs,
    trim_end_ms: trimEndMs,
    fade_in_ms: Math.max(0, Math.round((options.fade_in ?? 0) * 1000)),
    fade_out_ms: Math.max(0, Math.round((options.fade_out ?? 0) * 1000)),
    animation,
    video_segments: renderClips,
  };
};

export const STYLE_PRESETS: Record<string, Partial<SubtitleBurnOptions>> = {
  readable: {
    font_name: "Arimo",
    font_size: 38,
    font_color: "#FFFFFF",
    bold: true,
    outline_color: "#000000",
    outline_width: 2,
    shadow_width: 1,
    bg_enabled: false,
  },
  compact: {
    font_name: "Arial",
    font_size: 30,
    font_color: "#FFFFFF",
    bold: true,
    outline_color: "#000000",
    outline_width: 1,
    bg_enabled: true,
    bg_color: "#000000",
    bg_opacity: 0.72,
  },
  emphasis: {
    font_name: "Impact",
    font_size: 42,
    font_color: "#FFE45C",
    bold: true,
    uppercase: true,
    outline_color: "#111827",
    outline_width: 3,
    bg_enabled: false,
  },
};

