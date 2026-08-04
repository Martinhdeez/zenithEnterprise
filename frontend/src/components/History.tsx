/**
 * Past questions.
 *
 * Whose, is not this component's decision. The server reads the caller's permissions and
 * returns their own questions or the tenant's; there is deliberately no scope control here,
 * because a client-side toggle would imply the choice is the client's to make.
 *
 * `mine` is rendered when a shared history is being read, because a shared history is only
 * readable if you can tell whose question was whose.
 */

import { useCallback, useEffect, useState } from "react";

import { history, type HistoryEntry } from "../api/client";

export function History({ token }: { token: string }) {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (from: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const page = await history(token, from);
        // Appended rather than replaced: this is keyset pagination, so each page is the
        // continuation of the last and re-fetching from the top would be a different list.
        setEntries((current) => (from ? [...current, ...page.entries] : page.entries));
        setCursor(page.next_cursor);
      } catch {
        setError("Your history could not be loaded.");
      } finally {
        setLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  const shared = entries.some((entry) => !entry.mine);

  if (error) {
    return (
      <p role="alert" className="text-sm text-red-800">
        {error}
      </p>
    );
  }

  if (!loading && entries.length === 0) {
    return <p className="text-sm text-slate-500">You have not asked anything yet.</p>;
  }

  return (
    <section className="space-y-3">
      <ul className="space-y-3">
        {entries.map((entry) => (
          <li key={entry.query_id} className="rounded-md border border-slate-200 p-3">
            <p className="font-medium">{entry.question}</p>
            {entry.answer && (
              <p className="mt-1 line-clamp-2 text-sm text-slate-600">{entry.answer}</p>
            )}
            <p className="mt-2 flex flex-wrap gap-x-3 text-xs text-slate-500">
              <span>{new Date(entry.created_at).toLocaleString()}</span>
              <span>
                {entry.citations} citation{entry.citations === 1 ? "" : "s"}
              </span>
              {entry.model_used && <span>{entry.model_used}</span>}
              {shared && <span>{entry.mine ? "you" : "a colleague"}</span>}
            </p>
          </li>
        ))}
      </ul>

      {cursor && (
        <button
          type="button"
          onClick={() => void load(cursor)}
          disabled={loading}
          className="rounded border border-slate-300 px-3 py-1 text-sm disabled:opacity-50"
        >
          {loading ? "Loading…" : "Show older"}
        </button>
      )}
    </section>
  );
}
