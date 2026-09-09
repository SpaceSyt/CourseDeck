# Verification record

## Executed during implementation

Current gate: **67 backend tests + 3 frontend tests**; production TypeScript/Vite build;
Ruff lint/format and Prettier checks; actual `start.cmd` launch and Chromium UI smoke.

- Phase 1: 12 backend tests, production UI build, actual localhost browser exercise.
- Phase 2: 21 backend tests including Classroom fixtures/pagination/OAuth state, app restart and UI smoke.
- Phase 3: 26 backend tests including Gradescope markup and DST rejection. Dedicated Chromium
  profile cookie survives closing/reopening; reset is confined to that profile.
- Phase 4: 28 backend tests, app restart, UI browser smoke. WebAssign only fixture validation.
- Phase 5: 30 backend tests including Brightspace mapping/pagination/permission failures.
- Reliability: additional source-date preservation, failure isolation, refresh, OS vault chunking,
  UI timezone boundary tests. See final test commands for the latest totals.
- Actual Windows Credential Manager: synthetic long token written/read/deleted successfully.

UI smoke starts its own temporary database and server. It covers course creation and binding,
preserved notes across reload/binding, heartbeat refusal/timeout/recovery with cached tasks,
dark desktop and 390px mobile layout, navigation, and no JavaScript page errors.
Screenshots are local under data/debug. The live user database is not used by this test.

On 2026-09-08 (America/New_York), WebAssign was verified with one real course and five
assignments, each with a parsed source deadline. Dedicated Chrome login, OS vault session
restoration, the current student DOM, All Assignments, and restart reuse were exercised.
Submission status remains unknown; broader multi-course discovery still needs live coverage.
Gmail's hidden/collapsed message regression was reproduced and fixed: cached threads were
verified to have complete bodies, with no sync error. Each sync still reads only the latest 30 inbox threads.
Database v5 and browser tests cover multiple source courses grouped into one local course,
atomic ownership checks, migration, preserved task identities/notes, and email/course resolution.

On 2026-09-08, a visible NYU Brightspace browser confirmed that saved school login was valid.
In a fresh context, requesting enrollments before navigation returned HTTP 403; navigating to
the homepage and waiting for the Microsoft school SSO redirect to return made the same API
return HTTP 200. Browser sync now performs this initialization before reading APIs.
The Work To Do page showed five upcoming items. All five are present after reading assignment
folders plus dated content. The new traversal checked all 252 content records across seven
courses, including the second page of a 100-item result, and returned 33 tasks/deadlines.
Dated course reading items were previously omitted by the assignment-folder-only adapter.
Three assignment submission endpoints still return HTTP 403; the assignment titles/deadlines
are retained, and warnings identify the affected items. These are not treated as account logout.
Automated coverage includes browser SSO initialization across fresh contexts, content pagination,
untrusted pagination destinations, and preserving records with an unrecognized due date.
Source coverage warnings now remain visible on source cards instead of being collapsed.

Database v6 adds independent course aliases, disabled state and reversible local deletion.
Regression tests cover migration, source-name updates after clearing an alias, preservation
through source grouping and sync, and retained task notes. Browser checks exercise the alias
asterisk, clear/save, disable/enable, delete/restore and exclusion from the sidebar and Todo.

Known information gaps remain: Gmail scans only the latest 30 inbox threads per run; Classroom
does not cover archived courses, questions or grades; WebAssign submission status is unknown
and multi-course selectors have not been verified; Brightspace standalone quizzes, discussions,
announcements and deadlines stated only inside documents are not yet covered. None of these
adapters is certified as a complete information snapshot. See AGENTS.md for the acceptance rule.

Database v3 retires the old demo source. Migration tests verify removal of its courses,
tasks, local edits, aliases, connector state and history without altering other providers.
The application registers four real sources; engine fixtures live only under tests.

On 2026-09-07 (America/New_York), the user completed school login in a dedicated Chrome
profile. Classroom session validation and a real sync succeeded for one course and one assignment:
title, precise due timestamp (from the page's response), and Assigned status matched the source.
Session reuse also succeeded after closing the browser and restarting CourseDeck.
This is one-account validation, not broad connector certification. Full-page data and credentials
are not included in fixtures. Browser sync remains partial; archived courses, questions, grades,
other languages, pagination/lazy loading in large classes and expired-session recovery need more coverage.

## Still required for production acceptance

- Optionally validate Google Desktop OAuth; default browser mode needs no Cloud project.
- Complete campus SSO in each dedicated browser profile; validate expiry and reauthentication.
- Extend WebAssign validation to multiple-course selectors, other date locales, and expiry.
- Validate Gradescope markup, stable identity for inaccessible rows, and source timezone.
- Configure the actual Brightspace instance and validate API permission/version, individual
  extensions, browser fallback, and add remaining task types.

## Repeat locally

Run `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`; in frontend run
`npm run typecheck`, `npm test`, `npm run format:check`, `npm run build`.
Build the frontend before `scripts/smoke_ui.py`; it starts an isolated backend automatically.
Profile/vault smoke scripts run independently.
