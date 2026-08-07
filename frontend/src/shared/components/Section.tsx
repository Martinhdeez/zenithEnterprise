import type { ReactNode } from "react";

/**
 * A GitHub-sidebar-style section: a small uppercase label, a divider below, never a border
 * around the section itself — the rule that keeps a sidebar of several independent
 * components from turning into a stack of separately boxed widgets.
 *
 * Shared rather than private to `App`, because `Folders` needs the identical chrome and
 * needs to be able to withhold it entirely — see the comment there on why a section that
 * cannot partition anything renders nothing, not an empty label.
 */
export function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="border-b border-border px-4 py-3">
      <p className="mb-2 text-[11px] font-semibold tracking-wider text-muted-foreground/70 uppercase">
        {label}
      </p>
      {children}
    </div>
  );
}
