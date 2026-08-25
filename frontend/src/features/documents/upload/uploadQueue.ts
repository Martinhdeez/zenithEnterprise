/**
 * A batch of uploads, and why it is not a loop.
 *
 * The single-file screen sent one file, waited for the bytes, then waited for the *server*
 * to finish parsing, chunking and embedding it before touching the next one. For one file
 * that is exactly right — the wait is the thing the user is watching. For a migration it is
 * ruinous: ingestion on `low-spec` takes minutes per document, and two hundred files
 * serialised behind each other is a browser tab somebody has to leave open overnight.
 *
 * So the two waits are separated. Bytes go up with bounded concurrency; ingestion is
 * *watched*, not waited on, because the server queues it anyway — one worker, one document
 * at a time, whatever the browser does.
 *
 * **Concurrency is bounded rather than unlimited**, and the bound is small on purpose. The
 * API pool is ten connections for every user of the installation, so a client that opens
 * two hundred parallel uploads is a client that takes the pool away from everybody else.
 * Three is enough to hide the round trips and small enough to be a good neighbour.
 */

/**
 * `cancelled` is its own phase rather than a flavour of `error`.
 *
 * A batch of two hundred where somebody stopped forty on purpose must not report forty
 * failures — the count above the list is what tells them whether the migration went well,
 * and a deliberate stop is not a thing that went wrong.
 */
/**
 * `unresolved` is its own phase for the same reason `cancelled` is.
 *
 * The browser watches ingestion for two minutes and then stops. Stopping is correct — an
 * unbounded spinner is worse — but the document is still being processed on the server, and
 * the two states are not the same thing. Folding the unresolved ones into `done` was the
 * older behaviour and it made the batch summary claim work had finished that had not: on
 * `low-spec` a large document takes longer than the watch, so the row most likely to be
 * mislabelled is the one the user most needs to follow up on.
 *
 * It counts as *settled* for the batch — nothing in the browser is going to change it — but
 * never as *done*. The row says to look in Documents, which is where the truth is.
 */
export type ItemPhase =
  | "queued"
  | "uploading"
  | "processing"
  | "done"
  | "unresolved"
  | "error"
  | "cancelled";

/**
 * Whether this row can still be stopped from the browser.
 *
 * **`processing` cannot.** By then `POST /documents` has answered, the row exists and the
 * worker has the job; the browser has nothing left to abort. Offering a cancel button there
 * would claim the work stops when it does not — the honest route to undoing one of those is
 * deleting the document, which the library already does.
 */
export function cancellable(phase: ItemPhase): boolean {
  return phase === "queued" || phase === "uploading";
}

export interface QueueItem {
  /** Stable across the item's life. `File` has no id and two files may share a name. */
  id: string;
  file: File;
  phase: ItemPhase;
  /** 0–100, meaningful while `uploading`. */
  percent: number;
  /**
   * Transfer rate and time left, both meaningful only while `uploading`.
   *
   * `uploadProgress.ts` has computed these on every progress event since it was written, and
   * `formatRate`/`formatEta` have formatted them for nobody: the row showed a percentage and
   * dropped the rest. On a migration the percentage is the least useful of the three — it
   * says how far one file has got and nothing about whether the transfer is moving.
   */
  bytesPerSecond?: number | null;
  secondsRemaining?: number | null;
  /** Which ingestion stage the server last reported. Only while `processing`. */
  stage?: string;
  /** The document id, once the server has one. */
  documentId?: string;
  message?: string;
}

/** How many files have their bytes in flight at once. See the note above. */
export const CONCURRENCY = 3;

export function enqueue(files: File[]): QueueItem[] {
  return files.map((file, index) => ({
    // Index included: dropping the same file twice is a thing people do, and two items
    // sharing an id would update each other.
    id: `${index}-${file.name}-${file.size}`,
    file,
    phase: "queued",
    percent: 0,
  }));
}

export function update(items: QueueItem[], id: string, patch: Partial<QueueItem>): QueueItem[] {
  return items.map((item) => (item.id === id ? { ...item, ...patch } : item));
}

