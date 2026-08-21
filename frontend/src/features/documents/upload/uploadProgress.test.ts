/**
 * The arithmetic behind the bar, and the two places it would lie.
 *
 * A rate computed from a near-zero elapsed time reads in gigabytes per second with an ETA
 * of zero, on a transfer that has barely begun. And the phase after the last byte is
 * measured differently from the transfer: the server publishes no fraction, only which of
 * four ordered stages a document is in, so the bar moves on those instead of freezing full
 * for the longest part of the wait.
 */

import { describe, expect, it } from "vitest";

import { PROCESSING, formatEta, formatRate, processing, progress } from "./uploadProgress";

describe("percentage", () => {
  it("is the fraction sent", () => {
    expect(progress(250, 1000, 1000).percent).toBe(25);
  });

  it("never exceeds 100, whatever the transport reports", () => {
    // Chunked encoding can push `loaded` past `total` on the final event.
    expect(progress(1100, 1000, 1000).percent).toBe(100);
  });

  it("is zero rather than NaN for an empty file", () => {
    expect(progress(0, 0, 1000).percent).toBe(0);
  });
});

describe("rate and estimate", () => {
  it("reports bytes per second over the transfer so far", () => {
    // 500 kB in half a second is 1 MB/s.
    expect(progress(500_000, 1_000_000, 500).bytesPerSecond).toBeCloseTo(1_000_000);
  });

  it("estimates the remainder at that rate", () => {
    // Half of a 1 MB file sent at 1 MB/s leaves half a second, rounded up to one.
    expect(progress(500_000, 1_000_000, 500).secondsRemaining).toBe(1);
  });

  it("says nothing before there is enough elapsed time to divide by", () => {
    // The guard that stops "0s remaining" appearing on the first event of a long upload.
    const early = progress(1_000, 10_000_000, 5);

    expect(early.bytesPerSecond).toBeNull();
    expect(early.secondsRemaining).toBeNull();
    expect(early.percent).toBe(0);
  });

  it("says nothing before any bytes have moved", () => {
    expect(progress(0, 10_000_000, 5_000).bytesPerSecond).toBeNull();
  });

  it("reaches zero remaining only when everything is sent", () => {
    expect(progress(1_000_000, 1_000_000, 1_000).secondsRemaining).toBe(0);
  });
});

describe("the phase after the last byte", () => {
  it("is its own state, measured by stage rather than by bytes", () => {
    expect(PROCESSING.phase).toBe("processing");
    expect(PROCESSING.secondsRemaining).toBeNull();
  });

  it("advances as the server reports each ingestion stage", () => {
    // The server publishes no fraction, but it does publish which of four ordered stages a
    // document is in. That is a real measure at the resolution available, and the
    // alternative was a bar that stopped moving for the longest part of the wait.
    const percents = ["pending", "parsing", "chunking", "embedding"].map(
      (status) => processing(status).percent,
    );

    expect(percents).toEqual([...percents].sort((a, b) => a - b));
    expect(new Set(percents).size).toBe(4);
  });

  it("names the stage rather than saying only 'processing'", () => {
    expect(processing("embedding").stage).toBe("Building the index");
  });

  it("puts an unknown status in the middle instead of at zero", () => {
    // A status this mapping has not seen is somewhere in the run; claiming 0% would say
    // nothing has happened when the upload plainly finished.
    const unknown = processing("some-future-status");

    expect(unknown.percent).toBeGreaterThan(0);
    expect(unknown.percent).toBeLessThan(100);
  });

  it("is not what a completed upload reports on its own", () => {
    // `progress` only ever describes the transfer; reaching 100% there does not mean the
    // document is ready, and the caller moves the phase deliberately.
    expect(progress(1_000, 1_000, 1_000).phase).toBe("uploading");
  });
});

describe("formatting", () => {
  it("scales the rate to the unit a person reads", () => {
    expect(formatRate(1_400_000)).toBe("1.4 MB/s");
    expect(formatRate(52_000)).toBe("52 kB/s");
    expect(formatRate(300)).toBe("300 B/s");
  });

  it("shows nothing rather than a placeholder when there is no rate", () => {
    expect(formatRate(null)).toBe("");
    expect(formatEta(null)).toBe("");
  });

  it("counts seconds, then minutes", () => {
    expect(formatEta(45)).toBe("45s");
    expect(formatEta(125)).toBe("2m 05s");
  });

  it("refuses to claim precision beyond an hour", () => {
    // The estimate that far out comes from an average that has not seen most of the
    // transfer; "1h 47m" would state a confidence the number does not have.
    expect(formatEta(6_400)).toBe("over an hour");
  });
});
