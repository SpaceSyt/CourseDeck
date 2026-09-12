import { useEffect, useRef, useState } from 'react';
import { Pencil, Trash2, X } from 'lucide-react';
import type { Course, MailRule, MailMessage } from './types';
import { api } from './api';

type Proposal = {
  operation: 'add' | 'update' | 'delete' | 'reset-defaults';
  id?: string;
  rule?: Omit<MailRule, 'id'>;
};
type ImpactState = {
  classification: string;
  course_id: string | null;
  categories: string[];
  ignored: boolean;
  attention: boolean;
  candidate_course_ids: string[];
};
type Impact = {
  counts: Record<string, number>;
  total: number;
  has_more: boolean;
  messages: {
    id: string;
    subject: string;
    sender: string;
    deleted: boolean;
    body_complete: boolean;
    manual_override: boolean;
    changed: boolean;
    before: ImpactState;
    after: ImpactState;
  }[];
};

export function MailRules({
  courses,
  close,
  changed,
  email,
}: {
  courses: Course[];
  close: () => void;
  changed: () => void;
  email?: MailMessage | null;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [rules, setRules] = useState<MailRule[]>([]);
  const [field, setField] = useState<MailRule['field']>(email?.sender_email ? 'sender' : 'subject');
  const [contains, setContains] = useState(
    (email?.sender_email || email?.subject || '').slice(0, 200),
  );
  const [action, setAction] = useState<MailRule['action']>('course');
  const [value, setValue] = useState(email?.course_id || '');
  const [editing, setEditing] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [priority, setPriority] = useState(false);
  const [review, setReview] = useState<{ proposal: Proposal; impact: Impact } | null>(null);
  const generation = useRef(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const load = () => api<MailRule[]>('/mail/rules').then(setRules);
  const invalidate = () => {
    generation.current++;
    setReview(null);
  };
  const resetForm = () => {
    setEditing(null);
    setContains('');
    setValue('');
    setEnabled(true);
    setPriority(false);
    invalidate();
  };
  const edit = (rule: MailRule) => {
    invalidate();
    setEditing(rule.id);
    setField(rule.field);
    setContains(rule.contains);
    setAction(rule.action);
    const course = courses.find((c) => c.id === rule.value || c.workspace_id === rule.value);
    setValue(rule.action === 'course' ? (course?.id ?? rule.value) : rule.value);
    setEnabled(rule.enabled);
    setPriority(!!rule.priority);
  };
  useEffect(() => {
    ref.current?.showModal();
    void load().catch((e) => setError(e.message));
    return () => {
      generation.current++;
    };
  }, []);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await load();
      changed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const preview = async (proposal: Proposal, more = false) => {
    const sequence = ++generation.current;
    setBusy(true);
    setError('');
    if (!more) setReview(null);
    try {
      const impact = await api<Impact>('/mail/rules/preview', 'POST', {
        ...proposal,
        offset: more ? (review?.impact.messages.length ?? 0) : 0,
      });
      if (generation.current === sequence)
        setReview((current) => ({
          proposal,
          impact: {
            ...impact,
            messages:
              more && current ? [...current.impact.messages, ...impact.messages] : impact.messages,
          },
        }));
    } catch (e) {
      if (generation.current === sequence) setError((e as Error).message);
    } finally {
      if (generation.current === sequence) setBusy(false);
    }
  };
  const apply = () => {
    if (!review || busy) return;
    const proposal = review.proposal;
    void run(async () => {
      if (proposal.operation === 'reset-defaults') await api('/mail/rules/reset-defaults', 'POST');
      else if (proposal.operation === 'delete') await api(`/mail/rules/${proposal.id}`, 'DELETE');
      else
        await api(
          proposal.id ? `/mail/rules/${proposal.id}` : '/mail/rules',
          proposal.id ? 'PUT' : 'POST',
          proposal.rule,
        );
      resetForm();
    });
  };
  const courseName = (key: string) =>
    courses.find((c) => c.id === key || c.workspace_id === key)?.name ?? 'Missing course';
  const stateLabel = (state: ImpactState) =>
    [
      state.classification === 'unclassifiable'
        ? `Cannot classify${state.candidate_course_ids.length ? ': ' + state.candidate_course_ids.map(courseName).join(', ') : ''}`
        : state.course_id
          ? courseName(state.course_id)
          : 'No category',
      ...state.categories,
      state.ignored ? 'Auto-ignored' : 'Not ignored',
      ...(state.attention ? ['Priority'] : []),
    ].join(' · ');
  return (
    <dialog ref={ref} className="course-dialog mail-rules" onCancel={close}>
      <div className="dialog-heading">
        <h2>Mail rules</h2>
        <button aria-label="Close mail rules" onClick={close}>
          <X size={18} />
        </button>
      </div>
      <div className="rule-list">
        {rules.map((rule) => (
          <div key={rule.id} className={editing === rule.id ? 'rule-editing' : ''}>
            <input
              type="checkbox"
              aria-label={`Enable rule ${rule.contains}`}
              checked={rule.enabled}
              disabled={busy}
              onChange={() => {
                const { id, ...payload } = rule;
                edit(rule);
                setEnabled(!rule.enabled);
                void preview({
                  operation: 'update',
                  id,
                  rule: { ...payload, enabled: !rule.enabled },
                });
              }}
            />
            <span>
              <b>{rule.contains}</b>
              <small>
                {rule.field} →{' '}
                {rule.action === 'course'
                  ? (courses.find((c) => c.id === rule.value || c.workspace_id === rule.value)
                      ?.name ?? 'Missing course')
                  : rule.action === 'category'
                    ? rule.value
                    : rule.action === 'none'
                      ? 'No category'
                      : 'Auto-ignore'}
                {rule.priority ? ' · Priority' : ''}
              </small>
            </span>
            <button
              disabled={busy}
              aria-label={`Edit rule ${rule.contains}`}
              onClick={() => edit(rule)}
            >
              <Pencil size={15} />
            </button>
            <button
              disabled={busy}
              aria-label={`Delete rule ${rule.contains}`}
              onClick={() => void preview({ operation: 'delete', id: rule.id })}
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </div>
      <button
        className="restore-mail-rules"
        disabled={busy}
        onClick={() => void preview({ operation: 'reset-defaults' })}
      >
        Restore default keywords
      </button>
      {review && (
        <section className="rule-preview" aria-label="Rule impact">
          <h3>
            {review.proposal.operation === 'delete'
              ? 'Delete rule'
              : review.proposal.operation === 'reset-defaults'
                ? 'Restore defaults'
                : review.proposal.rule?.enabled === false
                  ? 'Disable rule'
                  : 'Rule preview'}
          </h3>
          {review.proposal.operation !== 'reset-defaults' && (
            <p>
              <b>
                {review.proposal.rule?.contains ||
                  rules.find((rule) => rule.id === review.proposal.id)?.contains}
              </b>
            </p>
          )}
          <p>
            {review.impact.counts.cached} cached · {review.impact.counts.matched} matched ·{' '}
            {review.impact.counts.changed} changed
          </p>
          <div className="rule-impact-counts">
            <span>{review.impact.counts.newly_ignored} newly ignored</span>
            <span>{review.impact.counts.restored} restored</span>
            <span>{review.impact.counts.classification_changed} course changes</span>
            <span>{review.impact.counts.category_changed} category changes</span>
            <span>{review.impact.counts.attention_changed} priority changes</span>
            <span>{review.impact.counts.new_conflicts} new conflicts</span>
          </div>
          {!!review.impact.counts.incomplete && (
            <p className="warning">
              {review.impact.counts.incomplete} cached emails have incomplete content; matches may
              be missing.
            </p>
          )}
          {!!review.impact.counts.manual_overrides && (
            <p>{review.impact.counts.manual_overrides} matching emails keep manual choices.</p>
          )}
          <div className="rule-preview-list">
            {review.impact.messages.map((message) => (
              <article key={message.id}>
                <b>{message.subject || '(No subject)'}</b>
                <small>
                  {message.sender}
                  {message.deleted ? ' · Deleted locally' : ''}
                </small>
                <span>{stateLabel(message.before)}</span>
                <span>{message.changed ? '→ ' + stateLabel(message.after) : 'Unchanged'}</span>
              </article>
            ))}
          </div>
          {review.impact.has_more && (
            <button disabled={busy} onClick={() => void preview(review.proposal, true)}>
              More matches
            </button>
          )}
          <div className="dialog-actions">
            <button disabled={busy} onClick={invalidate}>
              Cancel
            </button>
            <button className="primary" disabled={busy} onClick={apply}>
              Apply changes
            </button>
          </div>
        </section>
      )}
      <form
        onChange={invalidate}
        onSubmit={(e) => {
          e.preventDefault();
          void preview({
            operation: editing ? 'update' : 'add',
            ...(editing ? { id: editing } : {}),
            rule: { field, contains, action, value, enabled, priority },
          });
        }}
      >
        <label>
          Match in
          <select
            aria-label="Match in"
            value={field}
            disabled={busy}
            onChange={(e) => {
              const next = e.target.value as MailRule['field'];
              setField(next);
              if (email && !editing && (next === 'sender' || next === 'subject'))
                setContains(
                  (next === 'sender' ? email.sender_email || email.sender : email.subject).slice(
                    0,
                    200,
                  ),
                );
            }}
          >
            <option value="sender">Sender</option>
            <option value="subject">Subject</option>
            <option value="body">Body</option>
            <option value="any">Anywhere</option>
          </select>
        </label>
        <label>
          Contains
          <input
            required
            minLength={2}
            maxLength={200}
            value={contains}
            disabled={busy}
            onChange={(e) => setContains(e.target.value)}
          />
        </label>
        <label>
          Apply
          <select
            aria-label="Apply"
            value={action}
            disabled={busy}
            onChange={(e) => {
              setAction(e.target.value as MailRule['action']);
              setValue('');
            }}
          >
            <option value="course">Course</option>
            <option value="category">Category</option>
            <option value="none">No category</option>
            <option value="ignore">Auto-ignore</option>
          </select>
        </label>
        {action === 'course' && (
          <label>
            Course
            <select
              aria-label="Course"
              required
              value={value}
              disabled={busy}
              onChange={(e) => setValue(e.target.value)}
            >
              <option value="">Select course</option>
              {courses.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {action === 'category' && (
          <label>
            Category
            <input
              required
              maxLength={100}
              disabled={busy}
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
        )}
        <label className="rule-priority">
          <input
            type="checkbox"
            checked={priority}
            disabled={busy}
            onChange={(e) => setPriority(e.target.checked)}
          />
          Mark as priority
        </label>
        {error && (
          <p className="warning" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          {editing && (
            <button type="button" disabled={busy} onClick={resetForm}>
              Cancel edit
            </button>
          )}
          <button type="button" onClick={close}>
            Done
          </button>
          <button className="primary" disabled={busy}>
            Preview changes
          </button>
        </div>
      </form>
    </dialog>
  );
}
