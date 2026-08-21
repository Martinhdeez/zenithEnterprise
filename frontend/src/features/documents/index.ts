/**
 * The corpus: browsing it, uploading to it, and reading a page of one.
 *
 * `PdfViewer` is deliberately **not** re-exported, and this is load-bearing rather than an
 * oversight. `App.tsx` reaches it by dynamic `import()` so that `pdf.js` — about 1.4 MB of
 * worker plus runtime — stays out of the initial bundle until someone clicks a citation.
 * A static re-export here defeats that: Rollup sees the module statically reachable from
 * the barrel and stops splitting it, silently, with only a build warning to say so. It was
 * measured: adding this one line moved the main chunk from 615 kB to 983 kB.
 *
 * Import it as `@/features/documents/viewer/PdfViewer`, the one sanctioned deep import
 * in the app.
 */
export { Folders, type FolderSelection } from "./browse/Folders";
export { Documents } from "./browse/Documents";
export { Ingesting, inFlight, ready } from "./status/Ingesting";
export { Upload } from "./upload/Upload";
export { StatusBadge } from "./status/StatusBadge";
export {
  uploadDocument,
  listDocuments,
  deleteDocument,
  folders,
  type DocumentSummary,
  type DocumentPage,
  type Folder,
  type FolderTree,
  type UploadResponse,
} from "./api";
