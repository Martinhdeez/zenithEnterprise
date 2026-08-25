/**
 * Turning bytes-so-far into something worth reading.
 *
 * Kept out of the component because the interesting parts are arithmetic, not markup: a
 * rate that survives a stalled first sample, an estimate that does not swing wildly on
 * every event, and the transition to a phase where there is no percentage to show at all.
 *
 * **The upload is not the wait.** Reaching 100% means the bytes arrived; the server then
 * parses, chunks and embeds, which on the `low-spec` profile takes far longer than the
 * transfer did. A bar that sits full and frozen for that stretch reads as a hang, so the
 * phase after the last byte is a state of its own with its own label rather than a number
 * that stopped moving.
 */

export type UploadPhase = "idle" | "uploading" | "processing" | "done" | "error";

export interface UploadStats {
  phase: UploadPhase;
  /** 0–100, and meaningful only while `uploading`. */
  percent: number;
  /** Bytes per second over the whole transfer so far, or null before there is a sample. */
  bytesPerSecond: number | null;
  /** Seconds left at the current rate, or null when that cannot be said honestly. */
  secondsRemaining: number | null;
  /** Which ingestion stage the server last reported. Only set while `processing`. */
  stage?: string;
}

export const IDLE: UploadStats = {
  phase: "idle",
  percent: 0,
  bytesPerSecond: null,
  secondsRemaining: null,
};

/**
 * Progress, from the bytes and the time they took.
 *
 * The average since the start rather than the instantaneous rate between two events: an
 * upload's byte events arrive in bursts as buffers flush, and a rate computed between two
 * of them swings by an order of magnitude and takes the estimate with it. The average is
 * less responsive to a genuine slowdown and far more readable, which is the right trade
 * for a number somebody glances at.
 */
export function progress(loaded: number, total: number, elapsedMs: number): UploadStats {
  const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;

  // Under a tenth of a second there is no rate worth reporting: dividing by a near-zero
  // elapsed time produces a number in the gigabytes per second and an ETA of zero, on an
  // upload that has barely started.
  if (elapsedMs < 100 || loaded <= 0) {
    return { phase: "uploading", percent, bytesPerSecond: null, secondsRemaining: null };
  }

  const bytesPerSecond = (loaded / elapsedMs) * 1000;
  const remaining = Math.max(0, total - loaded);
  return {
    phase: "uploading",
    percent,
    bytesPerSecond,
    // Rounded up: "0 seconds remaining" on a transfer that is still running is a worse
    // lie than one second.
    secondsRemaining: bytesPerSecond > 0 ? Math.ceil(remaining / bytesPerSecond) : null,
  };
}

/**
 * How far through ingestion each status is.
 *
 * The server reports no fraction — there is no "62% embedded" anywhere — but it does report
 * which of four stages a document is in, and those are ordered. Mapping them to a
 * percentage is a real measure of progress at the resolution the server actually has,
 * which is better than both a frozen bar and an invented continuous number.
 *
 * The steps are uneven on purpose: parsing a PDF is quick, embedding thousands of chunks
 * is most of the wait, so the bar should not spend equal time on each. These are ordered
 * by where the time actually goes rather than spaced evenly.
 */
const STAGES: Record<string, { percent: number; label: string }> = {
  pending: { percent: 5, label: "Queued" },
  parsing: { percent: 25, label: "Reading the document" },
  chunking: { percent: 45, label: "Splitting into passages" },
  embedding: { percent: 70, label: "Building the index" },
  classifying: { percent: 90, label: "Filing the document" },
};

/** Every byte is in; the server has not finished with them. The percentage comes from the
    stage it reports, and the label says which stage that is. */
export function processing(status: string): UploadStats {
  const stage = STAGES[status];
  return {
    phase: "processing",
    // An unrecognised status is somewhere in the middle: better than 0, and better than
    // claiming a precision the mapping does not have for a state it does not know.
    percent: stage?.percent ?? 50,
    stage: stage?.label ?? "Processing",
    bytesPerSecond: null,
    secondsRemaining: null,
  };
}

/** The moment the last byte lands, before any status has come back. */
export const PROCESSING: UploadStats = processing("pending");

/** `1.4 MB/s`. Decimal units, matching what every file manager shows for transfer rates. */
export function formatRate(bytesPerSecond: number | null): string {
  if (bytesPerSecond === null) return "";
  if (bytesPerSecond >= 1_000_000) return `${(bytesPerSecond / 1_000_000).toFixed(1)} MB/s`;
  if (bytesPerSecond >= 1_000) return `${Math.round(bytesPerSecond / 1_000)} kB/s`;
  return `${Math.round(bytesPerSecond)} B/s`;
}

/**
 * `45s`, `2m 05s`, `1h 12m`.
 *
 * Anything past an hour is reported as "over an hour" rather than counted: an estimate
 * that far out is derived from an average that has not seen most of the transfer, and
 * printing "1h 47m" claims a precision the number does not have.
 */
export function formatEta(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  }
  return "over an hour";
}
