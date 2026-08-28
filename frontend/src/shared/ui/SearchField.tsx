/**
 * The search box, once.
 *
 * There were three of them — the access matrix, the history screen, the label manager — each
 * written where it was needed and each drifting from the others: two radii, two backgrounds,
 * one with the icon inside and one without. Aligning the class lists by hand would have
 * worked until the fourth one, so this is the shape and the three call it.
 *
 * **`rounded-full`, deliberately.** It is the shape the chat composer and the search screen
 * already use, and those are the two places a person types in this product most. A search
 * box that matches them everywhere else is what makes the product feel like one thing;
 * picking the more conservative radius would have meant changing the two screens that set
 * the tone to match the three that followed.
 *
 * **`bg-input/30`, not `bg-card`.** A field wants to read as a well — something you type
 * into — rather than as a card sitting on the panel. The base `Input` component is
 * deliberately not used here: it carries `dark:bg-input/30` of its own, which has outlived
 * an explicit background three times in this codebase and produced a translucent wash
 * somebody then had to debug.
 *
 * **A textarea that grows, not an input that scrolls.** A long question used to disappear
 * off the left edge of a single line, so reading back what you had typed meant scrolling
 * horizontally through it — and this product's whole premise is asking questions in
 * sentences, not keywords. The field now takes the height its content needs and the
 * question stays legible while it is being written.
 *
 * It stops at six lines and scrolls after that, because an unbounded field walks the submit
 * button off the bottom of the panel on a pasted page of text.
 */

import { Search, X } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";

import { useT } from "@/shared/i18n/useT";

/** One line of `text-sm`/`leading-6` plus the field's own `py-1`. */
const LINE = 24;
const SINGLE = LINE + 8;
const MAX = SINGLE + LINE * 5;

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** The accessible name. Two fields on one screen must not share it. */
  label: string;
  placeholder?: string;
  /** Runs on submit — Enter, or the button if one is given. */
  onSubmit?: () => void;
  /** A submit button, for the screens where searching is an act rather than a filter. */
  action?: ReactNode;
}

export function SearchField({ value, onChange, label, placeholder, onSubmit, action }: Props) {
  const t = useT();
  const field = useRef<HTMLTextAreaElement>(null);
  const [grown, setGrown] = useState(false);

  // Measured rather than counted: a line break can come from a newline the user typed or
  // from the browser wrapping a long word, and only the layout knows which happened.
  // `useLayoutEffect` because this runs before paint — in an effect the field visibly
  // snaps to its new height one frame after the character appears.
  useLayoutEffect(() => {
    const node = field.current;
    if (!node) return;
    node.style.height = "auto";
    const needed = node.scrollHeight;
    node.style.height = `${Math.min(needed, MAX)}px`;
    setGrown(needed > SINGLE + 1);
  }, [value]);

  return (
    <form
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onSubmit?.();
      }}
      // The pill is the shape of one line. Once the field has become a block of text a
      // fully-rounded edge reads as a lozenge rather than as a field, so the radius eases
      // down as it grows — and everything inside aligns to the first line instead of
      // floating in the middle of a paragraph.
      className={`flex min-w-0 flex-1 gap-1 border border-input bg-input/30 py-1 pr-1 pl-3.5 transition-[border-radius,border-color] focus-within:border-primary/40 ${
        grown ? "items-start rounded-2xl" : "items-center rounded-full"
      }`}
    >
      <Search className={`size-4 shrink-0 text-muted-foreground ${grown ? "mt-2" : ""}`} />
      <textarea
        ref={field}
        rows={1}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        // Enter still searches, because this is a search field and that is what Enter does
        // here. Shift+Enter is the way to add a line deliberately — the same bargain every
        // message composer makes, including this product's own.
        onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
          if (event.key !== "Enter" || event.shiftKey) return;
          event.preventDefault();
          onSubmit?.();
        }}
        placeholder={placeholder}
        aria-label={label}
        className="min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-2 py-1 text-sm leading-6 text-foreground placeholder:text-muted-foreground focus-visible:outline-none"
      />
      {/* Only once there is something to clear. A permanent × on an empty field is a
          control that does nothing, which teaches people that controls here might not. */}
      {value && (
        <button
          type="button"
          // Was an untranslated template literal, which the i18n guard cannot see: it
          // matches `prop="literal"`, and a backticked expression is neither. It was read
          // aloud in English on an otherwise Spanish screen.
          aria-label={t("Clear {name}", { name: label.toLowerCase() })}
          onClick={() => onChange("")}
          className={`shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:text-foreground ${grown ? "mt-1" : ""}`}
        >
          <X className="size-3.5" />
        </button>
      )}
      {action}
    </form>
  );
}
