# Architecture decisions · 2026-09-10

CourseDeck is a single-user, local process. FastAPI serves the compiled React/TypeScript UI
and a loopback API by default. An opt-in HTTPS LAN listener shares the same application.
No remote backend, daemon, telemetry or external fonts.
SQLite uses WAL, foreign keys and transactional writes. Standard-library SQL keeps the
persistence layer explicit. The primary database remains the source of task and course state.

`UI → local API → Database / SyncEngine → Connector → Transport → Parser → domain`

LAN access uses a separate exact RFC1918 IPv4 address and port; the loopback HTTP listener
remains available. `network.json` holds opt-in configuration and a salted scrypt password
verifier independently of database backups. Invalid configuration or TLS/bind failure leaves
only local access and a visible settings error. A secondary Uvicorn server shares the primary
application's lifespan, requires TLS 1.2+, ignores forwarded headers, and never maps router ports
or changes firewall rules. Disabling or changing configuration revokes sessions and closes it.

The outer ASGI boundary checks actual socket peer, explicit Host, scheme, Origin and Fetch
Metadata before routes or static mounts. Remote access exposes only the empty app shell/assets
and session endpoints before authentication; every private read, attachment, stream and write
requires a bounded in-memory session. Cookies are Secure, HttpOnly and SameSite=Strict; writes
also require matching Origin and the UI header. Login attempts are rate limited, WebSockets
are denied, and administrative/source/credential/collection configuration stays local. Source
configuration values are removed from remote snapshots. Authenticated LAN users otherwise
share the whole workspace, including Chat, sync and local edits. Revocation stops further
response bodies; partially delivered fixed-length files fail closed as interrupted downloads.
Network/session controls remain usable during database recovery.

The engine only understands normalized results and connector lifecycle operations. New
providers require a connector and a registry entry; transport knowledge stays in connectors.
Playwright manages dedicated local profiles, never the user's normal browser. OAuth secrets
belong in the OS keyring, with no plaintext fallback. Desktop packaging can reuse the UI/API.

Each provider's browser session has one current cookie snapshot in the OS keyring.
Login, sync, material collection and Chat use the same dedicated profile and snapshot.
Session-only cookies survive browser restarts; changed profile cookies take precedence over
an older snapshot. Browser cleanup saves rotations even after failed or cancelled reads,
and waits for in-flight keyring writes before releasing the profile. An empty snapshot is
authoritative and cannot fall back to legacy connector cookies. Disconnect clears the profile
and snapshot. Closing an interactive window without Finish login invalidates its older snapshot
because the final cookies can no longer be read. Server expiry still requires authentication;
sessions in an ordinary browser are separate and are not imported.
Credential reads, writes and chunk cleanup are serialized across threads and processes
for each data directory; Windows keyring's shared primary slot must not be updated in
parallel. An incomplete saved snapshot stops background reading with a visible error.
Explicit Reconnect may discard that snapshot and reuse the retained profile, but cannot
bypass a locked or unavailable credential store.

`coursedeck.sqlite3` holds courses, tasks, local preferences, mail, connector state, source
change baselines/events and evidence-link decisions/history.
`knowledge.sqlite3` holds indexed documents/evidence, non-secret Chat configuration and
conversation history, including citations and warnings. Source tasks are indexed with their
stable identity and freshness fields; reading or indexing a document never changes its task.
Settings creates a paired backup directory using SQLite's online backup API, with a manifest
containing file sizes, SHA-256 checksums and schema fingerprints. Both stores must validate;
browser profiles and OS credentials are not included. Recovery behavior is described below.

Source state and local state live in separate tables. Identity is escaped provider/course/task
identity. UTC-aware timestamps are mandatory; source due and closing dates are distinct.
Unknown dates remain unknown, never guessed midnight deadlines. UI dates use an IANA zone.

