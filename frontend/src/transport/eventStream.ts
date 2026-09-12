export type StreamState = "connecting" | "open" | "reconnecting" | "unauthorized" | "closed";

type StreamOptions = {
  signal: AbortSignal;
  headers: () => Record<string, string>;
  onMessage: (data: string) => void;
  onState?: (state: StreamState) => void;
  onAuthRequired?: () => Promise<boolean>;
};

/** Incremental SSE framing, including CRLF and multi-line data across chunks. */
export function createEventParser(onMessage: (data: string) => void) {
  let buffer = "";
  let data: string[] = [];
  let eventSize = 0;
  return (chunk: string) => {
    buffer += chunk;
    if (buffer.length + eventSize > 1024 * 1024) throw new Error("Event stream message is too large");
    let end: number;
    while ((end = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, end).replace(/\r$/, "");
      buffer = buffer.slice(end + 1);
      if (!line) {
        if (data.length) onMessage(data.join("\n"));
        data = [];
        eventSize = 0;
      } else if (line === "data" || line.startsWith("data:")) {
        const value = line === "data" ? "" : line.slice(5).replace(/^ /, "");
        data.push(value);
        eventSize += value.length;
      }
    }
  };
}

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delay);
    signal.addEventListener("abort", finish, { once: true });
    if (signal.aborted) finish();
  });
}

/** Fetch streaming attaches auth headers on every connection and bounds reconnect work. */
export async function streamEvents(url: string, options: StreamOptions): Promise<void> {
  const { signal, onState, onMessage } = options;
  let backoff = 1000;
  let authAttempts = 0;
  while (!signal.aborted) {
    onState?.("connecting");
    const connection = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    const cancel = () => {
      connection.abort();
      void reader?.cancel().catch(() => undefined);
    };
    signal.addEventListener("abort", cancel, { once: true });
    let idleTimer = setTimeout(cancel, 30000);
    try {
      const headers = options.headers();
      const response = await fetch(url, {
        headers: { Accept: "text/event-stream", ...headers },
        signal: connection.signal,
        cache: "no-store",
      });
      if (signal.aborted) break;
      if (response.status === 401 || response.status === 403) {
        await response.body?.cancel();
        if (response.status === 401 && authAttempts++ === 0 && options.onAuthRequired) {
          const refreshed = await options.onAuthRequired();
          if (refreshed && options.headers().Authorization !== headers.Authorization) continue;
        }
        onState?.("unauthorized");
        return;
      }
      if (!response.ok || !response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
        await response.body?.cancel();
        throw new Error(`Event stream unavailable (${response.status})`);
      }
      reader = response.body.getReader();
      if (signal.aborted) break;
      onState?.("open");
      authAttempts = 0;
      const decode = new TextDecoder();
      const parse = createEventParser(onMessage);
      while (!signal.aborted) {
        const { done, value } = await reader.read();
        if (done || signal.aborted) break;
        clearTimeout(idleTimer);
        idleTimer = setTimeout(cancel, 30000);
        backoff = 1000;
        parse(decode.decode(value, { stream: true }));
      }
    } catch {
      // The caller observes stream health and can poll while reconnection waits.
    } finally {
      clearTimeout(idleTimer);
      signal.removeEventListener("abort", cancel);
      connection.abort();
      await reader?.cancel().catch(() => undefined);
      reader?.releaseLock();
    }
    if (signal.aborted) break;
    onState?.("reconnecting");
    await waitForRetry(backoff, signal);
    backoff = Math.min(backoff * 2, 30000);
  }
  onState?.("closed");
}
