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

import { getDocument, uploadDocument, type DocumentSummary } from "../api";
import { LabelPicker, labels as fetchLabels, type Label } from "@/features/labels";
import { IN_FLIGHT } from "@/shared/api/tenant";
import {
  PROCESSING,
  processing,
  progress,
} from "./uploadProgress";
import {
  CONCURRENCY,
  enqueue,
  pooled,
  summarise,
  update,
  type QueueItem,
} from "./uploadQueue";
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

      // Bounded concurrency, and the bytes only. Waiting for the *server* to finish
      // ingesting each file before starting the next — which is what the single-file path
      // does, correctly, for one file — turns a migration into an overnight job: ingestion
      // is minutes per document on `low-spec`, and the server queues it regardless of what
      // the browser does. So uploads race, ingestion is watched.
      await pooled(items, CONCURRENCY, async (item) => {
        const started = Date.now();
        try {
          setQueue((current) => update(current, item.id, { phase: "uploading" }));
          // Per file, from the staging table. `selected` is the fallback for the
          // single-file path, which has no table and one set of labels for the one file.
          const forThisFile = perFile?.get(item.file) ?? [...selected];
          const uploaded = await uploadDocument(item.file, token, forThisFile, {
            ...options,
            onProgress: ({ loaded, total }) =>
              setQueue((current) =>
                update(current, item.id, { percent: progress(loaded, total, Date.now() - started).percent }),
              ),
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

          const settled = await untilSettled(token, uploaded.document, (status) =>
            setQueue((current) => update(current, item.id, { stage: processing(status).stage })),
          );
          setRecent((current) => current.map((row) => (row.id === settled.id ? settled : row)));
          setQueue((current) =>
            update(current, item.id, {
              phase: settled.status === "failed" ? "error" : "done",
              message: settled.status_detail ?? undefined,
            }),
          );
        } catch (error) {
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

      setBusy(false);
      onUploaded();
    },
    [token, onUploaded, selected],
  );

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
            <div className="flex size-11 shrink-0 items-center justify-center rounded-md border border-input bg-card text-primary">
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
              className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-card hover:text-foreground"
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
              aria-label="Upload PDF"
              onChange={(event) => {
                choose(event.target.files);
                if (fileInput.current) fileInput.current.value = "";
              }}
              disabled={busy}
            />
            <div className="flex size-12 items-center justify-center rounded-md border border-input bg-card">
              <UploadCloud className="size-6 text-muted-foreground" />
            </div>
            <p className="text-sm">
              <span className="font-semibold text-primary">Choose a PDF</span>
              <span className="text-muted-foreground"> or drop one here</span>
            </p>
            <p className="text-xs text-muted-foreground">Several files at once upload immediately.</p>
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

      {queue.length > 0 && <UploadQueue items={queue} />}

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
 * Wait for the worker to finish with a document.
 *
 * Polled rather than pushed: there is no channel from the worker to the browser, and
 * adding one for a progress label would be a websocket, a subscription and a reconnection
 * story for something a request every second and a half answers.
 *
 * Bounded, because an unbounded wait is a spinner that never stops. Ingestion of a large
 * document on `low-spec` is minutes, so this gives up long after it usually finishes and
 * returns whatever the last look said — the recents row then shows that status honestly
 * rather than claiming the document is ready.
 */
const POLL_MS = 1500;
const POLL_LIMIT = 80; // two minutes

async function untilSettled(
  token: string,
  created: DocumentSummary,
  onStage: (status: string) => void,
): Promise<DocumentSummary> {
  let latest = created;
  for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
    if (!IN_FLIGHT.includes(latest.status as (typeof IN_FLIGHT)[number])) return latest;
    onStage(latest.status);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    try {
      latest = await getDocument(token, created.id);
    } catch {
      // A failed poll is not a failed upload — the document is stored either way. Report
      // the last state actually seen rather than inventing one.
      return latest;
    }
  }
  return latest;
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
function UploadQueue({ items }: { items: QueueItem[] }) {
  const summary = summarise(items);

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-muted-foreground">
          {summary.done} of {summary.total} done
          {summary.failed > 0 && (
            <span className="text-destructive"> · {summary.failed} failed</span>
          )}
        </span>
        {!summary.finished && (
          <span className="text-muted-foreground">{summary.active} in progress</span>
        )}
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
                    : "text-muted-foreground"
              }`}
              // The API's own sentence, on the row it belongs to. A failure in a batch of
              // two hundred is unfindable if it is reported once at the top.
              title={item.message}
            >
              {item.phase === "queued" && "Queued"}
              {item.phase === "uploading" && `${item.percent}%`}
              {item.phase === "processing" && (item.stage ?? "Processing")}
              {item.phase === "done" && "Done"}
              {item.phase === "error" && (item.message ?? "Failed")}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
