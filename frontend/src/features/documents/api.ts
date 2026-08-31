/**
 * The corpus: uploading into it, listing it, deleting from it, and the folder tree the
 * server computes over its labels.
 */

import { ApiError, request } from "@/shared/api/http";

export interface DocumentSummary {
  id: string;
  filename: string;
  description: string | null;
  sha256: string;
  /** What kind of file this is, and therefore which viewer opens it. Needed to open a
      document that was never cited, where there is no citation to read it from. */
  media_type: string;
  status: string;
  status_detail: string | null;
  page_count: number | null;
  size_bytes: number;
  uploaded_by: string | null;
  created_at: string;
  /**
   * The labels this document carries, as ids. Names are resolved client-side against
   * `GET /labels`, which returns only what the caller reaches — an id with no name here
   * is a compartment they were admitted to the document through some other route, and
   * showing it would leak the one thing label listing is careful not to.
   */
  label_ids: string[];
}

/**
 * What `POST /documents` actually answers with — not the document alone.
 *
 * `deduplicated` and `labels` were part of the wire contract from the start (see the
 * backend's `UploadResponse` docstring) and this client was declaring a flatter shape that
 * didn't match it: `uploaded.id` read `undefined` on every upload, silently, because nothing
 * here ever threw — `fetch`+`.json()` doesn't care that the shape it parsed isn't the shape
 * a caller expected. The "recent uploads" list in `Upload.tsx` had been rendering blank rows
 * since the day it was written.
 */
export interface UploadResponse {
  document: DocumentSummary;
  labels: string[];
  deduplicated: boolean;
}

export function uploadDocument(
  file: File,
  token: string,
  labels?: string[],
  options?: {
    filename?: string;
    description?: string;
    /** Supplied to watch the bytes go up; switches the call to XHR. */
    onProgress?: (progress: UploadProgress) => void;
    /** Aborts the request in flight. Rejects with `ApiError(0, "aborted")`. */
    signal?: AbortSignal;
  },
): Promise<UploadResponse> {
  const body = new FormData();
  body.append("file", file);
  for (const label of labels ?? []) body.append("labels", label);
  // Both optional overrides. Sent only when there's something to send: an empty string
  // would tell the server "rename this to nothing" instead of "use the file's own name".
  if (options?.filename) body.append("filename", options.filename);
  if (options?.description) body.append("description", options.description);
  // No `Content-Type` header: the browser must set it, because only the browser knows the
  // multipart boundary it generated. Setting it by hand produces a body the server cannot
  // parse, and the error says nothing about why.
  if (!options?.onProgress) {
    return request<UploadResponse>("/documents", token, { method: "POST", body, signal: options?.signal });
  }
  return uploadWithProgress(body, token, options.onProgress, options.signal);
}

export interface UploadProgress {
  /** Bytes confirmed sent. */
  loaded: number;
  total: number;
}

/**
 * The same request, sent over `XMLHttpRequest` so its progress can be watched.
 *
 * `fetch` has no upload-progress event — the streaming request bodies that would give it
 * one need HTTP/2 and are unimplemented in Safari and Firefox — so a real percentage means
 * XHR, still, in 2026. Kept beside the `fetch` path rather than replacing it: everything
 * else in this client is `fetch`, and only this one call has bytes worth watching.
 *
 * Errors are unwrapped into the same `ApiError` every other call raises, so a caller
 * cannot tell which transport ran. The 413 that nginx used to answer with an HTML body is
 * exactly why this parses defensively rather than assuming JSON.
 */
function uploadWithProgress(
  body: FormData,
  token: string,
  onProgress: (progress: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    // Already cancelled before the pool reached this file. Sending the bytes and aborting
    // a moment later would upload a whole document to throw it away.
    if (signal?.aborted) {
      reject(new ApiError(0, "aborted", "The upload was cancelled."));
      return;
    }
    const request = new XMLHttpRequest();
    request.open("POST", "/documents");
    request.setRequestHeader("Authorization", `Bearer ${token}`);

    request.upload.addEventListener("progress", (event) => {
      // `lengthComputable` is false for a body of unknown size. It never is here — a
      // `FormData` of files has a length — but reporting a percentage of an unknown total
      // is how a bar reaches 100% and stays there.
      if (event.lengthComputable) onProgress({ loaded: event.loaded, total: event.total });
    });

    request.addEventListener("load", () => {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(request.responseText) as unknown;
      } catch {
        parsed = null;
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(parsed as UploadResponse);
        return;
      }
      const problem = (parsed ?? {}) as { code?: string; detail?: string; message?: string };
      reject(
        new ApiError(
          request.status,
          problem.code ?? "unknown",
          problem.detail ?? problem.message ?? "The upload failed.",
        ),
      );
    });

    request.addEventListener("error", () =>
      reject(new ApiError(0, "network_error", "The upload could not reach the server.")),
    );
    request.addEventListener("abort", () =>
      reject(new ApiError(0, "aborted", "The upload was cancelled.")),
    );

    // `once`, and removed when the request settles: a controller may outlive the upload it
    // cancelled, and a listener still holding this XHR keeps the whole `FormData` — the
    // file's bytes included — reachable for as long as the batch is on screen.
    const stop = () => request.abort();
    signal?.addEventListener("abort", stop, { once: true });
    request.addEventListener("loadend", () => signal?.removeEventListener("abort", stop));

    request.send(body);
  });
}

