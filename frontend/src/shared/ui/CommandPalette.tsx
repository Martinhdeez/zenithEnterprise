/**
 * Cmd+K: go anywhere, find anything.
 *
 * The shell has seven screens and a document list that pages. Reaching a document meant
 * Folders, then the right folder, then scrolling; reaching a question you asked yesterday
 * meant History and its search box. Both are two or three deliberate steps for something
 * you already know the name of.
 *
 * **Documents are searched on the server.** Filtering the first page in the browser would
 * work in a demo and quietly stop working at five hundred documents — the box would find
 * things near the top of the list and miss everything else, which is worse than not having
 * it, because it looks like the document is not there.
 *
 * Questions are filtered locally, and that asymmetry is deliberate: the recent ones are
 * already loaded, they are few, and a keystroke-per-request round trip for a list of twenty
 * strings is latency bought for nothing.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { FileText, MessageSquare, Search as SearchIcon } from "lucide-react";

import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { listDocuments, type DocumentSummary } from "@/features/documents";
import { history, type HistoryEntry } from "@/features/history";
import { useT } from "@/shared/i18n/useT";

/** Long enough that typing a word is one request, short enough to feel immediate. */
const DEBOUNCE_MS = 200;

export interface PaletteAction {
  /** Where the nav can go. Matches the shell's own `view` union. */
  go: (view: string) => void;
  /** Open a document in the viewer. */
  openDocument: (document: DocumentSummary) => void;
  /** Put a past question back in the ask box. */
  ask: (question: string) => void;
}

const SCREENS: Array<{ id: string; label: string }> = [
  { id: "search", label: "Search" },
  { id: "chat", label: "Chat" },
  { id: "folders", label: "Folders" },
  { id: "upload", label: "Upload" },
  { id: "history", label: "History" },
  { id: "admin", label: "Admin" },
  { id: "profile", label: "Profile" },
];

export function CommandPalette({ token, actions }: { token: string; actions: PaletteAction }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [questions, setQuestions] = useState<HistoryEntry[]>([]);

  // Cmd+K on a Mac, Ctrl+K elsewhere. Captured on the window rather than on a field,
  // because the whole point is that it works without first clicking anything.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Loaded when the palette opens, not on every keystroke: twenty strings already in the
  // database are not worth a request per character.
  useEffect(() => {
    if (!open) return;
    void history(token)
      .then((page) => setQuestions(page.entries))
      .catch(() => setQuestions([]));
  }, [open, token]);

  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => {
      void listDocuments(token, null, null, query.trim() || undefined)
        .then((page) => setDocuments(page.items))
        .catch(() => setDocuments([]));
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [open, token, query]);

  const matching = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return questions.slice(0, 5);
    return questions.filter((entry) => entry.question.toLowerCase().includes(needle)).slice(0, 5);
  }, [questions, query]);

  const run = useCallback((action: () => void) => {
    action();
    setOpen(false);
    setQuery("");
  }, []);

  const screens = SCREENS.filter((screen) =>
    screen.label.toLowerCase().includes(query.trim().toLowerCase()),
  );

  return (
    // `title` and `description` are the dialog's accessible name and hint — read aloud, never
    // drawn. They default to English inside the shadcn primitive, which is fine until the
    // interface is not, and a Spanish screen announced as "Command Palette" is exactly the
    // seam a screen-reader user hears first.
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      title={t("Command palette")}
      description={t("Go to a screen, find a document, or repeat a question…")}
    >
      {/* `CommandDialog` renders a Dialog and drops its children straight in — it does not
          wrap them in the cmdk root, which every Input and List needs to exist inside.
          Without this the palette throws "reading 'subscribe'" and takes the whole app
          down with it.

          `shouldFilter={false}` because the filtering already happened: documents are
          matched on the server and questions and screens in the callbacks above. Leaving
          cmdk's own matcher on would filter those results a second time against the same
          string and quietly drop the ones its fuzzy rules disagreed with. */}
      <Command shouldFilter={false}>
      <CommandInput
        placeholder={t("Go to a screen, find a document, or repeat a question…")}
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {/* One empty state for the whole palette, not one per group: three "no results"
            messages stacked on top of each other reads as three things being broken. */}
        <CommandEmpty>Nothing matches that.</CommandEmpty>

        {screens.length > 0 && (
          <CommandGroup heading="Go to">
            {screens.map((screen) => (
              <CommandItem
                key={screen.id}
                value={`screen ${screen.label}`}
                onSelect={() => run(() => actions.go(screen.id))}
              >
                <SearchIcon className="size-4 text-muted-foreground" />
                {screen.label}
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {documents.length > 0 && (
          <CommandGroup heading="Documents">
            {documents.slice(0, 6).map((document) => (
              <CommandItem
                key={document.id}
                value={`document ${document.filename}`}
                onSelect={() => run(() => actions.openDocument(document))}
              >
                <FileText className="size-4 text-muted-foreground" />
                <span className="truncate">{document.filename}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {matching.length > 0 && (
          <CommandGroup heading="Ask again">
            {matching.map((entry) => (
              <CommandItem
                key={entry.query_id}
                value={`question ${entry.question}`}
                onSelect={() => run(() => actions.ask(entry.question))}
              >
                <MessageSquare className="size-4 text-muted-foreground" />
                <span className="truncate">{entry.question}</span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
      </CommandList>
      </Command>
    </CommandDialog>
  );
}
