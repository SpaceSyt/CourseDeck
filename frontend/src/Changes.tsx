import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Check, RefreshCw } from 'lucide-react';
import type { Course } from './types';
import { api } from './api';
import './changes.css';

interface Change {
  id: number;
  task_id: string;
  course_id: string;
  source_course_id: string;
  provider: string;
  title: string;
  kind: string;
  severity: 'critical' | 'info';
  before: unknown;
  after: unknown;
  observed_at: string;
  read_at: string | null;
  evidence: {
    url: string | null;
    source_updated_at?: string | null;
    last_seen_at: string;
    outcome: string;
  };
}
interface ChangePage {
  changes: Change[];
  total: number;
  unread_count: number;
  critical_unread_count: number;
}
const kinds: Record<string, string> = {
  added: 'New task',
  deadline_earlier: 'Deadline moved earlier',
  deadline_delayed: 'Deadline extended',
  deadline_changed: 'Deadline changed',
  reopened: 'Reopened',
  status_changed: 'Status changed',
  status_confirmed: 'Status confirmed',
  body_changed: 'Instructions changed',
  title_changed: 'Title changed',
  missing: 'Missing from source',
  unrefreshed: 'Not refreshed',
  restored: 'Source available again',
  fields_unavailable: 'Fields unavailable',
  fields_restored: 'Fields available again',
};

function sourceUrl(value: string | null) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

