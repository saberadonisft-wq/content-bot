import { afterEach, expect, it, vi } from "vitest";
import type { Batch, InsightSummary, RunProgressEvent, TrendClusters } from "../../api";
import { DashboardRefresh, type DashboardData } from "./dashboardRefresh";

afterEach(() => vi.useRealTimers());

const batch = (state = "running") => ({ id: "batch", keyword_id: 1, state, source_runs: [] }) as unknown as Batch;
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

function fixture() {
  vi.useFakeTimers();
  const data: DashboardData[] = [];
  const onCompleted = vi.fn();
  const onError = vi.fn();
  let event!: (event: RunProgressEvent) => void;
  let state!: (state: "open" | "reconnecting") => void;
  let streamSignal: AbortSignal | undefined;
  const api = {
    items: vi.fn(async () => ({ total: 0, items: [] })),
    insightSummary: vi.fn(async () => ({ total_items: 0 }) as InsightSummary),
    insightClusters: vi.fn(async () => ({ total_items: 0 }) as TrendClusters),
    runs: vi.fn(async () => [batch()]),
    run: vi.fn(async () => batch()),
  };
  const subscribe = vi.fn(async (_id, callback, signal, options) => {
    event = callback; state = options.onState; streamSignal = signal;
    state("open");
  });
  const create = (keywordId = 1) => new DashboardRefresh({
    keywordId, filters: {}, onData: (next) => data.push(next), onCompleted, onError,
    onAuthRequired: async () => false,
  }, { api, subscribe });
  return { api, data, create, onCompleted, onError, subscribe, event: (value: RunProgressEvent) => event(value),
    state: (value: "open" | "reconnecting") => state(value), signal: () => streamSignal };
}

it("coalesces overlapping refresh requests and polls only after completion", async () => {
  const test = fixture();
  const slow = deferred<{ total: number; items: [] }>();
  test.api.items.mockImplementationOnce(() => slow.promise);
  const controller = test.create();
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  for (let i = 0; i < 10; i++) controller.refresh();
  await vi.advanceTimersByTimeAsync(20000);
  expect(test.api.items).toHaveBeenCalledOnce();
  slow.resolve({ total: 0, items: [] });
  await vi.advanceTimersByTimeAsync(400);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(30000);
  expect(test.api.items).toHaveBeenCalledTimes(2); // Healthy SSE suppresses polling.
  controller.dispose();
});

it("ignores late results from an old keyword even if fetch ignores abort", async () => {
  const test = fixture();
  const slow = deferred<{ total: number; items: [] }>();
  test.api.items.mockImplementationOnce(() => slow.promise);
  const old = test.create();
  old.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  old.dispose();
  const next = test.create(2);
  next.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  const count = test.data.length;
  slow.resolve({ total: 123, items: [] });
  await vi.advanceTimersByTimeAsync(1000);
  expect(test.data).toHaveLength(count);
  next.dispose();
});

it("suspends requests and stream while hidden and refreshes once on return", async () => {
  const test = fixture();
  const controller = test.create();
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  const stream = test.signal();
  controller.setVisible(false);
  for (let i = 0; i < 5; i++) controller.refresh();
  await vi.advanceTimersByTimeAsync(60000);
  expect(test.api.items).toHaveBeenCalledOnce();
  expect(stream?.aborted).toBe(true);
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  controller.dispose();
  expect(vi.getTimerCount()).toBe(0);
});

it("uses fallback polling during reconnect and stops on healthy stream", async () => {
  const test = fixture();
  const controller = test.create();
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  test.state("reconnecting");
  await vi.advanceTimersByTimeAsync(4999);
  expect(test.api.items).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(1);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  test.state("open");
  await vi.advanceTimersByTimeAsync(30000);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  controller.dispose();
});

it("fetches final results after terminal batch and completes exactly once", async () => {
  const test = fixture();
  const controller = test.create();
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  test.api.run.mockResolvedValue(batch("succeeded"));
  test.event({ type: "batch", batch_id: "batch" } as RunProgressEvent);
  test.event({ type: "source-run", batch_id: "batch" } as RunProgressEvent);
  await vi.advanceTimersByTimeAsync(400);
  expect(test.onCompleted).toHaveBeenCalledOnce();
  expect(test.api.items).toHaveBeenCalledTimes(2);
  expect(test.api.run.mock.invocationCallOrder[0]).toBeLessThan(test.api.items.mock.invocationCallOrder[1]);
  expect(test.signal()?.aborted).toBe(true);
  await vi.advanceTimersByTimeAsync(30000);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  controller.dispose();
});

it("backs off after API errors without concurrent retries", async () => {
  const test = fixture();
  test.api.items.mockRejectedValue(new Error("offline"));
  const controller = test.create();
  controller.setVisible(true);
  await vi.advanceTimersByTimeAsync(0);
  expect(test.onError).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(9999);
  expect(test.api.items).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(1);
  expect(test.api.items).toHaveBeenCalledTimes(2);
  controller.dispose();
});
