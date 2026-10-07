/**
 * Frame-accurate timecode formatting for the video player (#111).
 *
 * This is deliberately NOT drop-frame SMPTE timecode. SMPTE drop-frame
 * timecode encodes a *wall-clock* position within a recording that started
 * at some arbitrary (often non-zero) time-of-day, and periodically skips
 * frame numbers to keep that wall-clock alignment despite NTSC's fractional
 * frame rate (29.97, 59.94, …). What we show here is runtime elapsed from
 * frame 0 of the clip — a plain frame count converted to HH:MM:SS:FF. At a
 * fractional fps this drifts slightly from a "real" NTSC drop-frame TC the
 * further into the clip you get (visible in the fps=59.94, n=3600 case),
 * which is expected and fine for a player scrub/position readout.
 */

/**
 * Converts an absolute frame index into an `HH:MM:SS:FF` runtime string.
 *
 * `secs` is the whole second the frame falls into; `ff` is the frame's
 * offset within that second. The `1e-9` epsilon absorbs floating-point
 * error from the `fps` fraction (e.g. 30000/1001) landing a hair under a
 * whole second. `ff` is clamped to `[0, ceil(fps) - 1]` so a frame that
 * rounds up to the *next* second (e.g. frame 30 at 29.97fps, which is
 * `secs=1, ff` would otherwise compute as -0 something) still prints a
 * sane frame number instead of a negative or oversized one.
 */
export function frameToTimecode(n: number, fps: number): string {
  if (!isFinite(n) || !isFinite(fps) || fps <= 0) return '00:00:00:00';
  n = Math.max(0, n);

  const secs = Math.floor(n / fps + 1e-9);
  const maxFf = Math.max(0, Math.ceil(fps) - 1);
  const ff = Math.min(Math.max(0, n - Math.round(secs * fps)), maxFf);

  const h = Math.floor(secs / 3600);
  const m = Math.floor(secs / 60) % 60;
  const s = secs % 60;

  const pad = (x: number) => String(x).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(ff)}`;
}
