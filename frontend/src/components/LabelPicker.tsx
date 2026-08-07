/**
 * Choosing labels for an upload, in whichever shape the tenant's label count deserves.
 *
 * Two presentations over one selection model. Below `COMBOBOX_THRESHOLD` labels the chips
 * stay: every option visible at once, one click to file, nothing to type. That is strictly
 * better than a search box for the case this product was designed around — a handful of
 * departments — and it is the shape four rounds of design feedback landed on.
 *
 * Past the threshold the same grid becomes a wall: unpaginated, unsearchable, and taller
 * than the form it belongs to. So it switches to a searchable popover, which is worse for
 * three labels and much better for three hundred.
 *
 * The switch is on `available.length` — every label the caller can file under, which
 * `GET /labels` already returns in full — not on a search result count, so it never
 * flickers between the two while someone is typing.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Check, ChevronsUpDown, Plus, X } from "lucide-react";

import { ApiError, createLabel, deleteLabel, searchLabels, type Label } from "../api/client";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/**
 * Where a chip grid stops being the friendlier option.
 *
 * Picked rather than measured, and worth saying so: nobody has a tenant with this many
 * labels yet. It sits at the point where the grid stops fitting the form without
 * scrolling at the widths this panel is used at — roughly five rows of chips — which is
 * the thing a user would actually notice. Move it when a real tenant disagrees.
 */
export const COMBOBOX_THRESHOLD = 24;

interface Props {
  token: string;
  available: Label[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  /** Both label-mutating actions are the parent's, so it owns the one copy of the list. */
  onCreated: (label: Label) => void;
  onRemoved: (id: string) => void;
}

export function LabelPicker({
  token,
  available,
  selected,
  onToggle,
  onCreated,
  onRemoved,
}: Props) {
  const [newLabel, setNewLabel] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const created = await createLabel(token, name.trim());
        onCreated(created);
        setNewLabel("");
      } catch (failure) {
        // Most likely a duplicate name or a caller without `labels.manage` — both are
        // things the server already worded for a person.
        setError(message(failure, "That label couldn't be created."));
      }
    },
    [token, onCreated],
  );

  const remove = useCallback(
    async (id: string) => {
      setError(null);
      try {
        await deleteLabel(token, id);
        onRemoved(id);
      } catch (failure) {
        // The server refuses while documents still carry it — deleting the last label off
        // a document makes it visible tenant-wide, so that refusal is the guard, not a
        // glitch. Its wording is the only part the user can act on.
        setError(message(failure, "That label couldn't be removed."));
      }
    },
    [token, onRemoved],
  );

  return (
    <fieldset className="space-y-3 rounded-md border border-input bg-secondary p-5">
      <legend className="px-1 text-xs font-semibold tracking-wide text-foreground uppercase">
        Labels
      </legend>
      <p className="-mt-1 text-xs text-muted-foreground">
        File under {selected.size === 0 && "(no label — visible tenant-wide)"}
      </p>

      {available.length > COMBOBOX_THRESHOLD ? (
        <LabelCombobox
          token={token}
          available={available}
          selected={selected}
          onToggle={onToggle}
          onCreate={create}
        />
      ) : (
        <>
          <ChipGrid
            available={available}
            selected={selected}
            onToggle={onToggle}
            onRemove={(id) => void remove(id)}
          />
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
              placeholder="Label name"
              aria-label="New label name"
              maxLength={100}
              className="h-9 max-w-56 rounded-md border-input bg-background text-sm text-foreground placeholder:text-muted-foreground focus-visible:border-primary focus-visible:ring-primary/40"
            />
            <Button
              type="submit"
              variant="outline"
              disabled={!newLabel.trim()}
              className="h-9 gap-1.5 rounded-md border-dashed text-sm font-medium"
            >
              <Plus className="size-4" />
              New label
            </Button>
          </form>
        </>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}
    </fieldset>
  );
}

