/**
 * An honest "this is still working" bar.
 *
 * Indeterminate rather than a percentage, because there is nothing behind a percentage
 * here to make it true — the model doesn't report how much of the answer is left, and a
 * bar that fills to a number nobody computed would be a small lie dressed as information.
 * What *is* true and worth showing is that the request hasn't stalled, and how long it has
 * taken so far — F11 measured retrieval alone at 1.2–4.5 s and CPU generation adds several
 * more, so a silent ten seconds reads as "hung" without it.
 */

import { useEffect, useState } from "react";

export function ProgressBar({ label }: { label: string }) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    // Restarts at 0 for every new question — this component is only ever mounted while
    // one is in flight, so its own lifetime is the timer.
    const timer = setInterval(() => setSeconds((count) => count + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="space-y-2">
      <div className="relative h-0.5 w-full overflow-hidden rounded-full bg-secondary">
        <div className="progress-sweep absolute inset-y-0 w-1/3 rounded-full bg-primary" />
      </div>
      <p className="text-sm text-muted-foreground">
        {label} · {seconds}s
      </p>
    </div>
  );
}
