# F22 was already landed, and the branch pointer said otherwise

**Date:** 2026-08-30 · **Status:** tested

Recorded here because it was written into the body of merge commit `fcb85cd`, and
`main` only accepts "rebase and merge" — which drops merge commits. The reasoning would
have been destroyed by the very act of landing it.

Records containment. F22 is already in `integration/scaling` and has been since
`596d321` merged `design/sidebar-onto-integration`; this merge adds no content and
is deliberately `-s ours`.

The evidence, because the branch pointer says the opposite and the next reader will
believe the pointer:

  tree(feat/f22-user-profile @ 0939e13) == tree(da8cf82) == 339b84dd
  da8cf82 is an ancestor of integration/scaling

`da8cf82` is a rebase copy of F22's tip: same 22 subjects, same author dates, same
patch-ids, committer dates about an hour apart. Git cannot see that the two lineages
are the same work, so `git cherry` reports 22 unmerged commits and
`git merge` reports 69 conflicts. Both are artefacts of the rebase, not of divergence.
Merged against its true content ancestor the result is byte-identical to the base:

  git merge-tree --merge-base=da8cf82 integration/scaling feat/f22-user-profile
  -> fd7f40ec == tree(integration/scaling)

An ordinary merge would have resolved those 69 conflicts by re-adding three-week-old
copies of files the base has since moved twice -- `api/answer.ts` on top of
`features/chat/answer/answerState.ts`, `api/highlight.ts` on top of
`features/documents/viewer/highlight.ts`, and so on -- and would have reverted work
rather than landed any. The alternative was to delete the branch; recording the merge
is preferred because it stops every future `git branch --merged` and `git cherry`
reporting the same 22 phantom commits at whoever looks next.

The three migrations F22 carries -- 0006, 0007, 0008 -- are byte-identical to the ones
already in the base, so the two-heads collision CONTRIBUTING.md warns about does not
arise. Alembic head stays 0026.

