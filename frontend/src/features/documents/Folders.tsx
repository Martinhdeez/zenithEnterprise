/**
 * Browsing the corpus: folders, and what's inside them.
 *
 * There used to be a separate "Documents" screen this one redirected to on every click,
 * which meant leaving Folders the moment you actually wanted to look at something in it —
 * two nav items and a jump between them to do one thing a file browser does in one place.
 * Folders now *is* the document browser: the grid is its home state, and clicking into a
 * folder drills down in place. `Documents` is still the thing that lists rows and handles
 * delete — reused here as a subcomponent, not duplicated — but nothing outside this file
 * navigates to it independently anymore.
 *
 * `selection` is controlled by the shell rather than local state, so the breadcrumb in the
 * main header can show and drive it too ("Folders / Tax", each segment clickable) — a
 * second "← back" control living only inside this panel would either duplicate that or
 * drift from it. There is exactly one level of nesting to have an opinion about: labels are
 * flat (mvp.md 2.14 lists folder hierarchies as an explicit non-goal), so root → one folder
 * is the whole tree, never root → folder → folder.
 *
 * A pure presentation layer for the grouping itself, and that is the design rather than
 * laziness: a browser rebuilding it from the flat listing would have to re-implement the
 * rule that an unlabelled document is visible tenant-wide while a labelled one is not. That
 * rule lives in an RLS policy, and getting it wrong shows somebody a folder they cannot open
 * anything inside.
 */

import { useEffect, useState } from "react";
import { FileStack, Folder as FolderIcon } from "lucide-react";

import { leaf, parentPath } from "@/features/labels";

import { folders, type FolderTree } from "./api";
import type { Citation } from "@/features/chat";
import { Documents } from "./Documents";

/** `null` at the grid. `filter: null` on a selection means "All documents" specifically —
    every reachable document, not "nothing selected". */
export type FolderSelection = { name: string; filter: { labelId: string | null } | null } | null;

interface Props {
  token: string;
  onCitation: (citation: Citation) => void;
  selection: FolderSelection;
  onSelect: (next: FolderSelection) => void;
  /** Passed through to the document list, whose rows carry tag chips. */
  onSelectTag?: (name: string) => void;
  /** Bumped by the shell after an upload, so the counts follow ingestion. */
  refreshKey?: number;
  /** Passed through so the document list can offer the access inspector to an administrator. */
  permissions?: string[];
}

export function Folders({
  token,
  onCitation,
  selection,
  onSelect,
  onSelectTag,
  refreshKey = 0,
  permissions,
}: Props) {
  const [tree, setTree] = useState<FolderTree | null>(null);

  useEffect(() => {
    let cancelled = false;
    void folders(token)
      .then((result) => !cancelled && setTree(result))
      .catch(() => !cancelled && setTree(null));
    return () => {
      cancelled = true;
    };
  }, [token, refreshKey]);

  if (selection) {
    return (
      <Documents
        token={token}
        onCitation={onCitation}
        onSelectTag={onSelectTag}
        filter={selection.filter}
        refreshKey={refreshKey}
        permissions={permissions}
      />
    );
  }

  if (!tree) return <p className="text-sm text-muted-foreground">Loading folders…</p>;

  // Sorted so a namespace's folders sit together and in order. Grouping is presentational:
  // every folder is still its own label and its own filter, and no parent selects its
  // children — `legal` grants nothing over `legal/contracts`.
  const grouped = [...tree.folders]
    .map((folder) => ({
      folder,
      node: { parent: parentPath(folder.name), leaf: leaf(folder.name) },
    }))
    .sort((a, b) => a.folder.name.localeCompare(b.folder.name));

  return (
    <section className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <button
          type="button"
          onClick={() => onSelect({ name: "All documents", filter: null })}
          className="flex flex-col items-start gap-3 rounded-xl border border-border bg-card p-4 text-left transition-colors hover:border-primary/40 hover:bg-secondary/30"
        >
          <FileStack className="size-5 text-muted-foreground" />
          <div>
            <p className="text-sm font-medium text-foreground">All documents</p>
            <p className="text-xs text-muted-foreground">{tree.total_documents} total</p>
          </div>
        </button>

        {grouped.map(({ node, folder }) => (
          <button
            key={folder.label_id ?? "unlabelled"}
            type="button"
            onClick={() => onSelect({ name: folder.name, filter: { labelId: folder.label_id } })}
            className="flex flex-col items-start gap-3 rounded-xl border border-border bg-card p-4 text-left transition-colors hover:border-primary/40 hover:bg-secondary/30"
          >
            <FolderIcon className="size-5 text-muted-foreground" />
            <div className="w-full">
              {/* The namespace above the leaf, so `legal/contracts` reads as "contracts,
                  under legal" rather than as one long name that truncates to nothing. The
                  hierarchy is a naming convention over a flat table — `columns` of a sort
                  — and it exists only here and in `namespace.ts`; nothing about access
                  follows from it. */}
              {node.parent && (
                <p className="truncate text-[11px] text-muted-foreground/60">{node.parent}</p>
              )}
              <p className="truncate text-sm font-medium text-foreground">{node.leaf}</p>
              <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                <span>{folder.documents} documents</span>
                {folder.processing > 0 && (
                  <span className="text-zenith-amber" title="still being processed">
                    {folder.processing}⋯
                  </span>
                )}
                {folder.failed > 0 && (
                  // Surfaced per folder, not only in aggregate. A failed document is
                  // invisible in search, so this is the only place its absence can be
                  // explained — and "which folder" is the first thing anybody asks next.
                  <span className="text-destructive" title="failed to process">
                    {folder.failed} failed
                  </span>
                )}
              </p>
            </div>
          </button>
        ))}
      </div>

      {tree.folders.length === 0 && (
        <p className="rounded-xl border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
          No folders yet — they appear as soon as an uploaded document carries a label.
        </p>
      )}
    </section>
  );
}
