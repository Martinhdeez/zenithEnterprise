/**
 * Drag-and-drop upload, and the warning F11's numbers made necessary.
 *
 * Ingestion quadruples query latency on the target hardware — 906 ms becomes 3,870 ms while
 * the embedder is busy. A user who is told that is a user waiting; a user who is not is a
 * user filing a bug about the search being broken. The warning is not decoration, it is the
 * cheapest support ticket this product will ever avoid.
 */

import { useCallback, useState } from "react";

import { ApiError, uploadDocument, type UploadResult } from "../api/client";

interface Props {
  token: string;
  onUploaded: () => void;
}

export function Upload({ token, onUploaded }: Props) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [recent, setRecent] = useState<UploadResult[]>([]);

  const send = useCallback(
    async (files: FileList | null) => {
      if (!files?.length) return;
      setBusy(true);
      setMessage(null);
      try {
        for (const file of Array.from(files)) {
          const uploaded = await uploadDocument(file, token);
          setRecent((current) => [uploaded, ...current].slice(0, 10));
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
    [token, onUploaded],
  );

  return (
    <section className="space-y-3">
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          void send(event.dataTransfer.files);
        }}
        className={`rounded-md border-2 border-dashed p-6 text-center text-sm ${
          over ? "border-sky-400 bg-sky-50" : "border-slate-300"
        }`}
      >
        <label className="cursor-pointer">
          <input
            type="file"
            accept="application/pdf"
            multiple
            className="sr-only"
            aria-label="Upload PDF"
            onChange={(event) => void send(event.target.files)}
            disabled={busy}
          />
          <span className="font-medium text-sky-700">Choose a PDF</span>
          <span className="text-slate-500"> or drop one here</span>
        </label>
        <p className="mt-2 text-xs text-slate-500">
          Searches run more slowly while a document is being processed.
        </p>
      </div>

      {busy && <p className="text-sm text-slate-500">Uploading…</p>}
      {message && (
        <p role="alert" className="text-sm text-red-800">
          {message}
        </p>
      )}

      {recent.length > 0 && (
        <ul className="space-y-1 text-sm">
          {recent.map((document_) => (
            <li key={document_.id} className="flex justify-between gap-2">
              <span className="truncate">{document_.filename}</span>
              <span className="shrink-0 text-slate-500">{document_.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
