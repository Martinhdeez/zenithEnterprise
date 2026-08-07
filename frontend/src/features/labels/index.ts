/** Choosing labels for a document, and administering the set itself. */
export { LabelPicker } from "./LabelPicker";
export { TagChip, TagChips } from "./TagChip";
export {
  SEPARATOR,
  descendants,
  leaf,
  parentPath,
  segments,
  tree,
  within,
  type TagNode,
} from "./namespace";
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