export interface QueueSummary {
  total: number;
  done: number;
  failed: number;
  /** Stopped on purpose. Counted apart from `failed` — see `ItemPhase`. */
  cancelled: number;
  /** Uploaded, still ingesting when the browser stopped watching. See `ItemPhase`. */
  unresolved: number;
  /** Everything that has not settled — queued, uploading or processing. */
  active: number;
  /** 0–100 across the whole batch. */
  percent: number;
  finished: boolean;
}

/**
 * The one line above the list.
 *
 * Counted by file rather than by byte. A batch is usually many similar documents, and "14
 * of 200" is a number somebody can act on — "38% of 4.1 GB" is not, and it moves in jumps
 * that do not match what the rows are doing.
 */
export function summarise(items: QueueItem[]): QueueSummary {
  const done = items.filter((item) => item.phase === "done").length;
  const failed = items.filter((item) => item.phase === "error").length;
  // Settled, not successful: a cancelled row is finished with, so the batch can report
  // itself complete instead of hanging at "3 in progress" over rows nobody is waiting for.
  const cancelled = items.filter((item) => item.phase === "cancelled").length;
  // Settled but explicitly not done: the browser has stopped watching, so nothing here will
  // move it again, and reporting it as finished work would be the lie this phase exists to
  // prevent.
  const unresolved = items.filter((item) => item.phase === "unresolved").length;
  const settled = done + failed + cancelled + unresolved;
  return {
    total: items.length,
    done,
    failed,
    cancelled,
    unresolved,
    active: items.length - settled,
    percent: items.length === 0 ? 0 : Math.round((settled / items.length) * 100),
    finished: items.length > 0 && settled === items.length,
  };
}

/**
 * Run `work` over the queue, at most `limit` at a time.
 *
 * Workers pull from a shared cursor rather than the batch being sliced into fixed groups.
 * With slices, a group containing one large file holds up the two that finished instantly
 * beside it; pulling means a worker that finishes early takes the next thing waiting.
 */
export async function pooled<T>(
  items: T[],
  limit: number,
  work: (item: T) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const item = items[cursor++];
      if (item !== undefined) await work(item);
    }
  });
  await Promise.all(workers);
}

/**
 * How many documents are watched at once, whatever the batch size.
 *
 * Watching is bounded separately from uploading, and the two limits answer different
 * questions. `CONCURRENCY` is about bytes in flight competing for the API's connection pool.
 * This is about a poll every 1.5 seconds per watched document: detaching the watches from the
 * upload pool fixed the batch advancing three files at a time, and left two hundred watchers
 * running at once — roughly 130 status requests a second, from one browser tab, at the moment
 * the server is busiest ingesting what that tab just sent.
 *
 * Six is enough that the first files report progress immediately and small enough that the
 * poll traffic is a rounding error next to the uploads. A document waiting for a slot is not
 * losing anything: the server keeps its status, and the watch reads it whenever it starts.
 */
export const WATCHING = 6;

/**
 * A bounded queue that accepts work while it is still running.
 *
 * `pooled` cannot do this — it takes the whole list up front, and the watches arrive one at a
 * time as uploads complete. So: a fixed number of workers, a queue they pull from, and a
 * `close` that lets them finish and exit rather than waiting forever for work that is not
 * coming.
 *
 * Nothing here is generic beyond what the one caller needs, deliberately. A queue with
 * priorities, cancellation and backpressure is a library; this is twenty lines that stop a
 * browser tab issuing a hundred and thirty requests a second.
 */
export function relay(limit: number): {
  add: (work: () => Promise<void>) => void;
  close: () => Promise<void>;
} {
  const queue: (() => Promise<void>)[] = [];
  const waiting: (() => void)[] = [];
  let closed = false;

  const wake = () => {
    // Every waiter, not one: `close` has to release all of them, and a worker that finds the
    // queue empty afterwards simply exits.
    while (waiting.length) waiting.shift()?.();
  };

  const worker = async (): Promise<void> => {
    for (;;) {
      const work = queue.shift();
      if (work) {
        await work();
        continue;
      }
      if (closed) return;
      await new Promise<void>((resolve) => waiting.push(resolve));
    }
  };

  const workers = Array.from({ length: limit }, () => worker());

  return {
    add(work) {
      queue.push(work);
      wake();
    },
    async close() {
      closed = true;
      wake();
      await Promise.all(workers);
    },
  };
}
