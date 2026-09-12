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
 *
 * **The fill is 18%, not 10%, and the change is a repair rather than a preference.** These
 * chips were drawn on a card that used to sit about 0.066 in lightness above the page, so
 * a 10% wash had the card's own lift underneath it. The "Professional" palette makes
 * `--card` equal to `--background` — surfaces are separated by their border there, not by
 * fill — and that lift went away, taking the chips' contrast with it. 18% puts the fill
 * back where the eye had it.
 *
 * Not higher: these sit directly beside the passage text they describe, and a chip that
 * out-shouts the sentence it labels has stopped being a label.
 *
 * **The ink is split by theme, and it was not.** Every chip wrote its text at the 300 step
 * with no `dark:` half, which is a shade chosen to glow on a dark panel. The light theme
 * then inverted — the main panel became white — and 300 on a 15% wash of its own hue is
 * about 1.6:1 against it: the chips were legible in dark and guessed at in light. Light now
 * takes the 800 step and dark keeps 300, so the pill means the same thing on either ground
 * and clears 4.5:1 on both. The hue is untouched; a label that changed colour between themes
 * would stop being the stable, name-derived thing this whole file exists to be.
 */

import { Sparkle } from "lucide-react";

import { useT } from "@/shared/i18n/useT";
import { leaf } from "../namespace";

/**
 * One entry per colour, three whole class strings each, because three different surfaces
 * need the same hue at three different strengths.
 *
 * `chip` is the pill, on a card, beside the sentence it labels. `icon` and `edge` are the
 * folder tile on the Folders grid: a folder *is* a label, so the tile that opens it and the
 * chip that names it inside are the same colour by construction — not by two lists agreeing.
 *
 * The strings are written out rather than built from a colour name, and that is a hard
 * requirement rather than a style: Tailwind scans this file as text, so `text-${colour}-500`
 * produces a class that exists in the markup and in no stylesheet.
 *
 * `icon` and `edge` carry a `dark:` half where `chip` does not. The chip sits on a tinted
 * fill of its own hue, which lifts it on either theme; the folder icon sits directly on the
 * panel, where a shade tuned for dark goes pale on white. 500 in light, 400 in dark.
 */
const PALETTE = [
  {
    chip: "border-sky-600/35 bg-sky-500/15 text-sky-800 dark:border-sky-400/40 dark:bg-sky-400/18 dark:text-sky-300",
    icon: "text-sky-500 dark:text-sky-400",
    edge: "border-sky-500/25 hover:border-sky-500/60 dark:border-sky-400/30 dark:hover:border-sky-400/60",
  },
  {
    chip: "border-violet-600/35 bg-violet-500/15 text-violet-800 dark:border-violet-400/40 dark:bg-violet-400/18 dark:text-violet-300",
    icon: "text-violet-500 dark:text-violet-400",
    edge: "border-violet-500/25 hover:border-violet-500/60 dark:border-violet-400/30 dark:hover:border-violet-400/60",
  },
  {
    chip: "border-emerald-600/35 bg-emerald-500/15 text-emerald-800 dark:border-emerald-400/40 dark:bg-emerald-400/18 dark:text-emerald-300",
    icon: "text-emerald-500 dark:text-emerald-400",
    edge: "border-emerald-500/25 hover:border-emerald-500/60 dark:border-emerald-400/30 dark:hover:border-emerald-400/60",
  },
  {
    chip: "border-amber-600/35 bg-amber-500/15 text-amber-800 dark:border-amber-400/40 dark:bg-amber-400/18 dark:text-amber-300",
    icon: "text-amber-500 dark:text-amber-400",
    edge: "border-amber-500/25 hover:border-amber-500/60 dark:border-amber-400/30 dark:hover:border-amber-400/60",
  },
  {
    chip: "border-rose-600/35 bg-rose-500/15 text-rose-800 dark:border-rose-400/40 dark:bg-rose-400/18 dark:text-rose-300",
    icon: "text-rose-500 dark:text-rose-400",
    edge: "border-rose-500/25 hover:border-rose-500/60 dark:border-rose-400/30 dark:hover:border-rose-400/60",
  },
  {
    chip: "border-cyan-600/35 bg-cyan-500/15 text-cyan-800 dark:border-cyan-400/40 dark:bg-cyan-400/18 dark:text-cyan-300",
    icon: "text-cyan-500 dark:text-cyan-400",
    edge: "border-cyan-500/25 hover:border-cyan-500/60 dark:border-cyan-400/30 dark:hover:border-cyan-400/60",
  },
  {
    chip: "border-fuchsia-600/35 bg-fuchsia-500/15 text-fuchsia-800 dark:border-fuchsia-400/40 dark:bg-fuchsia-400/18 dark:text-fuchsia-300",
    icon: "text-fuchsia-500 dark:text-fuchsia-400",
    edge: "border-fuchsia-500/25 hover:border-fuchsia-500/60 dark:border-fuchsia-400/30 dark:hover:border-fuchsia-400/60",
  },
  {
    chip: "border-lime-700/35 bg-lime-600/15 text-lime-800 dark:border-lime-400/40 dark:bg-lime-400/18 dark:text-lime-300",
    icon: "text-lime-500 dark:text-lime-400",
    edge: "border-lime-500/25 hover:border-lime-500/60 dark:border-lime-400/30 dark:hover:border-lime-400/60",
  },
  {
    chip: "border-orange-600/35 bg-orange-500/15 text-orange-800 dark:border-orange-400/40 dark:bg-orange-400/18 dark:text-orange-300",
    icon: "text-orange-500 dark:text-orange-400",
    edge: "border-orange-500/25 hover:border-orange-500/60 dark:border-orange-400/30 dark:hover:border-orange-400/60",
  },
  {
    chip: "border-teal-600/35 bg-teal-500/15 text-teal-800 dark:border-teal-400/40 dark:bg-teal-400/18 dark:text-teal-300",
    icon: "text-teal-500 dark:text-teal-400",
    edge: "border-teal-500/25 hover:border-teal-500/60 dark:border-teal-400/30 dark:hover:border-teal-400/60",
  },
] as const;

