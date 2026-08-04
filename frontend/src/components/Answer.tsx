/**
 * Rendering an answer, and making its provisional-ness visible.
 *
 * Two things here are requirements rather than styling. The streamed text must read as
 * unfinished — F11 measured 1.2 s to first token and 4.5 s during an upload, and text that
 * looks settled while it is still arriving invites the user to act on half an answer. And
 * citation markers only become clickable once the result lands, because until then there is
 * no citation object behind them.
 */

import type { Citation, QueryResult } from "../api/stream";
import { displayed, isProvisional, type AnswerState } from "../api/answer";

const MARKER = /(\[\d+\])/g;

interface Props {
  state: AnswerState;
  onCitation: (citation: Citation) => void;
}

export function Answer({ state, onCitation }: Props) {
  if (state.phase === "idle") return null;

  if (state.phase === "error") {
    return (
      <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-4 text-red-900">
        {state.message}
      </div>
    );
  }

  if (state.phase === "retrieving") {
    // Named rather than a bare spinner. A second of silence is unavoidable — the retrieval
    // itself costs it — and saying what is happening is the difference between "working"
    // and "hung".
    return <p className="text-sm text-slate-500">Searching your documents…</p>;
  }

  const result = state.phase === "final" ? state.result : null;
  const provisional = isProvisional(state);

  return (
    <div>
      {result?.abstained && (
        <p className="mb-2 text-sm font-medium text-amber-800">
          No answer was found in your documents.
        </p>
      )}

      <p className="whitespace-pre-wrap leading-relaxed">
        {segments(displayed(state), result, provisional, onCitation)}
        {provisional && <span className="ml-0.5 animate-pulse text-slate-400">▍</span>}
      </p>

      {result && <Footer result={result} />}
    </div>
  );
}

function segments(
  text: string,
  result: QueryResult | null,
  provisional: boolean,
  onCitation: (citation: Citation) => void,
) {
  return text.split(MARKER).map((part, index) => {
    const match = /^\[(\d+)\]$/.exec(part);
    if (!match || !result || provisional) {
      // While streaming, a marker is plain text. It is not a broken link — the citations
      // simply have not arrived yet, and rendering it as a dead button would be a lie
      // about what is clickable.
      return <span key={index}>{part}</span>;
    }
    const citation = result.citations.find((item) => item.marker === Number(match[1]));
    if (!citation) return <span key={index}>{part}</span>;

    return (
      <button
        key={index}
        type="button"
        onClick={() => onCitation(citation)}
        title={`${citation.filename}, page ${citation.page_num}`}
        className="mx-0.5 rounded bg-sky-100 px-1 text-sm font-medium text-sky-800 hover:bg-sky-200"
      >
        {part}
      </button>
    );
  });
}

function Footer({ result }: { result: QueryResult }) {
  return (
    <div className="mt-3 space-y-1 text-xs text-slate-500">
      {result.degraded && (
        // Never swallowed. F11 found a configuration where the reranker was silently
        // failing on every request for as long as nobody looked; the UI is the last place
        // that can make that visible to whoever can fix it.
        <p className="text-amber-700">Answer quality reduced: {result.reason}</p>
      )}
      {result.abstained && result.consulted.length > 0 && (
        // mvp.md 2.10: an abstention says what it looked at. "I found nothing" and "I
        // looked at nothing" are different statements and only one is about the corpus.
        <p>
          Consulted {new Set(result.consulted.map((item) => item.filename)).size} document(s):{" "}
          {[...new Set(result.consulted.map((item) => item.filename))].join(", ")}
        </p>
      )}
      <p>
        {result.model && <>{result.model} · </>}
        retrieval {result.took_retrieval_ms} ms · generation {result.took_generation_ms} ms
      </p>
    </div>
  );
}
