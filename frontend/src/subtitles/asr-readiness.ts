import type { SubtitleAsrRuntimeStatus } from '../api';

export function asrReadiness(status: SubtitleAsrRuntimeStatus | null, model: string,
  requestedDevice: 'auto' | 'cpu' | 'cuda', computeType: string) {
  if (!status?.dependency_ready) return { canRun: false, message: status?.message ?? 'Đang kiểm tra runtime ASR.' };
  const device = requestedDevice === 'auto'
    ? status.devices.find(item => item.id === 'cuda') ?? status.devices.find(item => item.id === 'cpu')
    : status.devices.find(item => item.id === requestedDevice);
  if (!device) {
    if (requestedDevice === 'cuda' && status.runtimes?.cuda?.message) {
      return { canRun: false, message: status.runtimes.cuda.message };
    }
    return { canRun: false, message: 'Thiết bị đã chọn không khả dụng. Chọn CPU hoặc Tự động.' };
  }
  if (computeType !== 'auto' && !device.compute_types.includes(computeType)) {
    return { canRun: false, message: `Kiểu tính toán ${computeType} không hỗ trợ trên ${device.id === 'cuda' ? 'GPU NVIDIA' : 'CPU'}.` };
  }
  const availableModel = status.allow_download || status.models.some(item => item.id === model && item.state === 'ready');
  if (!availableModel) return { canRun: false, message: 'Model chưa cài đầy đủ và tải tự động đang tắt.' };
  return { canRun: true, message: `Sẵn sàng: ${device.id === 'cuda' ? 'GPU NVIDIA' : 'CPU'} · ${computeType === 'auto' ? 'tự động chọn precision' : computeType}.`, device };
}
