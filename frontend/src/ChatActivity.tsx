import { Loader2 } from 'lucide-react';
import { mergeActivity, type ChatActivity } from './chat-stream';

export function Activity({ items, busy = false }: { items: ChatActivity[]; busy?: boolean }) {
  const steps = items.reduce<ChatActivity[]>((result, item) => mergeActivity(result, item), []);
  const failures = steps.filter((step) => step.status === 'error').length;
  const display = (value: unknown) => (value == null ? 'Source default' : String(value));
  const fields: Record<string, string> = {
    due_override: 'Deadline override',
    due_at_override: 'Deadline',
    completion_override: 'Local status',
    dismissed: 'Dismissed',
    note: 'Note',
  };
  const ruleFields: Record<string, string> = {
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
                      step.mail_rule_change!.before?.[key] !== step.mail_rule_change!.after?.[key],
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
  );
}