/**
 * One document's current state.
 *
 * `POST /documents` returns as soon as the row exists — parsing, chunking and embedding
 * happen in the worker afterwards — so this is how the upload screen finds out when the
 * document it just sent is actually searchable.
 */
export function getDocument(token: string, documentId: string): Promise<DocumentSummary> {
  return request<DocumentSummary>(`/documents/${documentId}`, token);
}

export interface DocumentPage {
  items: DocumentSummary[];
  next_cursor: string | null;
}

/**
 * One page of the corpus the caller's labels reach — newest first, optionally narrowed to
 * one folder from `Folders`. `filter.labelId === null` means the *unlabelled* folder
 * specifically, not "no filter" — omit `filter` entirely for that.
 */
export function listDocuments(
  token: string,
  cursor?: string | null,
  filter?: { labelId: string | null } | null,
  /** Matched against the filename on the server, never filtered in the browser: a client
      filter over one page finds what is near the top and misses the rest, which reads as
      the document not existing. */
  search?: string,
): Promise<DocumentPage> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
  if (search?.trim()) params.set("search", search.trim());
  if (filter) {
    if (filter.labelId === null) params.set("unlabelled", "true");
    else params.set("label_id", filter.labelId);
  }
  const query = params.toString();
  return request<DocumentPage>(`/documents${query ? `?${query}` : ""}`, token);
}

export function deleteDocument(token: string, documentId: string): Promise<void> {
  return request<void>(`/documents/${documentId}`, token, { method: "DELETE" });
}

export interface Folder {
  label_id: string | null;
  name: string;
  is_default: boolean;
  documents: number;
  ready: number;
  processing: number;
  failed: number;
}

export interface FolderTree {
  folders: Folder[];
  total_documents: number;
}

/**
 * The folder tree, grouped by the server.
 *
 * The client is a presentation layer here on purpose: a browser rebuilding this from the
 * flat listing would have to re-implement the rule that an unlabelled document is visible
 * tenant-wide while a labelled one is not — which lives in an RLS policy, and getting it
 * wrong means showing a folder to somebody who cannot open anything inside it.
 */
export function folders(token: string): Promise<FolderTree> {
  return request<FolderTree>("/documents/folders", token);
}

/**
 * How the server's classifier ended, as `POST /labels/suggest` reports it.
 *
 * The server's `Outcome`, name for name — `backend/app/features/ingestion/classification.py`
 * is where the reasoning lives and it is worth reading rather than paraphrasing here. What
 * matters on this side is that **only `chose` carries ids**, and the rest are not the same
 * fact: `declined` is the model reading the document and finding no folder that fits,
 * `failed` is the call breaking.
 *
 * The other three are the ways nobody was asked, and they were one value — `unavailable` —
 * until it was found doing what this union exists to stop:
 *
 * - `unavailable`: the *installation* has no model. Ordinary and supported; an operator
 *   configures one. This is the only ending that says anything about the installation.
 * - `no_folders`: *this person* reaches no label that could be suggested — none at all, or
 *   only reserved ones. The model is fine. An administrator grants them a compartment.
 * - `too_many_folders`: this person's reach is past the server's ceiling, so the list is too
 *   long for a model to choose from well. Measured and permanent, not a gap waiting to be
 *   filled: `backend/eval/label-shortlist.json` scored the shortlist that would have lifted
 *   the ceiling and recommended against adopting it.
 *
 * Telling somebody "no model configured" when the model is answering every other question in
 * the product is what the split removes, and it is why a row must not fold these back
 * together to shorten a sentence.
 */
export type SuggestionOutcome =
  | "chose"
  | "declined"
  | "unavailable"
  | "no_folders"
  | "too_many_folders"
  | "failed";

