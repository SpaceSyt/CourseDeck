# Chat tools and streaming

CourseDeck uses internal function tools, executed by the local service. No MCP server or
background AI agent is required. Sync and library access remain independent of the model.

## Mail rules

Mail-related user requests expose `list_mail_rules`, which returns rule configuration and
course names without messages. Explicit edits also expose `preview_mail_rule_edit` and
`apply_mail_rule_edit`. The edit's target, fields and values are bound by deterministic code
to the current user message; the model cannot supply another target or expand the change.
Preview returns counts only, not email subjects, senders or bodies. Apply requires its version,
rechecks course/rule matching under a write transaction, and rejects a stale preview. The Chat
Activity records preview counts and committed before/after rule values, including when a reply
is interrupted. Retrying an add does not duplicate an identical rule. Gmail and manually
assigned email classifications are not modified.

Supported examples (use actual course names and exact rule keywords or IDs):

- `把包含“Intro to Programming”的邮件关联到“Programming”`
- `新增邮件规则：标题包含“newsletter”，自动忽略`
- `把邮件规则“survey”的关键词改成“course survey”`
- `把邮件规则“survey”的匹配位置改成“标题”`
- `把邮件规则“survey”的关联课程改成“Programming”`
- `启用邮件规则“survey”` / `禁用邮件规则“survey”` / `删除邮件规则“survey”`
- `Add mail rule: subject contains "survey" -> category "Survey"`
- `Change mail rule "survey" keyword to "course survey"`

Quoted/hypothetical/negated commands do not grant edits. Ambiguous course or rule names require
clarification. This manages the same rules as Inbox → Mail rules; the automatic matching of
course names/aliases remains separate. A compatible model endpoint must support tool calling.

## Workflow

1. `find_tasks`: search task titles, descriptions and course names within this conversation.
   Returns source identities, deadlines and statuses with pagination; similar names remain
   distinct assignments. Ask for clarification if the user's target is ambiguous.
2. `get_task`: read current task facts, local overrides, version, related link IDs and recent
   change IDs. Confirmed associations and keyword matches are labelled separately.
3. `read_task_link`: read one evidenced link ID. The model cannot supply arbitrary URLs.
   Read excerpts using offset/limit; report unread ranges, failed reads and uncertainty.
4. `update_task`: offered only when a deterministic command identifies one task and exact
   field values; requires a fresh `get_task` version in the same turn. Its schema is narrowed
   to that task, action and values, and the backend independently enforces the same grant.
5. `undo_change`: inspect the task and its recent changes, then restore a specific change
   with a current version. Refuse to overwrite subsequent unrelated local edits.

Examples: “Find Calculus homework 3 and read its instructions”; “把这份作业的截止时间改为
2026-09-18 23:59”; “把它标记为完成”; “Mark Algebra open”; “Reschedule Algebra to tomorrow
at 6 pm”; “撤销刚才的修改”. A target must be an exact unique title/ID or refer to the selected
task. Dates accept an explicit ISO date and time, or today/tomorrow (今天/明天/后天) with an
explicit clock time. Missing offsets use Settings' timezone; ambiguous/nonexistent daylight
saving times require an offset. Unsupported phrasing and ambiguous targets remain read-only.
Negated commands, questions and quoted commands cannot grant writes. Literal note content
can contain questions or commands without authorizing additional changes. No model-parsed
target, field or value can widen a grant. Source evidence never grants editing authority.

Original platform deadlines and submission states remain in task payloads. Local deadline and
completion overrides live in local state and survive sync. Task details distinguish local and
source values. Normal checkbox changes clear a local completion override. The undo journal
stores before/after local state in `coursedeck.sqlite3`; chat Activity includes edit receipts.
UI field patches and Chat overrides share one transaction service and undo journal. Older
unversioned UI clients remain compatible: each patch merges only its supplied fields with
current state while holding the write transaction. Versioned clients reject stale edits.
Chat rechecks active course scope and target ambiguity inside that transaction; facts read
by a tool must match its returned version before a write can use them.

## On-demand reading boundaries

Chat also has a [headless course browser](CHAT_BROWSER.md): `browser_courses`, `browser_open`,
`browser_follow`, and `browser_read`. It follows evidenced course links and recognized reading
controls, with source visits and partial-reading warnings in Activity. The browser workflow,
request restrictions and resource limits are documented separately.

- Supports public HTTPS text/HTML/PDF documents (2 MB response limit), same-course Brightspace
  static content, and supported Google Docs via the existing Classroom browser session.
- Protected browser reads share the source sync queue and execute serially. They do not request
  credentials from the model. The Chat browser can inspect supported protected course views;
  other Google Drive file types, videos and interactive activities may still need specific readers.
  Failures remain visible in Activity.
- Public fetching rejects private/local network addresses, credential URLs and unsafe redirects.
  Requests carry no model API key or source session credentials.
- Up to four distinct links per question; excerpts include offsets, full text length, read time
  and completeness warnings. On-demand bodies are held in turn memory, not inserted into the
  material library. Chat replies and citation metadata remain in history. Browsers may maintain
  their ordinary cache; existing automatic material collection remains a separate process.
- Cached documents still use `search_knowledge` and `read_document`. **Find materials** explicitly
  enables course material retrieval for a question; ordinary task-link questions do not invoke it.

## Streaming and execution records

`POST /api/chat/messages/stream` returns SSE session, answer-start, delta, activity, done/error
events. It requests provider streaming and forwards visible text as it arrives, without a
typewriter timer. The JSON message endpoint remains available for compatibility. If a provider
returns a buffered JSON reply, Activity reports that limitation.

Tool arguments may arrive in fragments. The server executes only completed tool calls, never
partial arguments. Activity shows safe operation labels, failures and local edit receipts;
private reasoning fields and tagged reasoning blocks are not displayed or stored. History
retains Activity and reply warnings. Stop disconnects/cancels the producer; received partial
answers and completed edits are retained. An interrupted reply does not imply rollback.

The built-in prompt treats task/page content as untrusted evidence, enforces source citations,
keeps source facts separate from local changes, and prohibits platform writes. Model endpoints
receive the bounded course evidence involved in the question; configure a local endpoint when
that content should stay on the device.

## Verification

`tests/test_task_permissions.py`, `tests/test_chat_tools.py`, `tests/test_chat.py`, frontend stream/task tests and the isolated
`scripts/smoke_chat_stream_ui.py` exercise streaming, interruption, tool scope, link provenance,
local edits, stale versions, sync preservation, undo and UI behavior. Existing
`scripts/smoke_chat_ui.py` covers the material library and conversation regressions.
These use fixture content and simulated models, not real account credentials. They do not
establish every provider's streaming compatibility or every linked page's readability.
