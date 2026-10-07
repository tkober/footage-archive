import { AfterViewInit, Component, ElementRef, OnDestroy, computed, effect, inject, input, output, signal, untracked, viewChild } from '@angular/core';

import { IconComponent } from '../icon/icon.component';
import { frameToTimecode } from './timecode';

/** localStorage key for the remembered volume/mute (#110). Wrapped in
    try/catch everywhere, matching the project's existing storage access
    (e.g. `theme.service.ts`, `preview-cache.service.ts`). */
const VOLUME_STORAGE_KEY = 'fa-player-volume';

const SPEEDS = [0.25, 0.5, 1, 1.5, 2] as const;

interface StoredVolume {
  volume: number;
  muted: boolean;
}

/**
 * Presentational inline video player (#110) for the detail panel's stage.
 * Knows nothing about the panel/file — just a `src` to play, an optional
 * `fps`/`codecLabel` for display, and three outputs (`closed`, `navigate`,
 * `unplayable`). `<video preload="metadata">` + fully custom controls on
 * desktop, native controls on touch/narrow viewports.
 *
 * Keyboard is centralised in `handleKey()` (a plain switch) so #111's frame
 * stepping can add cases there without touching the DOM-event plumbing.
 */
@Component({
  selector: 'app-video-player',
  standalone: true,
  imports: [IconComponent],
  templateUrl: './video-player.component.html',
  styleUrl: './video-player.component.css',
})
export class VideoPlayerComponent implements AfterViewInit, OnDestroy {
  /** Exposed to the host panel only so it can check `document.activeElement`
      against it (#110's "←/→ belongs to the panel again once the player
      loses focus" rule) — this version of `viewChild` has no `read` option,
      so the panel can't grab the host element through a template ref. */
  private hostRef = inject(ElementRef<HTMLElement>);

  // ── Inputs / outputs ──
  src = input.required<string>();
  /** e.g. "60000/1001" — only parsed for display for now; #111 uses it for
      frame-accurate stepping. */
  fps = input<string | null>(null);
  /** e.g. "HEVC · 10-bit" — shown in the unplayable overlay. */
  codecLabel = input<string | null>(null);

  closed = output<void>();
  /** -1 / +1 — step to the previous/next neighbour (Shift+←/→). */
  navigate = output<number>();
  unplayable = output<string>();

  // ── DOM refs ──
  private videoRef = viewChild<ElementRef<HTMLVideoElement>>('video');
  private wrapperRef = viewChild<ElementRef<HTMLElement>>('wrapper');
  private timelineRef = viewChild<ElementRef<HTMLElement>>('timeline');

  // ── Playback state ──
  playing = signal(false);
  currentTime = signal(0);
  duration = signal(0);
  bufferedEnd = signal(0);
  loading = signal(true);
  volume = signal(1);
  muted = signal(false);
  playbackRate = signal(1);
  unplayableReason = signal<string | null>(null);

  // ── Frame-accurate position (#111) ──
  /** Feature detection — Chrome/Edge/Safari support it, Firefox doesn't yet
      (checked once, the prototype doesn't change at runtime). */
  private readonly rvfcSupported = typeof HTMLVideoElement !== 'undefined'
    && 'requestVideoFrameCallback' in HTMLVideoElement.prototype;
  private rvfcHandle: number | null = null;
  /** The most recently *presented* frame's media time, from
      `requestVideoFrameCallback`'s metadata — more accurate than
      `currentTime`, which reflects the seek target, not what's on screen.
      Falls back to `currentTime` itself when rVFC isn't supported. */
  private mediaTime = signal(0);
  /** Tastenhilfe overlay (#111). */
  showHelp = signal(false);

  /** `fps()` parsed, with a fallback for the rare tracked video that has no
      stored fps (e.g. an old probe, or an unusual container). 30 is an
      arbitrary but reasonable default — frame-stepping still works, it's
      just not frame-accurate to the source without a real fps. */
  private effectiveFps = computed(() => this.frameRate() ?? 30);

