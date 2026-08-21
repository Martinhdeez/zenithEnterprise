/**
 * Rendering an answer, and making its provisional-ness visible.
 *
 * Two things here are requirements rather than styling. The streamed text must read as
 * unfinished — F11 measured 1.2 s to first token and 4.5 s during an upload, and text that
 * looks settled while it is still arriving invites the user to act on half an answer. And
 * citation markers only become clickable once the result lands, because until then there is
 * no citation object behind them.
 *
 * Markdown, not plain text: the model is free-writing prose with lists and emphasis (the
 * system prompt never asked for it, but nothing stops it either, and an 8B model reaches for
 * structure when a question has parts), and `whitespace-pre-wrap` on raw text used to leave
 * every `*` and `#` sitting there literally. Citation markers are converted to real markdown
 * links (`[1]` → `[[1]](#citation-1)`) before rendering rather than handled with a second
 * pass over the tree, so ordinary markdown syntax right next to a marker — `**[1]**`,
 * `- fact [2]` — composes correctly instead of the two systems fighting over the same text.
 */

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Citation, QueryResult } from "../stream/stream";
import { displayed, isProvisional, type AnswerState } from "./answerState";
import { transcript } from "./transcript";
import { ProgressBar } from "@/shared/components/ProgressBar";

interface Props {
  state: AnswerState;
  onCitation: (citation: Citation) => void;
}

export function Answer({ state, onCitation }: Props) {
  if (state.phase === "idle") return null;

  if (state.phase === "error") {
    return (
      <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-destructive">
        {state.message}
      </div>
    );
  }

  const result = state.phase === "final" ? state.result : null;
  const provisional = isProvisional(state);

  // Only markers the result actually vouches for become links — a number the model wrote
  // that doesn't match any citation (which `citations.py` would already have caught server
  // side, but this is the last line of defence) stays inert text rather than a link to
  // nothing.
  const source = result ? linkCitations(displayed(state), result.citations) : displayed(state);

  return (
    <div>
      {result?.abstained && (
        <p className="mb-2 text-sm font-medium text-zenith-amber">
          No answer was found in your documents.
        </p>
      )}

      {provisional && (
        // One `ProgressBar` instance across both "retrieving" and "streaming" — same tree
        // position, same `key` — so its internal timer keeps counting through the
        // transition instead of restarting the moment the first token arrives, which would
        // understate how long the question has actually been running. Only the label
        // changes, because retrieval and generation are different things to be slow at and
        // F9's prompt work fixed different failures in each.
        <ProgressBar
          key={state.question}
          label={state.phase === "retrieving" ? "Searching your documents" : "Writing the answer"}
        />
      )}

      {state.phase !== "retrieving" && (
        <div className="leading-relaxed text-foreground">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components(result, onCitation)}>
            {source}
          </ReactMarkdown>
          {provisional && <span className="ml-0.5 animate-pulse text-muted-foreground">▍</span>}
        </div>
      )}

      {result && !provisional && <Actions answer={displayed(state)} result={result} />}
      {result && <Footer result={result} />}
    </div>
  );
}

function linkCitations(text: string, citations: QueryResult["citations"]): string {
  return text.replace(/\[(\d+)\]/g, (whole, digits: string) => {
    const marker = Number(digits);
    return citations.some((citation) => citation.marker === marker)
      ? `[[${digits}]](#citation-${digits})`
      : whole;
  });
}

/** Markdown element overrides — styled for contrast against the dark theme rather than a
    generic `prose` reset: headings that read as headings, list markers and inline code with
    real color instead of the same grey as the surrounding prose, the way Gemini's own web
    client renders a streamed answer. The one element with actual logic is `a`, which has to
    check whether it's really a citation link before deciding what to render. */
