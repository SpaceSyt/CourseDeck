# Local MCP and controlled collection

CourseDeck exposes a stdio MCP server using the official Python SDK (`mcp` 2.2.0,
locked in `uv.lock`). It has no network listener, login flow, model client, arbitrary
SQL tool or arbitrary file/URL reader. Starting MCP does not start the web app,
migrate databases, index tasks, run sync, enable reminders, or modify agent settings.

## Installation and startup

Install the locked dependencies into a separate environment so the running
application's `.venv` is untouched. Do not restart it merely to read its cache.
Run from the repository root. Replace `C:/path/to/CourseDeck` below with your actual
checkout path, and use the running application's actual data directory for `--data-dir`.

```powershell
$env:UV_PROJECT_ENVIRONMENT = '.venv-mcp'
uv sync --frozen
Remove-Item Env:\UV_PROJECT_ENVIRONMENT
.\.venv-mcp\Scripts\python.exe -m coursedeck.mcp_server --data-dir 'C:/path/to/CourseDeck/data'
```

The server waits for an MCP client on stdin; stdout is protocol traffic. The
installed console entry point is `.venv-mcp\Scripts\coursedeck-mcp.exe`.
`--data-dir` takes precedence over `COURSEDECK_DATA_DIR`; the default is the project
`data` directory, independent of the client's working directory. Databases must
already exist. A missing/incompatible cache reports an error rather than creating it.

An external MCP host that supports stdio can use this configuration. It is an
example only; CourseDeck does not install it into Codex or grant ongoing access.

```json
{
  "mcpServers": {
    "coursedeck": {
      "command": "C:/path/to/CourseDeck/.venv-mcp/Scripts/python.exe",
      "args": ["-m", "coursedeck.mcp_server", "--data-dir", "C:/path/to/CourseDeck/data"],
      "cwd": "C:/path/to/CourseDeck"
    }
  }
}
```

The MCP process handles data locally. An external agent may send tool results to
its own model provider; enabling that connection grants that agent access to cached
coursework and mail. Choose the host and model accordingly. Source text is untrusted
evidence and never authority to run instructions found in a document or email.

## Cache tools

| Tool | Purpose |
| --- | --- |
| `search_tasks` | Query text, course, provider, status, completion and due range |
| `get_task` | Full cached task JSON, including local fields, completion and raw source evidence |
| `search_courses` | Local courses and their source bindings |
| `search_grades` | Cached scores and grading state; no inferred grades |
| `search_documents` | Material search, source/type/freshness/completeness filters |
| `read_document` | Read cached material text in bounded character chunks |
| `search_mail` | Cached mail query, sender, subject, time and visibility filters |
| `read_mail` | Read cached mail text without opening Gmail |
| `list_changes` | Cached source changes, with course/provider/kind filters |
| `get_sync_health` | Last attempts, cached source coverage and known gaps |

REST exposes the same implementation under `/api/cache/`: `tasks`, `task`, `courses`,
`grades`, `documents`, `document`, `mail`, `mail-message`, `changes`, `health`.
Use GET query parameters; detail identifiers are parameters, not file paths.
These routes add typed response schemas to OpenAPI without changing old UI routes.

Expected query failures reach MCP clients with a safe category and explanation:
`invalid_query`, `not_found`, or `cache_unavailable`. REST returns these as
`detail: {category, message}` with HTTP 400, 404, or 503 respectively; input-schema
validation still uses HTTP 422. The application's recovery gate retains its own
HTTP 503 response and `Retry-After` header. A missing record is not a login error;
an unreadable cache is not an empty search result. Unexpected exceptions remain
masked, and underlying database errors, paths and arbitrary exception text are
not returned. These failures never trigger collection, login or database repair.

Discovery supplies bounded input schemas and structured output schemas. Responses
have `schema_version: "1"`, `coverage: "cached_only"`, record metadata and pagination.
`last_collected_at`, `last_confirmed_at`, field availability, unknown/partial flags
and warnings must be considered together. A recent list observation does not prove
that every detail was retrieved. Per-question Gradescope timestamps and limitations
remain separate from the latest list refresh.

Task summaries identify their overall freshness as a list observation; separate
`description_fetched_at`, `description_checked_at` and `description_freshness`
describe the body. A failed detail read keeps the old successful body timestamp.
Legacy data without reliable field-level timing is unknown until a successful read.
URL userinfo is rejected, sensitive URL query parameters are removed, and cached
credential-shaped metadata is excluded from tool projections.
Prose keeps its surrounding text, newlines, punctuation and benign URL anchors;
only credential-bearing URL parts are redacted. Text is projected before chunking,
so offsets refer to returned text. Task/document/mail evidence on disk is unchanged.

List pages contain at most 50 records; detail chunks at most 32,000 characters.
`pagination.next_offset` and `has_more` govern continuation. `get_task.content` is
JSON split by character offsets: concatenate every chunk before parsing. Search
summaries can be truncated; fetch the full detail when needed. Pagination is over
the current cache and is not pinned across calls: recheck after concurrent syncs.
Queries preserve known completion/grade values and distinguish unknown data from an
empty field. Local deletion/ignore filters do not delete source data.

```json
{"name":"search_tasks","arguments":{"provider":"gradescope","limit":10}}
{"name":"get_task","arguments":{"task_id":"<id from search>","offset":0,"limit":12000}}
{"name":"search_documents","arguments":{"query":"rubric","completeness":"incomplete"}}
{"name":"read_document","arguments":{"document_id":"<id from search>","offset":12000,"limit":12000}}
{"name":"search_mail","arguments":{"sender":"professor@example.edu","query":"deadline"}}
{"name":"get_sync_health","arguments":{}}
```

