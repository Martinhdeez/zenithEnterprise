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

import { suggestLabels, suggestionAvailability, type SuggestionRefusal } from "../api";
import { excerpt } from "./excerpt";
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
 * Whether automatic labelling can be offered to *this person at all*, before anything is
 * read off disk and before a model is spoken to.
 *
 * Three of the six endings — `unavailable`, `no_folders`, `too_many_folders` — are settled
 * by the installation's configuration and by the caller's own reach, so none of them depends
 * on the document. `GET /labels/suggest/availability` is the front half of the same server
 * function that answers `POST /labels/suggest`, which is why the rule is asked for rather
 * than re-derived here: a browser holding its own copy of
 * `NOT is_quarantine AND NOT is_default AND reach <= MAX_LABELS` is a second implementation
 * of a predicate no test on this side can reach.
 *
 * **A pre-flight that could not be computed answers `null`, not a refusal.** Hiding a working
 * feature because one request failed is the worse of the two mistakes, and it is
 * indistinguishable to the person from a real refusal. If the model is genuinely out of
 * reach, the call that follows says so as `unreachable`, which is a sentence of its own.
 */
export async function offerable(token: string): Promise<SuggestionRefusal | null> {
  try {
    return (await suggestionAvailability(token)).reason;
  } catch {
    return null;
  }
}

/**
 * One file, from bytes on disk to an ending: read the opening pages here, send only the text.
 *
 * `null` means **nothing was asked, so there is nothing to report** — `excerpt` returned
 * empty, which is a scan with no text layer, an encrypted PDF, or a file that is not really a
 * PDF at all. It is emphatically not `unavailable`: that ending means "no model configured",
 * which would be a false statement about the installation made on the evidence of one bad
 * file. Nothing here can say why that file was unreadable, and saying nothing is the honest
 * version of not knowing.
 *
 * Both callers need exactly this — the bulk pass over a selection and the single-file review
 * panel — and the part worth sharing is not the two lines of plumbing but the decision above
 * them. Written twice, one copy would eventually stamp an outcome on a file nobody asked
 * about.
 */
export async function suggestForFile(token: string, file: File): Promise<Suggested | null> {
  const text = await excerpt(file);
  if (!text) return null;
  return await suggestFor(token, text);
}

/**
 * What one suggestion becomes on whatever asked for it: a proposal, and never a decision.
 *
 * The rule is one line and it is the safety model, so it exists once. In this product a
 * label is a permission — the policy is `label_ids && zenith_current_labels()` — so an answer
 * that lands in the same array as a person's own ticks is a machine's guess wearing a human
 * decision's clothes, on the one screen where that difference decides who can read the
 * document.
 *
 * **Keyed on `chose`, not on "the list came back non-empty".** They are the same today,
 * because the server sends ids under no other ending. Naming the ending is what stops this
 * from quietly becoming wrong the day a fifth one arrives carrying ids — the lesson
 * `phaseFor` in `uploadWatch.ts` records after "anything that is not failed" turned an
 * unfinished document into a finished one.
 */
export function proposalFrom(suggested: Suggested): {
  proposed: string[];
  suggestion: StagedOutcome;
  reason?: string;
} {
  return {
    proposed: suggested.outcome === "chose" ? [...suggested.labelIds] : [],
    suggestion: suggested.outcome,
    reason: suggested.detail,
  };
}

/**
 * What one suggestion does to its row.
 *
 * The shape it takes is `proposalFrom`'s, shared with the single-file review panel. The
 * outcome is recorded whatever it was, so the row can say what happened rather than what
 * usually happens.
 */
export function applySuggestion(
  rows: StagedFile[],
  rowId: string,
  suggested: Suggested,
): StagedFile[] {
  return rows.map((row) => (row.id === rowId ? { ...row, ...proposalFrom(suggested) } : row));
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
