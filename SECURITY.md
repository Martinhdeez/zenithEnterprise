# Security

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button under the repository's
Security tab, which opens a private advisory. Do not open a public issue for anything that
could be used against a running installation before it is fixed.

Include what you did, what happened, and what you expected. A proof of concept is welcome; a
working exploit against somebody else's installation is not.

This is a single-maintainer project with no service-level agreement. Expect acknowledgement
within a week or so. If a fix is not straightforward, the advisory is where the state of it
will be recorded.

## What this project operates

Nothing. There is no hosted service, no telemetry, and no endpoint under the maintainer's
control that this code reports to. Every installation is somebody else's, on their hardware,
with their data. A vulnerability report here changes the code; deploying the fix is the
operator's.

## What counts as a vulnerability here

Isolation is the product, so anything that crosses it is the most serious class of defect
this project has:

- A query, route, migration or diagnostic that returns rows belonging to another tenant, or
  to a compartment the caller does not hold.
- Anything that lets a tenant administrator reach `users.is_system_admin`, which is protected
  by a column-level `GRANT` precisely because the permission catalogue cannot protect it.
- Anything that rewrites or deletes `audit_events`, from any role, including the two that
  bypass row-level security.
- A new `SECURITY DEFINER` function that is not declared in `AUTHORISED_SECURITY_DEFINERS`,
  or any other path that bypasses row-level security without being named in the three session
  factories.
- An answer that carries a citation which does not resolve to a passage the caller was
  allowed to read.

Denial of service through an expensive query, and a missing rate limit, are bugs — open them
as ordinary issues.

## Deployment notes that are not vulnerabilities

These are documented behaviours, not defects, and a report about them will be closed with a
pointer here:

- `.env.example` ships development defaults. An installation that keeps them is misconfigured;
  `docs/deployment.md` sets both database role passwords by hand for this reason.
- `owner_session()` and `platform_session()` bypass row-level security by design. They are
  named so that grepping for them is a complete audit of the factories.
- The reranker being absent degrades recall and says so. That is the circuit breaker working.
