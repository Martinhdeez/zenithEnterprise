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
import { SearchField } from "@/shared/ui/SearchField";
import { useFormat, useT } from "@/shared/i18n/useT";

export function History({
  token,
  onAsk,
}: {
  token: string;
  /** Re-runs a past question through the ask box, exactly as if the user had typed it. */
  onAsk: (question: string) => void;
}) {
  const t = useT();
  const format = useFormat();
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // What is typed, and what has been asked for. Separate because the request is debounced:
  // firing on every keystroke would put a query per character on a table that grows with
  // every question anybody asks.
  const [typed, setTyped] = useState("");
  const [search, setSearch] = useState("");
  const [mine, setMine] = useState(false);
  const [unanswered, setUnanswered] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setSearch(typed), 250);
    return () => clearTimeout(timer);
  }, [typed]);

  const load = useCallback(
    async (from: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const page = await history(token, from, { search, mine, unanswered });
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
    [token, search, mine, unanswered],
  );

  useEffect(() => {
    // From the top whenever a filter changes. A cursor names a position in the *previous*
    // result set, and resuming a narrowed list from it would start partway down a list
    // nobody has seen the beginning of.
    void load(null);
  }, [load]);

  const shared = entries.some((entry) => !entry.mine);

  const filtering = search.trim() !== "" || mine || unanswered;

  const controls = (
    <div className="space-y-2">
      {/* One surface, the way the Search screen's composer is built: the border lives on
          the wrapper and the field is borderless inside it, so a field and a button read as
          one control rather than two shapes side by side.

          The button is deliberately not the only way to search — typing still searches, on a
          debounce. It is here because a search box without one looks like it has not
          understood you yet, and pressing something is what tells a person the box is a
          search box rather than a filter that might be broken. */}
      <SearchField
        value={typed}
        onChange={setTyped}
        label={t("Search questions")}
        placeholder={t("Search your questions")}
        // Commits what is typed now rather than waiting out the debounce. Same value, no
        // pause — which is the whole point of pressing it.
        onSubmit={() => setSearch(typed)}
        action={
          <Button type="submit" size="sm" className="shrink-0 rounded-full">{t("Search")}</Button>
        }
      />
      <div className="flex flex-wrap gap-1.5">
        {/* Only offered when there is somebody else's question to look away from. A filter
            that never changes anything is a control that teaches people to ignore controls. */}
        {shared && (
          <Chip active={mine} onClick={() => setMine((on) => !on)}>{t("Only mine")}</Chip>
        )}
        <Chip active={unanswered} onClick={() => setUnanswered((on) => !on)}>{t("Found nothing")}</Chip>
      </div>
    </div>
  );

  if (error) {
    return (
      <div className="space-y-3">
        {controls}
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </p>
      </div>
    );
  }

  if (!loading && entries.length === 0) {
    return (
      <div className="space-y-3">
        {controls}
        <div className="rounded-lg border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
          {/* The two empty states are different questions. "Nothing matched" is answered by
              changing the filter; "you have not asked anything" is answered by asking. */}
          {filtering ? t("No question matches that.") : t("You have not asked anything yet.")}
        </div>
      </div>
    );
  }

  return (
    <section className="space-y-4">
      {controls}
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
                <span>{format.dateTime(entry.created_at)}</span>
                <span>
                  {t("{count} citation", { count: entry.citations })}
                </span>
                {entry.model_used && <span className="font-mono">{entry.model_used}</span>}
                {shared && <span>{entry.mine ? t("you") : t("a colleague")}</span>}
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
          {loading ? t("Loading…") : t("Show older")}
        </Button>
      )}
    </section>
  );
}


/** A filter you can see the state of without reading the results. */
function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition-colors ${
        active
          ? "border-primary/50 bg-primary/10 text-foreground"
          : "border-input bg-input/60 text-muted-foreground hover:border-primary/40 hover:text-foreground"
      }`}
    >
      {children}
    </button>
  );
}
