import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Mail,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Star,
  Trash2,
  Undo2,
} from 'lucide-react';
import { api } from './api';
import { courseColor } from './colors';
import type { Course, MailMessage, MailPage } from './types';
import { MailRules } from './MailRules';

function linkedText(text: string) {
  return text.split(/(https?:\/\/[^\s<>"\u2028]+)/g).map((part, index) =>
    /^https?:\/\//i.test(part) ? (
      <a key={index} href={part} target="_blank" rel="noreferrer">
        {part}
      </a>
    ) : (
      part
    ),
  );
}

export function Inbox({
  courses,
  addTask,
  openId,
  opened,
}: {
  courses: Course[];
  addTask: (email: MailMessage) => void;
  openId: string | null;
  opened: () => void;
}) {
  const [data, setData] = useState<MailPage | null>(null);
  const [selected, setSelected] = useState<MailMessage | null>(null);
  const [deleted, setDeleted] = useState(false);
  const [ignored, setIgnored] = useState(false);
  const [offset, setOffset] = useState(0);
  const [rules, setRules] = useState(false);
  const [ruleEmail, setRuleEmail] = useState<MailMessage | null>(null);
  const [attentionOnly, setAttentionOnly] = useState(false);
  const [priorityFirst, setPriorityFirst] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const menu = useRef<HTMLDetailsElement>(null);
  const request = useRef(0);
  const detailRequest = useRef(0);
  const load = useCallback(async () => {
    const version = ++request.current;
    try {
      const page = await api<MailPage>(
        `/mail?deleted=${deleted}&ignored=${ignored}&offset=${offset}&attention_only=${attentionOnly}&order=${priorityFirst ? 'attention' : 'newest'}`,
      );
      if (version === request.current) setData(page);
    } catch {
      if (version === request.current)
        setError('Inbox unavailable. Retry when the local service reconnects.');
    }
  }, [deleted, ignored, offset, attentionOnly, priorityFirst]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 10000);
    return () => {
      clearInterval(timer);
      request.current++;
    };
  }, [load]);
  const open = useCallback(
    async (id: string) => {
      const version = ++detailRequest.current;
      setLoading(true);
      setError('');
      setSelected(null);
      try {
        const mail = await api<MailMessage>(`/mail/messages/${encodeURIComponent(id)}`, 'PATCH', {
          read: true,
        });
        if (version === detailRequest.current) {
          setSelected(mail);
          void load();
        }
      } catch (e) {
        if (version === detailRequest.current) setError((e as Error).message);
      } finally {
        if (version === detailRequest.current) setLoading(false);
      }
    },
    [load],
  );
  useEffect(() => {
    if (openId) {
      void open(openId);
      opened();
    }
  }, [openId, open, opened]);
  useEffect(
    () => () => {
      detailRequest.current++;
    },
    [],
  );
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError('');
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const patch = (message: MailMessage, values: Record<string, unknown>) =>
    run(async () => {
      const result = await api<MailMessage>(
        `/mail/messages/${encodeURIComponent(message.id)}`,
        'PATCH',
        values,
      );
      setSelected((current) => (current?.id === result.id ? result : current));
    });
  const connection = data?.connection;
  const label = (mail: MailMessage) =>
    mail.classification === 'none'
      ? 'No category'
      : mail.classification === 'unclassifiable'
        ? 'Cannot classify'
        : (courses.find((c) => c.id === mail.course_id)?.name ?? 'Cannot classify');
  const dot = (mail: MailMessage) => (
    <i
      className={`mail-dot ${mail.classification}`}
      style={{ backgroundColor: courseColor(courses.find((c) => c.id === mail.course_id)) }}
      title={label(mail)}
      aria-label={label(mail)}
    />
  );
  const connect = (operation: string) => run(() => api(`/mail/connection/${operation}`, 'POST'));
  return (
    <aside className="inbox-rail" aria-label="Inbox">
      <header className="inbox-heading">
        <h2>Inbox</h2>
        <details ref={menu} className="inbox-menu">
          <summary aria-label="Inbox menu">
            <MoreHorizontal size={21} />
          </summary>
          <div className="inbox-menu-panel">
            <label>
              <input
                type="checkbox"
                checked={deleted}
                onChange={(e) => {
                  setDeleted(e.target.checked);
                  setOffset(0);
                }}
              />
              Show deleted
            </label>
            <label>
              <input
                type="checkbox"
                checked={ignored}
                onChange={(e) => {
                  setIgnored(e.target.checked);
                  setOffset(0);
                }}
              />
              Show auto-ignored
            </label>
            <label>
              <input
                type="checkbox"
                checked={attentionOnly}
                onChange={(e) => {
                  setAttentionOnly(e.target.checked);
                  setOffset(0);
                }}
              />
              Priority only
            </label>
            <label>
              <input
                type="checkbox"
                checked={priorityFirst}
                onChange={(e) => {
                  setPriorityFirst(e.target.checked);
                  setOffset(0);
                }}
              />
              Priority first
            </label>
            <button
              onClick={() => {
                setRules(true);
                setRuleEmail(null);
                menu.current?.removeAttribute('open');
              }}
            >
              Mail rules
            </button>
            <button
              disabled={busy || connection?.syncing || connection?.status !== 'connected'}
              onClick={() => connect('sync')}
            >
              <RefreshCw size={14} />
              Sync mail
            </button>
            {connection?.status === 'connected' && (
              <button disabled={busy || connection.syncing} onClick={() => connect('disconnect')}>
                Disconnect Gmail
              </button>
            )}
            {connection?.account && <small>{connection.account}</small>}
            {connection?.warning && <small>{connection.warning}</small>}
          </div>
        </details>
      </header>
      {(attentionOnly || priorityFirst) && (
        <div className="inbox-view-filters">
          {attentionOnly && (
            <button
              aria-label="Clear priority filter"
              onClick={() => {
                setAttentionOnly(false);
                setOffset(0);
              }}
            >
              Priority only ×
            </button>
          )}
          {priorityFirst && (
            <button
              aria-label="Sort emails by newest"
              onClick={() => {
                setPriorityFirst(false);
                setOffset(0);
              }}
            >
              Priority first ×
            </button>
          )}
        </div>
      )}
      {error && (
        <p className="inbox-error" role="alert">
          {error}
        </p>
      )}
      {connection?.error && (
        <div className="inbox-error" role="alert">
          {connection.error}
          <button
            disabled={busy || connection.syncing}
            onClick={() => connect(connection.error_code === 'auth_required' ? 'connect' : 'sync')}
          >
            {connection.error_code === 'auth_required' ? 'Reconnect' : 'Retry sync'}
          </button>
        </div>
      )}
      {connection?.status !== 'connected' && data && (
        <div className="mail-connect">
          <button
            disabled={busy}
            onClick={() =>
              connect(connection?.status === 'login_pending' ? 'finish-login' : 'connect')
            }
          >
            <Mail size={16} />
            {connection?.status === 'login_pending' ? 'Finish login' : 'Connect Gmail'}
          </button>
          {connection?.status === 'login_pending' && <p>Sign in in the Gmail window.</p>}
        </div>
      )}
      {connection?.syncing && (
        <div className="mail-sync">
          <RefreshCw size={13} className="spin" />
          Syncing mail…
        </div>
      )}
      {loading ? (
        <p className="mail-empty">Loading email…</p>
      ) : selected ? (
        <div className="mail-detail">
          <div className="mail-detail-actions">
            <button
              aria-label="Back to inbox"
              onClick={() => {
                detailRequest.current++;
                setSelected(null);
              }}
            >
              <ArrowLeft size={18} />
            </button>
            <button
              disabled={busy}
              aria-label={selected.starred ? 'Unstar email' : 'Star email'}
              onClick={() => patch(selected, { starred: !selected.starred })}
            >
              <Star size={17} fill={selected.starred ? 'currentColor' : 'none'} />
            </button>
            <button
              disabled={busy}
              aria-label={selected.deleted ? 'Restore email' : 'Delete email locally'}
              onClick={() => patch(selected, { deleted: !selected.deleted })}
            >
              {selected.deleted ? <Undo2 size={17} /> : <Trash2 size={17} />}
            </button>
          </div>
          <h3>{selected.subject || '(No subject)'}</h3>
          <div className="mail-from">
            <b>{selected.sender}</b>
            <span>{selected.sender_email}</span>
            <time>{selected.date_label}</time>
          </div>
          <label className="mail-course">
            {dot(selected)}
            <select
              aria-label="Email course"
              value={selected.course_override}
              onChange={(e) => patch(selected, { course_id: e.target.value })}
              disabled={busy}
            >
              <option value="auto">
                {selected.course_override === 'auto' ? label(selected) : 'Automatic'}
              </option>
              <option value="none">No category</option>
              {courses.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          {selected.classification === 'unclassifiable' && (
            <p className="inbox-error">
              {selected.classification_reason || 'Course could not be determined'}
            </p>
          )}
          {!!selected.categories.length && (
            <div className="mail-tags">
              {selected.categories.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </div>
          )}
          {(selected.deleted || selected.ignored) && (
            <div className="mail-flags">
              {selected.deleted && <span>Deleted locally</span>}
              {selected.ignored && (
                <button disabled={busy} onClick={() => patch(selected, { ignored: false })}>
                  Auto-ignored · Restore
                </button>
              )}
            </div>
          )}
          <div className="mail-task-actions">
            <button className="primary" onClick={() => addTask(selected)}>
              <Plus size={16} />
              Add to do
            </button>
            <button
              onClick={() => {
                setRuleEmail(selected);
                setRules(true);
              }}
            >
              Create rule
            </button>
          </div>
          {!selected.body_complete && <p className="inbox-error">Email content incomplete</p>}
          <div className="mail-body">
            {linkedText(selected.body || selected.snippet || 'No text content')}
          </div>
          {/^https:\/\/mail\.google\.com\//.test(selected.url) && (
            <a href={selected.url} target="_blank" rel="noreferrer">
              Open in Gmail
              <ArrowUpRight size={14} />
            </a>
          )}
        </div>
      ) : (
        <>
          <div className="mail-list">
            {data?.messages.map((mail) => (
              <article
                key={mail.id}
                className={`mail-row ${mail.unread ? 'unread' : ''} ${mail.deleted || mail.ignored ? 'inactive' : ''}`}
              >
                <button
                  className="mail-open"
                  onClick={() => open(mail.id)}
                  aria-label={`Open email: ${mail.subject || '(No subject)'}`}
                >
                  {dot(mail)}
                  <span className="mail-lines">
                    <span className="mail-sender">{mail.sender || mail.sender_email}</span>
                    <span className="mail-subject">{mail.subject || '(No subject)'}</span>
                    <span className="mail-snippet">
                      {mail.classification === 'unclassifiable' && <em>Cannot classify · </em>}
                      {mail.deleted ? 'Deleted · ' : mail.ignored ? 'Ignored · ' : ''}
                      {mail.attention && (
                        <em className="mail-priority">
                          {mail.attention_reasons?.[0] || 'Priority'} ·{' '}
                        </em>
                      )}
                      {mail.snippet}
                    </span>
                  </span>
                </button>
                <div className="mail-row-side">
                  <time title={mail.date_label}>
                    {mail.date_text ||
                      (mail.received_at
                        ? new Intl.DateTimeFormat('en-US', {
                            month: 'short',
                            day: 'numeric',
                          }).format(new Date(mail.received_at))
                        : mail.date_label)}
                  </time>
                  <button
                    disabled={busy}
                    className={mail.starred ? 'starred' : ''}
                    aria-label={`${mail.starred ? 'Unstar' : 'Star'} ${mail.subject}`}
                    onClick={() => patch(mail, { starred: !mail.starred })}
                  >
                    <Star size={18} fill={mail.starred ? 'currentColor' : 'none'} />
                  </button>
                </div>
              </article>
            ))}
          </div>
          {data && !data.messages.length && (
            <p className="mail-empty">
              {connection?.syncing
                ? 'Waiting for mail…'
                : attentionOnly
                  ? 'No priority emails'
                  : connection?.status === 'connected'
                    ? 'No emails here'
                    : 'No cached emails'}
            </p>
          )}
          {data && (data.has_more || offset > 0) && (
            <div className="mail-pagination">
              <button
                aria-label="Previous emails"
                disabled={!offset}
                onClick={() => setOffset(Math.max(0, offset - 50))}
              >
                <ChevronLeft size={16} />
              </button>
              <span>
                {offset + 1}–{offset + data.messages.length} / {data.total}
              </span>
              <button
                aria-label="Next emails"
                disabled={!data.has_more}
                onClick={() => setOffset(offset + 50)}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          )}
        </>
      )}
      {rules && (
        <MailRules
          courses={courses}
          email={ruleEmail}
          close={() => setRules(false)}
          changed={() => {
            void load();
            if (selected) void open(selected.id);
          }}
        />
      )}
    </aside>
  );
}
