import { api, subscribeRunEvents } from "../../api";
import type { Batch, InsightSummary, Item, ItemFilters, RunProgressEvent, TrendClusters } from "../../api";
import type { StreamState } from "../../transport/eventStream";

export type DashboardData = {
  items: Item[]; summary: InsightSummary | null; clusters: TrendClusters | null;
  runs: Batch[]; batch: Batch | null; loading: boolean; clustersError: string | null;
};

export const emptyDashboard = (): DashboardData => ({
  items: [], summary: null, clusters: null, runs: [], batch: null, loading: false, clustersError: null,
});

const running = (batch: Batch | null) => Boolean(batch && ["queued", "running"].includes(batch.state));

export function applyProgress(batch: Batch | null, event: RunProgressEvent): Batch | null {
  if (!batch || event.batch_id !== batch.id || event.type !== "source-progress") return batch;
  return { ...batch, source_runs: batch.source_runs.map((source) => {
    if (source.id !== event.source_run_id) return source;
    const updated = { ...source };
    const fields = ["state", "phase", "progress_mode", "progress_current", "progress_total", "progress_percent",
      "message", "browser_state", "fetched_count", "ingested_count"] as const;
    for (const field of fields) {
      if (event[field] !== undefined) Object.assign(updated, { [field]: event[field] });
    }
    return updated;
  }) };
}

type Dependencies = { api: Pick<typeof api, "items" | "insightSummary" | "insightClusters" | "runs" | "run">; subscribe: typeof subscribeRunEvents };
type Options = {
  keywordId: number; filters: ItemFilters;
  onData: (data: DashboardData) => void;
  onError: (error: unknown) => void;
  onAuthRequired: () => Promise<boolean>;
  onCompleted: () => void;
};

/** One query owns its requests, stream and timers; disposal rejects late responses. */
export class DashboardRefresh {
  private data = emptyDashboard();
  private visible = false;
  private disposed = false;
  private request: AbortController | null = null;
  private stream: AbortController | null = null;
  private streamBatch: string | null = null;
  private streamState: StreamState = "closed";
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending = false;
  private failures = 0;
  private completed = new Set<string>();
  private batchRevision = 0;

  constructor(private options: Options, private dependencies: Dependencies = { api, subscribe: subscribeRunEvents }) {}

  setVisible(visible: boolean) {
    if (this.disposed || this.visible === visible) return;
    this.visible = visible;
    this.clearTimer();
    if (!visible) {
      this.request?.abort();
      this.closeStream();
    } else {
      this.pending = true;
      this.schedule(0);
    }
  }

  setBatch(batch: Batch | null) {
    if (this.disposed || (batch && batch.keyword_id !== this.options.keywordId)) return;
    this.batchRevision++;
    this.data = { ...this.data, batch };
    this.emit();
    this.syncStream();
    this.refresh();
  }

  refresh() {
    if (this.disposed) return;
    this.pending = true;
    if (this.visible && !this.request) this.schedule(0);
  }

  dispose() {
    this.disposed = true;
    this.visible = false;
    this.clearTimer();
    this.request?.abort();
    this.closeStream();
  }

  private emit() { if (!this.disposed) this.options.onData(this.data); }
  private clearTimer() { if (this.timer !== null) clearTimeout(this.timer); this.timer = null; }
  private schedule(delay: number) {
    this.clearTimer();
    if (!this.visible || this.disposed || this.request) return;
    this.timer = setTimeout(() => { this.timer = null; void this.load(); }, delay);
  }
  private closeStream() {
    this.stream?.abort(); this.stream = null; this.streamBatch = null; this.streamState = "closed";
  }

  private syncStream() {
    const id = this.data.batch?.id;
    if (!this.visible || !running(this.data.batch) || !id) { this.closeStream(); return; }
    if (this.streamBatch === id) return;
    this.closeStream();
    this.streamBatch = id;
    const stream = this.stream = new AbortController();
    void this.dependencies.subscribe(id, (event) => {
      if (stream.signal.aborted || this.disposed || (event.batch_id && event.batch_id !== id)) return;
      this.data = { ...this.data, batch: applyProgress(this.data.batch, event) };
      if (event.type === "source-progress") this.emit();
      if (["batch", "source-run", "parser-drift-alert", "connected"].includes(event.type)) {
        this.pending = true;
        if (!this.request && this.timer === null) this.schedule(400);
      }
    }, stream.signal, {
      onAuthRequired: this.options.onAuthRequired,
      onState: (state) => {
        if (stream.signal.aborted || this.disposed) return;
        this.streamState = state;
        if (state === "open" && !this.pending) this.clearTimer();
        if (["reconnecting", "unauthorized", "closed"].includes(state) && !this.request) this.schedule(5000);
      },
    });
  }

  private async load() {
    if (this.disposed || !this.visible || this.request) return;
    const controller = this.request = new AbortController();
    this.pending = false;
    const revision = this.batchRevision;
    const batchId = this.data.batch?.id;
    const { keywordId, filters } = this.options;
    const { api } = this.dependencies;
    this.data = { ...this.data, loading: true };
    this.emit();
    try {
      // Observe completion before loading results, so the terminal snapshot
      // includes commits that preceded the server's terminal batch state.
      const latestBatch = batchId ? await api.run(batchId, controller.signal) : null;
      if (controller.signal.aborted || this.disposed) return;
      const [items, summary, runs, clusters] = await Promise.allSettled([
        api.items(keywordId, filters, controller.signal),
        api.insightSummary(keywordId, filters, controller.signal),
        api.runs(keywordId, controller.signal),
        api.insightClusters(keywordId, filters, controller.signal),
      ]);
      if (this.disposed || controller.signal.aborted) return;
      for (const result of [items, summary, runs]) if (result.status === "rejected") throw result.reason;
      if (items.status !== "fulfilled" || summary.status !== "fulfilled" || runs.status !== "fulfilled") return;
      const nextBatch = revision === this.batchRevision ? (latestBatch ?? runs.value[0] ?? null) : this.data.batch;
      this.data = { items: items.value.items, summary: summary.value, runs: runs.value, batch: nextBatch,
        clusters: clusters.status === "fulfilled" ? clusters.value : null,
        clustersError: clusters.status === "rejected" ? String(clusters.reason instanceof Error ? clusters.reason.message : clusters.reason) : null,
        loading: false };
      this.failures = 0;
      if (batchId && nextBatch?.id === batchId && !running(nextBatch) && !this.completed.has(batchId)) {
        this.completed.add(batchId);
        this.pending = false; // This request already includes the final results.
        this.options.onCompleted();
      }
      this.emit();
      this.syncStream();
    } catch (error) {
      if (!controller.signal.aborted && !this.disposed) {
        this.failures++;
        this.options.onError(error);
      }
    } finally {
      this.request = null;
      if (!this.disposed && !controller.signal.aborted) {
        this.data = { ...this.data, loading: false };
        this.emit();
      }
      if (this.pending) this.schedule(400);
      else if (this.failures || (running(this.data.batch) && this.streamState !== "open")) {
        this.schedule(Math.min(30000, 5000 * 2 ** Math.min(this.failures, 3)));
      }
    }
  }
}
