/**
 * Watching the server finish with a document, and reporting honestly when it did not.
 *
 * Polled rather than pushed: there is no channel from the worker to the browser, and adding
 * one for a progress label would be a websocket, a subscription and a reconnection story for
 * something a request every second and a half answers.
 *
 * Bounded, because an unbounded wait is a spinner that never stops. Ingestion of a large
 * document on `low-spec` is minutes, so the watch gives up long after it usually finishes.
 *
 * **The watch returns why it stopped, not just what it last saw.** Returning the document
 * alone made three different endings indistinguishable — the server finished, the watch ran
 * out, the poll could not be sent — and the caller collapsed all of them into "done" unless
 * the status happened to be `failed`. A document still `parsing` when the two minutes elapsed
 * was reported as complete, which is exactly backwards: the slowest documents are both the
 * ones most likely to time out and the ones whose outcome the user most needs to know.
 *
 * Its own module rather than a helper inside `Upload.tsx`, so that the three endings can be
 * tested for what they are. The bug above survived because it lived in a 638-line component
 * with no way to ask it a question directly.
 */

import { getDocument, type DocumentSummary } from "../api";
import { IN_FLIGHT } from "@/shared/api/tenant";
import type { QueueItem } from "./uploadQueue";

export const POLL_MS = 1500;
export const POLL_LIMIT = 80; // two minutes

export type WatchOutcome =
  /** The server reached a terminal status. `document.status` is `ready` or `failed`. */
  | "settled"
  /** Still in flight when the watch ran out. The server is still working on it. */
  | "timeout"
  /** A poll could not be completed. The document is stored either way. */
  | "unreachable";

export interface Watched {
  outcome: WatchOutcome;
  document: DocumentSummary;
}

const inFlight = (document: DocumentSummary): boolean =>
  IN_FLIGHT.includes(document.status as (typeof IN_FLIGHT)[number]);

export async function untilSettled(
  token: string,
  created: DocumentSummary,
  onStage: (status: string) => void,
  { pollMs = POLL_MS, limit = POLL_LIMIT }: { pollMs?: number; limit?: number } = {},
): Promise<Watched> {
  let latest = created;
  for (let attempt = 0; attempt < limit; attempt += 1) {
    if (!inFlight(latest)) return { outcome: "settled", document: latest };
    onStage(latest.status);
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    try {
      latest = await getDocument(token, created.id);
    } catch {
      // A failed poll is not a failed upload — the document is stored either way. Report the
      // last state actually seen rather than inventing one.
      return { outcome: "unreachable", document: latest };
    }
  }
  // One last look before giving up: the loop sleeps *then* polls, so without this the final
  // poll's result is discarded and a document that finished on the very last attempt is
  // reported as timed out.
  return inFlight(latest)
    ? { outcome: "timeout", document: latest }
    : { outcome: "settled", document: latest };
}

/**
 * What a finished watch means for the row. See `ItemPhase` in `uploadQueue.ts`.
 *
 * **`ready` is named, rather than "anything that is not `failed`".** The old form was what
 * turned an unfinished document into a finished one, and it would do the same again the day a
 * fourth status appears. A status this function does not recognise is not a success.
 */
export function phaseFor({ outcome, document }: Watched): Pick<QueueItem, "phase" | "message"> {
  if (outcome === "settled") {
    return document.status === "ready"
      ? { phase: "done", message: document.status_detail ?? undefined }
      : { phase: "error", message: document.status_detail ?? undefined };
  }
  return {
    phase: "unresolved",
    message:
      outcome === "timeout"
        ? "Still processing — check Documents"
        : "Upload accepted; status unavailable — check Documents",
  };
}
