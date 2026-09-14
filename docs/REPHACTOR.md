# Rephactor

Connect through Sources → Rephactor, sign in to a student account in the dedicated
browser, then choose Finish login. Session cookies are kept in the system credential
store and restored into the dedicated browser for automatic sync; no plaintext
credential file is created.
CourseDeck keeps the connection tied to the original account to avoid mixing data.

The connector reads active and inactive courses, then the complete assignment
list for each course using the same read endpoints as the student Dashboard.
Published assignments are included regardless of whether they appear under
Pending or Past. Unpublished assignments and attendance/Easter Egg grade
aggregates are excluded, matching the Dashboard's assignment lists. It does not
change the platform's current course or submit answers.

Assignment IDs, descriptions and explicit UTC due dates are retained. Available
scores are displayed, but scores, teacher grading flags and Pending/Past sections
do not establish student completion. Completion remains unknown. Exercise tasks
link to their assignment page; Quick Check tasks link to Dashboard. Use course
management to bind this source to the same local course as another platform.

Unknown fields retain cached values. Unread course lists, malformed records,
authentication failures and verified empty lists remain distinct. Only fully
read course assignment lists can establish a previously seen task's absence.
Rephactor course materials and per-question completion are not currently read.

Validation includes synthetic parsing/session/coverage regressions and a dedicated
student-browser comparison of ten published assignments, including their IDs and
UTC deadlines. Broader course combinations and completion reporting remain
unverified. Reference: [Rephactor](https://www.rephactor.com/).
