/**
 * Which theme is on, and how it is remembered.
 *
 * Three settings, not two. "System" is what every user has before they touch anything, and
 * a binary toggle destroys it on the first click with no way back — someone who works in a
 * light room by day and a dark one at night has to flip the app by hand forever after. It is
 * the default here and it stays reachable.
 *
 * The class goes on `<html>`, never on a wrapper inside the app. Radix portals Select,
 * Dialog, Sheet, Tooltip and DropdownMenu to `document.body`, outside the component tree, so
 * a `.dark` scoped to a div gives you a white dropdown over a dark application.
 */
export type Theme = "system" | "light" | "dark";

const KEY = "zenith.theme";

/**
 * Every access is guarded. `localStorage` is not merely empty in a private window or with
 * site data blocked — reading it *throws*, and an exception here would take the whole
 * application down before it painted, over a preference.
 */
export function stored(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // No preference is reachable, so there is no preference. `system` is the honest answer.
  }
  return "system";
}

export function remember(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // The theme still applies for this session; it just will not survive a reload.
  }
}

/** What `system` resolves to right now. */
export function preferred(): "light" | "dark" {
  return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function apply(theme: Theme): void {
  const resolved = theme === "system" ? preferred() : theme;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}
