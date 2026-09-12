# Verification record

## Gmail pagination and cache integrity · 2026-09-12

This supersedes the older latest-30-thread limit recorded below. The dedicated
signed-in browser was used to traverse 19 Inbox pages. All 926 reference thread
IDs matched the resulting local header cache, without missing or extra IDs in
that comparison. Existing local mail actions were unchanged. Verification-only
header backfill used zero body reads; normal runs used the production eight-body
budget per page. Normal runs also verified continued historical checkpoints and
body reads after the header phase. The local service was restarted and its
heartbeat returned connected.

This is an Inbox header comparison for one account, not proof of complete
conversation bodies, every Gmail category, archived mail or attachments. Unread
cached bodies remain explicitly reported. Empty-inbox behavior was verified with
synthetic browser fixtures only; the real mailbox was not emptied.

Regression fixtures cover hidden old rows, delayed navigation back to page one,
native disabled pagination, unknown page ranges, missing/duplicate identities,
persisted checkpoints, fair retry, body timeouts and local-state preservation.
Stale retained text is excluded from current classification and automatic
association evidence. All 538 backend tests and 22 frontend tests passed, as did
browser UI smoke, the frontend build, formatting, lint and generated contracts.

## Publication checks · 2026-09-12

492 backend tests and 22 frontend tests passed locally. Ruff, generated task contracts,
frontend formatting, the production build and Git whitespace checks passed. The isolated
Chat browser tests are included in the backend suite; CI installs Chromium before running it.
These checks do not expand the source coverage documented below.

## Five-workflow integration · 2026-09-10

**370 backend tests and 6 frontend tests passed**, with a TypeScript/Vite build,
Ruff and Prettier checks. Seven isolated Chromium workflows cover task/course behavior,
Chat, Changes, associations, material search, mail-rule previews and backup/recovery.
Fixtures use temporary databases or intercepted synthetic responses, never restored user data.

Changes verifies an upgrade baseline, before/after evidence, earlier/later deadlines,
reopened assignments (including assigned/missing/incomplete), literal-text search, read state,
transaction rollback and repeated-sync deduplication. Associations verifies explicit identity,
ambiguous numbered work, repeated attempts, encoded source URLs, course merge/disable behavior,
recoverable decisions and independent source states. Related mail is not implicitly sent to Chat.

Material checks include full-body tail matches, Unicode/phrase ranking, source/kind/freshness/
completeness filters and task-scoped references. Mail previews are read-only until Apply, retain
manual overrides, and show priority/ignore/classification impact. The default inbox remains
chronological; active attention filters are visible and removable.

Recovery checks include hashes/schema/integrity/foreign keys, two-store restoration and rollback,
modified manifests, compatible legacy import, in-flight HTTP writes, available heartbeats,
interactive-login refusal, browser-close failures and pending-journal startup. A second failed
recovery cannot clear an existing journal or certify a mixed pair as a backup. Background polls
resume without duplication; unresolved recovery does not write sync diagnostics or resume work.

The updated local service completed its normal startup sync. A before/after read-only audit
confirmed preservation of all **74 source tasks, 4 local-state records, 62 cached emails and
148 knowledge records**. The migrated Changes baseline contained zero spurious events. The
real association scan found 12 explicit task-to-document links; uncertain matches remained
suggestions. The library exposed 74 non-assignment documents, with 11 scope/document warnings.
A new paired backup passed API preflight and both live databases passed integrity/reference checks.
No real database was restored. No source parser was changed in this five-workflow iteration.

Gradescope and WebAssign remained successful; Classroom and Brightspace remained partial with
their known gaps visible. These checks establish retained data and working local workflows,
not complete platform coverage. A compatible model endpoint is configured locally; this iteration
did not submit course data to that real endpoint. Model behavior is verified with mock transports.

Re-run after building the frontend:

```console
python -m pytest -q
python -m scripts.smoke_ui
python -m scripts.smoke_chat_ui
python -m scripts.smoke_changes_ui
python -m scripts.smoke_associations_ui
python -m scripts.smoke_material_search
python -m scripts.smoke_mail_rules_ui
python -m scripts.smoke_recovery_ui
```

## Earlier integration · 2026-09-10

Final gate: **262 backend tests + 6 frontend tests**, TypeScript/Vite production build,
Ruff lint/format, Prettier, existing task/course Chromium smoke and Chat/library Chromium
smoke all passed. Both real databases passed SQLite integrity checks; the live backup API
created a matched task/knowledge backup pair whose integrity was verified separately.

Automatic sync now collects Classroom materials/Stream announcements and Brightspace
content/announcements into `knowledge.sqlite3`, without a model. Explicit dated content
remains in Todo; ordinary materials do not create tasks. Source task facts are indexed
separately from document text, so new deadlines/statuses cannot overwrite extracted bodies.
Collection retains partial results, unreadable/deferred entries and old bodies, with separate
checked/fetched timestamps and visible document warnings. No prose deadline becomes a source fact.
Classroom writes each discovered/read document immediately, so a later timeout cannot discard
earlier progress. Per-document incomplete status includes unread attachments; a failed attachment
refresh retains previously extracted full text and explicitly labels the retained cache.

The dedicated Classroom browser verified six materials and one announcement. The syllabus
was read through the actual Google Docs plain-text download menu (over 27,000 characters);
the full text is available through the local library. Attachment links with unread contents
remain incomplete. Brightspace collection has been exercised across the three active source
courses, including TOC entries, static PDF text and news; external links remain explicit gaps.
These observations validate this account's pages, not complete platform coverage.

