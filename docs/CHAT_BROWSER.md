# Chat browser

Chat can inspect a task or course in the dedicated headless Chrome profile. Ask, for example,
“Open this assignment and find its rubric” or “Check what information is missing from this partial
task.” Choose the task or course first, or let Chat find its ID. No additional model configuration
is required beyond an OpenAI-compatible endpoint that supports tool calling.

## Workflow built into the system prompt

1. Read cached evidence with `get_task` / `search_knowledge`; determine what needs checking.
2. Use `browser_open(task_id=...)`, or `browser_courses` then
   `browser_open(source_course_id=...)`. Model-provided URLs and selectors are not accepted.
3. Read returned evidence and navigation targets. `browser_follow` accepts only an opaque target
   ID from the latest page snapshot. It follows a course link or uses a recognized reading
   control, such as Instructions, Next page or Load more. It cannot type or execute model code.
4. Use `browser_read(snapshot_id=..., offset=..., target_offset=...)` to read subsequent excerpts
   and targets. Reopening/navigating invalidates old controls; old text snapshots remain readable
   during the question. Evidence excerpts retain timestamps, source URLs and coverage warnings.
5. Supported Google Docs and same-course Brightspace static attachments discovered on a page
   use the existing bounded attachment reader. This closes the browser before reacquiring source
   locks. Other attachment formats and external interactive pages can remain unavailable.
6. Answer with citations and identify unread content, conflicts and uncertain associations.
   Browser observations do not update task deadlines/completion, certify sync completeness or
   automatically associate materials with tasks. They do not enter the local material library.

## Runtime boundaries

- This runs only during a Chat question. Automatic deterministic syncing is unchanged.
- Browser sessions are headless and use CourseDeck's source profiles, not the user's Edge profile.
  No focus changes or automatic login windows. An expired session reports a Sources reconnect
  action; the user explicitly opens login. Existing login windows must be finished first.
- The browser shares the engine queue/provider locks with sync and login. One profile is open
  per question at a time; changing source closes it first. Other sync work may wait while Chat
  browses. Attachment reads and material retrieval release the browser before taking those locks.
- At most ten browser actions per question, 45 seconds per action including queue wait; new
  actions stop after the 120-second browsing window. The existing overall 240-second Chat timeout
  also applies. Stop/cancel, exceptions and turn completion close contexts and release owned locks.
- Navigation is restricted to recognized reading URLs in the selected source course. WebAssign
  assignment URLs lacking a course ID must match a cached task URL from that course. Unknown
  deployments remain unverified. Session parameters stay server-side and are stripped from evidence.
- Controls are drawn from visible DOM/shadow DOM and selected by code. Submission controls and
  forms are excluded. Non-GET/HEAD/OPTIONS requests, known mutation routes, WebSockets, service
  workers, media and popups are blocked. Some platforms use POST for reading too: those requests
  currently remain blocked, with an explicit incomplete-reading warning. These restrictions are
  not a guarantee that all platforms treat every page visit as side-effect-free (view counters
  and read tracking can still change).
- Each snapshot checks at most eight frames and 60,000 characters / 200 controls per frame.
  Responses expose 6,500 text characters and 40 targets at a time. Inaccessible frames, excess
  content, blocked requests and loading timeouts are reported. No snapshot proves course coverage.
- Browser bodies stay in turn memory; replies, citations and Activity visits remain in local chat
  history. Relevant excerpts are sent to the user's configured model endpoint. Raw HTML, input
  values, cookies and authentication parameters are not provided as model tools or activity data.

## Verification

`tests/test_chat_browser.py` exercises real headless Chrome against intercepted fixture pages:
source scope, reading URLs, shadow DOM, expansion, paging, blocked writes, login failure,
cancellation, profile cleanup, attachment handoff, and a simulated model/tool/citation round trip.
`frontend/src/ChatActivity.test.tsx` verifies source links and coverage warnings in Activity.
These tests do not establish that every real LMS dynamic view works behind the read-only guard.
Real course data should be checked before claiming provider-wide browsing coverage.
