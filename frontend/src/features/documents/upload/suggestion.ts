/**
 * Asking the model where a staged file belongs, and reporting honestly when it did not say.
 *
 * The server distinguishes four endings — it chose, it declined, it was never asked, it
 * broke — and `backend/app/features/ingestion/classification.py` sets out at length why
 * collapsing them is unsafe. Three of them come back with no ids, so `label_ids` alone
 * cannot tell them apart, and until now the staging area did not try: it read *any* empty
 * answer as the model having read the document and found no folder that fits, and said
 * `no match — server will file it`.
 *
 * That sentence is true for exactly one of them. Under `failed` nothing vouched for the
 * document, so ingestion leaves it in a quarantine label only `admin` reaches — the
 * interface was promising the opposite of what happens, in the ending where being wrong
 * costs the most.
 *
 * **And the caller added a fifth ending of its own, by hand.** The call site was
 * `suggestLabels(...).catch(() => [])`, so a 500, an expired token and an unplugged network
 * all became the same empty array as a working model with nothing to suggest. Four endings
 * collapsed to one on the server, then a fifth collapsed into the same one here.
 *
 * Its own module rather than a closure inside `Staging.tsx`, for the reason `uploadWatch.ts`
 * is one: five endings that no test can ask about separately are five endings that drift
 * back into one. The component renders them; nothing about *which* ending happened is
 * decided in a 300-line component any more.
 */

import { suggestLabels } from "../api";
import type { StagedFile, StagedOutcome } from "./stagingState";
import type { T } from "@/shared/i18n/useT";

export interface Suggested {
  outcome: StagedOutcome;
  /**
   * Under `failed`, what the provider said about why. Absent otherwise, and absent under
   * `unreachable` — that ending is this browser's request breaking, and there is no provider
   * on the other side of it to have said anything.
   */
  detail?: string;
  /** Non-empty only under `chose`. */
  labelIds: string[];
}

/**
 * One suggestion, for one staged file. Never rejects.
 *
 * A rejection becomes `unreachable` rather than nothing, which is the whole point: the
 * caller is a loop over a hundred files and it must keep going, but "keep going" and "say
 * the model read it and declined" are different things and only one of them is true.
 */
export async function suggestFor(token: string, excerpt: string): Promise<Suggested> {
  try {
    return await suggestLabels(token, excerpt);
  } catch {
    return { outcome: "unreachable", labelIds: [] };
  }
}

/**
 * What one suggestion does to its row.
 *
 * **Keyed on `chose`, not on "the list came back non-empty".** They are the same today,
 * because the server sends ids under no other ending. Naming the ending is what stops this
 * from quietly becoming wrong the day a fifth one arrives carrying ids — the lesson
 * `phaseFor` in `uploadWatch.ts` records after "anything that is not failed" turned an
 * unfinished document into a finished one.
 *
 * The outcome is recorded whatever it was, so the row can say what happened rather than
 * what usually happens.
 */
export function applySuggestion(
  rows: StagedFile[],
  rowId: string,
  suggested: Suggested,
): StagedFile[] {
  return rows.map((row) => {
    if (row.id !== rowId) return row;
    // Into `proposed`, never into `labelIds`. A suggestion that lands in the same array as
    // a person's own ticks is a machine's guess wearing a human decision's clothes, and on
    // this screen that decision is who may read the document.
    const proposed = suggested.outcome === "chose" ? [...suggested.labelIds] : [];
    return { ...row, proposed, suggestion: suggested.outcome, reason: suggested.detail };
  });
}

/**
 * What the row says when it is carrying no labels.
 *
 * Deliberately plain: five sentences, one treatment, no colour and no icon. The visual
 * language for these is the designer's, and this is the neutral text it replaces.
 *
 * Translated here, with the keys written out as literals, so the Spanish catalogue test can
 * see them: it scans the source for translation calls and fails the build on a key with no
 * sentence, and a key assembled from a variable is one it cannot find. That is also why the
 * mapping is a switch rather than a lookup table.
 */
export function noteFor(t: T, outcome: StagedOutcome | undefined, reason?: string): string {
  switch (outcome) {
    // The model read the document and no folder fitted. The document goes to the tenant
    // default, and the server really does file it — this is the one ending the old copy
    // was correct about.
    case "declined":
      return t("no match — server will file it");
    // Nobody was asked: no model configured, or nothing to offer it. Not a breakdown, and
    // an installation without generation is an ordinary one.
    case "unavailable":
      return t("not offered — no model configured");
    // Asked, and it broke. Untagged, this is the ending that leaves the document waiting
    // for an administrator, so the row must not promise it has been filed.
    case "failed":
      // **And why, when the provider said why.** The sentence before the parenthesis is
      // what happens to the document and it does not change; the parenthesis is what the
      // person can act on, and only the provider knows it. `the language model returned
      // 429` sent an operator to check an endpoint, a model name and a key that were all
      // correct; `your prepayment credits are depleted` sends them to a billing page.
      //
      // Appended rather than translated. It is the provider's own prose, in whatever
      // language the provider writes, and putting it through the catalogue would mean
      // inventing a Spanish sentence for a message nobody here composed.
      return reason
        ? `${t("suggestion failed — tag it, or it may be held for review")} (${reason})`
        : t("suggestion failed — tag it, or it may be held for review");
    case "unreachable":
      return t("suggestion failed — the server could not be reached");
    // `chose` with no labels left means somebody cleared them, which is untagged again.
    default:
      return t("untagged");
  }
}
