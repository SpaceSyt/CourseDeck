# Connector research and remaining live validation

Updated 2026-09-07. A fixture proves parser behavior for that fixture, not compatibility with
every school instance. The user completed Classroom login locally; credentials were not shared in chat.

## Google Classroom

Default: dedicated browser session, preferring installed Chrome with a CourseDeck-only profile.
One real account validated course cards, Classwork Assignment rows, assignment details and Assigned
status. Waiting for the main landmark alone was too early; the reader now waits for data-bearing
elements and bounded DOM settling. Course-card headings take precedence over sidebar initials.
Deadline extraction observes the page's own batchexecute responses, requires matching course ID,
assignment ID, assignment type and title, and copies only exact timestamps. Response envelopes,
cookies and unrelated fields are not stored. This undocumented schema is guarded by synthetic tests.
Visible coverage remains partial: archived courses, Question, grades, non-English markup and large
class pagination/lazy loading still need validation. Missing fields preserve prior known values.

Optional official API:

Primary references:

- [Read-only student scopes](https://developers.google.com/workspace/classroom/guides/auth)
- [Desktop OAuth with loopback and PKCE](https://developers.google.com/identity/protocols/oauth2/native-app)
- [CourseWork date/time fields](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork)
- [Student submissions list](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions/list)

Uses courses.readonly and coursework.me.readonly. Only `studentId=me` courses and `userId=me`
submissions. Exhausts pagination, including the '-' coursework submissions resource. Courses
are fetched without an active-only filter so archived-course records are not falsely removed.
Zero assigned grades remain zero. Attachments, class rosters, emails and student passwords
are not requested by the official API adapter. Live API acceptance still requires Desktop OAuth;
the default browser path does not require a Cloud project.

## Gradescope

[nyuoss/gradescope-api](https://github.com/nyuoss/gradescope-api/tree/586ad513b6b4715682f09fc6fe3fff2e73304f74)
documents student retrieval via authenticated `/account` and `/courses/{id}` HTML.
Its current library exposes password login and teacher write methods; those do not match this
project's authentication/read-only boundary and were not used. LICENSE.md at this exact commit
was verified as MIT; see THIRD_PARTY_NOTICES.md.

Our transport reuses a dedicated Playwright context's request client. No public stable student
JSON assignment endpoint was established in this research. A conservative HTML parser handles
assignment IDs from links/buttons and explicit date attributes, including late-close dates.
Unidentified rows are reported, never assigned title-based identities. Need a live student
account to validate institution routing, no-course detection and current markup.

## WebAssign

- [Cengage: list assignments](https://help.cengage.com/webassign/student_guide/webassign/t_s_viewing_assignments.htm)
- [Cengage: submission semantics](https://webassign.com/support/student-support/faq/)

The student guide describes current/past assignment lists and local-time displays; the support
FAQ explains that questions may be submitted individually. These do not document an internal
JSON endpoint or enough current HTML structure to certify a production scraper.

The current adapter is explicitly experimental. It recognizes table links with stable
`dep`/`aid`/`assignmentId` parameters and dates with explicit machine-readable fields or a
user-verified format. Network observation stores only status/content-type counts, not requests,
credentials, query values or payloads. No endpoint is invented and labeled official.

Next live steps, using only the dedicated profile:

1. Open each course's complete assignment list, including past assignments.
2. Inspect DevTools Network Fetch/XHR and embedded JSON; establish pagination and identity.
3. Implement a JSON transport against observed read endpoints if available.
4. Create manually sanitized fixtures, covering assignments, extensions and submission state.
5. Compare task counts, IDs and every deadline against the course site; only then certify coverage.

This remaining work cannot be honestly completed without an authenticated WebAssign account.

## Brightspace

- [Own enrollments](https://docs.valence.desire2learn.com/res/enroll.html)
- [Dropbox folders, availability, own submissions and feedback](https://docs.valence.desire2learn.com/res/dropbox.html)
- [Read scopes](https://docs.valence.desire2learn.com/http-scopestable.html)
- [D2L token refresh example](https://community.d2l.com/brightspace/kb/articles/1105-brightspace-data-sets-headless-non-interactive-client-example)

API mode uses the supplied institutional OAuth token and optional refresh credentials.
Browser mode first tries the same API with the dedicated session; if the school denies it,
the browser transport attempts course tiles and assignment tables. It never uses another
student's submissions or instructor-grade export endpoints.

The initial API scope covers assignment dropboxes only. Full task aggregation also requires
quizzes/discussion deadlines and validation of special access/individual extensions. Enrollment
permissions can filter results even with HTTP 200, so full-provider completeness is never asserted.
The browser fallback is unverified and reports Partial. Empty unrecognized pages produce errors,
not successful empty snapshots. The school's base URL and login are still required.
