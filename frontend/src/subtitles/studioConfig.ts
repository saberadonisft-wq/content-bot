import {
  type SubtitleBurnOptions,
  type SubtitleCueV2,
  type SubtitleDocumentV2,
  type SubtitleRenderOptionsV2
} from "../api";
import {
  type VideoClip
} from "../subtitles/types";

export const GEMINI_PROMPT_TEMPLATE = `Bạn là biên tập viên phụ đề chuyên nghiệp. Hãy xem và nghe TOÀN BỘ video để dịch sang tiếng Việt. Nếu có phụ đề gốc trên hình, BẮT BUỘC dịch theo nội dung, ngữ cảnh và thời gian hiển thị của phụ đề gốc. Mỗi cue có thể là một câu hoặc một vế câu, không bắt buộc hoàn chỉnh; được tách ở dấu phẩy khi bám sát phụ đề gốc. Hoàn thiện nội dung và timing ngay trong lần tạo này; không trông chờ bước sửa hoặc căn thời gian sau đó.

GIỚI HẠN VIDEO — BẮT BUỘC
- Thời điểm kết thúc chính xác là VIDEO_END_MS={{VIDEO_DURATION_MS}} ms.
- Mọi cue phải thỏa \`0 <= start_ms < end_ms <= {{VIDEO_DURATION_MS}}\`. Cue bắt đầu tại hoặc sau VIDEO_END_MS là không hợp lệ và phải bị loại bỏ.
- Khi video kết thúc, dừng ngay. Nếu câu hoặc cảnh bị cắt, không hoàn thành phần còn lại, không suy diễn cảnh tiếp theo và không dùng kiến thức về phim/video gốc để viết tiếp.
- Khoảng dài không có lời thoại/phụ đề là hợp lệ. Không tạo cue để lấp timeline hoặc đạt một số lượng cue tối thiểu.

MỤC TIÊU VÀ THỨ TỰ ƯU TIÊN
1. Đọc từng phụ đề gốc và ghi mốc xuất hiện/biến mất; chỉ dùng audio làm nguồn chính ở vùng không có phụ đề gốc đọc được.
2. Đọc các phụ đề trước/sau và đối chiếu audio để hiểu đúng câu/vế đang dịch; không cần lập danh sách tên nhân vật hoặc xác định quan hệ.
3. Chép nguyên văn lời gốc nếu nghe/đọc đủ rõ.
4. Dịch tự nhiên, đúng ý và đúng sắc thái sang tiếng Việt.
5. Tách từng câu rồi đặt mốc theo phụ đề gốc; khi không có phụ đề gốc, bám âm đầu/âm cuối của chính câu đó. Trung thực về độ chính xác, không bịa timestamp.

QUY TẮC NGUỒN VÀ BẢN DỊCH
- \`text\` luôn là phụ đề tiếng Việt dùng để hiển thị.
- Lưu nguyên văn tương ứng từng cue vào \`source_text\`, ngôn ngữ vào \`source_language\`, nguồn vào \`content_source\`: screen nếu dựa trên chữ, mixed nếu audio xác nhận cùng lời, audio nếu không có phụ đề gốc đọc được. Không xác định được thì để null và \`needs_review: true\`.
- Nếu video có phụ đề gốc hiển thị trên hình, phụ đề gốc là nguồn transcript và mốc căn chính. Bám sát từng dòng, thứ tự, điểm bắt đầu và điểm kết thúc nhìn thấy của phụ đề gốc.
- Bám từng cue gốc với mốc xuất hiện/biến mất tương ứng, kể cả khi cue gốc chỉ là một vế câu. Không gộp các cue gốc để chờ đủ câu hoàn chỉnh, không dịch theo một bản chép audio khác khi đã đọc được phụ đề gốc. Nếu tách thêm ở dấu phẩy/ranh giới vế thì giữ trong phạm vi hiển thị khối gốc.
- Khối phụ đề gốc chứa nhiều câu vẫn phải tách riêng từng câu. Các cue con nằm trong thời gian hiển thị khối gốc, bám thay đổi chữ và ranh giới audio nếu khớp; cue đầu bám đầu khối, cue cuối bám cuối khối. Không chia đều thời gian hoặc tự gán cùng mốc cho tất cả cue con; thiếu bằng chứng về ranh giới thì đánh dấu cần kiểm tra.
- Nếu phụ đề gốc và audio lệch nhau, ghi nhận mốc theo phần xuất hiện thực tế trên hình, đánh dấu \`needs_review: true\` và giảm \`confidence\`; không tự làm tất cả cue liền nhau.
- Bám nghĩa và ngữ cảnh trước/sau của phụ đề gốc: giữ chủ thể, phủ định, câu hỏi, sắc thái, tên riêng, chức danh, đại từ, quan hệ nhân vật và thuật ngữ nhất quán. Không tóm tắt, thêm ý hay đổi người nói.
- Tự dùng ngữ cảnh có trong video, không cần người dùng điền thêm. Giữ cách xưng hô có bằng chứng trong lời gốc; không tự gán anh–em, vợ–chồng hoặc cấp bậc khi chưa rõ. Có thể lược đại từ nếu tiếng Việt vẫn tự nhiên và đúng nghĩa; nếu chưa đủ bằng chứng thì đánh dấu cần kiểm tra. Không suy ra tên/quan hệ để hoàn chỉnh lời dịch.
- Không đoán chữ bị che, bị nuốt âm hoặc bị tiếng ồn che. Dùng “[không rõ]” đúng tại vị trí không chắc chắn.
- Khi phụ đề gốc và audio khác nhau, ưu tiên phụ đề gốc đọc được và đánh dấu cần kiểm tra; không tạo hai bản dịch của cùng một lượt thoại. Chỉ dùng audio rõ để bổ sung phần chữ không đọc được.
- Phân biệt phụ đề gốc với logo, watermark, chữ trang trí. Chữ có nghĩa độc lập với lời nói vẫn được dịch riêng khi thực sự xuất hiện, kể cả trong im lặng. Không bịa lời cho nhạc hoặc hiệu ứng âm thanh.

QUY TẮC TIMING — RẤT QUAN TRỌNG
- Tất cả mốc tính từ đầu video, dùng integer milliseconds.
- Giới hạn cứng: \`0 <= start_ms < end_ms <= {{VIDEO_DURATION_MS}}\`; cue cuối cùng cũng không được vượt VIDEO_END_MS.
- Nếu có phụ đề gốc hiển thị trên hình, \`start_ms\`/\`end_ms\` phải bám theo thời điểm cue gốc xuất hiện để bản dịch xuất hiện đồng thời; nếu không có phụ đề gốc, dùng âm đầu tiên và sau âm cuối cùng của lời thoại.
- Dấu câu/ngữ nghĩa giúp chia câu; timestamp phải lấy từ hình/audio thực tế, không chia đều khoảng thời gian hoặc chia theo số ký tự bản dịch.
- Nếu giữa hai câu có im lặng và không còn phụ đề gốc trên hình, giữ khoảng trống: \`next.start_ms > previous.end_ms\`. Không kéo cue chạm nhau chỉ để lấp timeline, không tự cộng 500 ms vào mọi cue.
- Không dùng khoảng trống để che việc không nghe rõ; khi không chắc phải đặt \`needs_review: true\`.
- Các câu nối tiếp không được chồng lấn. Chỉ giữ chồng khi media thực sự có lời nói đồng thời hoặc chữ và lời khác nhau; không ép các nguồn đồng thời thành nối tiếp.
- \`timing_precision_ms\` mô tả độ tin cậy của Gemini: dùng 1000 nếu chỉ nhìn/nghe được gần từng giây; dùng 100 nếu xác định được gần 0,1 giây. Không đặt 10 hoặc 1 chỉ vì trường dữ liệu cho phép.
- Không làm tròn tất cả cue thành các mốc đều kết thúc đúng giây hoặc nối liên tục.

QUY TẮC CHIA CUE
- Mỗi cue có thể là một câu hoặc một vế câu, không cần hoàn chỉnh, thường dài 1–6 giây. Được tách ở dấu phẩy khi giữ đúng nội dung, ngữ cảnh, thứ tự và thời gian phụ đề gốc. Không ghép hai câu độc lập vào cùng cue.
- Ví dụ “Nếu người đã biết, hãy theo ta.” có thể thành “Nếu người đã biết,” và “hãy theo ta.” khi phụ đề gốc/nhịp nói hỗ trợ. Không bắt buộc tách mọi dấu phẩy, không cắt giữa tên riêng/cụm từ hoặc tự thêm lời để hoàn chỉnh vế câu.
- Tách khi đổi người nói, có khoảng nghỉ rõ, đổi ý hoặc câu quá dài.
- Mỗi cue tối đa 84 ký tự tiếng Việt, 2 dòng, 6000 ms; ưu tiên 35–60 ký tự. Câu đáp ngắn có thể dưới 1 giây, không kéo dài máy móc. Câu dài tách tại vế có nghĩa/nhịp nghỉ thực tế, giữ đủ lời và thứ tự.
- Ví dụ “Người biết chưa? Hôm nay mở tiệc. Đi thôi!” phải thành 3 cue riêng, mỗi cue một câu. Xuống dòng hoặc thay dấu chấm bằng dấu phẩy không thay thế việc tách cue. Không nhầm số thập phân, viết tắt hay dấu ba chấm với nhiều câu.
- Chia \`source_text\` tương ứng mỗi cue dịch, không lặp cả khối lời gốc vào từng cue con.
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
      "text": "Xin chào.",
      "source_text": "你好。",
      "source_language": "zh",
      "content_source": "screen",
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
- Đọc lại từng cue: một câu hoặc một vế câu bám sát phụ đề gốc, không cần hoàn chỉnh, tối đa 84 ký tự, 2 dòng, 6000 ms; tách mọi cue nhiều câu và đối chiếu lại mốc trước khi trả JSON.
- Nếu có phụ đề gốc, đối chiếu từng bản dịch với đúng câu gốc, ngữ cảnh trước/sau và mốc hiển thị; sửa ngay lỗi sai nghĩa/xưng hô hoặc trôi mốc sang câu khác.
- Rà mọi vùng chồng và mọi khoảng trống, kể cả khoảng ngắn, đầu/cuối video và toàn bộ khoảng trống dài. Chỉ bổ sung khi có lời/chữ thực sự, giữ im lặng hợp lệ và nội dung độc lập thực sự đồng thời.
- Kiểm tra hai chiều: mỗi cue có bằng chứng trong media và mỗi câu nói/chữ có nghĩa có cue tương ứng; không thiếu/lặp lời. Không đủ bằng chứng thì đánh dấu cần kiểm tra.
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

