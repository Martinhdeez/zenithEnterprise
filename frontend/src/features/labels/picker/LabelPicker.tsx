/**
 * Choosing labels for an upload, at a scale where you cannot see them all.
 *
 * This used to render a grid of every label the tenant had, and switched to a search box
 * only past a threshold. That inverts the real problem: a tenant that has been running for
 * a year has thousands of labels, most of them typos and abandoned experiments, and the
 * question at upload time is never "which of these two thousand" — it is "the one I used
 * yesterday" or "the one whose name starts with contra". So there is one mode, and it
 * always searches.
 *
 * Everything that narrows the list happens on the server. That is the whole point rather
 * than an implementation detail: the client never holds the full set, so filtering it here
 * could only ever filter the page it happens to have.
 *
 * Three filters, because they answer three different questions a person actually has:
 *
 * - **In use** — what the corpus actually carries, as opposed to what somebody once typed.
 * - **Mine** — labels on documents *you* uploaded, which is the short list you file under
 *   by habit. Derived from `documents.uploaded_by`; nothing new is stored to know it.
 * - **Selected** — purely local, and the only one that is: the client knows what it has
 *   ticked, and asking the server would be a round trip to learn something it already
 *   knows. It exists because ticking six labels out of thousands and then wanting to
 *   re-read them is otherwise impossible.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Loader2, Plus, Search, X } from "lucide-react";

import {
  createLabel,
  deleteLabel,
  searchLabels,
  type Label,
  type LabelSearchItem,
  type LabelSort,
} from "../api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useT } from "@/shared/i18n/useT";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const PAGE = 24;

interface Props {
  token: string;
  /** What is ticked. Owned by the parent, which is what actually uploads. */
  selected: Set<string>;
  /**
   * Takes the whole label, not just its id: a label ticked out of a search result may be
   * one the parent has never seen, and it needs the name to render the selected chip after
   * the search that produced it is gone.
   */
  onToggle: (label: Label) => void;
  /**
   * Names for the ticked ids. The selected chips have to render even when the current
   * search does not contain them — otherwise ticking a label and then searching for
   * something else would make the selection appear to vanish.
   */
  known: Map<string, Label>;
  onCreated: (label: Label) => void;
  onRemoved: (id: string) => void;
}

