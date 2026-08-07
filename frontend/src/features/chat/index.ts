/**
 * Asking questions, and the answer as it arrives.
 *
 * `stream` is part of the public surface on purpose: `Citation` and `QueryResult` are the
 * vocabulary a citation click speaks, and `documents` and `search` both need it. Chat owns
 * the type because chat is what produces one.
 */
export { Chat } from "./Chat";
export { Answer } from "./Answer";
export { History } from "./History";
export { history, type HistoryEntry, type HistoryPage } from "./api";
export { streamQuery, type Citation, type Consulted, type QueryResult } from "./stream";
export { reduce, displayed, isProvisional, type AnswerState } from "./answerState";
