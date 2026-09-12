import { API_BASE, getAuthHeader } from "../transport/client";
import { streamEvents, type StreamState } from "../transport/eventStream";
import type { RunProgressEvent } from "./types";
export const runEventsUrl = (batchId: string) =>
  `${API_BASE}/runs/${batchId}/events`;

export function subscribeRunEvents(
  batchId: string,
  onEvent: (event: RunProgressEvent) => void,
  signal: AbortSignal,
  options?: { onState?: (state: StreamState) => void; onAuthRequired?: () => Promise<boolean> },
) {
  return streamEvents(runEventsUrl(batchId), {
    signal, headers: getAuthHeader, ...options,
    onMessage: (data) => {
      try {
        onEvent(JSON.parse(data) as RunProgressEvent);
      } catch {
        // Ignore malformed events; the next snapshot restores canonical state.
      }
    },
  });
}