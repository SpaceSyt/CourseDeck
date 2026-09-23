# Chat tools and streaming

CourseDeck uses internal function tools, executed by the local service. No MCP server or
background AI agent is required. Sync and library access remain independent of the model.

## Conversations

Each conversation has its own draft, reply stream, Stop control and error state. Starting or
opening another conversation, or visiting another CourseDeck page, keeps existing replies running
while the app remains open. Returning to a loaded conversation uses its in-memory history immediately.
Closing or reloading the browser disconnects active replies; saved messages and completed local
changes remain in history. This is not a background job service that runs after the client closes.

The history controls support rename and deletion. Deletion removes the conversation, its messages
and Activity records, without deleting tasks, mail or course materials, and without undoing local
changes made in Chat. Stop an active reply before deleting its conversation. The server rejects
concurrent replies in the same conversation and allows replies in different conversations.

## How evidence is found

Automatic source collectors store supported course materials and their completeness metadata in
the local knowledge database. Chat retrieves from this cache using normalized Chinese/English
keywords and phrases, with title matches weighted above body matches, then reads matching excerpts
and additional text ranges. The search is lexical; no embedding model or vector database is used.

The model can reformulate a query using shorter keywords, synonyms and the language of the source
material, page through local search results, and use the course browser when cached evidence is
missing, incomplete or insufficient for the question. Browser navigation starts with known course,
task or material identities and follows evidenced reading links. It does not turn an empty search
into a claim that the source contains nothing. Tool results report pagination and evidence limits;
source permissions, unsupported formats and expired sessions remain explicit limitations.

Short retry requests reuse the preceding substantive question for retrieval, without replaying
its permission to change local data. Exact repeated reads reuse the turn's earlier tool result.
After six retrieval rounds, or two rounds without new findings, Chat asks the model to answer
from the verified evidence and identify remaining uncertainty. Material warnings follow the
documents actually read; source connection and sync failures remain visible.

This is a retrieval-augmented answering workflow. Semantic embeddings would add another retrieval
method, not replace source collection, course scoping, deadline facts, citations or browser reading.
Current sync and material access require neither a model nor embedding configuration.

## Personal memory

The Memories view manages durable user preferences and personal course context separately from
conversation history and source materials. Memories are stored in the local knowledge database,
included in its backups, and survive deleting a conversation. Export produces a Markdown snapshot;
editing that export does not change the database. Viewing, adding, editing, approving and deleting
memories require no model configuration.

In Automatic mode, Chat can use `remember_memory` to retain clearly stated, useful long-term
information. It must supply an exact supporting quote from the current user message. Uncertain
suggestions, or all suggestions in confirmation mode, remain pending until approved in Memories.
Pending entries are never supplied as remembered facts. Activity retains the saved or pending
receipt even if the answer is interrupted. Chat cannot edit or delete existing memories; these
actions are available in Memories with version checks to prevent concurrent overwrites.

General preferences apply across courses; course memories apply within their course scope.
Each answer receives a bounded excerpt of active memories and can use `search_memories` to find
others. Search remains local and does not require embeddings. Memories are user context, not
evidence of deadlines, grades, completion or current course content. Current user corrections take
precedence; conflicting entries should be edited rather than accumulated. Source documents,
email instructions, guessed facts, credentials and temporary task details are not personal memory.

Each entry retains its supporting quote, origin, timestamps and course scope. Chat-created entries
also retain source conversation and message IDs. Source text remains in the memory when its
conversation is deleted. The store limits the number and size of entries and reports capacity
limits explicitly; deletion takes effect for subsequent Chat turns. A request already sent to
a model cannot have its previously supplied context recalled.

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

## Inbox controls

**Inbox → ⋯ → Inbox filters** manages explicit, local exclusion rules. Choose Sender, Subject,
Body or Anywhere and Contains/Equals. Sender defaults to exact equality; keyword filters use
case-insensitive substring matching. Preview counts before saving. Filters apply to existing
cache and future synced mail without requiring AI or modifying Gmail. Matching mail is hidden
from default Inbox but remains available under **Show auto-ignored** and can be restored.

These rules use `action=filter`, independent of course classification: a positive sender or
content match can filter a mail even when its course is unknown. Missing/stale body text cannot
be guessed into a match. Manual restores remain visible across sync. Disabling or deleting a
filter reveals mail again unless another rule or local override still hides it. Existing
`action=ignore` rules retain their conservative classification behavior.

Chat supports `过滤发件人为“sender@example.edu”的邮件`, `过滤包含“newsletter”的邮件`,
`添加Inbox过滤规则：标题包含“Newsletter”`, and `禁用过滤规则“newsletter”` through the existing
versioned mail-rule preview/apply tools. Filters are persistent rules, unlike one-time local deletion.

Chat always has `search_inbox` and `read_inbox_mail` available for mail requests and their
follow-ups, including a date supplied after an earlier question. These read **cached** mail metadata
and body excerpts. Reading through Chat
does not mark mail as read. Search supports pagination and inclusion of locally deleted/ignored
mail; body reads require an ID returned by search in the current turn. Cached body evidence can
be cited, with incomplete/stale content identified. A selected course limits results to mail
associated with that course; use an unscoped conversation for unclassified or all Inbox mail.

