/**
 * The shell: sidebar, chat, viewer.
 *
 * Split-screen rather than a modal for the PDF, and that follows from what a citation is
 * for. The user is checking whether the answer's claim matches the page — a modal makes
 * them close the document to re-read the sentence it was supposed to support, which is
 * exactly the comparison the whole feature exists to enable.
 */

import { Suspense, lazy, useCallback, useEffect, useState } from "react";

import { tenantStatus, type TenantStatus } from "./api/client";
import type { Citation } from "./api/stream";
import { Chat } from "./components/Chat";
import { Login } from "./components/Login";
import { StatusBadge } from "./components/StatusBadge";
import { Upload } from "./components/Upload";

// Lazily loaded, and for a measured reason: `pdf.js` is roughly 1.4 MB of worker plus its
// own runtime, and none of it is needed until someone clicks a citation. F11 made
// time-to-first-answer the metric this client is judged on, and paying for a PDF engine
// before the first question is asked works directly against that.
const PdfViewer = lazy(() =>
  import("./components/PdfViewer").then((module) => ({ default: module.PdfViewer })),
);

// Session storage rather than local storage: the token is short-lived (15 minutes, F2) and
// this keeps it out of other tabs and out of the profile after the browser closes. Not a
// substitute for the httpOnly cookie an eventual hardening pass wants, and recorded as
// such rather than quietly treated as sufficient.
const TOKEN_KEY = "zenith.token";

export function App() {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem(TOKEN_KEY));
  const [status, setStatus] = useState<TenantStatus | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      setStatus(await tenantStatus(token));
    } catch {
      // A failed status must not take the application down. Search still works, and
      // `StatusBadge` renders its loading state rather than an error nobody can act on.
      setStatus(null);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    // Ingestion is asynchronous, so the counts change without the user doing anything.
    // Thirty seconds is slow enough to be invisible on the network and fast enough that a
    // finished upload appears before anyone reaches for a reload.
    if (!token) return;
    const timer = setInterval(() => void refresh(), 30_000);
    return () => clearInterval(timer);
  }, [token, refresh]);

  if (!token) {
    return (
      <Login
        onAuthenticated={(issued) => {
          sessionStorage.setItem(TOKEN_KEY, issued);
          setToken(issued);
        }}
      />
    );
  }

  return (
    <div className="flex h-screen bg-white text-slate-900">
      <nav className="flex w-64 shrink-0 flex-col gap-6 border-r border-slate-200 p-4">
        <div>
          <h1 className="text-lg font-semibold">Zenith</h1>
          <p className="text-xs text-slate-500">Ask your documents</p>
        </div>

        <StatusBadge status={status} />
        <Upload token={token} onUploaded={() => void refresh()} />

        <button
          type="button"
          onClick={() => {
            sessionStorage.removeItem(TOKEN_KEY);
            setToken(null);
          }}
          className="mt-auto text-left text-sm text-slate-500 hover:text-slate-900"
        >
          Sign out
        </button>
      </nav>

      <main className="flex-1 overflow-auto p-6">
        <Chat
          token={token}
          onCitation={setCitation}
          searchable={status?.searchable ?? true}
        />
      </main>

      <div className="w-[38rem] shrink-0 border-l border-slate-200">
        <Suspense
          fallback={<p className="p-6 text-sm text-slate-500">Opening the document…</p>}
        >
          <PdfViewer citation={citation} token={token} />
        </Suspense>
      </div>
    </div>
  );
}