Only a complete source snapshot or an explicitly exhausted course/task-type scope can mark
an absent task missing. Field-level failures may leave a result partial while one list is
fully covered; unread pages or skipped rows cannot claim coverage. Missing tasks remain
visible and never automatically enter Done or archive. Unrefreshed or unknown source states
are distinguished from absence; cached submission values are not fresh completion evidence.
Reappearance restores the source row without touching notes. Sync and disconnect never
physically delete data.

An omitted/undeclared empty description is an unknown observation, not proof of deletion:
retain the previous body and its original fetch time while reporting the unknown state.
Only an explicitly observed empty body clears it. API adapters declare empty only when the
source field is present and empty. A complete material body clears obsolete unknown/unread
description observations when enriching a task, without changing its deadline or local state.

Course grouping is many-to-many across providers, while each source course belongs to one
local course. Explicit course merges redirect source bindings and local references to the
destination, preserving its name and color. They do not merge assignment IDs, submission
states or notes. Aliases, colors, disabled/deleted state and editable/enabled mail keyword
rules are local preferences; automatic course-name matching and mail rules do not use AI.

Change events are recorded in the source snapshot transaction, independently of local task
edits. An upgrade adopts existing cache as its initial baseline without flooding the log with
old additions. Later additions, deadline/instruction changes, reopened tasks, missing fields
and source availability transitions retain before/after values and source evidence. Identical
snapshots do not repeatedly emit the same change. Failed reads cannot claim changed source
fields. Read/unread tracking affects the log only; reopening evidence is important even when
the user has locally dismissed the task.

Task relations link tasks, emails and documents without merging them. Unambiguous task-level
source IDs/URLs can establish direct links; assignment-number overlap or ambiguous references
remain suggestions requiring confirmation. Course-level URLs and title similarity alone are
not identity. Explicit confirm/reject/link/unlink decisions and their history persist, and
unavailable scope makes links inactive. Each provider retains its own deadline, submission
state and notes; completion is never propagated across a relation.

Mail rule preview evaluates proposed add/update/enable/delete/default-reset operations against
the current cached mailbox without applying them. The UI requires Apply changes to save and
invalidates its preview when the proposal changes. Preview reports conflicts, incomplete
mail content and preserved manual overrides; it is not a full-mailbox guarantee. Priority
matching uses explicit rules with conservative word/negation handling. The inbox remains
newest-first by default; priority filtering/sorting is an explicit view choice.

Cached data is readable before startup sync finishes. Course providers share a serial queue
with per-provider locks and timeouts. Startup, manual and periodic requests use the same queue;
duplicate queued requests and overlapping whole-source rounds are coalesced. A periodic loop
waits 10 minutes before its first round and after each finished round. Turning off startup sync
does not disable this loop. Gmail retains its separate five-minute poll. UI polls the local
snapshot (simple, reconnect-friendly, no required SSE broker). Shutdown cancels queued work
and the periodic loop before closing connector browsers.

Source readers enter `SyncEngine.source_access(provider)` for FIFO admission, provider
exclusivity and maintenance checks. Chat browser reads, protected task links and standalone
material reads share this boundary. Full external collection jobs reserve/release their
source through public methods and use `collect_source()` for one refresh; the job owns its
deadline and bounded retry loop, so engine retries cannot escape that budget. Automatic
material enrichment already runs inside the source lock and must not reacquire it.
Chat and bounded collection supply explicit `BrowserReadContext`/`BrowserReadScope` values
to the course browser reader; collection does not construct a substitute Chat service.

The CLI takes an OS-held lock on the selected data directory and reserves its loopback
listening socket before loading the ASGI app. Duplicate launches cannot initialize the
same databases or begin competing browser sessions, including on a different port.
The default data location is anchored to the project root; an explicit `--data-dir`
overrides `COURSEDECK_DATA_DIR`. The existing profile and credential namespace are retained.
Shutdown and startup failures close all background owners before database readers.

