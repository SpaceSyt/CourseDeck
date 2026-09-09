import { useEffect, useRef, useState } from 'react';
import { Trash2, X } from 'lucide-react';
import type { Course, MailRule } from './types';
import { api } from './api';

export function MailRules({
  courses,
  close,
  changed,
}: {
  courses: Course[];
  close: () => void;
  changed: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [rules, setRules] = useState<MailRule[]>([]);
  const [field, setField] = useState<MailRule['field']>('subject');
  const [contains, setContains] = useState('');
  const [action, setAction] = useState<MailRule['action']>('course');
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const load = () => api<MailRule[]>('/mail/rules').then(setRules);
  useEffect(() => {
    ref.current?.showModal();
    void load().catch((e) => setError(e.message));
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
  return (
    <dialog ref={ref} className="course-dialog mail-rules" onCancel={close}>
      <div className="dialog-heading">
        <h2>Mail rules</h2>
        <button aria-label="Close mail rules" onClick={close}>
          <X size={18} />
        </button>
      </div>
      <p className="form-help">
        Course names match automatically. Conflicting matches stay visible as Cannot classify.
      </p>
      <div className="rule-list">
        {rules.map((rule) => (
          <div key={rule.id}>
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
              </small>
            </span>
            <button
              disabled={busy}
              aria-label={`Delete rule ${rule.contains}`}
              onClick={() => run(() => api(`/mail/rules/${rule.id}`, 'DELETE'))}
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await api('/mail/rules', 'POST', { field, contains, action, value });
            setContains('');
          });
        }}
      >
        <label>
          Match in
          <select
            aria-label="Match in"
            value={field}
            onChange={(e) => setField(e.target.value as MailRule['field'])}
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
            onChange={(e) => setContains(e.target.value)}
          />
        </label>
        <label>
          Apply
          <select
            aria-label="Apply"
            value={action}
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
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
        )}
        {error && (
          <p className="warning" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" onClick={close}>
            Done
          </button>
          <button className="primary" disabled={busy}>
            Add rule
          </button>
        </div>
      </form>
    </dialog>
  );
}