function components(result: QueryResult | null, onCitation: (citation: Citation) => void): Components {
  return {
    p: ({ children }) => <p className="mb-3 text-foreground last:mb-0">{children}</p>,
    // Coloured markers via Tailwind's `marker:` variant rather than a custom bullet
    // element: `<li>` stays a plain `list-item` box, which is what makes `list-decimal`'s
    // counters render at all — a `flex` list item, the more obvious way to build a custom
    // bullet, silently drops the browser's own numbering the moment it's applied.
    ul: ({ children }) => (
      <ul className="mb-3 ml-5 list-disc space-y-1.5 pl-1 marker:text-zenith-cyan last:mb-0">{children}</ul>
    ),
    ol: ({ children }) => (
      <ol className="mb-3 ml-5 list-decimal space-y-1.5 pl-1 marker:font-semibold marker:text-zenith-cyan last:mb-0">
        {children}
      </ol>
    ),
    li: ({ children }) => <li className="pl-1 text-foreground">{children}</li>,
    // A visible rule under each heading, not just bigger text — the difference between
    // "this word is bold" and "this is a new section", which matters once an answer has
    // more than one of them.
    h1: ({ children }) => (
      <h3 className="mb-2.5 border-b border-border pb-1.5 text-base font-bold text-foreground">
        {children}
      </h3>
    ),
    h2: ({ children }) => (
      <h3 className="mb-2.5 border-b border-border pb-1.5 text-base font-bold text-foreground">
        {children}
      </h3>
    ),
    h3: ({ children }) => <h4 className="mb-2 text-sm font-bold text-foreground">{children}</h4>,
    strong: ({ children }) => <strong className="font-bold text-foreground">{children}</strong>,
    em: ({ children }) => <em className="text-foreground/90 italic">{children}</em>,
    blockquote: ({ children }) => (
      <blockquote className="mb-3 rounded-r-md border-l-2 border-zenith-cyan/50 bg-zenith-cyan/5 py-1 pl-3 text-foreground/80 last:mb-0">
        {children}
      </blockquote>
    ),
    code: ({ children }) => (
      <code className="rounded-md border border-zenith-cyan/20 bg-zenith-cyan/10 px-1.5 py-0.5 font-mono text-[0.85em] text-zenith-cyan">
        {children}
      </code>
    ),
    pre: ({ children }) => (
      <pre className="mb-3 overflow-x-auto rounded-lg border border-border bg-background p-3 font-mono text-xs text-foreground last:mb-0 [&_code]:border-0 [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-foreground">
        {children}
      </pre>
    ),
    table: ({ children }) => (
      <div className="mb-3 overflow-x-auto rounded-lg border border-border last:mb-0">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th className="border-b border-border bg-secondary px-3 py-1.5 text-left font-semibold text-foreground">
        {children}
      </th>
    ),
    td: ({ children }) => <td className="border-b border-border/60 px-3 py-1.5 text-foreground">{children}</td>,
    a: ({ href, children }) => {
      const marker = /^#citation-(\d+)$/.exec(href ?? "")?.[1];
      const citation = marker && result?.citations.find((item) => item.marker === Number(marker));
      if (citation) {
        return (
          <button
            type="button"
            onClick={() => onCitation(citation)}
            title={`${citation.filename}, page ${citation.page_num}`}
            className="mx-0.5 rounded-sm bg-primary/20 px-1 text-sm font-semibold text-primary hover:bg-primary/30"
          >
            {children}
          </button>
        );
      }
      // Not a citation — the model wrote an ordinary link. Rare given the system prompt
      // (rule 1: passages only), but a dead click is worse than an opened tab.
      return (
        <a href={href} target="_blank" rel="noreferrer" className="font-medium text-primary underline">
          {children}
        </a>
      );
    },
  };
}

/**
 * What you can do with a finished answer.
 *
 * Only once it is finished — `provisional` gates this above. A copy control beside a
 * half-written answer invites copying half an answer, and the person pasting it has no way
 * to tell that is what they got.
 */
function Actions({ answer, result }: { answer: string; result: QueryResult }) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard.writeText(transcript(answer, result));
        setCopied(true);
        // Long enough to be read, short enough that the button is ready again before
        // somebody wants it twice.
        setTimeout(() => setCopied(false), 2000);
      }}
      className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-input px-3 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
    >
      {copied ? <Check className="size-3.5 text-zenith-cyan" /> : <Copy className="size-3.5" />}
      {copied ? "Copied with sources" : "Copy answer"}
    </button>
  );
}

function Footer({ result }: { result: QueryResult }) {
  return (
    <div className="mt-3 space-y-1 text-xs text-muted-foreground">
      {result.degraded && (
        // Never swallowed. F11 found a configuration where the reranker was silently
        // failing on every request for as long as nobody looked; the UI is the last place
        // that can make that visible to whoever can fix it.
        <p className="text-zenith-amber">Answer quality reduced: {result.reason}</p>
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
        {/* A turn answered from the conversation searched nothing, and "retrieval 0 ms"
            reads as a retrieval that was instant rather than one that never ran. The
            absence of the number is the more accurate statement. */}
        {result.consulted.length > 0 && <>retrieval {result.took_retrieval_ms} ms · </>}
        generation {result.took_generation_ms} ms
      </p>
    </div>
  );
}
