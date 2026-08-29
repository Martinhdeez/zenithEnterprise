/** Choosing labels for a document, and administering the set itself. */
export { LabelPicker } from "./picker/LabelPicker";
export { TagChip, TagChips, labelTone, type LabelTone } from "./tags/TagChip";
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
export { TagManager } from "./manager/TagManager";
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