  private currentFrameTime = computed(() => this.rvfcSupported ? this.mediaTime() : this.currentTime());

  /** Index of the frame currently on screen. */
  currentFrameIndex = computed(() => Math.round(this.currentFrameTime() * this.effectiveFps()));

  /** Index of the last frame in the clip (0 when duration isn't known yet). */
  lastFrameIndex = computed(() => {
    const d = this.duration();
    if (!(d > 0)) return 0;
    // round, not floor: 2.002 s × 29.97 fps comes out as 59.9999…, which
    // would drop the clip's last frame.
    return Math.max(0, Math.round(d * this.effectiveFps()) - 1);
  });

  /** `HH:MM:SS:FF` runtime-from-0 timecode for the displayed frame / the clip's total duration. */
  timecodeCurrent = computed(() => frameToTimecode(this.currentFrameIndex(), this.effectiveFps()));
  timecodeTotal = computed(() => frameToTimecode(this.lastFrameIndex() + 1, this.effectiveFps()));

  // ── Timeline interaction ──
  dragging = signal(false);
  hoverTime = signal<number | null>(null);
  hoverX = signal(0);

  // ── Fullscreen / controls visibility ──
  isFullscreen = signal(false);
  controlsVisible = signal(true);

  // ── Mobile: native controls instead of the custom bar ──
  nativeControls = signal(false);
  private mobileQuery = window.matchMedia('(max-width: 760px), (pointer: coarse)');
  private readonly onMobileQueryChange = (e: MediaQueryListEvent) => this.nativeControls.set(e.matches);

  private mounted = false;
  private hideControlsTimer?: ReturnType<typeof setTimeout>;
  private qualityCheckTimer?: ReturnType<typeof setTimeout>;
  private readonly onFullscreenChange = () => {
    const wrapper = this.wrapperRef()?.nativeElement;
    this.isFullscreen.set(!!wrapper && document.fullscreenElement === wrapper);
    this.controlsVisible.set(true);
  };

  constructor() {
    this.nativeControls.set(this.mobileQuery.matches);
    this.mobileQuery.addEventListener('change', this.onMobileQueryChange);
    document.addEventListener('fullscreenchange', this.onFullscreenChange);
    this.loadStoredVolume();

    // Reacts to the video element becoming available (mount) and to every
    // later `src` change — `handleSrcChange` tells the two apart via
    // `this.mounted` and only autoplays on the very first one (the Play
    // button click that created this component was the user gesture).
    effect(() => {
      const el = this.videoRef()?.nativeElement;
      const src = this.src();
      if (!el) return;
      // untracked: handleSrcChange reads volume/muted/playbackRate, which
      // must not become dependencies — changing the volume would reload the clip.
      untracked(() => this.handleSrcChange(el, src));
    });
  }

  ngAfterViewInit() {
    // Keyboard control starts on the player right away — the Play button
    // click that created this component is a reasonable place to hand
    // focus over (#110).
    this.focusSelf();
  }

  ngOnDestroy() {
    this.mobileQuery.removeEventListener('change', this.onMobileQueryChange);
    document.removeEventListener('fullscreenchange', this.onFullscreenChange);
    clearTimeout(this.hideControlsTimer);
    clearTimeout(this.qualityCheckTimer);
    const el = this.videoRef()?.nativeElement;
    this.unregisterFrameCallback(el);
    if (el) {
      el.pause();
      el.removeAttribute('src');
      el.load();
    }
  }

  focusSelf() {
    this.wrapperRef()?.nativeElement.focus({ preventScroll: true });
  }

  /** True while the keyboard focus is anywhere inside this component. */
  containsFocus(): boolean {
    return document.activeElement != null && this.hostRef.nativeElement.contains(document.activeElement);
  }

  // ── src lifecycle ──

