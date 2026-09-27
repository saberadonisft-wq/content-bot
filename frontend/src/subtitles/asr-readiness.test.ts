import { describe, expect, it } from 'vitest';
import { asrReadiness } from './asr-readiness';

const status = { dependency_ready: true, allow_download: false, message: null,
  models: [{ id: 'small', state: 'ready' as const }],
  devices: [{ id: 'cpu' as const, compute_types: ['int8'] }, { id: 'cuda' as const, compute_types: ['float16', 'int8'] }] };

describe('ASR runtime readiness', () => {
  it('uses CUDA for auto only when the status exposes a real device', () => {
    expect(asrReadiness(status, 'small', 'auto', 'auto')).toMatchObject({ canRun: true, device: { id: 'cuda' } });
    expect(asrReadiness({ ...status, devices: [status.devices[0]] }, 'small', 'auto', 'auto')).toMatchObject({ canRun: true, device: { id: 'cpu' } });
  });
  it('does not claim readiness for an unsupported selected precision', () => {
    expect(asrReadiness(status, 'small', 'cpu', 'float16')).toMatchObject({ canRun: false, message: expect.stringContaining('không hỗ trợ') });
  });
  it('surfaces a missing CUDA provider instead of claiming GPU readiness', () => {
    expect(asrReadiness({ ...status, devices: [status.devices[0]], runtimes: { cuda: { ready: false, message: 'Thiếu runtime cuBLAS.' } } }, 'small', 'cuda', 'float16'))
      .toMatchObject({ canRun: false, message: 'Thiếu runtime cuBLAS.' });
  });
  it('explains a missing local model when downloading is disabled', () => {
    expect(asrReadiness(status, 'medium', 'cpu', 'auto')).toMatchObject({ canRun: false, message: expect.stringContaining('chưa cài') });
  });
});
