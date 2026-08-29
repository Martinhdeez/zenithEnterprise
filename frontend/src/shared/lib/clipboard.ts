/**
 * Copying text, on an installation that is often not on HTTPS.
 *
 * `navigator.clipboard` exists only in a secure context. An on-premise install reached over
 * plain HTTP on the customer's own network — which is the normal case for this product, not
 * an edge one — has no `navigator.clipboard` at all.
 *
 * The three call sites in this tree each guessed differently, and two of the guesses were
 * wrong in opposite directions:
 *
 * - `SingleUseLink` used `navigator.clipboard?.writeText(...)`. Optional chaining, so on
 *   HTTP the click does nothing at all: no copy, no error, no way for the administrator to
 *   know the single-use link they are about to close the panel on was never on their
 *   clipboard.
 * - `Answer` used `navigator.clipboard.writeText(...)` with no guard, which **throws** on
 *   the same installation.
 *
 * So: one function that knows the rule, and returns whether it worked so a caller can say
 * so. Silence is the one outcome that is never right here — the whole point of a copy
 * button is that the text is now somewhere else, and the user cannot see a clipboard.
 *
 * The fallback is `document.execCommand("copy")`. It is deprecated, it is also the only
 * thing that works without a secure context, and this product's deployment target is
 * exactly where that matters.
 */

export async function copy(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission refused, or a context that reports the API and then denies it. Fall
    // through rather than giving up: the older path frequently still works.
  }

  try {
    // Off-screen rather than hidden: a `display: none` element cannot be selected, which is
    // what this whole approach depends on.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-9999px";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
