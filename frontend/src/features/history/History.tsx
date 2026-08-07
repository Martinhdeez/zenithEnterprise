/**
 * Past questions.
 *
 * Whose, is not this component's decision. The server reads the caller's permissions and
 * returns their own questions or the tenant's; there is deliberately no scope control here,
 * because a client-side toggle would imply the choice is the client's to make.
 *
 * `mine` is rendered when a shared history is being read, because a shared history is only
 * readable if you can tell whose question was whose.
 *
 * Every row is a button, not a list item with a link inside it — clicking anywhere on a
 * past question re-asks it. History exists so a question is never typed twice.
 */

import { useCallback, useEffect, useState } from "react";

import { history, type HistoryEntry } from "./api";
import { Button } from "@/components/ui/button";

export function History({
  token,
  onAsk,
}: {
  token: string;
  /** Re-runs a past question through the ask box, exactly as if the user had typed it. */
  onAsk: (question: string) => void;
}) {
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
      <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
        {error}
      </p>
    );
  }

  if (!loading && entries.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
        You have not asked anything yet.
      </div>
    );
  }

  return (
    <section className="space-y-4">
      {/* Separate cards rather than one ruled block, matching the document list. A history
          entry is a question *and* the answer it got — two or three lines of prose each —
          and hairline dividers between paragraphs of running text leave the eye with
          nothing to tell it where one entry stops. The gap does that; the raised background
          makes each one a thing on the page rather than text on the panel. */}
      <ul className="space-y-2">
        {entries.map((entry) => (
          <li key={entry.query_id}>
            <button
              type="button"
              onClick={() => onAsk(entry.question)}
              className="w-full rounded-lg border border-border bg-secondary px-4 py-3.5 text-left transition-colors hover:bg-secondary/70"
            >
              <p className="font-medium text-foreground">{entry.question}</p>
              {entry.answer && (
                <p className="mt-1.5 line-clamp-2 text-sm text-muted-foreground">{entry.answer}</p>
              )}
              <p className="mt-2.5 flex flex-wrap gap-x-3 text-xs text-muted-foreground/80">
                <span>{new Date(entry.created_at).toLocaleString()}</span>
                <span>
                  {entry.citations} citation{entry.citations === 1 ? "" : "s"}
                </span>
                {entry.model_used && <span className="font-mono">{entry.model_used}</span>}
                {shared && <span>{entry.mine ? "you" : "a colleague"}</span>}
              </p>
            </button>
          </li>
        ))}
      </ul>

      {cursor && (
        <Button
          type="button"
          variant="outline"
          onClick={() => void load(cursor)}
          disabled={loading}
          className="border-border text-foreground hover:bg-secondary/50"
        >
          {loading ? "Loading…" : "Show older"}
        </Button>
      )}
    </section>
  );
}
