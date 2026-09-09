import { API_BASE } from '../api';

export class VoiceApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function voiceRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await voiceFetch(path, init);
  return response.json() as Promise<T>;
}
export async function voiceFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  const token = localStorage.getItem('content_bot_access_token');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${API_BASE}/voiceover${path}`, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new VoiceApiError(typeof body.detail === 'string' ? body.detail : `Yêu cầu giọng đọc thất bại (${response.status})`, response.status);
  }
  return response;
}
export async function voiceDownload(path: string, filename: string, init?: RequestInit) {
  const response = await voiceFetch(path, init);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
