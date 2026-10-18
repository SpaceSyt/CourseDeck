# Verification scope

CourseDeck supports multiple courses and sources. Coverage depends on each
account's permissions, platform instance, course configuration and page layout.
Successful login, synchronization or automated tests do not prove complete coverage.
Follow [AGENTS.md](../AGENTS.md#安装与验收同步) for per-account acceptance.

## Automated verification

Backend tests cover parsing, stable identities, field uncertainty, cache retention,
local edits, migrations, material checkpoints and synchronization recovery.
Frontend checks cover types, formatting, production builds and UI behavior.
Isolated browser workflows exercise application behavior using synthetic data;
they do not establish coverage of a live account.

Run the release checks documented in [AGENTS.md](../AGENTS.md#验证与交付).
Use the CI result for the exact commit being installed; older test counts are not
proof that a later tree was verified. Platform and installation requirements are
listed in [USAGE.md](USAGE.md).

## Source boundaries

| Source | Scope to verify | Remaining boundaries |
| --- | --- | --- |
| Google Classroom | Current and archived courses, all Classwork themes, assignment details, visible submissions/grades, materials, Stream and accessible Docs | Questions, other attachments, language variants, unavailable grades, permissions and reading budgets can leave gaps. Failed field reads preserve cached values with unread metadata. |
| Gradescope | Current and historical student courses, every assignment row, deadlines and submission state | Detail pages, rubrics, attachments and other layouts require separate verification. Rows without stable identities remain visible as warnings. |
| WebAssign | Every course, All Assignments including history, deadlines and ordinary assignment submission details | Restricted or timed work is not entered. Detail reads rotate across sync rounds within a budget; unknown or incomplete question coverage cannot establish submission. Multi-course selectors and language variants require verification. |
| Rephactor | Current and inactive courses, published assignments, instructions, deadlines and accessible grades | Grades do not establish completion. Unpublished work, attendance and Easter Egg summaries are not assignments. |
| Brightspace | Course/term inventory, Assignments, every TOC level/page, structured deadlines, Quizzes, due-dated Discussions, News and accessible text/PDF | Permissions, unopened resources, conflicting states, external content, unsupported attachments and textless PDFs can leave gaps. Reading or opening material does not prove submission. |
| Gmail | Inbox pagination, historical rotation, all messages within threads, bodies and local filters | A single round does not read all history. Attachments and archived mail are outside scope. Opening messages in the browser may mark them read. |

## Integrity and efficiency

Synchronization preserves stable task identities, source deadlines, local notes
and cached content when reads fail. Missing, unread, conflicting and confirmed
empty results must remain distinct. Materials are stored separately; only explicit
tasks or structured deadlines create todos. Ordinary synchronization and Materials
do not require a model.

Incremental material reads reuse unchanged content. Bounded detail reads retain
progress across rounds so historical work can be revisited. These policies reduce
repeated work and improve eventual coverage; they do not certify that every field
was refreshed in one round or guarantee a particular sync duration.

## Per-account acceptance

Compare source pages and local records by stable identity, not only counts. Check
all pages, collapsed content, current/history filters, bodies, deadlines, timezones,
submission states and grades. Repeat synchronization after a repair to verify no
duplicate tasks, lost notes or unsupported completion/deletion transitions.

Report each source as verified, partially verified or unverifiable, with its
observed scope and unresolved gaps. Keep account-specific inventories, timestamps,
counts, screenshots and private findings in ignored `data/review/`; do not publish
course content, mail, credentials, browser profiles or authenticated URLs.