/** Rendered for a document carrying no labels at all. Frontend-only and deliberately so:
    the empty array already means "uncategorised, visible tenant-wide" everywhere in the
    schema, and creating a real label for it would change that document's visibility from
    unconditional to whoever-holds-the-label. */
const UNCATEGORISED = {
  chip: "border-border bg-secondary text-muted-foreground",
  icon: "text-muted-foreground",
  edge: "border-border hover:border-primary/40",
} as const;

/** The tone shared by every surface that stands for one label. */
export type LabelTone = (typeof PALETTE)[number] | typeof UNCATEGORISED;

/** Stable across sessions and users: the same name is always the same colour. djb2, which
    is small, has no dependency, and spreads short strings well enough for ten buckets. */
function hue(name: string): LabelTone {
  let hash = 5381;
  for (let index = 0; index < name.length; index += 1) {
    hash = ((hash << 5) + hash + name.charCodeAt(index)) | 0;
  }
  return PALETTE[Math.abs(hash) % PALETTE.length] ?? PALETTE[0]!;
}

/**
 * The colour for a label, for anything that is not the chip.
 *
 * Exported so the Folders grid can tint a tile with the same hue the chip inside it will
 * use. `null` is the unlabelled folder, which takes the neutral tone for the same reason
 * `TagChips` gives an unlabelled document the `Uncategorized` pill: an empty label set is
 * a real state, not a missing colour.
 */
export function labelTone(name: string | null): LabelTone {
  return name === null ? UNCATEGORISED : hue(name);
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
  /**
   * A label a model has put forward and nobody has accepted. Drawn dashed, unfilled, and
   * dimmed — the same hue as the filed chip, so it is recognisably the same label, and
   * unmistakably not the same state.
   *
   * The distinction is carried by shape rather than by colour or motion on purpose. An
   * animation is seen once; a screen of a hundred rows is read cold, hours later, possibly
   * by somebody who cannot separate the hues, possibly from a screenshot. A dashed outline
   * survives all four of those, and it means "provisional" in every interface anybody has
   * already used.
   */
  proposed?: boolean;
}

export function TagChip({
  name,
  onClick,
  short = false,
  uncategorised = false,
  proposed = false,
}: Props) {
  const t = useT();
  const label = short ? leaf(name) : name;
  const tone = uncategorised ? UNCATEGORISED : hue(name);
  // Bigger, heavier and better padded than the 12px pill these used to be. A label is read
  // at a glance from a metre away in a room, not studied; `text-xs` at regular weight lost
  // that fight on every screen it appeared on.
  const shared = proposed
    ? `inline-flex max-w-48 items-center gap-1 rounded-full border border-dashed bg-transparent px-2.5 py-1 text-[0.8125rem] leading-tight font-medium opacity-90 ${tone.edge} ${tone.icon}`
    : `inline-flex max-w-48 items-center rounded-full border px-2.5 py-1 text-[0.8125rem] leading-tight font-medium ${tone.chip}`;

  if (!onClick) {
    return (
      <span className={shared} title={proposed ? t("Suggested — not yet applied") : name}>
        {proposed && <Sparkle aria-hidden className="size-3 shrink-0" />}
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
      title={t("Filter by {name}", { name })}
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
  proposed,
}: {
  names: string[];
  onSelect?: (name: string) => void;
  short?: boolean;
  /** Renders every chip as a proposal. No uncategorised pill: an empty proposal is nothing
      to show, not a state to name. */
  proposed?: boolean;
}) {
  if (names.length === 0) {
    return proposed ? null : <TagChip name="Uncategorized" uncategorised />;
  }
  return (
    <>
      {names.map((name) => (
        <TagChip key={name} name={name} onClick={onSelect} short={short} proposed={proposed} />
      ))}
    </>
  );
}