function ChipGrid({
  available,
  selected,
  onToggle,
  onRemove,
}: {
  available: Label[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="flex flex-wrap justify-center gap-2">
      {available.map((label) => (
        <span
          key={label.id}
          className={`group relative inline-flex items-center rounded-full border text-sm transition-colors ${
            selected.has(label.id)
              ? "border-primary bg-primary/15 font-medium text-primary"
              : "border-input bg-card text-muted-foreground hover:border-muted-foreground/60 hover:text-foreground"
          }`}
        >
          {/* Equal padding on both sides — the delete button is an absolute overlay rather
              than a flex sibling precisely so it doesn't eat into the right side and leave
              the label text looking shoved left. */}
          <button
            type="button"
            aria-pressed={selected.has(label.id)}
            onClick={() => onToggle(label.id)}
            className="px-3.5 py-1.5"
          >
            {label.name}
          </button>
          <button
            type="button"
            aria-label={`Delete label ${label.name}`}
            // The tenant's own labels, not this product's fixed set — every one of them,
            // default included, has to stay removable or "configurable" is a lie the
            // moment the first mistaken one gets created.
            onClick={() => onRemove(label.id)}
            className="absolute top-1/2 right-1 -translate-y-1/2 rounded-full bg-inherit p-1 text-muted-foreground/50 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-destructive/10 hover:text-destructive"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
    </div>
  );
}

function LabelCombobox({
  token,
  available,
  selected,
  onToggle,
  onCreate,
}: {
  token: string;
  available: Label[];
  selected: Set<string>;
  onToggle: (id: string) => void;
  onCreate: (name: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Label[] | null>(null);
  const byId = useMemo(() => new Map(available.map((label) => [label.id, label])), [available]);
  const chosen = [...selected].map((id) => byId.get(id)).filter((label): label is Label => !!label);

  // Server-side search rather than filtering `available` in the browser. Past the
  // threshold the point is that the list is large, and `GET /labels/search` is the
  // endpoint that paginates and matches it under the same RLS rules the full list obeys.
  // Debounced because every keystroke would otherwise be a request.
  const latest = useRef(0);
  useEffect(() => {
    if (!open) return;
    const ticket = ++latest.current;
    const timer = setTimeout(() => {
      void searchLabels(token, { q: query || undefined, limit: 50 })
        .then((page) => {
          // A slower earlier request must not overwrite a faster later one — otherwise
          // the list shows results for a query the box no longer contains.
          if (ticket === latest.current) setResults(page.items);
        })
        .catch(() => ticket === latest.current && setResults([]));
    }, 150);
    return () => clearTimeout(timer);
  }, [token, query, open]);

  const trimmed = query.trim();
  const shown = results ?? available.slice(0, 50);
  const exists = shown.some((label) => label.name.toLowerCase() === trimmed.toLowerCase());

  return (
    <div className="space-y-2">
      {chosen.length > 0 && (
        <div className="flex flex-wrap justify-center gap-2">
          {chosen.map((label) => (
            <button
              key={label.id}
              type="button"
              onClick={() => onToggle(label.id)}
              aria-label={`Remove ${label.name}`}
              className="inline-flex items-center gap-1.5 rounded-full border border-primary bg-primary/15 py-1.5 pr-2 pl-3.5 text-sm font-medium text-primary"
            >
              {label.name}
              <X className="size-3" />
            </button>
          ))}
        </div>
      )}

      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            className="h-9 w-full justify-between rounded-md border-input text-sm font-normal"
          >
            {selected.size > 0 ? `${selected.size} selected` : "Search labels…"}
            <ChevronsUpDown className="size-4 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-(--radix-popover-trigger-width) p-0" align="start">
          {/* `shouldFilter={false}`: the server already decided what matches, and letting
              cmdk filter again would hide results whose match is in a part of the name the
              client-side matcher scores differently. */}
          <Command shouldFilter={false}>
            <CommandInput
              value={query}
              onValueChange={setQuery}
              placeholder="Search labels…"
              aria-label="Search labels"
            />
            <CommandList>
              <CommandEmpty>No labels match.</CommandEmpty>
              <CommandGroup>
                {shown.map((label) => (
                  <CommandItem
                    key={label.id}
                    value={label.id}
                    onSelect={() => onToggle(label.id)}
                    data-checked={selected.has(label.id)}
                  >
                    <Check
                      className={`size-4 ${selected.has(label.id) ? "opacity-100" : "opacity-0"}`}
                    />
                    {label.name}
                  </CommandItem>
                ))}
                {trimmed && !exists && (
                  <CommandItem
                    value={`__create__${trimmed}`}
                    onSelect={() => {
                      void onCreate(trimmed);
                      setQuery("");
                    }}
                  >
                    <Plus className="size-4" />
                    Create label “{trimmed}”
                  </CommandItem>
                )}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}

function message(failure: unknown, fallback: string): string {
  // The server's own wording wherever there is one: a duplicate name, a missing
  // permission, "N documents still carry this label" — all of them say something the user
  // can act on, and replacing them with a generic failure throws that away.
  return failure instanceof ApiError ? failure.message : fallback;
}