  private handleSrcChange(videoEl: HTMLVideoElement, newSrc: string) {
    const isFirstMount = !this.mounted;
    this.mounted = true;

    clearTimeout(this.qualityCheckTimer);
    this.playing.set(false);
    this.currentTime.set(0);
    this.duration.set(0);
    this.bufferedEnd.set(0);
    this.unplayableReason.set(null);
    this.loading.set(true);

    if (!isFirstMount) {
      // Chrome keeps buffering the old clip unless the src is actually
      // removed (not just reassigned) before `load()`.
      videoEl.pause();
      videoEl.removeAttribute('src');
      videoEl.load();
    }
    videoEl.volume = this.volume();
    videoEl.muted = this.muted();
    videoEl.playbackRate = this.playbackRate();
    videoEl.src = newSrc;
    videoEl.load();

    // Re-arm the frame-position callback for the new clip (#111) — the old
    // registration's handle belongs to the previous `src` and would report
    // stale media times otherwise.
    this.unregisterFrameCallback(videoEl);
    this.mediaTime.set(0);
    this.pendingFrames = 0;
    this.registerFrameCallback(videoEl);

    if (isFirstMount) {
      // The click that set the player's src was the user gesture; a
      // rejected autoplay (policy, slow network) just leaves it paused —
      // never surfaced as an error.
      videoEl.play().catch(() => {});
    }
  }

  // ── Frame-position tracking (#111) ──

  /** Registers a one-shot `requestVideoFrameCallback`; re-registers itself
      from inside the callback so it effectively runs on every presented
      frame for as long as this video element is in use. Never runs when
      the browser doesn't support it (Firefox) — callers fall back to
      `currentTime` via `currentFrameTime()`. */
  private registerFrameCallback(el: HTMLVideoElement) {
    if (!this.rvfcSupported) return;
    this.rvfcHandle = (el as any).requestVideoFrameCallback((_now: number, metadata: { mediaTime: number }) => {
      this.mediaTime.set(metadata.mediaTime);
      this.registerFrameCallback(el);
    });
  }

  private unregisterFrameCallback(el: HTMLVideoElement | null | undefined) {
    if (this.rvfcHandle != null && el && this.rvfcSupported) {
      (el as any).cancelVideoFrameCallback(this.rvfcHandle);
    }
    this.rvfcHandle = null;
  }

  // ── Central keyboard dispatcher (#110/#111) ──

  /** Returns true when the key was handled — the wrapper's keydown handler
      then `preventDefault()`s + `stopPropagation()`s so the panel's/
      browser's own `document:keydown` listeners never see it. Unhandled
      keys (anything with Meta/Ctrl/Alt, or not listed below) fall through
      untouched so e.g. Cmd+K quick-jump keeps working while the player has
      focus. #111 adds its frame-step keys as new cases here. */
  handleKey(e: KeyboardEvent): boolean {
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    switch (e.key) {
      case ' ':
      case 'Spacebar':
      case 'k':
      case 'K':
        this.togglePlay();
        return true;
      case 'ArrowLeft':
        if (e.shiftKey) this.navigate.emit(-1);
        else this.seekRelative(-5);
        return true;
      case 'ArrowRight':
        if (e.shiftKey) this.navigate.emit(1);
        else this.seekRelative(5);
        return true;
      case 'j':
      case 'J':
        this.seekRelative(-10);
        return true;
      case 'l':
      case 'L':
        this.seekRelative(10);
        return true;
      case ',':
        this.stepFrame(-1);
        return true;
      case '.':
        this.stepFrame(1);
        return true;
      case 'Home':
        this.jumpToStart();
        return true;
      case 'End':
        this.jumpToEnd();
        return true;
      case '?':
        this.toggleHelp();
        return true;
      case 'f':
      case 'F':
        this.toggleFullscreen();
        return true;
      case 'm':
      case 'M':
        this.toggleMute();
        return true;
      case 'Escape':
        if (this.showHelp()) this.showHelp.set(false);
        else if (this.isFullscreen()) this.exitFullscreen();
        else this.closed.emit();
        return true;
      default:
        // 0-9 jump to 0%-90% of the duration — checked last since it needs
        // its own guard (Shift+digit types a special char on a DE keyboard,
        // so that combination is left alone rather than treated as "0").
        if (!e.shiftKey && e.key.length === 1 && e.key >= '0' && e.key <= '9') {
          this.jumpToPercent(Number(e.key) * 10);
          return true;
        }
        return false;
    }
  }

