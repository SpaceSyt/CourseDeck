# Five workflow improvements

This is the acceptance record for the user-authorized five-part iteration. Existing local
data, source-task identities, notes and explicit deadlines must survive every change.
No part of this scope requires a model for normal operation. Completion requires working
UI/API flows and regression evidence, not just the presence of new modules.

## 1. Sync accuracy and changes

- [x] Persist before/after task changes with source evidence and observation time.
- [x] Distinguish new work, deadline advances/extensions, reopened work, state/body changes,
      verified absence and unread fields; failed retrieval never fabricates source changes.
- [x] Baseline existing installations and deduplicate repeated snapshots/notifications.
- [x] Provide searchable/filterable change history, unread state and prominent critical changes.
- [x] Retry transient failures without duplicate work; preserve cache and visible failure state.

## 2. Cross-source task context

- [x] Link tasks, source descriptions, submission entries, relevant mail and materials within
      current course bindings using explicit identities/links first.
- [x] Present ambiguous number/name matches as candidates, with evidence and confirm/reject.
- [x] Support manual links/unlinks and preserve decisions across sync and course merges.
- [x] Keep separate source task identities and submission states, including attempts/repeated work.
- [x] Open every linked task, email, material or source from assignment details.

## 3. Find materials and ask grounded questions

- [x] Search title/full text without AI; filter by course, source, kind, freshness and completeness.
- [x] Show source/read times, unread content and retained-cache conditions accurately.
- [x] Surface associated/relevant descriptions, readings and announcements in task context.
- [x] Pass selected task context and verifiable references to optional Chat without implicit mail
      disclosure or cross-course retrieval; retain conflicts and uncertainty.

## 4. Inbox rules and attention

- [x] Create a prefilled editable rule directly from a message.
- [x] Preview affected cached messages, counts and classification/ignore changes before applying;
      preview is read-only and does not fetch mail.
- [x] Expose action/reply/survey/deadline-change attention through editable deterministic rules,
      with user-controlled filtering/order and normal chronological inbox behavior by default.
- [x] Keep manual overrides, conflicts and recoverable ignored/deleted messages visible.

## 5. Recovery and maintenance

- [x] Identify authentication, transport, parsing and partial-coverage failures with actionable stages.
- [x] Use bounded, serialized retries and recover after temporary network failure without loops.
- [x] List/verify paired backups with a manifest, integrity checks and schema compatibility.
- [x] Preview and restore a selected backup, preserve a pre-restore backup, and roll back both files
      on failure; reject arbitrary paths or modified/untrusted backup files.
- [x] Quiesce HTTP writes/Chat/source/Gmail work during restore and resume scheduled operation safely.
- [x] Extend synthetic connector/workflow regressions; validate the integrated desktop/mobile UI
      and retained real local data without exporting credentials or personal fixtures.

## Verification evidence

Completed on 2026-09-10. `python -m pytest -q`: 370 passed; `npm run test`: 6 passed.
TypeScript/Vite production build, Ruff lint/format and Prettier passed.

Browser commands (after building `frontend/dist`):

```console
python -m scripts.smoke_ui
python -m scripts.smoke_chat_ui
python -m scripts.smoke_changes_ui
python -m scripts.smoke_associations_ui
python -m scripts.smoke_material_search
python -m scripts.smoke_mail_rules_ui
python -m scripts.smoke_recovery_ui
```

All seven passed. The recovery smoke also reproduces long legacy backup names and verifies
collapsed/expanded error details at 390px. Real local UI checks covered task associations,
task context, Changes, Settings and mobile layout after the final build.

The updated service completed startup synchronization. A local before/after audit preserved
74 source task IDs, 4 local-state records, 62 mail records and 148 knowledge records; both
databases passed integrity/foreign-key checks. The migrated change log had no false new-task
events. Real association discovery produced 12 explicit task/document links and retained
uncertain matches as suggestions. The new paired backup passed preflight; real data was never
restored. Independent review added regressions for a second interrupted restore, unknown
journals, failed browser shutdown and duplicate background polling.

Source coverage remains bounded: Classroom/Brightspace are still partial; source-hidden
fields, unread external files and Gmail's latest-30-thread window remain documented gaps.
These workflows preserve and expose those limits rather than certify full platform coverage.
Chat retrieval and citations have mock-model verification; no request was sent to the user's
configured model during this iteration. Detailed evidence is in [VERIFICATION.md](VERIFICATION.md).
