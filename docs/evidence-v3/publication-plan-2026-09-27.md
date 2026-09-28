# Local publication preparation for evidence v3

Historical plan from 2026-09-27. The current local review graph, exact refs,
and gate states are in [the v10 maintainer series](../evidence-v3-hardening/maintainer-series.md)
and [publication manifest](../evidence-v3-hardening/publication-manifest.json).

No branch has been pushed, no remote PR opened, and no merge or deployment
performed in this validation session. Read-only `gh auth status` identifies
`Kripta-Studios`; `origin` is its fork and `upstream` is
`Martinhdeez/zenithEnterprise`. Both remote `main` refs were
`33b48812c95150348c52d2519159780252c92db2` at the final read-only check.
Recheck these and branch permissions when publication is separately approved.
The preserved stack still ends at `0a24cc3`; the additive local
release-validation branch's latest executable/test/configuration content is
`1862072e55f3d79877334964b0e1b3a8f473a39f`; the complete local
`make check` exited 0 there (1,013 backend passed, nine skipped; 450 frontend
passed; static and licence checks passed). Authenticated browser acceptance
passed again at this head with a fresh fixture. The child of
`fb0a35b488632e5ad8f4909065a6c6b6938c69c3` changes only a
checkout-guard self-test. The additive branch's source repairs and acceptance
harness belong after the security slice in the stacked review.
Any later documentation-only report commit must identify both its SHA and
the gate-tested SHA without implying that remote CI ran.

## Review graph and intermediate viability

The preserved local integration stack is PR 01 `b354b8e`, PR 02 `e5f37c2`,
PR 03 `bab1578`, PR 04 `92e42bd`, PR 05 `1867532`, experimental R1
`2c08f10`, PR 06 `e8a61b8`, PR 07 `9076e63`, PR 08 `afb33db`, and the
stacked frontend lockfile patch `0a24cc3`. The additive release-validation
branch starts at that tip. R1 remains experimental and off by default, but
PR 06's evaluation code depends on R1 helpers; it cannot simply be omitted
from this exact stack without a separately reviewed extraction.

The historical PR 04/06/07 `make check` logs stopped at the old Windows
`os.uname` Pyright error; their Linux-target typing and focused tests passed
as recorded in the local handoff. That Windows fix appears in PR 08 and does
not by itself establish every earlier tip's full CI success. More materially,
PR 05 changes Vite proxy values to `apiTarget`, while the backend route guard
at PR 05 through PR 07 accepts only literal HTTP strings. PR 08 repairs the
guard, leaving those intermediate proposed main states with a known Linux CI
test failure. A local additive backport branch
`fix/evidence-v3-proxy-guard-backport` was cut from PR 04 at `d5a1062`.
Publish and merge this small guard repair after PR 04 and before PR 05, or
redraw the review boundaries with the same repair included. The historical
feature commits stay intact. PR merge refs must be checked against their
actual proposed bases; a final integrated green gate alone does not certify
each old tip. The backport tip passed the three proxy tests. A local simulated
PR 05 merge commit `7831e0680bfb749a0b248efea96f86cc08965315` merged the
historical PR 05 into that backport without conflicts; its three proxy tests
passed and Linux-target Pyright reported zero errors. This is representative
merge-ref evidence, not a full per-tip CI run.

The lockfile security patch is one file only. The local standalone branch
`chore/evidence-v3-frontend-audit-standalone` starts from unchanged upstream
`main` and copies only that lockfile diff. Its local commit is
`c11755806dd4b6bd159bee37d8c7aae49f7f5d5f`; `npm ci`, frontend lint,
437 standalone frontend tests, production build, and production-only audit
passed. It is an optional independent
security submission. The historical stacked tip `0a24cc3` includes the whole
v3 ancestry and must never be submitted to upstream as an independent
security-only PR. The clean branch recorded zero production npm advisories
after `npm ci`; all remaining five audit findings are development tooling:
`@vitest/mocker`, `esbuild`, `vite`, `vite-node`, and `vitest`. The audit's
suggested fix is a Vitest major upgrade. These tools run in local development
and CI, not in the production dependency set; their dev-server exposure and
untrusted PR code still deserve a separate planned upgrade, without forcing
one into this release-validation patch.

The current CI workflow runs on `pull_request` and `main` pushes on an Ubuntu
runner, installs backend/frontend dependencies, runs lint, format, typing,
tests, and licence checks. It contains no Jev key or production secret step.
Thus fork PR checks can run without granting provider credentials to untrusted
code, subject to the destination repository's Actions approval/settings.
Local browser, builds, and `npm audit --omit=dev` remain explicit
prepublication gates because the workflow does not run them. No remote CI was
triggered in this assignment.

## Valid remote sequence after separate authorization

For fork-internal draft review, push the preserved slice branches to the
**fork**, then create each PR against a branch that already exists **in the
fork**: PR 01 against fork `main`; PR 02 against PR 01; PR 03 against PR 02;
PR 04 against PR 03; proxy backport against PR 04; PR 05 against the backport;
R1 against PR 05; PR 06 against R1; PR 07 against PR 06; PR 08 against PR 07;
stacked security against PR 08; release validation against stacked security.
Review the incremental diffs and CI merge refs. The backport is not an
ancestor of the historical PR 05 branch, so the PR 05 merge result with that
base needs an explicit guard test. Retarget dependent PRs as bases merge.
Follow the repository's `CONTRIBUTING.md` policy: keep `main` green, require
the checks on each proposed merge result, and use **Rebase and merge** without
squashing the commits. Local final-head success does not replace those gates.

For upstream contribution, submit sequentially against **upstream `main`**
as it exists after each prior merge: PR 01–04, the proxy guard backport,
PR 05, R1, PR 06–08, the stacked security slice, then release validation.
Do not name a fork-only branch as the base of an upstream PR. Recheck the
upstream head and rerun the relevant merge-ref checks at each step. A
maintainer may decline R1 or ask for different boundaries; then prepare a
new, separately tested extraction rather than silently skipping its helper
dependency. If the independent security branch is submitted to upstream
first, omit the later stacked security submission and prepare/test a clean
hardening diff on the newly merged main; the old integrated test result cannot
be transferred to that recomposed head automatically.

Commands such as `git push -u origin <branch>` and `gh pr create --repo
Kripta-Studios/zenithEnterprise --base <existing-fork-base> --head
Kripta-Studios:<branch> --draft --body-file <reviewed-draft>` are templates
only. No publishing command, including a purported dry run that writes a
remote ref, was invoked here. Runtime rollback is TEI, legacy search, and all
optional external-processing/direct/packet/strict flags off; code rollback
follows reviewed dependencies in reverse order. The standalone lockfile patch
can be reverted independently.