  onWrapperKeydown(e: KeyboardEvent) {
    if (this.handleKey(e)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  // ── Transport ──

  togglePlay() {
    const el = this.videoRef()?.nativeElement;
    if (!el || this.unplayableReason()) return;
    if (el.paused) el.play().catch(() => {});
    else el.pause();
  }

  seekRelative(deltaSeconds: number) {
    this.pendingFrames = 0; // any other jump cancels queued frame steps
    const el = this.videoRef()?.nativeElement;
    if (!el || !isFinite(el.duration)) return;
    el.currentTime = Math.min(Math.max(0, el.currentTime + deltaSeconds), el.duration);
  }

  /** Jumps to `percent`% (0-90, in steps of 10) of the clip's duration. */
  jumpToPercent(percent: number) {
    this.pendingFrames = 0; // any other jump cancels queued frame steps
    const el = this.videoRef()?.nativeElement;
    if (!el || !isFinite(el.duration) || el.duration <= 0) return;
    el.currentTime = Math.min(Math.max(0, (percent / 100) * el.duration), el.duration);
  }

  // ── Frame stepping (#111) ──

  /** Steps one frame back (`delta` -1) or forward (`delta` +1). Pauses
      first (stepping while playing would just be fought by playback), and
      lands on the *middle* of the target frame (`(n + 0.5) / fps`) rather
      than its boundary — seeking exactly to a frame's start time is prone
      to being rounded down into the *previous* frame by the decoder,
      producing an off-by-one.

      Steps requested while a seek is still in flight are not dropped but
      summed into `pendingFrames` and applied as one combined seek on
      `seeked` (see `onSeeked`). On slow software decodes (4K 10-bit 4:2:2
      takes ~150 ms per seek) dropping them would make ten quick `.` presses
      land a few frames short; queueing every seek would build a backlog.
      Coalescing keeps the count exact with at most one seek in flight. */
  private stepFrame(delta: -1 | 1) {
    const el = this.videoRef()?.nativeElement;
    if (!el || !isFinite(el.duration) || el.duration <= 0) return;
    if (!el.paused) el.pause();
    if (el.seeking) {
      this.pendingFrames += delta;
      return;
    }
    this.seekByFrames(el, delta);
  }

  /** Frames requested via `,`/`.` while a seek was still running (#111). */
  private pendingFrames = 0;

  private seekByFrames(el: HTMLVideoElement, delta: number) {
    const fps = this.effectiveFps();
    // Paused: `currentTime` is the frame-midpoint we last seeked to (or
    // where playback stopped), so `floor` gives the frame on screen right
    // away — rVFC's `mediaTime` may still lag one seek behind here.
    const base = el.paused ? Math.floor(el.currentTime * fps + 1e-6) : this.currentFrameIndex();
    const target = Math.min(Math.max(0, base + delta), this.lastFrameIndex());
    el.currentTime = (target + 0.5) / fps;
  }

  private jumpToStart() {
    this.pendingFrames = 0; // any other jump cancels queued frame steps
    const el = this.videoRef()?.nativeElement;
    if (!el) return;
    if (!el.paused) el.pause();
    const hasDuration = isFinite(el.duration) && el.duration > 0;
    el.currentTime = hasDuration ? 0.5 / this.effectiveFps() : 0;
  }

  private jumpToEnd() {
    this.pendingFrames = 0; // any other jump cancels queued frame steps
    const el = this.videoRef()?.nativeElement;
    if (!el || !isFinite(el.duration) || el.duration <= 0) return;
    if (!el.paused) el.pause();
    const fps = this.effectiveFps();
    el.currentTime = (this.lastFrameIndex() + 0.5) / fps;
  }

  toggleHelp() {
    this.showHelp.update(v => !v);
  }

  onVideoClick() {
    this.togglePlay();
  }

  onVideoDblClick() {
    this.toggleFullscreen();
  }

  // ── Video element events ──

  onPlay() {
    this.playing.set(true);
    this.scheduleQualityCheck();
  }

  onPause() {
    this.playing.set(false);
  }

  onTimeUpdate(el: HTMLVideoElement) {
    this.currentTime.set(el.currentTime);
  }

  /** Updates the fallback (no-rVFC) position the moment a seek lands,
      rather than waiting for the next `timeupdate` tick (#111) — matters
      for frame stepping, where the whole point is an immediate readout. */
  onSeeked(el: HTMLVideoElement) {
    this.currentTime.set(el.currentTime);
    if (this.pendingFrames !== 0) {
      const delta = this.pendingFrames;
      this.pendingFrames = 0;
      this.seekByFrames(el, delta);
    }
  }

  onDurationChange(el: HTMLVideoElement) {
    if (isFinite(el.duration)) this.duration.set(el.duration);
  }

  onProgress(el: HTMLVideoElement) {
    const b = el.buffered;
    this.bufferedEnd.set(b.length ? b.end(b.length - 1) : 0);
  }

  onWaiting() {
    this.loading.set(true);
  }

  onCanPlay() {
    this.loading.set(false);
  }

  /** HEVC-without-decoder case: audio plays, no error fires, but the
      element never reports any pixels. */
  onLoadedMetadata(el: HTMLVideoElement) {
    if (isFinite(el.duration)) this.duration.set(el.duration);
    if (el.videoWidth === 0) this.markUnplayable(this.noDecoderMessage());
  }

  onError(el: HTMLVideoElement) {
    this.markUnplayable(this.mediaErrorMessage(el.error));
  }

  /** Belt-and-suspenders check for a decoder that reports metadata fine,
      produces no error, but still never actually decodes a frame. */
  private scheduleQualityCheck() {
    clearTimeout(this.qualityCheckTimer);
    this.qualityCheckTimer = setTimeout(() => {
      const el = this.videoRef()?.nativeElement;
      if (!el || this.unplayableReason() || el.paused || el.currentTime <= 0) return;
      const quality = el.getVideoPlaybackQuality?.();
      if (quality && quality.totalVideoFrames === 0) this.markUnplayable(this.noDecoderMessage());
    }, 3000);
  }

  private noDecoderMessage(): string {
    return `This browser has no decoder for ${this.codecLabel() ?? 'this codec'}.`;
  }

  private mediaErrorMessage(err: MediaError | null): string {
    switch (err?.code) {
      case MediaError.MEDIA_ERR_NETWORK: return 'A network error interrupted playback.';
      case MediaError.MEDIA_ERR_DECODE: return 'The video could not be decoded.';
      case MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED: return 'This format is not supported by this browser.';
      default: return 'Playback failed.';
    }
  }

  private markUnplayable(message: string) {
    if (this.unplayableReason()) return;
    const el = this.videoRef()?.nativeElement;
    if (el) {
      el.pause();
      el.removeAttribute('src');
      el.load();
    }
    this.playing.set(false);
    this.loading.set(false);
    this.unplayableReason.set(message);
    this.unplayable.emit(message);
  }

  // ── Timeline (scrub bar) ──

  onTimelinePointerDown(e: PointerEvent) {
    if (this.unplayableReason()) return;
    this.dragging.set(true);
    this.seekToPointer(e);
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  }

  onTimelinePointerMove(e: PointerEvent) {
    if (this.dragging()) {
      this.seekToPointer(e);
    } else {
      const el = this.timelineRef()?.nativeElement;
      if (!el || !this.duration()) return;
      const rect = el.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      this.hoverTime.set(ratio * this.duration());
      this.hoverX.set(e.clientX - rect.left);
    }
  }

  onTimelinePointerUp(e: PointerEvent) {
    this.dragging.set(false);
    (e.target as HTMLElement).releasePointerCapture?.(e.pointerId);
  }

  onTimelineLeave() {
    this.hoverTime.set(null);
  }

  private seekToPointer(e: PointerEvent) {
    const el = this.timelineRef()?.nativeElement;
    const video = this.videoRef()?.nativeElement;
    if (!el || !video || !this.duration()) return;
    const rect = el.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const t = ratio * this.duration();
    video.currentTime = t;
    this.currentTime.set(t);
    this.hoverTime.set(t);
    this.hoverX.set(e.clientX - rect.left);
  }

  // ── Volume ──

  toggleMute() {
    const el = this.videoRef()?.nativeElement;
    if (!el) return;
    el.muted = !el.muted;
    this.muted.set(el.muted);
    this.saveVolume();
  }

  onVolumeInput(value: number) {
    const el = this.videoRef()?.nativeElement;
    if (!el) return;
    this.volume.set(value);
    el.volume = value;
    if (value > 0 && el.muted) {
      el.muted = false;
      this.muted.set(false);
    }
    this.saveVolume();
  }

  private loadStoredVolume() {
    try {
      const raw = localStorage.getItem(VOLUME_STORAGE_KEY);
      if (!raw) return;
      const stored = JSON.parse(raw) as StoredVolume;
      if (typeof stored.volume === 'number') this.volume.set(Math.min(1, Math.max(0, stored.volume)));
      if (typeof stored.muted === 'boolean') this.muted.set(stored.muted);
    } catch {
      // localStorage unavailable (private mode, blocked) — defaults stand.
    }
  }

  private saveVolume() {
    try {
      const value: StoredVolume = { volume: this.volume(), muted: this.muted() };
      localStorage.setItem(VOLUME_STORAGE_KEY, JSON.stringify(value));
    } catch {
      // Ignore — nothing to persist to.
    }
  }

  // ── Speed ──

  cycleSpeed() {
    const el = this.videoRef()?.nativeElement;
    if (!el) return;
    const i = SPEEDS.indexOf(this.playbackRate() as any);
    const next = SPEEDS[(i + 1) % SPEEDS.length];
    this.playbackRate.set(next);
    el.playbackRate = next;
  }

  // ── Fullscreen ──

  toggleFullscreen() {
    if (document.fullscreenElement) this.exitFullscreen();
    else this.enterFullscreen();
  }

  private enterFullscreen() {
    this.wrapperRef()?.nativeElement.requestFullscreen?.().catch(() => {});
  }

  private exitFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }

  // ── Activity (fullscreen auto-hide of controls + cursor) ──

  onActivity() {
    this.controlsVisible.set(true);
    if (!this.isFullscreen()) return;
    clearTimeout(this.hideControlsTimer);
    this.hideControlsTimer = setTimeout(() => this.controlsVisible.set(false), 2500);
  }

  // ── Display helpers ──

  /** Parses an ffprobe-style "60000/1001" fraction into a decimal fps —
      only used for display today; #111's frame-stepping will reuse it to
      compute a frame duration. */
  frameRate(): number | null {
    const raw = this.fps();
    if (!raw) return null;
    const m = raw.match(/^(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/);
    if (m) {
      const num = parseFloat(m[1]), den = parseFloat(m[2]);
      return den ? num / den : null;
    }
    const n = parseFloat(raw);
    return isNaN(n) ? null : n;
  }

  formatTime(seconds: number | null): string {
    if (seconds == null || !isFinite(seconds) || seconds < 0) seconds = 0;
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor(total / 60) % 60;
    const s = total % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }

  progressPercent(): number {
    const d = this.duration();
    return d ? (this.currentTime() / d) * 100 : 0;
  }

  bufferedPercent(): number {
    const d = this.duration();
    return d ? (this.bufferedEnd() / d) * 100 : 0;
  }

  hoverPercent(): number {
    const d = this.duration();
    const t = this.hoverTime();
    return d && t != null ? (t / d) * 100 : 0;
  }
}
