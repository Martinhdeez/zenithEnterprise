/**
 * The record of past questions.
 *
 * Its own feature rather than part of `chat`, and the split holds up under inspection:
 * nothing here imports `chat` and nothing in `chat` imports this. The component takes a
 * token and an `onAsk` callback, and the shell is what wires a click through to the ask
 * box — so the coupling people assume exists is actually the shell's, not the feature's.
 *
 * The endpoint is `GET /query/history`, which is why this used to sit in `chat/api.ts`.
 * That file held nothing else: chat itself speaks `stream.ts`. One owns asking, the other
 * owns what was asked.
 */
export { History } from "./History";
export { history, type HistoryEntry, type HistoryPage } from "./api";
