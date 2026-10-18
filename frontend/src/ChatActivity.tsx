import { Loader2 } from 'lucide-react';
import { mergeActivity, type ChatActivity } from './chat-stream';
import { MailDeletionPlan } from './MailDeletionPlan';

export function Activity({ items, busy = false }: { items: ChatActivity[]; busy?: boolean }) {
  const steps = items.reduce<ChatActivity[]>((result, item) => mergeActivity(result, item), []);
  const failures = steps.filter((step) => step.status === 'error').length;
  const plans = [
    ...new Map(
      steps
        .filter((step) => step.mail_deletion_plan)
        .map((step) => [step.mail_deletion_plan!.id, step.mail_deletion_plan!]),
    ).values(),
  ];
  const inboxActions: Record<string, string> = {
    delete: 'Deleted locally',
    restore: 'Restored locally',
    read: 'Marked read locally',
    unread: 'Marked unread locally',
  };
  const display = (value: unknown) => (value == null ? 'Source default' : String(value));
  const fields: Record<string, string> = {
    due_override: 'Deadline override',
    due_at_override: 'Deadline',
    completion_override: 'Local status',
    dismissed: 'Dismissed',
    note: 'Note',
  };
  const ruleFields: Record<string, string> = {
    match: 'Match',
    field: 'Match in',
    contains: 'Contains',
    action: 'Apply',
    value: 'Value',
    enabled: 'Enabled',
    priority: 'Priority',
  };
  const ruleValue = (rule: Record<string, unknown> | null | undefined, key: string) =>
    String(
      key === 'value' && rule?.action === 'course'
        ? (rule.course_name ?? rule.value ?? '—')
        : (rule?.[key] ?? '—'),
    );
  return (
    <>
      <details className="chat-activity">
        <summary>
          {busy && <Loader2 size={14} className="spin" />} {busy ? 'Working…' : 'Activity'}
          {failures > 0 && ` · ${failures} failed`}
        </summary>
        <ol>
          {steps.map((step, index) => (
            <li key={step.id ?? index}>
              <span>
                {step.label} ·{' '}
                {step.status === 'running' && !busy ? 'Interrupted' : (step.status ?? 'done')}
              </span>
              {step.detail && <p className="warning">{step.detail}</p>}
              {step.memory_change && (
                <div className="chat-change-receipt">
                  <span>
                    {step.memory_change.status === 'active'
                      ? 'Saved to memory'
                      : 'Needs confirmation in Memories'}
                  </span>
                  <div>{step.memory_change.text}</div>
                </div>
              )}
              {step.inbox_preview && (
                <p>
                  {step.inbox_preview.matched} matched · {step.inbox_preview.changed} to change
                </p>
              )}
              {step.inbox_change && (
                <div className="chat-change-receipt">
                  <span>
                    {inboxActions[step.inbox_change.action] ?? 'Inbox updated'} ·{' '}
                    {step.inbox_change.changed}
                  </span>
                  {step.inbox_change.messages.map((mail) => (
                    <div key={mail.id}>{mail.subject || '(No subject)'}</div>
                  ))}
                  {step.inbox_change.has_more && <span>{step.inbox_change.matched} matched</span>}
                </div>
              )}
              {step.browser_visit && (
                <div>
                  <a href={step.browser_visit.url} target="_blank" rel="noreferrer">
                    {step.browser_visit.title}
                  </a>
                  {' · '}
                  <time dateTime={step.browser_visit.checked_at}>
                    {new Date(step.browser_visit.checked_at).toLocaleTimeString()}
                  </time>
                  {step.browser_visit.warnings.map((warning) => (
                    <p key={warning} className="warning">
                      {warning}
                    </p>
                  ))}
                </div>
              )}
              {step.mail_rule_preview && (
                <p>
                  {step.mail_rule_preview.changed} affected · {step.mail_rule_preview.newly_ignored}{' '}
                  ignored
                  {' · '}
                  {step.mail_rule_preview.new_conflicts} conflicts
                </p>
              )}
              {step.mail_rule_change && (
                <div className="chat-change-receipt">
                  <span>
                    {!step.mail_rule_change.changed
                      ? 'No change'
                      : step.mail_rule_change.after
                        ? 'Mail rule saved'
                        : 'Mail rule deleted'}
                  </span>
                  {Object.entries(ruleFields)
                    .filter(
                      ([key]) =>
                        step.mail_rule_change!.before?.[key] !==
                        step.mail_rule_change!.after?.[key],
                    )
                    .map(([key, label]) => (
                      <div key={key}>
                        {label}: {ruleValue(step.mail_rule_change!.before, key)}
                        {' → '}
                        {ruleValue(step.mail_rule_change!.after, key)}
                      </div>
                    ))}
                </div>
              )}
              {step.change && (
                <div className="chat-change-receipt">
                  <span>
                    {step.change.undone
                      ? 'Local change undone'
                      : step.change.changed
                        ? 'Local change saved'
                        : 'No change'}
                  </span>
                  {step.change.after &&
                    Object.entries(fields)
                      .filter(([key]) => step.change!.after![key] !== step.change!.before?.[key])
                      .map(([key, label]) => (
                        <div key={key}>
                          {label}: {display(step.change!.before?.[key])} →{' '}
                          {display(step.change!.after![key])}
                        </div>
                      ))}
                </div>
              )}
            </li>
          ))}
        </ol>
      </details>
      {plans.map((plan) => (
        <MailDeletionPlan key={plan.id} initial={plan} />
      ))}
    </>
  );
}