export function LabelPicker({ token, selected, onToggle, known, onCreated, onRemoved }: Props) {
  const t = useT();
  // Built here, not as a module constant. A constant is evaluated once at import — before
  // anyone has chosen a language and long before they can change it — so a translated label
  // frozen there stays in whatever language the first render happened to want. It also puts
  // the strings where a reader can see them: a key held in a data table is invisible to
  // anything that scans call sites, including the test that guarantees every key has a
  // Spanish sentence.
  const SORTS: { value: LabelSort; label: string }[] = [
    { value: "last_used", label: t("Recently used") },
    { value: "name", label: t("Name") },
    { value: "usage_count", label: t("Most used") },
    { value: "created_at", label: t("Newest") },
  ];
  const [query, setQuery] = useState("");
  // "Recently used" first: at this scale the label you want is usually one you have used
  // before, and alphabetical order buries it among thousands you have not.
  const [sort, setSort] = useState<LabelSort>("last_used");
  const [inUse, setInUse] = useState(false);
  const [mine, setMine] = useState(false);
  const [onlySelected, setOnlySelected] = useState(false);

  const [items, setItems] = useState<LabelSearchItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newLabel, setNewLabel] = useState("");

  // Every fetch carries a ticket and a stale one is dropped. Without it a slow request for
  // "contr" lands after a fast one for "contratos" and the list shows results for a query
  // the box no longer holds.
  const latest = useRef(0);

  const load = useCallback(
    async (append?: string) => {
      const ticket = ++latest.current;
      setLoading(true);
      setError(null);
      try {
        const page = await searchLabels(token, {
          q: query || undefined,
          sort,
          cursor: append,
          limit: PAGE,
          inUse: inUse || undefined,
          mine: mine || undefined,
        });
        if (ticket !== latest.current) return;
        setItems((current) => (append ? [...current, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (failure) {
        if (ticket !== latest.current) return;
        setError(message(failure, "Those labels couldn't be loaded."));
      } finally {
        if (ticket === latest.current) setLoading(false);
      }
    },
    [token, query, sort, inUse, mine],
  );

  // Debounced on the query — typed a character at a time — and immediate on everything
  // else, which is a single deliberate click. The cursor is deliberately not a dependency:
  // it belongs to the ordering that issued it, and the server rejects one carried across a
  // sort change rather than quietly returning a page from the wrong position.
  useEffect(() => {
    const timer = setTimeout(() => void load(), query ? 200 : 0);
    return () => clearTimeout(timer);
  }, [load, query]);

  const create = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const created = await createLabel(token, name.trim());
        onCreated(created);
        setNewLabel("");
        void load();
      } catch (failure) {
        setError(message(failure, "That label couldn't be created."));
      }
    },
    [token, onCreated, load],
  );

  const remove = useCallback(
    async (id: string) => {
      setError(null);
      try {
        await deleteLabel(token, id);
        onRemoved(id);
        setItems((current) => current.filter((item) => item.id !== id));
      } catch (failure) {
        // The server refuses while documents still carry it, and says how many — deleting
        // a document's last label leaves it visible tenant-wide. That wording is the only
        // part the user can act on.
        setError(message(failure, "That label couldn't be removed."));
      }
    },
    [token, onRemoved],
  );

  const chosen = useMemo(
    () => [...selected].map((id) => known.get(id)).filter((label): label is Label => !!label),
    [selected, known],
  );
  const shown = onlySelected ? chosen : items;
  const trimmed = query.trim();
  const exists = items.some((item) => item.name.toLowerCase() === trimmed.toLowerCase());

  return (
    <fieldset className="space-y-3 rounded-2xl border border-input bg-secondary p-5">
      <legend className="px-1 text-xs font-semibold tracking-wide text-foreground uppercase">{t("Labels")}</legend>
      <p className="-mt-1 text-xs text-muted-foreground">
        {t("File under")}
        {selected.size === 0 && ` ${t("(no label — visible tenant-wide)")}`}
      </p>

      {selected.size > 0 && (
        <div className="flex flex-wrap justify-center gap-2 border-b border-input pb-3">
          {chosen.map((label) => (
            <button
              key={label.id}
              type="button"
              onClick={() => onToggle(label)}
              aria-label={`Remove ${label.name}`}
              className="inline-flex items-center gap-1.5 rounded-full border border-primary bg-primary/15 py-1.5 pr-2 pl-3.5 text-sm font-medium text-primary"
            >
              {label.name}
              <X className="size-3" />
            </button>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-48 flex-1">
          <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("Search labels")}
            aria-label={t("Search labels")}
            disabled={onlySelected}
            className="h-9 rounded-full bg-card border-input hover:border-muted-foreground/40 dark:bg-background dark:border-muted-foreground/35 dark:shadow-[inset_0_1px_3px_rgb(0_0_0/0.45)] dark:hover:border-muted-foreground/55 pl-9 text-sm text-foreground placeholder:text-muted-foreground focus-visible:border-primary focus-visible:ring-primary/40"
          />
        </div>
        <Select
          value={sort}
          onValueChange={(value) => setSort(value as LabelSort)}
          disabled={onlySelected}
        >
          <SelectTrigger
            className="h-9 w-40 rounded-full border-input bg-card"
            aria-label={t("Sort labels")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SORTS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex flex-wrap gap-2">
        <Toggle on={inUse} onClick={() => setInUse(!inUse)} disabled={onlySelected}>{t("In use")}</Toggle>
        <Toggle on={mine} onClick={() => setMine(!mine)} disabled={onlySelected}>{t("Mine")}</Toggle>
        <Toggle
          on={onlySelected}
          onClick={() => setOnlySelected(!onlySelected)}
          disabled={selected.size === 0}
        >
          {t("Selected")} {selected.size > 0 && `(${selected.size})`}
        </Toggle>
      </div>

      <div className="flex flex-wrap justify-center gap-2">
        {shown.map((label) => (
          <span
            key={label.id}
            className={`group relative inline-flex items-center rounded-full border text-sm transition-colors ${
              selected.has(label.id)
                ? "border-primary bg-primary/20 font-medium text-primary"
                : quarantine(label)
                  ? // The one label the product itself treats differently: unfiled uploads
                    // land here and only an administrator reaches it. Amber is already the
                    // colour of "the system could not decide" in this interface, which is
                    // exactly what an unclassified document is.
                    "border-zenith-amber/40 bg-zenith-amber/10 text-zenith-amber hover:bg-zenith-amber/20"
                  : // Solid in dark, 60% in light, and the split is not cosmetic. `--input`
                    // does two jobs in this codebase — it is `border-input` on every control
                    // and `bg-input/60` on some thirty surfaces — and in light those jobs
                    // pull opposite ways: a boundary has to go dark to be seen against white,
                    // a raised surface has to stay light. `--input` follows the boundary,
                    // because that is the job with a standard behind it, and a fill taken
                    // from it needs the alpha the rest of the app already uses.
                    //
                    // Light leans on the border rather than the fill, which is what a strong
                    // `--input` buys: at 3.11:1 the edge does the separating, so the fill can
                    // stay out of the way. A 60% fill was measured first and was not
                    // unreadable — 9.94:1 for its text — it was simply heavy: seventeen
                    // mid-grey slugs on a near-white card, darker than the white filters
                    // above them, which inverted the raised/sunk story the dark theme tells.
                    //
                    // Dark keeps the solid value: at 50% a chip landed 0.025 above its own
                    // panel, a third of the 0.068 step measured as the minimum anyone can see
                    // from across a room, and the row read as one flat field.
                    "border-input bg-input/15 text-foreground hover:bg-input/30 dark:bg-input dark:hover:bg-input/80"
            }`}
          >
            {/* Equal padding on both sides at rest — the delete button is an absolute
                overlay rather than a flex sibling precisely so it does not eat into the
                right side and leave the label text looking shoved left.

                On hover the chip makes room for it instead. An overlay with nothing behind
                it sat on top of the last character or two of every name long enough to fill
                the chip, so the moment you reached for the delete button was the moment you
                could no longer read what you were deleting. The padding is animated for the
                same reason it is added: the chip growing is the thing that explains where
                the button came from. */}
            <button
              type="button"
              aria-pressed={selected.has(label.id)}
              onClick={() => onToggle(label)}
              className="py-1.5 pr-3.5 pl-3.5 transition-[padding] duration-150 group-hover:pr-7"
            >
              <LabelName name={label.name} />
            </button>
            <button
              type="button"
              aria-label={t("Delete label {label}", { label: label.name })}
              // The tenant's own labels, not this product's fixed set — every one of them,
              // default included, has to stay removable or "configurable" is a lie the
              // moment the first mistaken one gets created.
              onClick={() => void remove(label.id)}
              className="absolute top-1/2 right-1 -translate-y-1/2 rounded-full bg-inherit p-1 text-muted-foreground/50 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-destructive/10 hover:text-destructive"
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
      </div>

      {loading && (
        <p className="flex items-center justify-center gap-2 py-1 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" />{t("Searching…")}</p>
      )}

      {!loading && shown.length === 0 && (
        <p className="py-2 text-center text-xs text-muted-foreground">
          {trimmed
            ? t("No labels match “{term}”.", { term: trimmed })
            : t("No labels match those filters.")}
        </p>
      )}

      {cursor && !onlySelected && !loading && (
        <Button
          type="button"
          variant="outline"
          onClick={() => void load(cursor)}
          className="h-8 w-full rounded-md text-xs"
        >{t("Load more")}</Button>
      )}

      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          if (newLabel.trim()) void create(newLabel);
        }}
        className="flex items-center gap-2 border-t border-input pt-3"
      >
        <Input
          value={newLabel}
          onChange={(event) => setNewLabel(event.target.value)}
          placeholder={t("Label name")}
          aria-label={t("New label name")}
          maxLength={100}
          className="h-9 max-w-56 rounded-full bg-card border-input hover:border-muted-foreground/40 dark:bg-background dark:border-muted-foreground/35 dark:shadow-[inset_0_1px_3px_rgb(0_0_0/0.45)] dark:hover:border-muted-foreground/55 px-4 text-sm text-foreground placeholder:text-muted-foreground focus-visible:border-primary focus-visible:ring-primary/40"
        />
        <Button
          type="submit"
          variant="outline"
          disabled={!newLabel.trim() || exists}
          className="h-9 gap-1.5 rounded-md border-dashed text-sm font-medium"
        >
          <Plus className="size-4" />{t("New label")}</Button>
      </form>

      {error && <p className="text-xs text-destructive">{error}</p>}
    </fieldset>
  );
}

function Toggle({
  on,
  onClick,
  disabled,
  children,
}: {
  on: boolean;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        on
          ? "border-primary bg-primary/20 text-primary"
          : // `--card`, one step under the panel. It was `--background` for a day and that
            // was wrong for a reason worth keeping written down: the fields sit at
            // `--background` too, so recessing the filters that far made a control you
            // press look identical to a box you type in. Four things on this panel, four
            // levels, and the fields keep the bottom of the ladder to themselves.
            "border-input bg-card text-muted-foreground hover:bg-card/70 hover:text-foreground"
      }`}
    >
      {children}
    </button>
  );
}

/** The tenant's quarantine label, seeded by migration 0017 and reserved by 0020. */
function quarantine(label: { name: string }): boolean {
  return label.name === "Unclassified";
}

/**
 * A label like `finance/2026/invoices` is a path, and the leaf is the part that identifies
 * it — the parents repeat across every sibling. Dimming them puts the weight on the word
 * that differs instead of setting seventeen chips at one uniform value and asking the
 * reader to find the end of each. Encodes hierarchy that is already in the name; adds no
 * colour to do it.
 */
function LabelName({ name }: { name: string }) {
  const cut = name.lastIndexOf("/");
  if (cut < 0) return <>{name}</>;
  return (
    <>
      <span className="opacity-55">{name.slice(0, cut + 1)}</span>
      {name.slice(cut + 1)}
    </>
  );
}

function message(failure: unknown, fallback: string): string {
  // The server's own wording wherever there is one: a duplicate name, a missing
  // permission, "N documents still carry this label" — all of them say something the user
  // can act on, and replacing them with a generic failure throws that away.
  return failure instanceof ApiError ? failure.message : fallback;
}
