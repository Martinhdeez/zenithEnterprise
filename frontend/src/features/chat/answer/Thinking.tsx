/**
 * The chat's wait, which is long enough to need explaining: F11 measured retrieval alone at
 * 1.2 s, and 4.5 s while somebody is uploading, before CPU generation adds several more.
 *
 * Deliberately **not** `ProgressBar`. That component is shared with Search, and Search must
 * not look like it is thinking — its own landing copy says "nothing is generated here", and
 * hybrid retrieval is deterministic ranking, not cogitation. Dressing it in a mark that
 * traces itself would contradict the one sentence that distinguishes the two screens. So the
 * chat gets its own, and `ProgressBar` stays exactly as it is.
 *
 * What it keeps from the bar it replaces, because all three were right:
 *   - indeterminate, with no percentage invented for it;
 *   - the stage named, since retrieval and generation are different things to be slow at;
 *   - the elapsed count, because a silent ten seconds reads as "hung".
 *
 * One instance spans both phases so its timer runs continuously — remounting at the first
 * token would restart the count and understate how long the question has actually taken.
 */

import { useEffect, useState } from "react";

/** Retrieval draws the mark; generation falls back to the sweep, because by then there is
 *  prose arriving and a second moving thing beside it competes with the words. */
export function Thinking({ retrieving, label }: { retrieving: boolean; label: string }) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setSeconds((count) => count + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  const elapsed = (
    <p className="text-sm text-muted-foreground">
      <span role="status">{label}</span>
      {/* Outside the live region: announcing a number every second is noise, not status. */}
      <span aria-hidden> · {seconds}s</span>
    </p>
  );

  if (!retrieving) {
    return (
      <div className="space-y-2">
        <div className="relative h-0.5 w-full overflow-hidden rounded-full bg-secondary">
          <div className="progress-sweep absolute inset-y-0 w-1/3 rounded-full bg-primary" />
        </div>
        {elapsed}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <svg
          aria-hidden="true"
          className="zenith-trace size-9 shrink-0"
          viewBox="0 0 40 40"
          fill="none"
        >
          <defs>
            {/* Mixed from the tokens rather than given its own values, so the stroke follows
                the theme instead of becoming a colour the product has to explain. */}
            <linearGradient id="zenith-trace-ink" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--primary)" />
              <stop offset="100%" stopColor="var(--zenith-cyan)" />
            </linearGradient>
          </defs>
          {/* `pathLength="100"` normalises the dash maths, so the timing does not have to be
              re-derived if the letter is ever redrawn. */}
          <path
            d="M11 12 H29 L11 28 H29"
            pathLength={100}
            stroke="url(#zenith-trace-ink)"
            strokeWidth={3.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        {elapsed}
      </div>
      {/* Passage-shaped, and in the place the answer will occupy: the wait becomes a preview
          of the shape of what is coming, and the layout does not jump when the first token
          replaces it. Decorative — the sentence above already says what is happening. */}
      <div aria-hidden="true" className="space-y-2">
        <div className="ghost-line h-3 w-full" />
        <div className="ghost-line h-3 w-[88%]" />
        <div className="ghost-line h-3 w-[64%]" />
      </div>
    </div>
  );
}
