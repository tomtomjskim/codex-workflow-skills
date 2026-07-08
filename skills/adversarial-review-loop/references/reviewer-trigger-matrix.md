# Reviewer Trigger Matrix

Select reviewer lenses from changed surfaces. Do not run every reviewer by default.

| Changed surface | Required or recommended lens |
|---|---|
| auth, authorization, roles, tenant boundary, secrets, file IO, shell, network | security |
| data mutation, database schema, migration, seed, backfill, retention | security, QA, DBA if available |
| public API, SDK, routing, integration contract | code, API |
| tests, fixtures, mocks, assertions, CI failures | QA, test coverage |
| UI, forms, navigation, loading/error/empty states | UX, accessibility, QA |
| user-facing flow, form submit, routing, auth/permission path, persistence, checkout/order/payment, upload/download, realtime, cross-page state | QA with Playwright or equivalent browser evidence when available |
| hot path, query, cache, rendering, concurrency, memory | performance |
| architecture, module boundaries, multi-layer design | architect, code |
| docs-only, copy-only, narrow config comments | reviewer optional unless policy requires |

For skipped reviewers, record the reason.