export function Changes({
  courses,
  revision,
  onOpenTask,
  timezone,
}: {
  courses: Course[];
  revision?: number;
  onOpenTask?: (taskId: string) => void;
  timezone?: string;
}) {
  const [page, setPage] = useState<ChangePage | null>(null);
  const [courseId, setCourseId] = useState('');
  const [provider, setProvider] = useState('');
  const [kind, setKind] = useState('');
  const [query, setQuery] = useState('');
  const [unread, setUnread] = useState(false);
  const [critical, setCritical] = useState(false);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const live = useRef(true);
  const visibleCourses = courses.filter((course) => !course.disabled && !course.deleted);
  const providers = [
    ...new Set(visibleCourses.flatMap((course) => course.providers ?? [course.provider])),
  ];

  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    try {
      const params = new URLSearchParams({
        offset: String(offset),
        limit: '50',
        unread: String(unread),
        critical: String(critical),
      });
      if (courseId) params.set('course_id', courseId);
      if (provider) params.set('provider', provider);
      if (kind) params.set('kind', kind);
      if (query) params.set('query', query);
      const result = await api<ChangePage>(`/changes?${params}`);
      if (live.current && request === generation.current) {
        setPage(result);
        setError('');
      }
    } catch (failure) {
      if (live.current && request === generation.current) setError((failure as Error).message);
    } finally {
      if (live.current && request === generation.current) setLoading(false);
    }
  }, [courseId, provider, kind, query, unread, critical, offset]);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      generation.current += 1;
    };
  }, []);
  useEffect(() => {
    void load();
  }, [load, revision]);

  const mark = async (change?: Change) => {
    setBusy(true);
    setError('');
    try {
      if (change) await api(`/changes/${change.id}`, 'PATCH', { read: !change.read_at });
      else
        await api('/changes/read', 'POST', {
          ids: page?.changes.filter((item) => !item.read_at).map((item) => item.id) ?? [],
        });
      if (live.current) await load();
    } catch (failure) {
      if (live.current) setError((failure as Error).message);
    } finally {
      if (live.current) setBusy(false);
    }
  };
  const changeFilter = (set: (value: string) => void, value: string) => {
    setOffset(0);
    set(value);
  };
  const date = (value: string) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? value
      : parsed.toLocaleString(undefined, {
          dateStyle: 'medium',
          timeStyle: 'short',
          timeZone: timezone,
        });
  };
  const valueText = (value: unknown, isDate = false): string => {
    if (value === null || value === undefined) return 'None';
    if (typeof value === 'string') return isDate ? date(value) : value.replaceAll('_', ' ');
    if (Array.isArray(value))
      return value.map((item) => String(item).replaceAll('_', ' ')).join(', ') || 'None';
    if (typeof value === 'object') {
      const item = value as {
        title?: string;
        due_at?: string | null;
        submission_status?: string;
        status_known?: boolean;
      };
      return [
        item.title,
        item.due_at ? date(item.due_at) : 'No deadline',
        item.status_known ? item.submission_status?.replaceAll('_', ' ') : 'Status unknown',
      ]
        .filter(Boolean)
        .join(' · ');
    }
    return String(value);
  };
  return (
    <section className="changes-view" aria-label="Source changes">
      <div className="changes-heading">
        <div>{page && <span>{page.unread_count} unread</span>}</div>
        <div className="changes-actions">
          <button
            onClick={() => void load()}
            disabled={loading || busy}
            aria-label="Refresh changes"
          >
            <RefreshCw size={16} />
          </button>
          <button
            onClick={() => void mark()}
            disabled={busy || loading || !page?.changes.some((change) => !change.read_at)}
          >
            <Check size={16} /> Mark shown read
          </button>
        </div>
      </div>
      <div className="changes-filters">
        <input
          type="search"
          aria-label="Search changes"
          placeholder="Search changes"
          value={query}
          onChange={(event) => changeFilter(setQuery, event.target.value)}
        />
        <select
          aria-label="Filter changes by course"
          value={courseId}
          onChange={(event) => changeFilter(setCourseId, event.target.value)}
        >
          <option value="">All courses</option>
          {visibleCourses.map((course) => (
            <option key={course.id} value={course.id}>
              {course.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter changes by source"
          value={provider}
          onChange={(event) => changeFilter(setProvider, event.target.value)}
        >
          <option value="">All sources</option>
          {providers.map((item) => (
            <option key={item} value={item}>
              {item.replaceAll('_', ' ')}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter change type"
          value={kind}
          onChange={(event) => changeFilter(setKind, event.target.value)}
        >
          <option value="">All changes</option>
          {Object.entries(kinds).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
        <label>
          <input
            type="checkbox"
            checked={unread}
            onChange={(event) => {
              setOffset(0);
              setUnread(event.target.checked);
            }}
          />{' '}
          Unread
        </label>
        <label>
          <input
            type="checkbox"
            checked={critical}
            onChange={(event) => {
              setOffset(0);
              setCritical(event.target.checked);
            }}
          />{' '}
          Important
        </label>
      </div>
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
      {loading && (
        <p role="status" className="changes-status">
          Loading…
        </p>
      )}
      {!loading && page?.total === 0 && <p className="changes-status">No changes found.</p>}
      <div className="changes-list" aria-busy={loading}>
        {page?.changes.map((change) => {
          const course = courses.find((item) => item.id === change.course_id);
          const url = sourceUrl(change.evidence.url);
          const body = change.kind === 'body_changed';
          const delta = (
            <div className="changes-diff">
              <div>
                <span>Before</span>
                <p>
                  {body
                    ? String(change.before ?? '')
                    : valueText(change.before, change.kind.startsWith('deadline_'))}
                </p>
              </div>
              <div>
                <span>After</span>
                <p>
                  {body
                    ? String(change.after ?? '')
                    : valueText(change.after, change.kind.startsWith('deadline_'))}
                </p>
              </div>
            </div>
          );
          return (
            <article
              key={change.id}
              className={`change-entry ${change.severity === 'critical' ? 'change-critical' : ''} ${!change.read_at ? 'change-unread' : ''}`}
            >
              <div className="change-top">
                <span className="change-kind">
                  {kinds[change.kind] ?? change.kind}
                  {change.severity === 'critical' && (
                    <span className="change-priority">Important</span>
                  )}
                </span>
                <time dateTime={change.observed_at}>{date(change.observed_at)}</time>
              </div>
              <h3>
                {onOpenTask ? (
                  <button onClick={() => onOpenTask(change.task_id)}>{change.title}</button>
                ) : (
                  change.title
                )}
              </h3>
              <p className="change-course">
                {course?.name ?? change.source_course_id} · {change.provider.replaceAll('_', ' ')}
              </p>
              {body ? (
                <details>
                  <summary>View instruction changes</summary>
                  {delta}
                </details>
              ) : (
                delta
              )}
              <div className="change-bottom">
                <span>
                  {change.evidence.source_updated_at
                    ? `Source updated ${date(change.evidence.source_updated_at)}`
                    : `Last seen ${date(change.evidence.last_seen_at)}`}
                </span>
                <div>
                  {url && (
                    <a href={url} target="_blank" rel="noreferrer">
                      Source <ArrowUpRight size={14} />
                    </a>
                  )}
                  <button disabled={busy} onClick={() => void mark(change)}>
                    {change.read_at ? 'Mark unread' : 'Mark read'}
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {page && page.total > 50 && (
        <div className="changes-pagination">
          <button
            disabled={loading || offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 50))}
          >
            Previous
          </button>
          <span>
            {offset + 1}–{Math.min(offset + 50, page.total)} / {page.total}
          </span>
          <button
            disabled={loading || offset + 50 >= page.total}
            onClick={() => setOffset(offset + 50)}
          >
            Next
          </button>
        </div>
      )}
    </section>
  );
}