Search accepts keywords, exact sender, subject substring, and `before`/`after` dates in YYYY-MM-DD.
Dates use Settings' timezone: before excludes midnight on that date; after includes it. Missing or
unreliable source dates are counted and excluded from date filters, never replaced with cache time.

`preview_inbox_edit` and `apply_inbox_edit` support local restoration and read/unread changes,
bound to the current user instruction. Examples:

- `恢复邮件“邮件标题或 ID”`
- `把邮件“邮件标题或 ID”标为已读` / `把邮件“邮件标题或 ID”标为未读`
- `把所有未读邮件标为已读`
- `Mark all unread emails as read`

Ordinary batches exclude locally deleted and ignored messages. Explicit single-message targets
can include either state. Ambiguous subjects require an ID; unclear criteria such as “unimportant”
do not grant an edit. Preview lists up to 20 subjects and the full matched/changed counts; all
matched messages are handled atomically, up to 2,000 per operation. Larger selections must be
narrowed. Model-supplied extra fields/IDs cannot widen the authorized edit.

Apply requires the preview version. Source updates, local changes or matching-set changes reject
a stale preview. A repeated apply in one turn returns its existing receipt. **Only local_payload
is updated**; Gmail connectors, browsers, source payloads and linked tasks are untouched. Gmail
sync preserves these local overrides. Local deletion can be reversed through Chat or Show deleted.
Activity distinguishes preview from committed changes; Inbox refreshes via GET after a receipt
or interrupted turn, preserving explicit unread flags.

`tests/test_chat_inbox.py` covers these boundaries, source preservation across sync, stale previews,
batch rollback, course scoping and the model/tool loop. No live Gmail access is needed to edit Inbox.

### Reviewed deletion

`preview_mail_deletion` accepts a destination (`local` or `gmail`) and either IDs returned by
search in this turn or deterministic search filters. It creates a visible, reviewable card without
deleting anything. The model has no tool to execute deletion. The user must click the card's
destination-specific confirmation button. For example: “本地删除 2026-07-31 之前的邮件” or
“把 2026-07-31 之前的邮件移入 Gmail 垃圾箱”. An unspecified destination or year needs clarification.

Plans bind the displayed mail versions and selected IDs. Concurrent changes invalidate the preview;
newly arriving matches cannot be silently added. Ordinary searches exclude local deleted/ignored
mail unless explicitly included, and only cover the synced cache, not the entire Gmail account.
One local plan includes at most 2,000 cached emails; a Gmail plan includes at most 20 conversations.
The card reports additional matches and unknown dates so a partial batch is never presented as all mail.
Unexecuted plans expire after one hour or a service restart and must be prepared again.

Local confirmation atomically updates only local deletion flags. Gmail confirmation uses the existing
dedicated browser, verifies the connected account and each conversation's identity, then uses the
visible Trash control. Gmail conversation deletion includes every message in that conversation.
Opening it for verification can mark it read. Only verified Trash membership is reported as success
and hidden locally; failures or unverified outcomes remain visible and are not automatically retried.
Permanent deletion, sending, and other Gmail account modifications are not exposed. Restore Gmail
conversations in Gmail itself; a local restore affects only CourseDeck.

Final outcomes are retained in Chat Activity. Tests use synthetic Gmail pages for actual clicks;
real-page inspection verifies identity and control selectors without deleting personal mail.

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
- Cached documents still use `search_knowledge` and `read_document`. **Refresh materials** explicitly
  requests an upfront course material refresh. Local search and targeted browser reading are
  available without that toggle; the model may request a course refresh when cached evidence is
  insufficient and a selected course supports retrieval. A single task-link question should use
  its evidenced link before requesting a broader refresh.

Mail citation controls read the current local cache through a conversation-scoped citation route;
only mail cited in that conversation and still within its course scope can be opened. Chat reads
do not mark mail as read. On-demand browser and attachment citations open their original source;
their full text is not a persistent library document.

## Streaming and execution records

`POST /api/chat/messages/stream` returns SSE session, answer-start, delta, activity, done/error
events. It requests provider streaming and forwards visible text as it arrives, without a
typewriter timer. The JSON message endpoint remains available for compatibility. If a provider
returns a buffered JSON reply, Activity reports that limitation.

Both endpoints request an 8,192-token output budget. A provider's `length` finish reason is
reported as `output_limit`; its incomplete tool calls are not executed or automatically retried.

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

`tests/test_task_permissions.py`, `tests/test_chat_tools.py`, `tests/test_chat.py` and
`tests/test_chat_reliability.py` exercise tool scope, link provenance, local edits, stale versions,
sync preservation, undo, concurrent replies, deletion, cancellation and retrieval limits.
Frontend session/stream tests and `scripts/smoke_chat_stream_ui.py` cover simultaneous streams,
cross-page navigation, draft/error isolation, history caching, deletion, stopping and scroll position.
`scripts/smoke_chat_ui.py` covers the material library, citations and conversation regressions.
These use fixture content and simulated models, not real account credentials. They do not
establish every provider's streaming compatibility or every linked page's readability.
