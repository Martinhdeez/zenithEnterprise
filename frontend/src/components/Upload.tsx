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

import {
  ApiError,
  labels as fetchLabels,
  uploadDocument,
  type DocumentSummary,
  type Label,
} from "../api/client";
import { LabelPicker } from "./LabelPicker";
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
  const [available, setAvailable] = useState<Label[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [staged, setStaged] = useState<Staged | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchLabels(token)
      .then((result) => {
        if (cancelled) return;
        setAvailable(result);
        // Pre-check the tenant's default so the common case — one label, everyone files
        // under it — is a single click, not a click to open the list plus one to check it.
        const fallback = result.find((label) => label.is_default);
        if (fallback) setSelected(new Set([fallback.id]));
      })
      .catch(() => !cancelled && setAvailable([]));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const toggle = useCallback((id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Creating and deleting happen inside `LabelPicker`, but the list they change lives
  // here — one copy, so the two presentations it can render never disagree about what
  // exists. A newly created label is selected immediately: someone who just typed a name
  // into an upload form meant to file this document under it.
  const added = useCallback((label: Label) => {
    setAvailable((current) => [...current, label]);
    setSelected((current) => new Set(current).add(label.id));
  }, []);

  const removed = useCallback((id: string) => {
    setAvailable((current) => current.filter((label) => label.id !== id));
    setSelected((current) => {
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  }, []);

  const send = useCallback(
    async (files: File[], options?: { filename?: string; description?: string }) => {
      if (!files.length) return;
      setBusy(true);
      setMessage(null);
      const labelIds = [...selected];
      try {
        for (const file of files) {
          const uploaded = await uploadDocument(file, token, labelIds, options);
          setRecent((current) => [uploaded.document, ...current].slice(0, 10));
        }
        onUploaded();
      } catch (error) {
        // Every one of these is a message the API wrote for a person: a duplicate, a file
        // that is not a PDF, a tenant at its document limit. Replacing them with a generic
        // failure would throw away the only part the user can act on.
        setMessage(error instanceof ApiError ? error.message : "The upload failed.");
      } finally {
        setBusy(false);
      }
    },
    [token, onUploaded, selected],
  );

  const choose = useCallback((files: FileList | null) => {
    if (!files?.length) return;
    const file = files[0];
    if (files.length === 1 && file) {
      // Pause for the optional name/description — a batch of several skips straight to
      // upload below, since there's no single filename to prefill against.
      setStaged({ file, filename: file.name, description: "" });
      return;
    }
    void send(Array.from(files));
  }, [send]);

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
    <section className="mx-auto max-w-2xl space-y-6 py-4">
      <LabelPicker
        token={token}
        available={available}
        selected={selected}
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
        {/* `busy && !staged` is the several-files-at-once path — those upload immediately
            with no review step, so the button itself stays disabled (nothing staged) while
            this still needs to say what's happening. */}
        {busy ? "Uploading…" : "Upload"}
      </Button>
      {busy && !staged && <p className="text-sm text-muted-foreground">Uploading the selected files…</p>}

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