Transient network and rate-limit failures schedule at most two retries, after 15 and 60
seconds, through the same queue. Manual requests replace pending retries; authentication
failures are not repeatedly retried by automatic rounds. Source diagnostics expose the stage,
category, attempt count and next retry rather than converting failure into success.
Completed/interrupted source diagnostics also report `queue_wait_seconds` separately from
`execution_seconds`. These measure waiting and service time without changing the serial
browser concurrency policy.

## Recovery

Backup, legacy import and restore share a maintenance gate. It stops admitting ordinary API
requests and waits for in-flight requests to drain, then stops background Gmail work, cancels
queued/running source work, acquires source locks and closes non-interactive browsers. A live
sign-in window or undrained request produces an actionable conflict instead of being seized.
Heartbeat and recovery routes stay accessible. Cache readers and writers cannot observe a
half-restored pair; successful completion clears material cursors/cache and resumes polling.

Preflight validates the manifest, allowed local paths, checksums, SQLite integrity/foreign
keys and exact schema compatibility with the running installation. Restore rechecks preflight
and creates a paired pre-restore backup before copying. A durable journal identifies the
target and rollback snapshot. If copying fails, both databases are rolled back; if rollback
or process execution is interrupted, the journal keeps ordinary access and sync blocked until
recovery completes. The copies are sequential, not a cross-file atomic disk transaction.
While a prior journal exists, ordinary backup creation is refused. Recovery preserves that
journal and its validated rollback snapshot instead of certifying the potentially mixed live
files as a new pair. A failed second recovery cannot clear the journal or restart normal work.

Legacy timestamp-matched database files may be imported into a manifest-backed directory
without changing the live databases or deleting the originals. Missing pairs, invalid files
and incompatible schemas are rejected explicitly; this path does not migrate old schemas.

## Materials and optional Chat

Brightspace and Classroom source syncs also collect supported materials under the same
source lock, without a model. Brightspace traverses the TOC and reads accessible text/PDF
content and News announcements. Classroom reads supported Classwork materials, Stream
announcements and accessible Google Docs text. Entry, body, file-size and time budgets bound
each run. Unread or deferred scope produces warnings; failed body refreshes preserve the old
body, its original freshness information and the latest error. Available records do not
prove that every page, attachment or history item was covered.

Local search retains its exact Unicode word/phrase matching and ranking. Small scopes use
a revision-aware bounded text cache; larger scopes stream SQLite rows instead of copying
the whole corpus into normalized rows. Term counting stops at the scoring cap, and queries
compile patterns once. This needs no schema migration, FTS table or external search service.
`scripts/benchmark_material_search.py` measures latency and Python allocation peaks using
only synthetic disposable databases; broad searches still retain complete matching bodies.

Ordinary materials and announcements belong in the library. Explicit source tasks and
content with structured deadlines still belong in Todo as well; text that merely mentions
a date does not create a task. Model output never rewrites source deadlines, marks completion
or merges tasks. `Chat → Materials` browses/searches the local index without API credentials.
The library filters provider, kind, actual body-read age and completeness; freshness is based
on `fetched_at`, not the latest unsuccessful check. Chinese/English terms, quoted phrases and
matching excerpts aid retrieval. A complete document is not evidence of complete course
coverage. Assignment evidence stays available to task Chat rather than cluttering the material
listing.

The Chat navigation item sits above Todo. Settings accepts an OpenAI-compatible base URL,
model ID and a write-only API key stored in the OS keyring. Changing the endpoint clears the
old key; retrieval returns only key presence or a credential-store error. The built-in prompt
is readable but not editable in the UI.

Only a user Chat request invokes the configured model endpoint. Relevant course evidence
and conversation context are sent to that endpoint, so hosted models receive that selected
content. Routine source/mail sync and library access remain entirely independent of AI.
The local service mediates bounded search, document reads, evidenced task-link reads and
version-checked local task overrides with an undo journal. It cannot submit work or mutate
source tasks, mail or accounts. The streaming response holds the maintenance gate until the
producer finishes or is cancelled. See [Chat tools](CHAT_TOOLS.md). Course
scope follows current bindings and merge redirects. Missing evidence, tool limits, cached
fallbacks and unverified references remain visible in the saved reply. Document text is
untrusted input; the UI renders it as text and validates source links.

