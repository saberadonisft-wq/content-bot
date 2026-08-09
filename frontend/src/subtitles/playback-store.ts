export type PlaybackSnapshot = {
  currentMs: number;
  durationMs: number;
  isPlaying: boolean;
};

type SnapshotListener = () => void;
type FrameListener = (currentMs: number) => void;

export class PlaybackClockStore {
  private snapshot: PlaybackSnapshot = {
    currentMs: 0,
    durationMs: 0,
    isPlaying: false,
  };

  private readonly snapshotListeners = new Set<SnapshotListener>();
  private readonly frameListeners = new Set<FrameListener>();

  getSnapshot = () => this.snapshot;

  subscribe = (listener: SnapshotListener) => {
    this.snapshotListeners.add(listener);
    return () => {
      this.snapshotListeners.delete(listener);
    };
  };

  subscribeFrame = (listener: FrameListener) => {
    this.frameListeners.add(listener);
    return () => {
      this.frameListeners.delete(listener);
    };
  };

  setCurrentMs(currentMs: number) {
    const nextMs = Math.max(
      0,
      Math.min(
        this.snapshot.durationMs || Number.MAX_SAFE_INTEGER,
        Math.round(currentMs),
      ),
    );
    if (nextMs === this.snapshot.currentMs) return;
    this.snapshot = { ...this.snapshot, currentMs: nextMs };
    this.frameListeners.forEach((listener) => listener(nextMs));
    this.snapshotListeners.forEach((listener) => listener());
  }

  setDurationMs(durationMs: number) {
    const nextDuration = Math.max(0, Math.round(durationMs));
    if (nextDuration === this.snapshot.durationMs) return;
    this.snapshot = {
      ...this.snapshot,
      durationMs: nextDuration,
      currentMs: Math.min(this.snapshot.currentMs, nextDuration || 0),
    };
    this.snapshotListeners.forEach((listener) => listener());
    this.frameListeners.forEach((listener) => listener(this.snapshot.currentMs));
  }

  setPlaying(isPlaying: boolean) {
    if (isPlaying === this.snapshot.isPlaying) return;
    this.snapshot = { ...this.snapshot, isPlaying };
    this.snapshotListeners.forEach((listener) => listener());
  }

  reset() {
    this.snapshot = { currentMs: 0, durationMs: 0, isPlaying: false };
    this.snapshotListeners.forEach((listener) => listener());
    this.frameListeners.forEach((listener) => listener(0));
  }
}
