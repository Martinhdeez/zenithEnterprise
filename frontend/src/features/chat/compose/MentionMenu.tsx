/**
 * The list that opens when you type `@` in the composer.
 *
 * Rendered above the input rather than below it: the composer is pinned to the bottom of
 * the panel, and a menu that opens downwards would be off-screen.
 *
 * Keyboard first. Someone typing `@han` is already on the keyboard and reaching for the
 * mouse to pick the file they were halfway through naming is the failure this replaces —
 * so Up/Down move, Enter and Tab accept, Escape closes, and the handler lives in the
 * composer where the keystrokes arrive.
 */

import { FileText } from "lucide-react";

import type { DocumentSummary } from "@/features/documents";

export function MentionMenu({
  documents,
  active,
  onPick,
  onHover,
}: {
  documents: DocumentSummary[];
  /** Index of the highlighted row — owned by the composer, which reads the arrow keys. */
  active: number;
  onPick: (document: DocumentSummary) => void;
  onHover: (index: number) => void;
}) {
  if (documents.length === 0) {
    return (
      <div className="mb-2 rounded-xl border border-input bg-input/60 p-3 text-sm text-muted-foreground shadow-lg">
        No documents match that.
      </div>
    );
  }

  return (
    <ul
      role="listbox"
      aria-label="Documents"
      className="mb-2 max-h-64 overflow-y-auto rounded-xl border border-input bg-input/60 py-1 shadow-lg"
    >
      {documents.map((document, index) => (
        <li key={document.id}>
          <button
            type="button"
            role="option"
            aria-selected={index === active}
            // `onMouseDown` with `preventDefault`, not `onClick`: a click would blur the
            // input first, and the blur handler closes this menu — so the click would land
            // on an element that is already gone about half the time.
            onMouseDown={(event) => {
              event.preventDefault();
              onPick(document);
            }}
            onMouseEnter={() => onHover(index)}
            className={`flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors ${
              index === active ? "bg-secondary text-foreground" : "text-muted-foreground"
            }`}
          >
            <FileText className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{document.filename}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