Ask about task opens Chat with an explicit task ID, current task facts and related material
context. Existing document links are distinguished from keyword matches. Opening this view
does not call a model. Related emails remain local; linking an email does not implicitly add
its body to model context. Task scope is resolved again when the request runs, so course merges
or disabled/deleted scope cannot silently broaden evidence access.

API contracts, local tools, error handling, key controls and UI behavior have synthetic/mock
coverage. Individual model compatibility and answer quality cannot be inferred from these
tests and require separate live verification.

## Verification gates

1. Core with test-only synthetic fixtures: persistence, isolation, UI and real localhost smoke test.
2. Classroom: documented read-only API, OAuth and token refresh, fixtures + transport tests.
3. Gradescope: isolated login, validated course/assignment HTML, conservative completeness.
4. WebAssign: never invent private endpoints. Authenticated discovery uses the existing session;
   per-account verification distinguishes observed scope from untested course/page variants.
5. Brightspace: official API and browser-session transport, instance-specific validation.
6. Recovery, safe debug exports, tests, build, lint and actual browser exercise.
7. Materials/Chat: deterministic collection and local library work without a model; test scope,
   preservation of unread evidence, persisted warnings, restricted tools and write-only secrets.

Authenticated campus SSO/2FA and Google client registration cannot be validated without the
user's institutional access. An implemented connector is not evidence of a live verified one.
No third-party source has been copied; dependencies retain their own licenses.

## Reliability contracts

Deadline reminders are opt-in and local. The tray claims a batch through the protected
loopback API every 30 seconds; the web UI only changes preferences, reads the most recent
batch and snoozes. UTC instants govern thresholds; the configured IANA timezone governs
quiet hours. The engine reuses task completion/visibility projections and source deadline
change records, and compares effective local deadlines for rescheduling. SQLite write
transactions serialize claims, persist per-deadline deduplication and a 30-minute snooze,
and combine resume catch-up into one batch with a five-minute cooldown. Ordinary overdue
backlogs are skipped; a newly advanced deadline can still warn after it has passed.
Claim-before-delivery avoids duplicate notifications after restart; OS delivery is not
acknowledged, so the last batch remains reviewable and can be snoozed from Settings.
The registered reminders schema participates in paired backup and migration validation.
No source sync, OS permission, autostart setting or model call is triggered by reminders.

Task completion and field observations are defined in `coursedeck/task_state.py`.
`scripts/export_task_contract.py` exports the matching frontend JSON and TypeScript types;
CI rejects stale generated files. Raw platform statuses remain available independently of
their open/done/unknown interpretation. Explicitly unread or unknown fields retain cached
values; an explicitly empty field can clear a previous value.

Both UI patches and Chat commands use `TaskEdits` transactions. The UI exposes local edit
history and version-checked undo; Chat additionally binds permission to an unambiguous
task, action, fields and values, then rechecks that permission inside the transaction.
See `CHAT_TOOLS.md` for supported commands and limitations.

Schema components are registered centrally; see `DATABASE_MIGRATIONS.md`. Material cursors
belong to the knowledge database and therefore participate in paired backups/restores.
Material scheduling consumes connector capability declarations. Retries are bounded to
transient failures of an individual material read, preserving partial results and warnings.

SQLite revision observers invalidate relation, indexing and search caches after external
commits and restores. They hold no read transactions and are explicitly closed for
maintenance and shutdown. The application shares a knowledge store between Chat and
material collection. Workspace ETags combine database and runtime source state; polling
keeps cached content on failure and coalesces concurrent reloads without losing mutations.
