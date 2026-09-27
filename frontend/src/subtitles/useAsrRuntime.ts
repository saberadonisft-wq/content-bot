import { useEffect, useState } from 'react';
import { api, type SubtitleAsrRuntimeStatus } from '../api';

export function useAsrRuntime(enabled: boolean) {
  const [status, setStatus] = useState<SubtitleAsrRuntimeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const value = await api.subtitleAsrRuntimeStatus(controller.signal);
        if (!controller.signal.aborted) setStatus(value);
      } catch (error) {
        if (!controller.signal.aborted) setError(String(error));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [enabled, reload]);
  return { status, error, loading, refresh: () => setReload(value => value + 1) };
}
