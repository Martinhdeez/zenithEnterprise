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
 */

import { Search, X } from "lucide-react";
import type { FormEvent, ReactNode } from "react";

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
  return (
    <form
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onSubmit?.();
      }}
      className="flex min-w-0 flex-1 items-center gap-1 rounded-full border border-input bg-input/30 py-1 pr-1 pl-3.5 transition-colors focus-within:border-primary/40"
    >
      <Search className="size-4 shrink-0 text-muted-foreground" />
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className="min-w-0 flex-1 bg-transparent px-2 py-1 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none"
      />
      {/* Only once there is something to clear. A permanent × on an empty field is a
          control that does nothing, which teaches people that controls here might not. */}
      {value && (
        <button
          type="button"
          aria-label={`Clear ${label.toLowerCase()}`}
          onClick={() => onChange("")}
          className="shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="size-3.5" />
        </button>
      )}
      {action}
    </form>
  );
}
