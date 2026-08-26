/**
 * Drag-and-drop upload, and the warning F11's numbers made necessary.
 *
 * Ingestion quadruples query latency on the target hardware — 906 ms becomes 3,870 ms while
 * the embedder is busy. A user who is told that is a user waiting; a user who is not is a
 * user filing a bug about the search being broken. The warning is not decoration, it is the
 * cheapest support ticket this product will ever avoid.
 *
 * Labels are chosen here, not applied after the fact. The server accepts an unlabelled
 * upload and falls back to the tenant's default label — that fallback exists so a first
 * upload before any label is even created doesn't hard-fail — but a *user* picking no label
 * on a tenant that has several is very rarely what they meant, and there is nothing later in
 * this UI that catches "I uploaded the tax filing under the wrong access label" after the
 * fact. Asking here, once, is cheaper than that support ticket too.
 *
 * A single dropped file pauses in a review step — name and description, both optional,
 * both editable before the bytes go anywhere — because those are the two things nothing
 * later in the product ever lets you attach to a document again. Several files at once skip
 * the review: nobody wants to hand-title twenty PDFs one dialog at a time, and the sha256
 * dedup makes "just fix the name after" cheap if it turns out to matter for one of them.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, UploadCloud, X } from "lucide-react";

import { uploadDocument, type DocumentSummary } from "../api";
import { LabelPicker, labels as fetchLabels, type Label } from "@/features/labels";
import { PROCESSING, formatEta, formatRate, processing, progress } from "./uploadProgress";
import {
  CONCURRENCY,
  WATCHING,
  cancellable,
  enqueue,
  pooled,
  relay,
  summarise,
  update,
  type QueueItem,
} from "./uploadQueue";
import { phaseFor, untilSettled } from "./uploadWatch";
import { Staging } from "./Staging";
import { stage, type StagedFile } from "./stagingState";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  token: string;
  onUploaded: () => void;
}

/** A single dropped/chosen file, paused for its optional name and description. */
interface Staged {
  file: File;
  filename: string;
  description: string;
}

