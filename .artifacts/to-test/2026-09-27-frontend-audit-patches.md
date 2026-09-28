# Frontend production dependency audit patches

The final clean v3 installation exposed five pre-existing transitive production
npm advisories (three high, two moderate). `frontend/package-lock.json` is
byte-identical to upstream main at the PR 08 base, so this is a separate local
security slice rather than a change to the evidence feature. Apply npm's
non-major lockfile-only resolution for `fast-uri`, `hono`, `js-yaml`, `nanoid`,
and `qs`, then verify `npm ci`, production-only `npm audit --omit=dev`, TypeScript
lint, Vitest, and production build. Keep the unresolved development-tool audit
items explicit; do not force a major Vitest migration into this slice.

The modified lockfile should not change application source, API routes, or
feature defaults. Rollback is reverting this lockfile commit. It is stacked on
PR 08 and must be retested at the final integrated SHA before any deployment
decision. No remote publication is authorized.
