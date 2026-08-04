/**
 * The folder tree, rendered from what the server grouped.
 *
 * A pure presentation layer, and that is the design rather than laziness: a browser
 * rebuilding this from the flat listing would have to re-implement the rule that an
 * unlabelled document is visible tenant-wide while a labelled one is not. That rule lives
 * in an RLS policy, and getting it wrong shows somebody a folder they cannot open anything
 * inside.
 *
 * A folder that is absent is absent because the caller cannot reach it. There is nothing to
 * render for it and nothing to hint at.
 */

import { useEffect, useState } from "react";

import { folders, type FolderTree } from "../api/client";

interface Props {
  token: string;
  selected: string | null;
  onSelect: (labelId: string | null) => void;
  /** Bumped by the shell after an upload, so the counts follow ingestion. */
  refreshKey?: number;
}

export function Folders({ token, selected, onSelect, refreshKey = 0 }: Props) {
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

  if (!tree || tree.folders.length === 0) return null;

  return (
    <nav className="space-y-1 text-sm">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={`flex w-full justify-between rounded px-2 py-1 ${
          selected === null ? "bg-slate-100 font-medium" : "hover:bg-slate-50"
        }`}
      >
        <span>All documents</span>
        <span className="text-slate-500">{tree.total_documents}</span>
      </button>

      {tree.folders.map((folder) => (
        <button
          key={folder.label_id ?? "unlabelled"}
          type="button"
          onClick={() => onSelect(folder.label_id)}
          className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1 ${
            selected === folder.label_id ? "bg-slate-100 font-medium" : "hover:bg-slate-50"
          }`}
        >
          <span className="truncate">{folder.name}</span>
          <span className="flex shrink-0 items-center gap-1 text-xs text-slate-500">
            {folder.processing > 0 && (
              <span className="text-sky-700" title="still being processed">
                {folder.processing}⋯
              </span>
            )}
            {folder.failed > 0 && (
              // Surfaced per folder, not only in aggregate. A failed document is invisible
              // in search, so this is the only place its absence can be explained — and
              // "which folder" is the first thing anybody asks next.
              <span className="text-red-700" title="failed to process">
                {folder.failed}!
              </span>
            )}
            <span>{folder.documents}</span>
          </span>
        </button>
      ))}
    </nav>
  );
}
