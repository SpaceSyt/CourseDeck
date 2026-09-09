# Architecture decision · 2026-09-04

CourseDeck is a single-user, local process. FastAPI serves the compiled React/TypeScript UI
and a loopback-only API. No accounts, remote backend, daemon, telemetry or external fonts.
SQLite uses WAL, foreign keys and atomic snapshot transactions. Standard-library SQL keeps
the small persistence layer explicit; an ORM adds little value to these five entities.

`UI → local API → Database / SyncEngine → Connector → Transport → Parser → domain`

The engine only understands normalized results and connector lifecycle operations. New
providers require a connector and a registry entry; transport knowledge stays in connectors.
Playwright manages dedicated local profiles, never the user's normal browser. OAuth secrets
belong in the OS keyring, with no plaintext fallback. Desktop packaging can reuse the UI/API.

Source state and local state live in separate tables. Identity is escaped provider/course/task
identity. UTC-aware timestamps are mandatory; source due and closing dates are distinct.
Unknown dates remain unknown, never guessed midnight deadlines. UI dates use an IANA zone.

Only `success + complete` snapshots increment missing counts. At least three such snapshots
and seven days unseen archive a task. History is retained and archive is visible in the UI;
reappearance restores the source row without touching notes. Failures and partial results
never advance disappearance. Data is never physically deleted by sync or disconnect.

Cached data is readable before startup sync finishes. Providers run independently with locks
and timeouts. UI polls the local snapshot (simple, reconnect-friendly, no required SSE broker).
V1 supports startup/manual sync only. No periodic refresh or desktop wrapper is necessary yet.

## Verification gates

1. Core with test-only synthetic fixtures: persistence, isolation, UI and real localhost smoke test.
2. Classroom: documented read-only API, OAuth and token refresh, fixtures + transport tests.
3. Gradescope: isolated login, validated course/assignment HTML, conservative completeness.
4. WebAssign: never invent private endpoints. Authenticated discovery needs the user's session;
   experimental fallback must report partial/parse errors, with no claim of live validation.
5. Brightspace: official API and browser-session transport, instance-specific validation.
6. Recovery, safe debug exports, tests, build, lint and actual browser exercise.

Authenticated campus SSO/2FA and Google client registration cannot be validated without the
user's institutional access. An implemented connector is not evidence of a live verified one.
No third-party source has been copied; dependencies retain their own licenses.
