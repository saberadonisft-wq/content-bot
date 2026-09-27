import { useEffect, useState } from 'react';
import { voiceRequest } from './api';
import type { VoiceDocument } from './types';

export function VoiceTextPreview({ text, document }: { text: string; document: VoiceDocument | null }) {
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState('');
  const pronunciation = JSON.stringify(document?.pronunciation ?? {});
  const normalization = document?.text_normalization ?? 'off';
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (controller.signal.aborted) return;
      setResult(null); setError('');
      void voiceRequest<{text: string}>('/text-preview', { method: 'POST', signal: controller.signal,
        body: JSON.stringify({ text, pronunciation: JSON.parse(pronunciation), text_normalization: normalization }) })
        .then(value => { if (!controller.signal.aborted) setResult(value.text); })
        .catch(value => { if (!controller.signal.aborted) setError(String(value)); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [text, pronunciation, normalization]);
  return <div className="voice-text-preview">
    <small>Nội dung gửi cho giọng đọc</small>
    {error ? <p role="alert">{error}</p> : <p role="status">{result ?? 'Đang chuẩn bị lời đọc…'}</p>}
  </div>;
}
