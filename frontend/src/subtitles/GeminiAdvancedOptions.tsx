import type { GeminiSubtitleOptions } from "../api";

export function GeminiAdvancedOptions({ value, disabled, onChange }: {
  value: GeminiSubtitleOptions; disabled: boolean; onChange: (value: GeminiSubtitleOptions) => void;
}) {
  const policy = value.chunk_policy ?? { target_ms: 120000, max_chunk_ms: 600000, min_pause_ms: 800, context_ms: 2000 };
  return <details className="subtitle-generation-advanced"><summary>Thiết lập nâng cao</summary><fieldset disabled={disabled} style={{ display: "grid", gap: 10 }}>
    <p>Tự động dùng tất cả key đang bật, mỗi key xử lý một đoạn đồng thời. Key đang chờ hạn mức sẽ được dùng lại khi hết thời gian chờ.</p>
    <p>Khi gặp lỗi tạm thời, AI tự chờ và bổ sung các đoạn còn thiếu, giữ nguyên các đoạn đã xong. Có thể hủy trong lúc chờ.</p>
    <label>Độ dài đoạn mục tiêu (giây) <input type="number" min={30} max={policy.max_chunk_ms / 1000} value={policy.target_ms / 1000}
      onChange={e => onChange({ ...value, chunk_policy: { ...policy, target_ms: Math.min(policy.max_chunk_ms, Math.max(30000, Math.round(Number(e.target.value) * 1000))) } })} /></label>
    <label>Khoảng nghỉ tối thiểu để chia đoạn (giây) <input type="number" min={0.1} max={10} step={0.1} value={policy.min_pause_ms / 1000}
      onChange={e => onChange({ ...value, chunk_policy: { ...policy, min_pause_ms: Math.min(10000, Math.max(100, Math.round(Number(e.target.value) * 1000))) } })} /></label>
    <label>Căn thêm theo lời gốc <select value={value.alignment_mode ?? "off"} onChange={e => onChange({ ...value, alignment_mode: e.target.value as GeminiSubtitleOptions["alignment_mode"] })}>
      <option value="off">Tắt</option><option value="review">Chỉ cue cần kiểm tra</option><option value="all">Tất cả cue đủ điều kiện</option>
    </select></label>
    <small>Căn thêm dùng model ASR trên máy. Nếu chưa có model hoặc chưa đủ bằng chứng, phụ đề giữ timing cũ và có cảnh báo.</small>
    <small>Không cần điền ngữ cảnh. AI đối chiếu phụ đề gốc, các câu trước/sau và audio để dịch.</small>
    <label>Ghi chú dịch bổ sung (không bắt buộc) <textarea rows={3} maxLength={8000} value={value.shared_context ?? ""}
      onChange={e => onChange({ ...value, shared_context: e.target.value })} /></label>
  </fieldset></details>;
}
