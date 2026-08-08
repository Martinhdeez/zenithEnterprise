/**
 * What the installation has been asked, and what it read to answer.
 *
 * Almost none of this is new data. `queries` and `query_citations` have logged every
 * question, every latency and every passage an answer leaned on since the first migration,
 * and nothing had ever read them. This is the screen those tables were built for.
 *
 * Two numbers are deliberately not what a dashboard usually shows.
 *
 * **Cost is reported with its gaps.** A local model reports no token usage and a gateway may
 * strip it, so some queries have no cost attached. Summing those as zero would put a
 * confident, wrong figure in front of somebody who budgets against it — so the count of
 * queries with no usage sits beside the total rather than being folded into it.
 *
 * **Abstentions are shown as a headline, not hidden in a footnote.** An installation that
 * abstains often is one whose corpus is missing something people keep asking for, and that
 * is the most actionable thing on this page.
 */

import { useEffect, useState } from "react";
import { FileText, Loader2, MessageSquare, ShieldAlert, Timer, Users } from "lucide-react";

import { type Analytics as Data, analytics as fetchAnalytics } from "./api";

export function Analytics({ token }: { token: string }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchAnalytics(token)
      .then((result) => !cancelled && setData(result))
      .catch((problem: Error) => !cancelled && setError(problem.message));
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (error) {
    return (
      <p role="alert" className="flex items-start gap-2 text-sm text-destructive">
        <ShieldAlert className="mt-0.5 size-4 shrink-0" />
        {error}
      </p>
    );
  }

  if (!data) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Reading the query log…
      </p>
    );
  }

  const { totals } = data;
  const tokens = totals.prompt_tokens + totals.completion_tokens;

  return (
    <div className="space-y-6">
      <p className="text-xs text-muted-foreground">Last {data.window_days} days</p>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi icon={<MessageSquare className="size-4" />} label="Questions" value={totals.queries} />
        <Kpi icon={<Users className="size-4" />} label="People asking" value={totals.users} />
        <Kpi
          icon={<Timer className="size-4" />}
          label="Average answer"
          value={`${((totals.average_retrieval_ms + totals.average_generation_ms) / 1000).toFixed(1)}s`}
          note={`${totals.average_retrieval_ms} ms retrieval`}
        />
        <Kpi
          icon={<FileText className="size-4" />}
          label="Found nothing"
          value={totals.abstentions}
          // The most actionable number here: questions the corpus could not answer are
          // what is missing from it.
          note={
            totals.queries > 0
              ? `${Math.round((totals.abstentions / totals.queries) * 100)}% of questions`
              : undefined
          }
          tone={totals.abstentions > 0 ? "warn" : undefined}
        />
      </div>

      <Panel title="Tokens">
        {tokens === 0 && totals.queries_without_usage > 0 ? (
          // Said plainly rather than shown as a zero. "Nothing was spent" and "nobody
          // reported what was spent" are different answers, and only one is ever true.
          <p className="text-sm text-muted-foreground">
            This provider does not report token usage, so there is no cost to show.
          </p>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 text-sm">
            <span className="text-foreground">
              <span className="text-lg font-medium">{tokens.toLocaleString()}</span> total
            </span>
            <span className="text-muted-foreground">
              {totals.prompt_tokens.toLocaleString()} prompt ·{" "}
              {totals.completion_tokens.toLocaleString()} completion
            </span>
            {totals.queries_without_usage > 0 && (
              <span className="text-zenith-amber">
                {totals.queries_without_usage} question
                {totals.queries_without_usage === 1 ? "" : "s"} reported no usage — not
                counted
              </span>
            )}
          </div>
        )}
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Most active">
          <Rows
            empty="Nobody has asked anything yet."
            rows={data.most_active.map((person) => ({
              key: person.user_id ?? person.email ?? "gone",
              // A deleted user's queries survive them — `queries.user_id` is ON DELETE SET
              // NULL, so the activity is real and the person is not there to name.
              left: person.email ?? "a deleted user",
              right: `${person.queries}`,
            }))}
          />
        </Panel>

        <Panel title="Most cited documents">
          <Rows
            empty="No answer has cited anything yet."
            rows={data.top_cited.map((document_) => ({
              key: document_.document_id,
              left: document_.filename,
              right: `${document_.answers}`,
            }))}
          />
        </Panel>
      </div>

      <Panel title="Audit log">
        {data.recent.length === 0 ? (
          <p className="text-sm text-muted-foreground">No questions in this window.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th className="p-2 font-medium">When</th>
                  <th className="p-2 font-medium">Who</th>
                  <th className="p-2 font-medium">Question</th>
                  <th className="p-2 font-medium">Read</th>
                </tr>
              </thead>
              <tbody>
                {data.recent.map((entry) => (
                  <tr key={entry.query_id} className="border-b border-border/60 last:border-0">
                    <td className="p-2 align-top whitespace-nowrap text-xs text-muted-foreground">
                      {new Date(entry.asked_at).toLocaleString()}
                    </td>
                    <td className="max-w-40 truncate p-2 align-top text-xs text-muted-foreground">
                      {entry.email ?? "—"}
                    </td>
                    <td className="p-2 align-top">
                      <span className="text-foreground">{entry.question}</span>
                      <span className="ml-2 text-xs text-muted-foreground">
                        {entry.model} · {(entry.latency_ms / 1000).toFixed(1)}s
                      </span>
                    </td>
                    <td className="p-2 align-top">
                      {/* The column an auditor actually reads. What an answer opened is
                          not derivable from the answer text, which is the whole reason
                          `query_citations` exists. */}
                      {entry.documents.length === 0 ? (
                        <span className="text-xs text-zenith-amber">found nothing</span>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          {entry.documents.join(", ")}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function Kpi({
  icon,
  label,
  value,
  note,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  note?: string;
  tone?: "warn";
}) {
  return (
    <div className="space-y-1 rounded-lg border border-border p-3">
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        {label}
      </p>
      <p
        className={`text-2xl font-medium ${tone === "warn" ? "text-zenith-amber" : "text-foreground"}`}
      >
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Rows({
  rows,
  empty,
}: {
  rows: { key: string; left: string; right: string }[];
  empty: string;
}) {
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="divide-y divide-border rounded-md border border-border">
      {rows.map((row) => (
        <li key={row.key} className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
          <span className="min-w-0 truncate text-foreground">{row.left}</span>
          <span className="shrink-0 text-muted-foreground">{row.right}</span>
        </li>
      ))}
    </ul>
  );
}
