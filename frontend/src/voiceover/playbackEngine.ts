import { voiceFetch } from './api';
import { AudioAssetCache } from './audioAssetCache';
import { voiceDuckGain } from './duck';
import { PlaybackIndex } from './playbackIndex';
import { voiceClipStart, type VoiceClip, type VoiceDocument } from './types';

type Deck = { audio: HTMLAudioElement; gain: GainNode; clipId: string | null; assetId: string | null };

/** Owns one project's audio graph. Video time remains the transport clock. */
export class VoicePlaybackEngine {
  private readonly context = new AudioContext();
  private readonly master = this.context.createGain();
  private readonly first: Deck;
  private second: Deck | null = null;
  private activeDeck: Deck;
  private readonly cache: AudioAssetCache;
  private doc: VoiceDocument | null = null;
  private index = new PlaybackIndex([]);
  private sourceVolume = 1;
  private windowIndex = -1;
  private wanted: string[] = [];
  private frame = 0;
  private buffered = false;
  private disposed = false;
  private errorReported = false;
  private muted: boolean;

  constructor(private readonly video: HTMLVideoElement, private readonly onError: (message: string) => void) {
    this.muted = video.muted;
    this.master.connect(this.context.destination);
    this.first = this.createDeck();
    this.activeDeck = this.first;
    this.cache = new AudioAssetCache({
      load: async (id, signal) => (await voiceFetch(`/assets/${id}`, { signal })).blob(),
      onReady: this.wake, onError: this.report,
    });
    for (const [event, handler] of Object.entries(this.events)) video.addEventListener(event, handler);
    if (!video.paused) this.playing();
  }

  update(doc: VoiceDocument, sourceVolume: number) {
    if (doc.clips !== this.doc?.clips) {
      this.index = new PlaybackIndex(doc.clips);
      this.windowIndex = -1;
    }
    this.doc = doc;
    this.sourceVolume = Math.max(0, Math.min(1, sourceVolume));
    this.wake();
  }

  private report = (error: unknown) => {
    if (error instanceof DOMException && error.name === 'AbortError') return;
    if (!this.errorReported && !this.disposed) {
      this.errorReported = true;
      this.onError(`Không phát được giọng: ${String(error)}`);
    }
  };

  private createDeck(): Deck {
    const audio = new Audio();
    audio.preservesPitch = true;
    audio.preload = 'auto';
    audio.addEventListener('loadeddata', this.wake);
    const gain = this.context.createGain();
    this.context.createMediaElementSource(audio).connect(gain).connect(this.master);
    return { audio, gain, clipId: null, assetId: null };
  }

  private clearDeck(deck: Deck) {
    deck.audio.pause();
    deck.audio.removeAttribute('src');
    deck.audio.load();
    deck.clipId = deck.assetId = null;
  }

  private assign(deck: Deck, clip: VoiceClip, url: string) {
    deck.audio.pause();
    deck.audio.src = url;
    deck.clipId = clip.id;
    deck.assetId = clip.asset_id;
    deck.audio.load();
  }

  private wake = () => {
    if (!this.disposed && !this.frame) this.frame = requestAnimationFrame(this.tick);
  };
  private pauseAudio = () => {
    this.first.audio.pause();
    this.second?.audio.pause();
    cancelAnimationFrame(this.frame);
    this.frame = 0;
  };
  private pause = () => { this.pauseAudio(); this.wake(); };
  private waiting = () => { this.buffered = true; this.pauseAudio(); };
  private playing = () => {
    this.buffered = false;
    void this.context.resume().then(this.wake).catch(this.report);
    this.wake();
  };
  private seeked = () => { this.buffered = false; this.windowIndex = -1; this.wake(); };
  private volumeChanged = () => {
    // Ducking writes video.volume itself; only mute changes need another tick.
    if (this.muted !== this.video.muted) { this.muted = this.video.muted; this.wake(); }
  };
  private readonly events = {
    pause: this.pause, ended: this.pause, seeking: this.pause, seeked: this.seeked,
    waiting: this.waiting, playing: this.playing, play: this.playing,
    ratechange: this.wake, volumechange: this.volumeChanged,
  };

