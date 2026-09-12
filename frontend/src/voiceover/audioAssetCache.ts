type Entry = { url: string; bytes: number };
type AssetLoader = (id: string, signal: AbortSignal) => Promise<Blob>;

/** Bounded cached bytes and concurrent loads, including loads still aborting. */
export class AudioAssetCache {
  private readonly entries = new Map<string, Entry>();
  private readonly pending = new Map<string, AbortController>();
  private attempted = new Set<string>();
  private wanted: string[] = [];
  private pinned = new Set<string>();
  private closed = false;
  private bytes = 0;

  constructor(private readonly options: {
    load: AssetLoader;
    onReady: () => void;
    onError: (error: unknown) => void;
    maxBytes?: number;
    maxEntries?: number;
    concurrency?: number;
  }) {}

  get byteSize() { return this.bytes; }
  get entryCount() { return this.entries.size; }
  get inflight() { return this.pending.size; }

  get(id: string): string | null {
    const entry = this.entries.get(id);
    if (!entry) return null;
    this.entries.delete(id);
    this.entries.set(id, entry);
    return entry.url;
  }

  setWindow(ids: readonly string[], pinned: ReadonlySet<string>, retry = false) {
    if (this.closed) return;
    const changed = ids.length !== this.wanted.length || ids.some((id, index) => id !== this.wanted[index]);
    this.pinned = new Set(pinned);
    if (changed || retry) {
      this.wanted = [...new Set(ids)];
      // One attempt per asset/window prevents a too-large prefetch window from
      // continuously downloading entries that its own byte limit just evicted.
      this.attempted = new Set();
      for (const [id, controller] of this.pending) {
        if (!this.wanted.includes(id)) controller.abort();
      }
    }
    this.pump();
  }

  private evict(id: string, entry: Entry) {
    URL.revokeObjectURL(entry.url);
    this.entries.delete(id);
    this.bytes -= entry.bytes;
  }

  private pump() {
    if (this.closed) return;
    const concurrency = Math.max(1, this.options.concurrency ?? 3);
    while (this.pending.size < concurrency) {
      const id = this.wanted.find(candidate => !this.entries.has(candidate) && !this.pending.has(candidate) && !this.attempted.has(candidate));
      if (!id) break;
      this.attempted.add(id);
      const controller = new AbortController();
      this.pending.set(id, controller);
      void this.load(id, controller);
    }
  }

  private async load(id: string, controller: AbortController) {
    try {
      const blob = await this.options.load(id, controller.signal);
      if (this.closed || controller.signal.aborted || !this.wanted.includes(id)) return;
      const maxBytes = this.options.maxBytes ?? 16 * 1024 * 1024;
      const maxEntries = Math.max(1, this.options.maxEntries ?? 64);
      if (blob.size > maxBytes) throw new Error('Đoạn giọng vượt giới hạn bộ nhớ nghe thử; hãy chia thành đoạn ngắn hơn.');
      for (const [cachedId, entry] of this.entries) {
        if (this.bytes + blob.size <= maxBytes && this.entries.size < maxEntries) break;
        if (!this.pinned.has(cachedId)) this.evict(cachedId, entry);
      }
      if (this.bytes + blob.size > maxBytes || this.entries.size >= maxEntries) return;
      this.entries.set(id, { url: URL.createObjectURL(blob), bytes: blob.size });
      this.bytes += blob.size;
      this.options.onReady();
    } catch (error) {
      if (!this.closed && !controller.signal.aborted) this.options.onError(error);
    } finally {
      this.pending.delete(id);
      this.pump();
    }
  }

  dispose() {
    this.closed = true;
    for (const controller of this.pending.values()) controller.abort();
    for (const [id, entry] of this.entries) this.evict(id, entry);
    this.wanted = [];
  }
}
