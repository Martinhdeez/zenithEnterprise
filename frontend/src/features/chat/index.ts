/**
 * Asking questions, and the answer as it arrives. What was asked *previously* is
 * `@/features/history` — a separate feature, since neither imports the other.
 *
 * `stream` is part of the public surface on purpose: `Citation` and `QueryResult` are the
 * vocabulary a citation click speaks, and `documents` and `search` both need it. Chat owns
 * the type because chat is what produces one.
 */
export { Chat } from "./Chat";
export { Answer } from "./answer/Answer";
export { streamQuery, type Citation, type Consulted, type QueryResult } from "./stream/stream";
export { reduce, displayed, isProvisional, type AnswerState } from "./answer/answerState";
