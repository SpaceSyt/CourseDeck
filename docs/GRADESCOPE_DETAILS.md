# Gradescope detail collection boundaries

Normal source sync continues to read assignment lists only. It records whether
an assignment URL actually appeared as a link (`detail_url_observed`). A targeted
collection job may call `collect_task_detail(context, task, base_url=...)` using
the existing authorized, serialized browser session, then
`store_task_detail(db, task.id, result)` to save evidence on that task alone.

The collector requires both `graded=True` and `submission_status="graded"`.
Older cached tasks without an observed-link marker need an ordinary source list
refresh before detail collection. This prevents fabricated URLs and opening
unsubmitted or timed assessments. It permits at most two HTTP GET requests:
the observed assignment URL and, if present, its explicitly labelled submission
view link. Both must be HTTPS, same-origin, and match the selected course and
assignment. Redirects, queries, arbitrary URLs, edit/regrade/submission actions,
JavaScript execution, private API guessing and external asset downloads are
excluded. Login/permission and rate-limit failures are returned explicitly.
If the first page supplies evidence but the linked second page requires login,
the result remains `partial` with that evidence and a
`login_or_permission_required` warning. This does not claim access to the second page.

The HTML adapter reads question/score tables with explicit column labels and
explicitly labelled rubric, feedback and submitted-code/answer sections.
Zero scores remain zero; absent scores remain unknown. Rubric deductions are
never summed to invent grades. Each field includes availability and a source
URL; observations include collection/confirmation times and truncation. Limits
per page are 2 MB HTML, 40,000 extracted text characters and 200 question rows. The HTTP
client buffers a response before enforcing the HTML limit; this is a parser
budget, not a streaming download-byte guarantee.

All detail results are **partial**: no authenticated Gradescope detail DOM was
available for live validation in this implementation. Fixtures verify semantic
HTML extraction, not current Gradescope selector coverage. JavaScript-rendered
views, additional question views, scanned/PDF submission contents and hidden
rubrics may therefore remain unavailable with `not_present_in_readable_html`.
An empty field does not imply the assignment has no feedback or no submission.

Official Gradescope documentation confirms that publication and instructor
visibility settings affect what students can read: see
[Reviewing Grades](https://guides.gradescope.com/hc/en-us/articles/22067099093517-Reviewing-Grades)
and [Grading submissions with rubrics](https://guides.gradescope.com/hc/en-us/articles/22249389005709-Grading-submissions-with-rubrics).
Those documents establish feature/permission boundaries, not HTML selectors or
permission to access unpublished content.

Successful readable evidence is stored in `raw_data.gradescope_detail`; the
latest attempt is also stored in `raw_data.gradescope_detail_attempt`. Failed or
empty attempts do not erase previously collected evidence. When a later partial
read omits a formerly readable field, that field stays cached with availability
`stale`, reason `cached_not_reconfirmed`, and its prior confirmation time. Subsequent list
sync preserves these independent observations and their original timestamps.
Detail collection does not change task grades, completion, local notes, source
sync timestamps, or other tasks. No real account was remotely scraped by tests.
