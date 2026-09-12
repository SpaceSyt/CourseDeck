import { useEffect, useState } from 'react';
import { api } from './api';
import type { Task } from './types';
import './task-relations.css';

type ItemKind = 'task' | 'mail' | 'document';
interface RelatedItem {
  kind: ItemKind;
  id: string;
  title: string;
  provider?: string;
  course_id?: string | null;
  url?: string | null;
  status: string;
  complete?: boolean | null;
}
interface Relation {
  id: string;
  state: 'automatic' | 'confirmed' | 'suggested' | 'rejected';
  active: boolean;
  target: RelatedItem;
  evidence: { kind: string; label: string }[];
  history: { action: string; created_at: string }[];
}
interface RelationPage {
  task_id: string;
  relations: Relation[];
  available: RelatedItem[];
}
interface Props {
  task: Task;
  onOpenTask: (id: string) => void;
  onOpenMail: (id: string) => void;
  onOpenDocument: (id: string) => void;
}

function safeLink(value?: string | null) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password)
      return undefined;
    for (const key of [...url.searchParams.keys()]) {
      if (
        /token|secret|pass|auth|session|signature|saml|credential/i.test(key) ||
        ['code', 'state', 'ticket'].includes(key.toLowerCase())
      )
        url.searchParams.delete(key);
    }
    url.hash = '';
    return url.href;
  } catch {
    return undefined;
  }
}

const names: Record<ItemKind, string> = { task: 'Task', mail: 'Email', document: 'Material' };

export function TaskRelations({ task, onOpenTask, onOpenMail, onOpenDocument }: Props) {
  const [page, setPage] = useState<RelationPage | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState('');
  const [selection, setSelection] = useState('');
  const path = `/associations/task/${encodeURIComponent(task.id)}`;

  useEffect(() => {
    let live = true;
    setPage(null);
    setError('');
    setAdding(false);
    setSelection('');
    setQuery('');
    api<RelationPage>(`${path}?include_rejected=true`)
      .then((value) => {
        if (live) setPage(value);
      })
      .catch((reason: Error) => {
        if (live) setError(reason.message);
      });
    return () => {
      live = false;
    };
  }, [path, task.last_seen_at]);

  async function decide(item: RelatedItem, action: 'link' | 'unlink' | 'confirm' | 'reject') {
    setBusy(true);
    setError('');
    try {
      const result = await api<RelationPage>(path, 'POST', {
        target_kind: item.kind,
        target_id: item.id,
        action,
      });
      // A parent may open a different task before the request returns.
      setPage((previous) => (previous?.task_id === result.task_id ? result : previous));
      if (action === 'link') {
        setAdding(false);
        setSelection('');
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not update links.');
    } finally {
      setBusy(false);
    }
  }

  function open(item: RelatedItem) {
    if (item.kind === 'task') onOpenTask(item.id);
    else if (item.kind === 'mail') onOpenMail(item.id);
    else onOpenDocument(item.id);
  }

  const relations = page?.task_id === task.id ? page.relations : [];
  const visible = relations.filter((relation) => showHistory || relation.state !== 'rejected');
  const linked = new Set(
    relations
      .filter((relation) => relation.active && relation.state !== 'suggested')
      .map((relation) => JSON.stringify([relation.target.kind, relation.target.id])),
  );
  const available = (page?.available ?? []).filter(
    (item) =>
      !linked.has(JSON.stringify([item.kind, item.id])) &&
      `${item.title} ${item.provider} ${names[item.kind]}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const selected = available.find((item) => JSON.stringify([item.kind, item.id]) === selection);
  const sourceUrl = safeLink(task.url);

  return (
    <section className="task-relations" aria-label="Task details and linked items">
      {task.description && <div className="task-relations-description">{task.description}</div>}
      {sourceUrl && (
        <a
          className="task-relations-source"
          href={sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open source ↗
        </a>
      )}
      <div className="task-relations-heading">
        <h3>Related items</h3>
        <button type="button" onClick={() => setAdding(!adding)} disabled={busy}>
          Link item
        </button>
      </div>
      {error && (
        <p role="alert" className="task-relations-error">
          {error}
        </p>
      )}
      {!page && !error && <p role="status">Loading links…</p>}
      {adding && (
        <form
          className="task-relations-add"
          onSubmit={(event) => {
            event.preventDefault();
            if (selected) void decide(selected, 'link');
          }}
        >
          <input
            aria-label="Find an item to link"
            placeholder="Find task, email or material"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setSelection('');
            }}
          />
          <select
            aria-label="Item to link"
            value={selection}
            onChange={(event) => setSelection(event.target.value)}
          >
            <option value="">Select item</option>
            {available.map((item) => (
              <option
                key={JSON.stringify([item.kind, item.id])}
                value={JSON.stringify([item.kind, item.id])}
              >
                {names[item.kind]} · {item.title} · {item.provider}
              </option>
            ))}
          </select>
          <button type="submit" disabled={!selected || busy}>
            Link
          </button>
        </form>
      )}
      <ul className="task-relations-list">
        {visible.map((relation) => {
          const item = relation.target;
          const url = safeLink(item.url);
          const candidate = relation.state === 'suggested';
          return (
            <li key={relation.id} className={!relation.active ? 'task-relation-inactive' : ''}>
              <div className="task-relation-main">
                <button className="task-relation-title" type="button" onClick={() => open(item)}>
                  {item.title}
                </button>
                <span className="task-relation-meta">
                  {names[item.kind]} · {item.provider} · {item.status.replaceAll('_', ' ')}
                </span>
                {candidate && (
                  <span className="task-relation-candidate">Suggested · Needs confirmation</span>
                )}
                {!relation.active && relation.state !== 'rejected' && (
                  <span>Link inactive · Item or course unavailable</span>
                )}
                {relation.state === 'rejected' && <span>Dismissed link</span>}
              </div>
              <div className="task-relation-actions">
                {url && (
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${item.title} at source`}
                  >
                    ↗
                  </a>
                )}
                {candidate && (
                  <>
                    {relation.active && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void decide(item, 'confirm')}
                      >
                        Confirm
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void decide(item, 'reject')}
                    >
                      Dismiss
                    </button>
                  </>
                )}
                {relation.state === 'rejected' && (
                  <button type="button" disabled={busy} onClick={() => void decide(item, 'link')}>
                    Restore
                  </button>
                )}
                {!candidate && relation.state !== 'rejected' && (
                  <button type="button" disabled={busy} onClick={() => void decide(item, 'unlink')}>
                    Unlink
                  </button>
                )}
              </div>
              <details className="task-relation-evidence">
                <summary>Evidence</summary>
                {relation.evidence.map((evidence, index) => (
                  <p key={index}>{evidence.label}</p>
                ))}
                {showHistory &&
                  relation.history.map((event, index) => (
                    <p key={index}>
                      {event.action.replaceAll('_', ' ')} ·{' '}
                      {new Date(event.created_at).toLocaleString()}
                    </p>
                  ))}
              </details>
            </li>
          );
        })}
      </ul>
      {page && visible.length === 0 && <p>No linked items.</p>}
      {relations.length > 0 && (
        <label className="task-relations-history">
          <input
            type="checkbox"
            checked={showHistory}
            onChange={(event) => setShowHistory(event.target.checked)}
          />
          Show dismissed links and history
        </label>
      )}
    </section>
  );
}
