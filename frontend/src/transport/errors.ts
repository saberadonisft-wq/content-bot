const labels: Record<string, string> = {
  spacing: 'Giãn chữ', font_size: 'Cỡ chữ', outline_width: 'Độ dày viền',
  shadow_width: 'Độ dày bóng', video_speed: 'Tốc độ video', volume: 'Âm lượng',
  start_ms: 'Thời điểm bắt đầu', end_ms: 'Thời điểm kết thúc', text: 'Nội dung phụ đề',
};

export function responseErrorMessage(detail: unknown, status: number): string {
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') {
    return detail.message;
  }
  if (Array.isArray(detail)) {
    const messages = detail.slice(0, 3).flatMap(error => {
      if (!error || typeof error !== 'object' || typeof error.msg !== 'string') return [];
      const location = Array.isArray(error.loc) ? error.loc.filter((part: unknown) =>
        typeof part === 'string' || typeof part === 'number') as (string | number)[] : [];
      const path = location.filter(part => part !== 'body');
      const field = String(path.at(-1) ?? 'Dữ liệu');
      const cueIndex = path.indexOf('segments');
      const cue = cueIndex >= 0 && typeof path[cueIndex + 1] === 'number'
        ? `Phụ đề ${(path[cueIndex + 1] as number) + 1} · ` : '';
      const message = error.type === 'int_from_float' ? 'cần là số nguyên' : error.msg;
      return [`${cue}${labels[field] ?? path.join('.')}: ${message}`];
    });
    if (messages.length) return `Dữ liệu chưa hợp lệ: ${messages.join('; ')}`;
  }
  return `Request failed (${status})`;
}
