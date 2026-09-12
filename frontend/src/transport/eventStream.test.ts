import { afterEach, describe, expect, it, vi } from "vitest";
import { createEventParser, streamEvents } from "./eventStream";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function response(chunks: Uint8Array[] = [], close = false, cancel = () => {}) {
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      if (close) controller.close();
    },
    cancel,
  }), { headers: { "Content-Type": "text/event-stream" } });
}

describe("authenticated event stream", () => {
  it("parses split CRLF, comments and multiple data lines", () => {
    const messages: string[] = [];
    const parse = createEventParser((data) => messages.push(data));
    parse(": heartbeat\r\ndata: first\r");
    parse("\ndata: second\r\n\r");
    parse("\ndata: next\n\n");
    expect(messages).toEqual(["first\nsecond", "next"]);
  });

  it("sends bearer headers and decodes Unicode split between bytes", async () => {
    const controller = new AbortController();
    const bytes = new TextEncoder().encode('data: {"text":"Tiếng Việt"}\n\n');
    const fetcher = vi.fn(async () => response([...bytes].map((byte) => new Uint8Array([byte]))));
    vi.stubGlobal("fetch", fetcher);
    const messages: string[] = [];
    await streamEvents("https://api.example/runs/1/events", {
      signal: controller.signal,
      headers: () => ({ Authorization: "Bearer test-token" }),
      onMessage(data) { messages.push(data); controller.abort(); },
    });
    expect(messages).toEqual(['{"text":"Tiếng Việt"}']);
    expect(fetcher).toHaveBeenCalledWith("https://api.example/runs/1/events", expect.objectContaining({
      headers: { Accept: "text/event-stream", Authorization: "Bearer test-token" },
    }));
  });

  it("reconnects with fresh credentials and cancels pending backoff", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const states: string[] = [];
    let token = "first";
    const fetcher = vi.fn(async () => response([], true));
    vi.stubGlobal("fetch", fetcher);
    const pending = streamEvents("/events", {
      signal: controller.signal, headers: () => ({ Authorization: `Bearer ${token}` }),
      onMessage: vi.fn(), onState: (state) => states.push(state),
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(states).toContain("reconnecting");
    token = "second";
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenLastCalledWith("/events", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer second" }),
    }));
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetcher).toHaveBeenCalledTimes(2);
    controller.abort();
    await pending;
    expect(states.at(-1)).toBe("closed");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("refreshes once on expiry and reconnects only when the token changed", async () => {
    const controller = new AbortController();
    let token = "expired";
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(response([new TextEncoder().encode("data: connected\n\n")]));
    vi.stubGlobal("fetch", fetcher);
    const refresh = vi.fn(async () => { token = "refreshed"; return true; });
    await streamEvents("/events", {
      signal: controller.signal, headers: () => ({ Authorization: token }),
      onMessage: () => controller.abort(), onAuthRequired: refresh,
    });
    expect(refresh).toHaveBeenCalledOnce();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenLastCalledWith("/events", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "refreshed" }),
    }));
  });

  it.each([401, 403])("stops retrying rejected credentials (%s)", async (status) => {
    const fetcher = vi.fn(async () => new Response(null, { status }));
    vi.stubGlobal("fetch", fetcher);
    const state = vi.fn();
    const refresh = vi.fn(async () => true); // No new token was obtained.
    await streamEvents("/events", {
      signal: new AbortController().signal, headers: () => ({ Authorization: "unchanged" }),
      onMessage: vi.fn(), onState: state, onAuthRequired: refresh,
    });
    expect(fetcher).toHaveBeenCalledOnce();
    expect(state).toHaveBeenLastCalledWith("unauthorized");
    expect(refresh).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
  });

  it("closes an idle reader and reports reconnecting for polling fallback", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const cancelled = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => response([], false, cancelled)));
    const state = vi.fn();
    const pending = streamEvents("/events", {
      signal: controller.signal, headers: () => ({}), onMessage: vi.fn(), onState: state,
    });
    await vi.advanceTimersByTimeAsync(30000);
    expect(cancelled).toHaveBeenCalledOnce();
    expect(state).toHaveBeenLastCalledWith("reconnecting");
    controller.abort();
    await pending;
    expect(vi.getTimerCount()).toBe(0);
  });
});