  private tick = () => {
    this.frame = 0;
    if (this.disposed || !this.doc) return;
    const { video, doc, index } = this;
    if (!doc.mix.enabled) {
      this.pauseAudio();
      this.master.gain.value = 0;
      this.cache.setWindow([], new Set());
      this.windowIndex = -1;
      video.volume = this.sourceVolume;
      return;
    }
    const ms = video.currentTime * 1000;
    const conflict = index.conflictAt(ms);
    if (conflict && !doc.mix.muted && doc.mix.gain > 0 && !video.paused) {
      video.pause();
      this.pauseAudio();
      this.onError(`Audio chồng nhau trong vùng ${(conflict.start / 1000).toFixed(3)}–${(conflict.end / 1000).toFixed(3)} giây. Chỉnh vùng này hoặc tắt tiếng giọng để xem tiếp.`);
      return;
    }
    const nextIndex = index.nextIndex(ms);
    const active = index.active(ms, nextIndex);
    const next = index.clips[nextIndex];
    if (nextIndex !== this.windowIndex) {
      this.wanted = index.windowAssets(ms, nextIndex);
      this.windowIndex = nextIndex;
    }
    const playing = !video.paused && !video.ended && !video.seeking && !this.buffered && video.readyState >= 2;
    this.first.audio.muted = video.muted;
    if (this.second) this.second.audio.muted = video.muted;
    this.master.gain.value = doc.mix.muted ? 0 : doc.mix.gain;
    const url = active?.asset_id ? this.cache.get(active.asset_id) : null;
    if (active && url) {
      if (this.activeDeck.clipId !== active.id || this.activeDeck.assetId !== active.asset_id) {
        const standby = this.activeDeck === this.first ? this.second : this.first;
        if (standby?.clipId === active.id && standby.assetId === active.asset_id) {
          this.activeDeck.audio.pause();
          this.activeDeck = standby;
        } else this.assign(this.activeDeck, active, url);
      }
      const desired = Math.max(0, (ms - voiceClipStart(active)) / 1000 * active.rate);
      if (Math.abs(this.activeDeck.audio.currentTime - desired) > 0.08) this.activeDeck.audio.currentTime = desired;
      this.activeDeck.audio.playbackRate = Math.max(0.25, Math.min(4, active.rate * video.playbackRate));
      this.activeDeck.gain.gain.value = active.gain;
      if (playing && this.activeDeck.audio.paused && this.context.state === 'running') void this.activeDeck.audio.play().catch(this.report);
      if (!playing) this.activeDeck.audio.pause();
    } else if (this.activeDeck.clipId !== null) this.clearDeck(this.activeDeck);

    // Swap into playback before preloading the following clip, preserving the
    // standby deck's existing audio instead of overwriting it just before use.
    const nextUrl = next?.asset_id ? this.cache.get(next.asset_id) : null;
    if (next && nextUrl) {
      this.second ??= this.createDeck();
      const standby = this.activeDeck === this.first ? this.second : this.first;
      if (standby.clipId !== next.id || standby.assetId !== next.asset_id) this.assign(standby, next, nextUrl);
    } else {
      const standby = this.activeDeck === this.first ? this.second : this.first;
      // A distant seek can leave an obsolete standby pinned. Release it so a
      // large current asset can use the byte budget while its replacement loads.
      if (standby?.clipId && standby.assetId !== next?.asset_id) this.clearDeck(standby);
    }
    const pinned = new Set([active?.asset_id, next?.asset_id, this.first.assetId, this.second?.assetId].filter((id): id is string => Boolean(id)));
    this.cache.setWindow(this.wanted, pinned);
    if ('voiceAudio' in window) (window as unknown as {voiceAudio: HTMLAudioElement}).voiceAudio = this.activeDeck.audio;
    const duck = doc.mix.mode === 'duck' && !doc.mix.muted && doc.mix.gain > 0
      ? voiceDuckGain(index.clips, nextIndex, ms, video.playbackRate) : 1;
    video.volume = doc.mix.mode === 'voice' ? 0 : Math.min(1, this.sourceVolume * doc.mix.original_gain * duck);
    if (playing) this.wake();
  };

  dispose() {
    this.disposed = true;
    this.pauseAudio();
    for (const [event, handler] of Object.entries(this.events)) this.video.removeEventListener(event, handler);
    for (const deck of [this.first, this.second]) {
      if (!deck) continue;
      deck.audio.removeEventListener('loadeddata', this.wake);
      this.clearDeck(deck);
      deck.gain.disconnect();
    }
    this.cache.dispose();
    this.master.disconnect();
    void this.context.close();
    this.video.volume = this.sourceVolume;
  }
}
