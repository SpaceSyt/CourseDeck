# Inbox and custom tasks

CourseDeck supports one Gmail mailbox using its own Chrome profile. Sign in through
Inbox → Connect Gmail, complete school SSO, then choose Finish login. No OAuth
project or AI service is required. The account is fixed after the first connection;
connecting a different account is rejected to prevent mixing cached mail.

Each sync caches all visible headers on the latest inbox page and one historical
page, then attempts up to eight conversation bodies per page. The historical
page checkpoint survives restarts and cycles back after the last verified page.
Read attempts rotate so a failing conversation cannot permanently block others.
Both pages' headers are committed before body reads; each page's body work has
an independent time budget. Unknown pagination leaves the checkpoint unchanged.
It syncs on startup when enabled, every five minutes while running, and through
Inbox → ⋯ → Sync mail. Archived mail, attachments and sending are not supported.
This covers the displayed Inbox list, not every mailbox folder or alternate
category/view. Gmail DOM changes may require adapter updates.
Opening threads to read their body can mark them read in Gmail.

Dates come from the source's full timestamp; unrecognized dates retain their
previous order or sort below existing mail and produce a coverage warning.
Changed or unreadable conversations retain cached text with an incomplete/stale
indicator. Stale text does not establish current classifications or automatic
task associations. The status reports unread cached bodies separately from
listed headers; successfully visiting every page does not prove complete bodies.

Mail deletion, starring, restoring ignored mail and course overrides only change
the local database. Disconnect removes the dedicated browser session, retaining
cached messages, rules and task links. No AI is used and no mail is sent to an AI
service. Bodies render as text, without remote images, scripts or HTML execution.

## Classification

- **Classified:** one course matches its name or a user-defined rule. Renamed
  local courses also match their original source name.
- **No category:** readable content matches no course, or the user explicitly
  chooses no course. This is not an error.
- **Cannot classify:** content is incomplete, course matches conflict, or a rule
  references an unavailable course. These messages remain visible.

Rules match sender, subject, body or all text, using case-insensitive literal
phrases and word boundaries. They can assign a course/category, mark no category,
or auto-ignore. All matching course rules are considered, so ordering cannot
silently resolve a conflict. Built-in `action required` and `survey` keywords
only add category labels. There are no built-in ignore rules.

Inbox → ⋯ controls visibility of locally deleted and auto-ignored messages.
Restoring an ignored message gives it a persistent local exception. Removing an
ignore rule immediately re-evaluates cached messages. An incomplete or ambiguous
message is never automatically ignored. Course assignment can be overridden in
the message detail and reset to Automatic.

## Tasks and storage

Add task creates an independent local task with optional course, email and due
date. Add to do in an email prefills the subject and course; it does not infer
deadlines. Dates are entered in the browser's timezone, displayed by the date
field, and shown in the workspace's chosen timezone in the task list. Custom
tasks can be edited, completed, pinned, hidden and annotated. Multiple tasks can
reference the same email. Deleting mail locally does not delete linked tasks.

SQLite schema version 4 adds `custom_tasks`, `mail_messages` and `mail_rules`.
Source task identity, data and local notes are preserved. Email bodies have a
separate detail endpoint and are absent from `/api/snapshot`. Mail lists are
paginated. LMS sync does not overwrite custom tasks. Course links survive binding
a local course to its remote course. Existing database backups include the new
tables; no separate cloud storage is used.

Run `python -m pytest`, frontend tests/build, and `python -m scripts.smoke_ui`.
Browser smoke uses only a temporary database; its sample messages never enter
the user's workspace.