The seven active source-course bindings were grouped into four local courses after a database
backup. All 73 stored task identities and their local states were compared before/after and
preserved. Tests cover repeated merges, aliases, shared/custom colors, source ownership,
mail associations and custom-task references. Built-in mail keyword rules can now be edited,
disabled, deleted or reset; deleted defaults do not silently reappear on restart.

Chat uses a user-configured compatible endpoint and local source evidence. Mock HTTP tests
cover tool calls, long-document tail reads, source citations, failed replies/history recovery,
course merges, incomplete evidence, endpoint-bound credentials and compatible parameter/tool
fallbacks. No real model has been configured or called. Chromium checks cover the no-key
library, paging/course filtering, source references, API settings, persisted warnings, actual
read/check timestamps and 390px mobile layout. The real local UI was also checked against the
four merged courses and saved materials; existing course/task UI smoke passes.

A startup check found WebAssign's saved source session had expired. Its dedicated visible
browser was reauthenticated using Chrome's saved login on the official page, without reading
or logging the password. The same connector then verified six current assignments, including
one newly published item; the previous five remained present and Past was explicitly empty.
No unsupported session-expiry workaround was added. Reauthentication can still be necessary
when the source expires its session; failed attempts retain all cached tasks.

Earlier audits below are historical; their material/announcement exclusions and older counts
are superseded by this integration. Remaining gaps include Gmail's latest-30-thread window,
unread external attachments, source-hidden grades, and prose-only deadline interpretation.

## Executed during implementation

Further partial-source audit (2026-09-09): all changes below run as ordinary connector
code, without a model. Classroom's completed assignment card no longer looks like an
unsupported item. The visible Turned in late status is submitted; an explicit no-due-date
record clears an old deadline while an unreadable date preserves it. Both real assignments
now refresh their bodies and current states; grades remain unavailable in this account.

WebAssign's unrestricted Homework details expose per-part Submissions Used. The adapter
checks the complete question/part inventory, refuses limited/timed/unknown restriction
layouts, and validates the final detail URL before reading. All five real tasks now have
known status and the source returns Success. Nonzero scores alone are never completion
evidence; unreadable or partial inventories still produce Unknown and Partial.

Brightspace dated content now reads topic descriptions and same-course static PDF/HTML/text
files. Three real content descriptions were filled, including a three-page PDF checked
visually against its extracted text. Nine content metadata endpoints return 404: their
deadlines remain, their bodies stay unavailable, and no inference of deletion/unpublication
is made. Text extraction does not interpret every diagram or infer prose deadlines.

The optional AI operator workflow was exercised on two selected announcements. One contained
a deadline conflict with an existing assignment and its mail reminder; the other was an old,
role-dependent orientation notice. Evidence was staged locally without creating duplicate
tasks, inventing a current-year date, or changing source/local task state. This is a documented
agent workflow, not a background AI integration or automatic model-output importer.

Partial-source audit: a visible Classroom profile and its View your work page were checked.
The adapter now captures assignment instructions, scans the archived-course page, expands
and scrolls Classwork, and isolates failures per course/detail. The real archived page was
empty; nonempty archived courses and non-English pages still need live validation.

Brightspace student assignment lists resolved three submission API permission errors without
changing source state. Quiz lists were paginated (one course spans two pages); 29 quizzes
were read, with 28 confirmed submitted using native Quiz Summary completed-attempt counts.
Quiz attempt APIs returned 403; DOM fallback reads summary only and never starts an attempt.
All visible discussion forums were traversed; current topics had no explicit deadlines and
were not added as fabricated due tasks. Total Brightspace records increased from 33 to 62.
News announcements and deadlines embedded in documents remain outside aggregation.

WebAssign's loading shell previously produced a false successful zero-task result. The
adapter now waits for All Assignments and both Current and Past sections to contain rows
or an explicit empty state. Delayed-render regressions cover Current arriving before Past.
The dedicated visible browser confirmed five current tasks and an empty past list; all five
refresh correctly. Submission status is not exposed and remains explicitly unknown.

Earlier gate: **188 backend tests + 6 frontend tests**; production TypeScript/Vite build;
Ruff lint/format and Prettier checks; actual `start.cmd` launch and Chromium UI smoke.

Course alignment and task-state update: the UI reserves a course-code line when empty and
aligns card actions. Browser regression checks cover Done, source-confirmed completion,
unknown/unrefreshed/missing states, left-to-right strike followed by row exit, failed-save
recovery, and reduced-motion preferences. Actual Gradescope DOM showed a Submitted item;
the refreshed local UI places it in Done. An expired WebAssign session returned a login page;
cached tasks remain visible as unrefreshed, rather than disappearing or becoming completed.

Database v7 removes absence-based automatic archival. Only an exhausted course/task-type
scope can establish absence; incomplete coverage instead produces an unrefreshed state.
Submission values retained from an older snapshot are not fresh completion evidence.
Tests cover scoped absence, failed pages, reappearance, revoked/unreadable status and migration.
Brightspace reads all completion states and refreshes previously cached content even after
its deadline is removed. Required file reading with a valid completion timestamp may be Done;
an LTI/assignment wrapper's viewed state is not treated as submitted work. The mapper follows
the [Brightspace content completion fields](https://docs.valence.desire2learn.com/res/content.html#Content.ScheduledItem).

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

At this earlier audit, information gaps included Gmail's latest 30 inbox threads per run; Classroom
questions, materials, announcements and document deadlines were not imported, and nonempty
archived-course pages and grades need more live coverage. WebAssign status requires complete
per-part submission records or explicit status labels; restricted work and multi-course selectors
need more verification. Brightspace announcements
and deadlines stated only inside documents are not aggregated. None of these
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
