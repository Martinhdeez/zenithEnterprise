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
    return request<UploadResponse>("/documents", token, { method: "POST", body });
  }
  return uploadWithProgress(body, token, options.onProgress);
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
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
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
): Promise<DocumentPage> {
  const params = new URLSearchParams();
  if (cursor) params.set("cursor", cursor);
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
