import { useEffect, useState } from 'react';
import { API_BASE, getAuthHeader } from '../transport/client';

/** Mounted per candidate, so switching clips aborts loading and releases the WAV. */
export function SyncCandidateAudio({ auditId, clipId }: { auditId: string; clipId: string }) {
  const [requested, setRequested] = useState(false);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!requested) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void (async () => {
      try {
        const response = await fetch(`${API_BASE}/subtitles/v2/voice-sync/${auditId}/clips/${encodeURIComponent(clipId)}/audio`,
          { headers: getAuthHeader(), signal: controller.signal });
        if (!response.ok) throw new Error('Audio kiểm tra không còn khả dụng. Hãy kiểm tra lại đoạn này.');
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch (e) { if (!controller.signal.aborted) setError(String(e)); }
    })();
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [auditId, clipId, requested]);
  return <div>
    {!requested && <button type="button" onClick={() => setRequested(true)}>Nghe WAV sau căn thời gian</button>}
    {requested && !url && !error && <p>Đang tải audio kiểm tra…</p>}
    {error && <p className="voice-error">{error}</p>}
    {url && <audio controls preload="metadata" src={url} aria-label="Giọng sau căn thời gian" />}
  </div>;
}