export function Upload({ token, onUploaded }: Props) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [recent, setRecent] = useState<DocumentSummary[]>([]);
  // Not "every label in the tenant" any more — the picker searches server-side and never
  // holds the whole set. This is only the names behind the ticked ids, so the selected
  // chips can render even when the current search does not contain them.
  const [known, setKnown] = useState<Map<string, Label>>(new Map());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [staged, setStaged] = useState<Staged | null>(null);
  // One row per file. Replaces the single progress bar: a batch has no one percentage
  // worth showing, and the question during a migration is which files are stuck, not how
  // far along the average is.
  const [queue, setQueue] = useState<QueueItem[]>([]);
  // Files waiting on a decision. Nothing here has left the browser.
  const [staging, setStaging] = useState<StagedFile[]>([]);
  // One controller per queued file, so a cancel reaches the row it was clicked on rather
  // than the batch. A ref and not state: aborting must not wait for a render, and these
  // are read inside the upload pool, which closes over whatever it was given at call time.
  const controllers = useRef(new Map<string, AbortController>());
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchLabels(token)
      .then((result) => {
        if (cancelled) return;
        setKnown(new Map(result.map((label) => [label.id, label])));
        // Pre-check the tenant's default so the common case — one label, everyone files
        // under it — is a single click, not a click to open the list plus one to check it.
        const fallback = result.find((label) => label.is_default);
        if (fallback) setSelected(new Set([fallback.id]));
      })
      .catch(() => !cancelled && setKnown(new Map()));
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Takes the whole label so a pick out of a search result is remembered by name — the
  // picker paginates server-side, so the row that produced this click may be gone from the
  // list by the time the selected chips render.
  const toggle = useCallback((label: Label) => {
    setKnown((current) => (current.has(label.id) ? current : new Map(current).set(label.id, label)));
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(label.id)) next.delete(label.id);
      else next.add(label.id);
      return next;
    });
  }, []);

  // Creating and deleting happen inside `LabelPicker`, but the list they change lives
  // here — one copy, so the two presentations it can render never disagree about what
  // exists. A newly created label is selected immediately: someone who just typed a name
  // into an upload form meant to file this document under it.
  const added = useCallback((label: Label) => {
    setKnown((current) => new Map(current).set(label.id, label));
    setSelected((current) => new Set(current).add(label.id));
  }, []);

  const removed = useCallback((id: string) => {
    setKnown((current) => {
      const next = new Map(current);
      next.delete(id);
      return next;
    });
    setSelected((current) => {
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  }, []);

  const send = useCallback(
    async (
      files: File[],
      options?: { filename?: string; description?: string },
      perFile?: Map<File, string[]>,
    ) => {
      if (!files.length) return;
      setBusy(true);
      setMessage(null);
      const items = enqueue(files);
      setQueue(items);
      // Built before the pool starts, so a cancel clicked on a row that has not begun
      // uploading yet still has something to abort.
      controllers.current = new Map(items.map((item) => [item.id, new AbortController()]));

      // Bounded concurrency, and the bytes only. Waiting for the *server* to finish
      // ingesting each file before starting the next — which is what the single-file path
      // does, correctly, for one file — turns a migration into an overnight job: ingestion
      // is minutes per document on `low-spec`, and the server queues it regardless of what
      // the browser does. So uploads race, ingestion is watched.
      //
      // The watches live out here rather than inside the pool so that they outlive the slot
      // that started them — and go through a bound of their own, because detaching them from
      // the upload pool left a two-hundred-file batch running two hundred pollers at once.
      // `WATCHING` says why six.
      const watching = relay(WATCHING);

      await pooled(items, CONCURRENCY, async (item) => {
        const started = Date.now();
        const controller = controllers.current.get(item.id);
        // Cancelled while it sat in the queue. The pool still hands it over — it holds a
        // plain list and knows nothing about cancellation — so the check belongs here,
        // before any bytes are read off disk.
        if (controller?.signal.aborted) {
          setQueue((current) => update(current, item.id, { phase: "cancelled" }));
          return;
        }
        try {
          setQueue((current) => update(current, item.id, { phase: "uploading" }));
          // Per file, from the staging table. `selected` is the fallback for the
          // single-file path, which has no table and one set of labels for the one file.
          const forThisFile = perFile?.get(item.file) ?? [...selected];
          const uploaded = await uploadDocument(item.file, token, forThisFile, {
            ...options,
            signal: controller?.signal,
            onProgress: ({ loaded, total }) => {
              const stats = progress(loaded, total, Date.now() - started);
              setQueue((current) =>
                update(current, item.id, {
                  percent: stats.percent,
                  bytesPerSecond: stats.bytesPerSecond,
                  secondsRemaining: stats.secondsRemaining,
                }),
              );
            },
          });

          // The bytes are in and the request is already answered: `POST /documents`
          // returns once the row exists. The wait the user notices starts *here*.
          setQueue((current) =>
            update(current, item.id, {
              phase: "processing",
              percent: 100,
              documentId: uploaded.document.id,
              stage: PROCESSING.stage,
            }),
          );
          setRecent((current) => [uploaded.document, ...current].slice(0, 10));

          // **Started, not awaited, and that is the whole point of this line.** Awaiting it
          // here kept the pool slot for the length of the *server's* work, so three files
          // being ingested stopped the other hundred and ninety-seven from uploading at all
          // — for up to two minutes each. A twenty-file batch advanced in groups of three
          // and looked frozen, which is the opposite of what bounded concurrency was for.
          //
          // The bound that matters is on bytes in flight, because that is what competes for
          // the API's connection pool. Watching a document costs one small request every
          // 1.5 s and does not.
          watching.add(async () => {
            const watched = await untilSettled(token, uploaded.document, (status) =>
              setQueue((current) => update(current, item.id, { stage: processing(status).stage })),
            );
            setRecent((current) =>
              current.map((row) => (row.id === watched.document.id ? watched.document : row)),
            );
            setQueue((current) => update(current, item.id, phaseFor(watched)));
          });
        } catch (error) {
          // A cancellation arrives here as a rejection like any other, but it is not a
          // failure and must not be counted as one — somebody asked for it.
          // `aborted` from the XHR path this screen always takes; `AbortError` is what
          // `fetch` raises, for the caller that passes no progress handler.
          const stopped =
            (error instanceof ApiError && error.code === "aborted") ||
            (error instanceof DOMException && error.name === "AbortError");
          if (stopped) {
            setQueue((current) => update(current, item.id, { phase: "cancelled" }));
            return;
          }
          // Per file, not per batch. One duplicate in two hundred must not abandon the
          // other hundred and ninety-nine, and the API's own message — a duplicate, a file
          // that is not a PDF, a tenant at its limit — is the only part the user can act on.
          setQueue((current) =>
            update(current, item.id, {
              phase: "error",
              message: error instanceof ApiError ? error.message : "The upload failed.",
            }),
          );
        }
      });

      // Every byte is up and every row exists server-side. Refreshing here rather than after
      // the watches means the library and its folder counts are right within seconds of the
      // transfer instead of trailing ingestion by up to two minutes per file.
      onUploaded();

      await watching.close();
      setBusy(false);
      // Again, now that the statuses have stopped moving: the call above listed most of these
      // documents as still processing, which was true then and is not now.
      onUploaded();
    },
    [token, onUploaded, selected],
  );

  /**
   * Stop one file.
   *
   * The row is marked here rather than waiting for the abort to come back through the
   * `catch`, because a file still sitting in the queue has no request in flight to reject —
   * for that one, this is the only thing that will ever move it off "Queued".
   */
  const cancelOne = useCallback((id: string) => {
    controllers.current.get(id)?.abort();
    setQueue((current) =>
      current.map((item) =>
        item.id === id && cancellable(item.phase) ? { ...item, phase: "cancelled" } : item,
      ),
    );
  }, []);

  /**
   * Stop everything still stoppable. Files already being ingested are left alone.
   *
   * Every controller is aborted, not only the ones belonging to stoppable rows: aborting
   * an `XMLHttpRequest` that has already settled does nothing, and the alternative — asking
   * which rows are stoppable from inside the state updater — puts a side effect somewhere
   * React is free to run twice.
   */
  const cancelAll = useCallback(() => {
    for (const controller of controllers.current.values()) controller.abort();
    setQueue((current) =>
      current.map((item) => (cancellable(item.phase) ? { ...item, phase: "cancelled" } : item)),
    );
  }, []);

  const confirmBatch = useCallback(() => {
    const perFile = new Map(staging.map((row) => [row.file, row.labelIds]));
    void send(
      staging.map((row) => row.file),
      undefined,
      perFile,
    ).then(() => setStaging([]));
  }, [staging, send]);

  const choose = useCallback((files: FileList | null) => {
    if (!files?.length) return;
    const file = files[0];
    if (files.length === 1 && file && staging.length === 0) {
      // One file, nothing staged: the review step that lets somebody rename it and write a
      // description. A batch has no single filename to prefill against, and nobody is going
      // to write a description a thousand times.
      setStaged({ file, filename: file.name, description: "" });
      return;
    }
    // Everything else stages. Uploading on drop is what put a thousand documents under the
    // default label before anybody had said what any of them were.
    setStaging((current) => stage(Array.from(files), current));
  }, [staging.length]);

  const confirmStaged = useCallback(() => {
    if (!staged) return;
    const name = staged.filename.trim();
    const description = staged.description.trim();
    void send([staged.file], {
      filename: name && name !== staged.file.name ? name : undefined,
      description: description || undefined,
    });
    setStaged(null);
  }, [staged, send]);

  return (
    <section className="space-y-6 py-4">
      <LabelPicker
        token={token}
        selected={selected}
        known={known}
        onToggle={toggle}
        onCreated={added}
        onRemoved={removed}
      />

      {staged ? (
        // `bg-card` would be invisible here: the page's own `<main>` wrapper is already
        // `bg-card` (App.tsx), so this panel needs a genuinely different tone — `bg-secondary`
        // — plus `border-input` rather than `border-border`, which reads a shade too close to
        // both to register as an edge on its own.
        <div className="space-y-5 rounded-md border border-input bg-secondary p-6">
          <div className="flex items-start gap-3.5">
            <div className="flex size-11 shrink-0 items-center justify-center rounded-md border border-input bg-input/50 text-primary">
              <FileText className="size-5" />
            </div>
            <div className="min-w-0 flex-1 space-y-0.5 pt-0.5">
              <p className="truncate text-sm font-medium text-foreground">{staged.file.name}</p>
              <p className="text-xs text-muted-foreground">
                {(staged.file.size / 1024).toFixed(1)} KB
              </p>
            </div>
            <button
              type="button"
              aria-label="Cancel"
              onClick={() => setStaged(null)}
              className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-input/60 hover:text-foreground"
            >
              <X className="size-4" />
            </button>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="staged-filename" className="text-xs font-medium text-muted-foreground">
              Name <span className="font-normal">(optional — defaults to the file's own name)</span>
            </label>
            <Input
              id="staged-filename"
              value={staged.filename}
              onChange={(event) => setStaged((current) => current && { ...current, filename: event.target.value })}
              maxLength={255}
              className="rounded-md border-input bg-background text-foreground focus-visible:border-primary focus-visible:ring-primary/40"
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="staged-description" className="text-xs font-medium text-muted-foreground">
              Description <span className="font-normal">(optional)</span>
            </label>
            <Textarea
              id="staged-description"
              value={staged.description}
              onChange={(event) => setStaged((current) => current && { ...current, description: event.target.value })}
              placeholder="What is this document, or why does it matter?"
              maxLength={2000}
              className="rounded-md border-input bg-background text-foreground focus-visible:border-primary focus-visible:ring-primary/40"
            />
          </div>

          <div className="flex justify-end border-t border-input pt-4">
            <Button type="button" variant="outline" onClick={() => setStaged(null)} disabled={busy} className="rounded-md">
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        // The dropzone is the page's one job, so it leads: a plain-bordered, higher-contrast
        // panel rather than a dashed outline floating on the same background as everything
        // else around it. `bg-secondary` + `border-input`, not `bg-card`/`border-border` —
        // the page's own `<main>` wrapper is already `bg-card` with a `border-border` frame
        // one level up, so those two tokens read as "the page", not "a panel on the page".
        <div
          onDragOver={(event) => {
            event.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(event) => {
            event.preventDefault();
            setOver(false);
            choose(event.dataTransfer.files);
          }}
          className={`rounded-md border p-12 text-center transition-colors ${
            over ? "border-primary bg-primary/5" : "border-input bg-secondary hover:bg-secondary/80"
          }`}
        >
          <label className="flex cursor-pointer flex-col items-center gap-3">
            <input
              ref={fileInput}
              type="file"
              accept="application/pdf"
              multiple
              className="sr-only"
              aria-label="Upload PDFs"
              onChange={(event) => {
                choose(event.target.files);
                if (fileInput.current) fileInput.current.value = "";
              }}
              disabled={busy}
            />
            <div className="flex size-12 items-center justify-center rounded-md border border-input bg-input/60">
              <UploadCloud className="size-6 text-muted-foreground" />
            </div>
            {/* Plural, because the control is. The singular copy this replaced ("Choose a
                PDF or drop one here") described a dropzone that had accepted `multiple`
                since it was written, and people believed it: nobody tries to drag five
                files at something that asks for one. */}
            <p className="text-sm">
              <span className="font-semibold text-primary">Choose PDFs</span>
              <span className="text-muted-foreground"> or drop them here</span>
            </p>
            {/* The line here used to read "Several files at once upload immediately", which
                was true of the flow the staging area replaced and is now the opposite of
                what happens — a batch is held in the browser until Confirm & Process. Stale
                copy that contradicts the product is worse than no copy: it teaches people
                the screen is not to be trusted. */}
            <p className="text-xs text-muted-foreground">
              Drop several to tag them together before anything is sent.
            </p>
          </label>
          <p className="mt-6 text-xs text-muted-foreground">
            Searches run more slowly while a document is being processed.
          </p>
        </div>
      )}

      {/* One persistent Upload button rather than one buried inside the staged-review card:
          same position whether a file is chosen or not, so it's the answer to "how do I
          finish this" instead of something that only appears once a file already has. Off
          while `staged` is null — there's nothing to send yet — on once a single file is
          chosen; the several-files-at-once path never sets `staged` at all, since those
          upload immediately with no review step for this button to gate. */}

      {staging.length > 0 && (
        <Staging
          token={token}
          rows={staging}
          known={known}
          onChange={setStaging}
          onConfirm={confirmBatch}
          busy={busy}
        />
      )}

      {queue.length > 0 && (
        <UploadQueue items={queue} onCancel={cancelOne} onCancelAll={cancelAll} />
      )}

      <Button
        type="button"
        onClick={confirmStaged}
        disabled={!staged || busy}
        // The base `Button` component's own `disabled:opacity-50` on a solid `bg-primary`
        // still reads as "the same vivid button, just slightly dimmer" — not obviously
        // inert. `bg-muted` was tried first and is worse, not better: it's the exact same
        // token as the page's own `<main>` background (`--muted` and `--card` share a
        // value in the dark palette), so the button vanished into the page instead of
        // reading as disabled. `bg-secondary` is a real step up from the page without any
        // of `bg-primary`'s colour, which is the actual "there's nothing to click yet"
        // signal — visibly boxed, visibly not the vivid action colour.
        className="w-full rounded-md disabled:cursor-not-allowed disabled:border-input disabled:bg-secondary disabled:text-muted-foreground disabled:opacity-100"
      >
        {/* Below the file list rather than above it. It is the last control pressed, and
            a button that sits before the thing it acts on asks somebody to scroll back up
            to check what they are about to send. */}
        {busy ? "Uploading…" : "Upload"}
      </Button>

      {message && (
        <p role="alert" className="text-sm text-destructive">
          {message}
        </p>
      )}

      {recent.length > 0 && (
        <ul className="space-y-1.5 text-sm">
          {recent.map((document_) => (
            <li
              key={document_.id}
              className="flex items-center justify-between gap-2 rounded-md border border-border bg-secondary px-3 py-2"
            >
              <span className="truncate text-foreground">{document_.filename}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{document_.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}


/**
 * One row per file, and a count above them.
 *
 * A batch has no single percentage worth showing. The question during a migration is not
 * "how far along is the average" — it is *which files are stuck and which failed*, and an
 * averaged bar answers neither while looking like it answers both.
 *
 * The list scrolls in its own box rather than growing the page: two hundred rows below a
 * dropzone pushes the dropzone off screen, and the dropzone is where the next batch goes.
 * Not virtualised — two hundred rows of two spans is nothing, and a windowing library here
 * would be a dependency bought against a cost nobody has measured.
 */
function UploadQueue({
  items,
  onCancel,
  onCancelAll,
}: {
  items: QueueItem[];
  onCancel: (id: string) => void;
  onCancelAll: () => void;
}) {
  const summary = summarise(items);
  const stoppable = items.some((item) => cancellable(item.phase));

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="text-muted-foreground">
          {summary.done} of {summary.total} done
          {summary.failed > 0 && (
            <span className="text-destructive"> · {summary.failed} failed</span>
          )}
          {/* Neither "done" nor "failed". A batch where somebody stopped forty on purpose
              should not read as forty things having gone wrong. */}
          {summary.cancelled > 0 && <span> · {summary.cancelled} cancelled</span>}
          {/* Also neither. These uploaded fine and are still being ingested on the server;
              amber rather than red because nothing has gone wrong yet, and separate from
              `done` because nothing has finished either. */}
          {summary.unresolved > 0 && (
            <span className="text-zenith-amber"> · {summary.unresolved} still processing</span>
          )}
        </span>
        <span className="flex shrink-0 items-center gap-3">
          {!summary.finished && (
            <span className="text-muted-foreground">{summary.active} in progress</span>
          )}
          {/* Disappears once nothing can be stopped, rather than greying out: while files
              are ingesting there is still activity on screen, and a permanently dimmed
              Cancel beside it invites clicks that cannot do anything. */}
          {stoppable && (
            <button
              type="button"
              onClick={onCancelAll}
              className="rounded-full px-2 py-0.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            >
              Cancel remaining
            </button>
          )}
        </span>
      </div>

      {/* Counted by file, not by byte: "14 of 200" is actionable, and a byte percentage
          moves in jumps that do not match what the rows are doing. */}
      <div className="h-1 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-300"
          style={{ width: `${summary.percent}%` }}
        />
      </div>

      <ul className="max-h-72 space-y-1 overflow-y-auto rounded-md border border-input p-1.5">
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-2 px-2 py-1.5 text-sm">
            <span className="min-w-0 flex-1 truncate text-foreground">{item.file.name}</span>
            <span
              className={`shrink-0 text-xs ${
                item.phase === "error"
                  ? "text-destructive"
                  : item.phase === "done"
                    ? "text-zenith-cyan"
                    : item.phase === "unresolved"
                      ? "text-zenith-amber"
                      : "text-muted-foreground"
              }`}
              // The API's own sentence, on the row it belongs to. A failure in a batch of
              // two hundred is unfindable if it is reported once at the top.
              title={item.message}
            >
              {item.phase === "queued" && "Queued"}
              {item.phase === "uploading" && <Transferring item={item} />}
              {item.phase === "processing" && (item.stage ?? "Processing")}
              {item.phase === "done" && "Done"}
              {item.phase === "unresolved" && (item.message ?? "Still processing")}
              {item.phase === "error" && (item.message ?? "Failed")}
              {item.phase === "cancelled" && "Cancelled"}
            </span>
            {/* Reserved whether or not this row can be stopped, so the percentages beside
                it stay on one vertical line instead of shifting as rows settle. */}
            <span className="flex size-6 shrink-0 items-center justify-center">
              {cancellable(item.phase) && (
                <button
                  type="button"
                  onClick={() => onCancel(item.id)}
                  aria-label={`Cancel ${item.file.name}`}
                  className="rounded-full p-1 text-muted-foreground transition-colors hover:bg-secondary hover:text-destructive"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * What a row says while its bytes are moving.
 *
 * The percentage alone is the least useful of the three numbers on a migration: it says how
 * far *this* file has got and nothing about whether the transfer is still moving. A rate is
 * what tells somebody watching a 400 MB upload that it has not stalled, and an estimate is
 * what tells them whether to wait.
 *
 * Both were computed on every progress event and thrown away — `formatRate` and `formatEta`
 * existed, were tested, and were called by nothing but their own tests. Dead code wearing a
 * feature's clothes, which is the same thing `uploadAbort.test.ts` was written about.
 *
 * They appear only once there is a sample worth reporting. `progress()` returns null for both
 * in the first tenth of a second, because dividing by a near-zero elapsed time claims
 * gigabytes per second and no time remaining on a transfer that has barely started.
 */
function Transferring({ item }: { item: QueueItem }) {
  const rate = formatRate(item.bytesPerSecond ?? null);
  const eta = formatEta(item.secondsRemaining ?? null);
  return (
    <>
      {item.percent}%{rate && ` · ${rate}`}
      {eta && <span className="text-muted-foreground/70"> · {eta} left</span>}
    </>
  );
}
