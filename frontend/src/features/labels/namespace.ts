/**
 * Reading a hierarchy out of names that have none.
 *
 * A label is one row with one name. There is no parent column, no tree table, and
 * deliberately so: `documents.label_ids` is a flat array and the RLS policy is a single
 * `&&` against it — `label_ids && zenith_current_labels()`. That comparison is what makes
 * every document query fast and every access decision the database's. A real hierarchy
 * would mean a recursive lookup inside a policy evaluated on every row of every query,
 * which is a different product's performance.
 *
 * So the hierarchy is a naming convention — `legal/contracts`, `finance/2026/invoices` —
 * and it lives entirely in this file and the components that render it. Nothing here
 * reaches the database, and nothing here decides what anybody may see.
 *
 * The consequence worth stating plainly: `legal/contracts` grants nothing over `legal`.
 * They are two unrelated labels that happen to share a prefix, and a role holding one does
 * not reach the other. Anything that implied otherwise — a parent checkbox that selects
 * its children, a filter on `legal/*` that claimed to cover a label the caller cannot
 * reach — would be an access-control statement made in the browser, which is exactly where
 * this product refuses to make them.
 */

export const SEPARATOR = "/";

export interface TagNode<T> {
  /** The last path element: `contracts` in `legal/contracts`. */
  name: string;
  /** The whole path, which is also the label's real name when one exists at this node. */
  path: string;
  /**
   * The label itself, when a label with exactly this name exists. Null for an implied
   * parent: `finance/2026/invoices` alone implies `finance` and `finance/2026`, and neither
   * is a row anybody can file a document under.
   */
  value: T | null;
  children: TagNode<T>[];
}

/** `["legal", "contracts"]` from `legal/contracts`. Empty segments are dropped, so a
    stray double slash or a trailing one does not produce a nameless level. */
export function segments(name: string): string[] {
  return name
    .split(SEPARATOR)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** The namespace a label belongs to: `legal/2026` for `legal/2026/invoices`, `""` for a
    label with no separator at all. */
export function parentPath(name: string): string {
  const parts = segments(name);
  return parts.slice(0, -1).join(SEPARATOR);
}

/** What to show when the full path is already implied by the level above it. */
export function leaf(name: string): string {
  const parts = segments(name);
  return parts[parts.length - 1] ?? name;
}

/**
 * Group labels into the tree their names describe.
 *
 * Intermediate levels are created as needed and carry `value: null` — `legal` is a real
 * node in the display even when no label is named exactly `legal`, because otherwise
 * `legal/contracts` and `legal/policies` would render as two unrelated top-level entries
 * and the convention would buy nothing.
 *
 * Sorted at every level so the same set of labels always renders the same way; a tree that
 * reorders itself between requests is unreadable however correct it is.
 */
export function tree<T>(labels: readonly T[], nameOf: (label: T) => string): TagNode<T>[] {
  const roots: TagNode<T>[] = [];
  const byPath = new Map<string, TagNode<T>>();

  for (const label of [...labels].sort((a, b) => nameOf(a).localeCompare(nameOf(b)))) {
    const parts = segments(nameOf(label));
    if (parts.length === 0) continue;

    let path = "";
    let siblings = roots;
    for (const [index, part] of parts.entries()) {
      path = path ? `${path}${SEPARATOR}${part}` : part;
      let node = byPath.get(path);
      if (!node) {
        node = { name: part, path, value: null, children: [] };
        byPath.set(path, node);
        siblings.push(node);
      }
      // Only the last segment names this label. An intermediate node keeps `value: null`
      // unless some other label is named exactly that path.
      if (index === parts.length - 1) node.value = label;
      siblings = node.children;
    }
  }

  return roots;
}

/**
 * Whether `name` is inside `namespace` — used by the `legal/*` filter.
 *
 * Prefix matching on *segments*, not on characters: `legal-hold` starts with `legal` as a
 * string and is not in the `legal` namespace, and a filter that swept it up would show
 * documents the user did not ask for under a heading that says otherwise.
 */
export function within(name: string, namespace: string): boolean {
  if (!namespace) return true;
  const target = segments(namespace);
  const parts = segments(name);
  return (
    parts.length >= target.length && target.every((part, index) => parts[index] === part)
  );
}

/** Every label at or under a namespace, for turning `legal/*` into the flat id list the
    API actually filters on. */
export function descendants<T>(
  labels: readonly T[],
  namespace: string,
  nameOf: (label: T) => string,
): T[] {
  return labels.filter((label) => within(nameOf(label), namespace));
}