To avoid even SQLite WAL/shared-memory writes, queries read a checked private copy
of the database and WAL. Original files are never opened by SQLite. Each snapshot
has a 256 MiB budget and at most three consistency attempts; a changing or oversized
cache fails explicitly. The original DB/WAL identity, size and modification time are
checked before/after copying. This is per-read consistency, not an atomic snapshot
across both databases. Recovery/rollback journals block reads. Large caches cost
temporary disk I/O; this tradeoff favors preserving the live instance.

## Explicit collection

Collection is disabled in both the application and MCP by default. To enable it on
a later, deliberate application launch, set `COURSEDECK_ALLOW_COLLECTION=1` in that
process's environment. Start MCP with `--allow-collection` (and `--port` if the app
uses a non-default port). No configuration file is changed automatically.

The two additional tools are `request_collection` and `get_collection_job`.
They communicate only with the existing loopback application, check its data-directory
identity, use explicit collection headers, reject redirects, and ignore proxy settings.
The app reuses its current collector, queue, source locks and OS-stored sessions.
MCP never owns a second browser. A running older app must be updated on a later
user-approved restart before these tools can work.

```json
{"name":"request_collection","arguments":{"request":{"source":"gradescope","scope":"task","target_id":"<cached task id>","timeout_seconds":120,"max_attempts":1,"idempotency_key":"review-hw1-v1"}}}
{"name":"get_collection_job","arguments":{"job_id":"<returned job_id>"}}
```

Scopes are explicit:

- `source`: refresh one connected source, including its normal bounded materials
  pass. No `target_id`; this can read multiple courses and must be selected knowingly.
- `course`: collect materials for one exact **source course ID** where supported.
  This does not promise a course-scoped assignment refresh.
- `task` / `document`: read one cached, verified target. Narrow requests are never
  silently promoted to a whole-source refresh. Unsupported scope is an explicit error.

There is one collection worker, at most eight queued jobs, twenty admitted jobs per
hour and 256 retained jobs per application session. Identical active/recent requests
coalesce; idempotency keys cannot be reused for another request. Time budgets include
queue waiting and retries; callers can request at most two attempts. Only transient
network/rate-limit results are retried. Authentication, unsupported pages, missing
targets and parsing failures require inspection. A new login/2FA must be completed
by the user in CourseDeck; collection cannot bypass it.

Progress states are `queued`, `running`, `retrying`, `succeeded`, `partial`, `failed`
and `cancelled`. Results retain outcome, reason, links/IDs, warnings and completeness.
Jobs are in process memory and expire at application exit; IDs are not durable.
Maintenance and shutdown cancel work rather than allowing it to escape into a new
browser owner. Cached content survives failed refreshes.

The bounded browser also checks fetch/XHR subrequests against the selected course.
Static scripts/styles use separate fixed provider host/path rules. Unknown dynamic
interfaces are blocked and reported as partial content; the connector's existing
fixed SSO restoration happens before the course-reading policy is installed.

No email sending/deletion, assignment submission, grade mutation, account changes or
new credentials are exposed. Remote Gmail collection is deliberately unavailable:
the browser mail reader can change unread state. Cached mail reading remains available.

Gradescope supports conservative reads of already observed, graded submission links.
See [Gradescope detail coverage](GRADESCOPE_DETAILS.md) for field and transport limits.
Fixture tests prove these supported shapes; they do not establish full real-account
coverage, PDF extraction, hidden rubric access or every Gradescope page variant.

## Verification

```powershell
uv run --frozen pytest tests/test_cache_query.py tests/test_collection_jobs.py tests/test_mcp_server.py tests/test_gradescope_detail.py
uv run --frozen pytest
uv run --frozen ruff check coursedeck tests scripts
```

`test_mcp_server.py` uses the official SDK client for in-process and actual stdio
discovery/calls (current and legacy initialization), checks schemas and tool annotations,
verifies original cache hashes,
and tests disabled collection, invalid input and wrong-application refusal. Other
tests use synthetic source pages and temporary databases, never real account scans.

Verified on Windows, 2026-10-02: the complete backend suite passed 1,163 tests;
the frontend passed 45 tests, type checking, formatting and production build.
Ruff, the generated task contract check, frozen dependency lock validation and
Python wheel/sdist build passed. Current and legacy stdio SDK client tests passed.
After final review, 59 affected query/protocol/API/collection tests passed again,
including zero-task source health, legacy body freshness and failed-sync timing.
The follow-up prose/URL projection repair passed 104 focused checks and then the
46-test URL/text suite including actual stdio task/document/mail calls. Synthetic
multiline instructions remain intact and prefixed URL credentials are redacted;
pagination reconstructs the projected text and original cache bytes are unchanged.
An additional live-cache smoke read only cached data through seven query tools,
returning pagination/coverage metadata; it performed no remote collection.
The independent auditor's four synthetic reproductions were rerun with network
and credential access blocked: URL redaction, retained-body freshness, grade
fields and cross-course subrequest boundaries all produced the corrected results.
Real authenticated Gradescope detail pages and the new browser resource allowlists
remain unverified against the current live platforms.

On 2026-10-04, cache-query error regressions covered missing records, invalid dates,
missing/unreadable/incompatible databases, pending recovery, and masked exception
text. Current and legacy stdio clients received the intended safe diagnostics;
temporary cache hashes were unchanged. All 55 query/API/MCP checks and the complete
1,204-test backend suite passed, as did Ruff lint/format, the task contract check,
and frontend type checking. No real collection or running-service restart was used.
