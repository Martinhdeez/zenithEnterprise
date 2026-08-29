/**
 * The one input style every administration form shares.
 *
 * Extracted when the panels moved into their own folders: three files needed it and copying a
 * class string is how two of them end up looking subtly different from the third after
 * somebody adjusts one.
 */
export const FIELD =
  "border-border bg-card text-foreground placeholder:text-muted-foreground " +
  "focus-visible:border-primary focus-visible:ring-primary/40";
