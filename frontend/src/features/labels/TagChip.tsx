/**
 * One label, wherever a label is shown.
 *
 * Documents, search results and citations all needed the same thing, and three
 * near-identical spans would have drifted the moment one of them grew a hover state.
 *
 * **Colour is derived from the name, not stored.** `access_labels` has no colour column and
 * this does not add one: a colour picker is a settings screen, a migration and a default
 * for every existing label, to solve a problem — telling two chips apart at a glance — that
 * a hash solves without any of it. The same name always produces the same colour, in every
 * session and for every user, which is the property that makes it useful; it is decoration
 * that happens to be stable, not data.
 *
 * The palette is the theme's own accents at low opacity. Ten of them: enough that adjacent
 * chips rarely collide, few enough that every one stays legible on the dark surface these
 * sit on.
 */

import { leaf } from "./namespace";

const PALETTE = [
  "border-sky-400/30 bg-sky-400/10 text-sky-300",
  "border-violet-400/30 bg-violet-400/10 text-violet-300",
  "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  "border-amber-400/30 bg-amber-400/10 text-amber-300",
  "border-rose-400/30 bg-rose-400/10 text-rose-300",
  "border-cyan-400/30 bg-cyan-400/10 text-cyan-300",
  "border-fuchsia-400/30 bg-fuchsia-400/10 text-fuchsia-300",
  "border-lime-400/30 bg-lime-400/10 text-lime-300",
  "border-orange-400/30 bg-orange-400/10 text-orange-300",
  "border-teal-400/30 bg-teal-400/10 text-teal-300",
];

/** Rendered for a document carrying no labels at all. Frontend-only and deliberately so:
    the empty array already means "uncategorised, visible tenant-wide" everywhere in the
    schema, and creating a real label for it would change that document's visibility from
    unconditional to whoever-holds-the-label. */
const UNCATEGORISED = "border-border bg-secondary text-muted-foreground/70";

/** Stable across sessions and users: the same name is always the same colour. djb2, which
    is small, has no dependency, and spreads short strings well enough for ten buckets. */
function hue(name: string): string {
  let hash = 5381;
  for (let index = 0; index < name.length; index += 1) {
    hash = ((hash << 5) + hash + name.charCodeAt(index)) | 0;
  }
  return PALETTE[Math.abs(hash) % PALETTE.length] ?? PALETTE[0]!;
}

interface Props {
  name: string;
  /** Clicking filters the view to this label. Omitted where there is nothing to filter. */
  onClick?: (name: string) => void;
  /**
   * Show only the last path segment — `contracts` rather than `legal/contracts`. For lists
   * already grouped under the parent, where repeating it is noise.
   */
  short?: boolean;
  uncategorised?: boolean;
}

export function TagChip({ name, onClick, short = false, uncategorised = false }: Props) {
  const label = short ? leaf(name) : name;
  const tone = uncategorised ? UNCATEGORISED : hue(name);
  const shared = `inline-flex max-w-48 items-center rounded-full border px-2 py-0.5 text-xs ${tone}`;

  if (!onClick) {
    return (
      <span className={shared} title={name}>
        <span className="truncate">{label}</span>
      </span>
    );
  }

  return (
    <button
      type="button"
      // The row underneath is usually clickable too — opening the document — so a chip has
      // to keep its own click to itself.
      onClick={(event) => {
        event.stopPropagation();
        onClick(name);
      }}
      title={`Filter by ${name}`}
      className={`${shared} transition-opacity hover:opacity-80`}
    >
      <span className="truncate">{label}</span>
    </button>
  );
}

/** A document's labels, or the uncategorised pill when it has none. */
export function TagChips({
  names,
  onSelect,
  short,
}: {
  names: string[];
  onSelect?: (name: string) => void;
  short?: boolean;
}) {
  if (names.length === 0) {
    return <TagChip name="Uncategorized" uncategorised />;
  }
  return (
    <>
      {names.map((name) => (
        <TagChip key={name} name={name} onClick={onSelect} short={short} />
      ))}
    </>
  );
}