export interface Suggestion {
  outcome: SuggestionOutcome;
  /** Non-empty only under `chose`. */
  labelIds: string[];
  /**
   * Under `failed`, what the provider said about why. Absent otherwise.
   *
   * The outcome says what happens to the document; this says what to do about it, and the
   * two are not interchangeable. `failed` is the same value whether the connector is
   * misconfigured or the billing account is empty — an operator told only the first spends
   * half an hour on three settings that were correct.
   *
   * The server sends it only for its own `GenerationUnavailableError`, whose message was
   * written for a person and had credentials stripped out of it. It is still the provider's
   * prose, so it arrives in whatever language the provider writes and is not translated.
   */
  detail?: string;
}

/**
 * Which of *your* labels this text belongs under, according to the configured model.
 *
 * A suggestion, not an assignment: nothing is written. The staging area sends an excerpt
 * extracted in the browser rather than the file, so a document that is only being
 * considered never leaves the machine.
 *
 * The server resolves candidates from the caller's own reach, so this can only ever name
 * labels they already hold — and answers `200` with no ids rather than an error when no
 * model is configured, because an installation without generation still uploads documents.
 *
 * **Returns the ending as well as the ids.** It used to return `string[]`, which made those
 * three empty endings one value and left the caller to guess — and the caller guessed the
 * comfortable one. See `upload/suggestion.ts`.
 *
 * Still rejects on a transport failure, like every other function here. Turning a rejection
 * into a fifth outcome is the *caller's* decision and it is made in one named place, not
 * hidden behind a client that quietly reports success.
 */
export function suggestLabels(token: string, excerpt: string): Promise<Suggestion> {
  return request<{ label_ids: string[]; outcome: SuggestionOutcome; detail?: string | null }>(
    "/labels/suggest",
    token,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ excerpt }),
    },
  ).then((response) => ({
    outcome: response.outcome,
    labelIds: response.label_ids,
    // `null` on the wire for every ending but `failed`; `undefined` here, so that "there is
    // no detail" is one value on this side rather than two a caller has to remember.
    detail: response.detail ?? undefined,
  }));
}

/**
 * The three endings that are settled before a model is spoken to.
 *
 * Derived from `SuggestionOutcome` with `Extract` rather than written out again, so removing
 * or renaming one of them fails the build here instead of leaving a dead branch behind.
 */
export type SuggestionRefusal = Extract<
  SuggestionOutcome,
  "unavailable" | "no_folders" | "too_many_folders"
>;

export interface SuggestionAvailability {
  /** `null` when automatic labelling can be offered to this person. */
  reason: SuggestionRefusal | null;
}

/**
 * Whether automatic labelling can be offered to *this signed-in person* — asked before the
 * button is drawn rather than learned from a note on every row afterwards.
 *
 * `unavailable`, `no_folders` and `too_many_folders` are decided by the caller's reach and by
 * the installation's configuration, so none of them depends on the document. Learning them
 * per file, after a pass over a hundred of them, means offering a prominent action that could
 * never have done anything — and on a fresh tenant that is the ordinary case, not the edge:
 * four of the six tenants in `backend/eval/label-shortlist.json` hold labels and no offerable
 * ones.
 *
 * **The rule is not evaluated here, and deliberately so.** "Offerable" is
 * `NOT is_quarantine AND NOT is_default`, `LabelResponse` publishes neither flag beyond
 * `is_default`, and the ceiling is a server constant — a client that re-derived any of it
 * would be a second implementation of a predicate no test on this side can reach, free to
 * drift from the server the day the filter changes. `reason` is the same `Outcome` value
 * `POST /labels/suggest` would report for this caller, produced by the front half of the same
 * function, so the two cannot disagree.
 *
 * `chose`, `declined` and `failed` are never returned: they describe how a call went, and no
 * call has been made. A model that is configured but broken looks available here and reports
 * `failed` per file, which is correct — nothing short of calling it can know.
 *
 * Rejects on a transport failure like every other function in this module. A caller that
 * cannot reach the server does not know the action is unavailable, and hiding the button on a
 * failed pre-flight would remove a working one.
 */
export function suggestionAvailability(token: string): Promise<SuggestionAvailability> {
  return request<SuggestionAvailability>("/labels/suggest/availability", token);
}

/** What a document is made of, and how much it has been used. */
export interface DocumentInsights {
  /** Passages after chunking — the unit retrieval actually searches. */
  chunks: number;
  /** How many distinct answers have cited it. Zero is a fact worth showing. */
  answers: number;
  /** The uploader's address. Null once that user has been deleted — a document outlives
      the person who added it. */
  uploaded_by: string | null;
}

/**
 * Its own request, not a field on the document.
 *
 * Both numbers are aggregates over other tables. Carrying them on every row of every page
 * — to render a list that shows neither — is work nobody asked for; a detail panel is
 * opened one document at a time.
 */
export function documentInsights(token: string, documentId: string): Promise<DocumentInsights> {
  return request<DocumentInsights>(`/documents/${documentId}/insights`, token);
}
