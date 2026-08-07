/** Choosing labels for a document, and administering the set itself. */
export { LabelPicker, COMBOBOX_THRESHOLD } from "./LabelPicker";
export { TagManager } from "./TagManager";
export {
  labels,
  createLabel,
  deleteLabel,
  searchLabels,
  mergeLabels,
  type Label,
  type LabelSearchItem,
  type LabelSearchPage,
  type LabelSort,
  type LabelMergeResult,
} from "./api";
