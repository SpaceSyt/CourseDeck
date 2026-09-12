import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Archive,
  Bell,
  ArrowUpRight,
  BookOpen,
  Check,
  CheckCheck,
  ChevronRight,
  Clock3,
  Eye,
  EyeOff,
  FileText,
  Layers3,
  LayoutList,
  Link2,
  Loader2,
  Mail,
  MessageSquare,
  Pin,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  X,
} from 'lucide-react';
import type { Course, LocalState, MailMessage, Settings, SyncLog, Task } from './types';
import { SourceCard, type Action } from './SourceCard';
import { bucket, completed, dayKey, dayNumber, sourceWarning } from './tasks';
import { useHeartbeat } from './useHeartbeat';
import { useWorkspaceSnapshot } from './useWorkspaceSnapshot';
import { CourseDialog } from './CourseDialog';
import { TaskDialog } from './TaskDialog';
import { Inbox } from './Inbox';
import { AISettings, Chat } from './Chat';
import { Changes } from './Changes';
import { TaskRelations } from './TaskRelations';
import { TaskHistory } from './TaskHistory';
import { RecoverySettings } from './RecoverySettings';
import { api } from './api';
import { courseColor, sourceSyncStatus } from './colors';
import './style.css';
import './dark.css';
import './inbox.css';
const names: Record<string, string> = {
  google_classroom: 'Classroom',
  gradescope: 'Gradescope',
  webassign: 'WebAssign',
  brightspace: 'Brightspace',
  custom: 'Custom',
};
const safeUrl = (url: string | null) => (url && /^https?:\/\//i.test(url) ? url : undefined);
function App() {
  const { data, setData, offline, recoveryRequired, load } = useWorkspaceSnapshot();
  const [page, setPage] = useState('Todo');
  const [chatTarget, setChatTarget] = useState<{
    courseId?: string;
    taskId?: string;
    documentId?: string;
  }>({});
  const [query, setQuery] = useState('');
  const [provider, setProvider] = useState('all');
  const [courseFilter, setCourseFilter] = useState('all');
  const [courseDialog, setCourseDialog] = useState(false);
  const [bindingCourse, setBindingCourse] = useState<Course | null>(null);
  const [showDeletedCourses, setShowDeletedCourses] = useState(false);
  const [taskDialog, setTaskDialog] = useState<{ task?: Task; email?: MailMessage } | null>(null);
  const [emailToOpen, setEmailToOpen] = useState<string | null>(null);
  const [inboxVisible, setInboxVisible] = useState(false);
  const connection = useHeartbeat();
  const [view, setView] = useState('active');
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<SyncLog[]>([]);
  const [clock, setClock] = useState(new Date());
  const [completion, setCompletion] = useState<{
    task: Task;
    phase: 'striking' | 'exiting';
  } | null>(null);
  const completing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => {
      setClock(new Date());
    }, 3000);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (
      data &&
      courseFilter !== 'all' &&
      !data.courses.some((c) => c.id === courseFilter && !c.disabled && !c.deleted)
    ) {
      const linked = data.courses.find(
        (c) => c.workspace_id === courseFilter && !c.disabled && !c.deleted,
      );
      setCourseFilter(linked?.id ?? 'all');
    }
  }, [data, courseFilter]);
  useEffect(() => {
    if (page === 'Sources')
      void api<SyncLog[]>('/history')
        .then(setLogs)
        .catch(() => {});
  }, [page, data?.revision]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const action = async (path: string, method = 'POST', body?: unknown) => {
    setBusy(true);
    setMessage('');
    try {
      const result = await api<{ message?: string }>(path, method, body);
      if (result.message && /\/(connect|finish-login|backup)$/.test(path))
        setMessage(result.message);
      await load();
      return true;
    } catch (e) {
      setMessage((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const update = (task: Task, patch: Partial<LocalState>) =>
    action(`/tasks/${encodeURIComponent(task.id)}/local`, 'PATCH', patch);
  const toggleComplete = async (task: Task) => {
    if (completing.current) return;
    if (task.local.dismissed || task.local.completion_override === 'done')
      return update(task, { dismissed: false });
    completing.current = true;
    setBusy(true);
    setMessage('');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const pause = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
    setCompletion({ task, phase: 'striking' });
    try {
      await Promise.all([
        api(`/tasks/${encodeURIComponent(task.id)}/local`, 'PATCH', { dismissed: true }),
        pause(reducedMotion ? 0 : 320),
      ]);
      if (!mounted.current) return;
      setData((current) =>
        current
          ? {
              ...current,
              tasks: current.tasks.map((t) =>
                t.id === task.id
                  ? { ...t, local: { ...t.local, dismissed: true, completion_override: null } }
                  : t,
              ),
            }
          : current,
      );
      void load();
      setCompletion({ task, phase: 'exiting' });
      await pause(reducedMotion ? 0 : 260);
    } catch (error) {
      if (mounted.current) setMessage((error as Error).message);
    } finally {
      completing.current = false;
      if (mounted.current) {
        setCompletion(null);
        setBusy(false);
      }
    }
  };
  const zone = data?.settings.timezone ?? 'America/New_York';
  const date = (value: string | null, full = false) =>
    value
      ? new Intl.DateTimeFormat('en-US', {
          timeZone: zone,
          month: 'short',
          day: 'numeric',
          ...(full ? { year: 'numeric' as const } : {}),
          hour: 'numeric',
          minute: '2-digit',
        }).format(new Date(value))
      : 'No due date';
  const course = (task: Task) => data?.courses.find((c) => c.id === task.course_id);
  const activeCourses = data?.courses.filter((c) => !c.disabled && !c.deleted) ?? [];
  const courseEnabled = (t: Task) => !course(t)?.disabled && !course(t)?.deleted;
  const active =
    data?.tasks.filter(
      (t) => courseEnabled(t) && !t.archived && !t.local.hidden && !completed(t),
    ) ?? [];
  const scopedActive = active.filter(
    (t) =>
      (provider === 'all' ||
        t.provider === provider ||
        (t.provider === 'custom' && (course(t)?.providers ?? []).includes(provider))) &&
      (courseFilter === 'all' || t.course_id === courseFilter),
  );
  const sync = data?.sources.some((s) => s.syncing);
  // Preserve the row during polling and the save so its exit is never cut short.
  const visibleTasks = (data?.tasks ?? []).map((t) =>
    t.id === completion?.task.id ? completion.task : t,
  );
  const filtered = visibleTasks
    .filter((t) => {
      const relevant =
        view === 'hidden'
          ? t.local.hidden
          : view === 'archive'
            ? t.archived
            : !t.local.hidden && !t.archived;
      return (
        courseEnabled(t) &&
        relevant &&
        (view !== 'done' || completed(t)) &&
        (view !== 'active' || data?.settings.show_completed || !completed(t)) &&
        (provider === 'all' ||
          t.provider === provider ||
          (t.provider === 'custom' && (course(t)?.providers ?? []).includes(provider))) &&
        (courseFilter === 'all' || t.course_id === courseFilter) &&
        `${t.title} ${course(t)?.name ?? ''}`.toLowerCase().includes(query.toLowerCase())
      );
    })
    .sort(
      (a, b) =>
        Number(b.local.pinned) - Number(a.local.pinned) ||
        b.local.priority - a.local.priority ||
        (a.due_at ?? '9999').localeCompare(b.due_at ?? '9999'),
    );
  const task = data?.tasks.find((t) => t.id === selected);
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage('Todo');
          }}
        >
          <span className="brand-icon">
            <Layers3 size={22} />
          </span>
          CourseDeck
        </a>
        <nav>
          {[
            { name: 'Chat', icon: MessageSquare },
            { name: 'Todo', icon: LayoutList },
            { name: 'Changes', icon: Bell },
            { name: 'Courses', icon: BookOpen },
            { name: 'Sources', icon: Link2 },
            { name: 'Settings', icon: Settings2 },
          ].map(({ name, icon: Icon }) => (
            <button
              key={name}
              className={`nav-item ${page === name ? 'selected' : ''}`}
              onClick={() => {
                setPage(name);
                if (name === 'Chat') setChatTarget({});
                setCourseFilter('all');
                setProvider('all');
                setQuery('');
              }}
            >
              <Icon size={19} />
              {name}
              {name === 'Todo' && <span className="nav-count">{active.length}</span>}
              {name === 'Changes' && !!data?.changes?.unread_count && (
                <span className="nav-count">{data.changes.unread_count}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="course-nav">
          <div className="sidebar-section-heading">
            <span className="nav-label">COURSES</span>
            <button
              aria-label="Add course"
              title="Add course"
              disabled={!data}
              onClick={() => {
                setBindingCourse(null);
                setCourseDialog(true);
              }}
            >
              <Plus size={15} />
            </button>
          </div>
          {activeCourses.map((c) => (
            <button
              key={c.id}
              title={c.name}
              className={`course-nav-item ${page === 'Todo' && courseFilter === c.id ? 'selected' : ''}`}
              onClick={() => {
                setCourseFilter(c.id);
                setProvider('all');
                setPage('Todo');
                setQuery('');
              }}
            >
              <i className="dot" style={{ backgroundColor: courseColor(c) }} />
              <span>{c.name}</span>
              {c.needs_binding ? (
                <Link2 size={12} />
              ) : (
                <small>{active.filter((t) => t.course_id === c.id).length}</small>
              )}
            </button>
          ))}
          {data && !activeCourses.length && (
            <small className="sidebar-empty">No active courses</small>
          )}
        </div>
        <div className="source-nav">
          <span className="nav-label">SOURCES</span>
          {data?.sources.map((s) => {
            const syncStatus = sourceSyncStatus(s);
            return (
              <button
                key={s.key}
                title={`${names[s.key] ?? s.name} · ${syncStatus.label}`}
                aria-label={`${names[s.key] ?? s.name}: ${syncStatus.label}`}
                onClick={() => {
                  setProvider(s.key);
                  setCourseFilter('all');
                  setPage('Sources');
                  window.setTimeout(
                    () =>
                      document
                        .getElementById(`source-${s.key}`)
                        ?.scrollIntoView({ behavior: 'smooth', block: 'center' }),
                    0,
                  );
                }}
              >
                <i
                  className="dot"
                  style={{ backgroundColor: syncStatus.color }}
                  aria-hidden="true"
                />
                {names[s.key] ?? s.name}
              </button>
            );
          })}
        </div>
      </aside>
      <main className={page === 'Chat' ? 'chat-page' : undefined}>
        <header className="topbar">
          <div>
            <span
              className={`connection-light ${connection === 'disconnected' ? 'red' : connection === 'connecting' ? 'pending' : ''}`}
            />
            <span
              role="status"
              aria-label={`Local service: ${connection}`}
              title={`Local service: ${connection}`}
            >
              {connection === 'connected'
                ? 'Local service'
                : connection === 'connecting'
                  ? 'Local service · Connecting…'
                  : 'Local service · Disconnected'}
            </span>
            <span className="top-divider" />
            <span>{zone.replaceAll('_', ' ')}</span>
          </div>
        </header>
        {message && (
          <div className="notice" role="status">
            <span>{message}</span>
            <button aria-label="Dismiss message" onClick={() => setMessage('')}>
              <X size={16} />
            </button>
          </div>
        )}
        <div
          className={`workspace-body ${page === 'Todo' ? 'has-inbox' : ''} ${inboxVisible ? 'mail-visible' : ''}`}
        >
          <div
            className={`content ${page === 'Todo' ? 'content-todo' : page === 'Chat' ? 'content-chat' : ''}`}
          >
            {page !== 'Chat' && (
              <div className="page-heading">
                <div>
                  <div className="eyebrow">
                    {new Intl.DateTimeFormat('en-US', {
                      timeZone: zone,
                      weekday: 'long',
                      month: 'long',
                      day: 'numeric',
                    }).format(clock)}
                  </div>
                  <h1>
                    {page === 'Todo'
                      ? courseFilter !== 'all'
                        ? (data?.courses.find((c) => c.id === courseFilter)?.name ?? 'Assignments')
                        : 'Assignments'
                      : page === 'Courses'
                        ? 'Courses'
                        : page === 'Sources'
                          ? 'Sources'
                          : page}
                  </h1>
                </div>
                {page !== 'Settings' && (
                  <div className="page-actions">
                    {page === 'Todo' && (
                      <>
                        <button className="inbox-toggle" onClick={() => setInboxVisible(true)}>
                          <Mail size={16} />
                          Inbox
                        </button>
                        <button
                          className="primary"
                          disabled={!data}
                          onClick={() => setTaskDialog({})}
                        >
                          <Plus size={16} />
                          Add task
                        </button>
                      </>
                    )}
                    <button
                      className="primary"
                      disabled={busy || sync}
                      onClick={() => action('/sync')}
                    >
                      <RefreshCw size={16} className={sync ? 'spin' : ''} />
                      {sync ? 'Syncing…' : 'Sync all'}{' '}
                    </button>
                  </div>
                )}
              </div>
            )}
            {recoveryRequired ? (
              <div className="settings-panel">
                <RecoverySettings onRestored={() => window.location.reload()} />
              </div>
            ) : !data ? (
              <div className="empty">
                <Loader2 className="spin" />
                <h2>{offline ? 'Local service unavailable' : 'Loading…'}</h2>
                {offline && <p>Start CourseDeck to reconnect.</p>}
              </div>
            ) : (
              <>
                {page === 'Chat' && (
                  <Chat
                    key={JSON.stringify(chatTarget)}
                    courses={activeCourses}
                    initialCourseId={chatTarget.courseId}
                    initialTaskId={chatTarget.taskId}
                    initialDocumentId={chatTarget.documentId}
                  />
                )}
                {page === 'Changes' && (
                  <Changes
                    courses={activeCourses}
                    revision={data.revision}
                    timezone={data.settings.timezone}
                    onOpenTask={(id) => setSelected(id)}
                  />
                )}
                {page === 'Todo' && (
                  <>
                    {connection === 'disconnected' && (
                      <div className="warning">Offline · Showing cached assignments</div>
                    )}
                    {!!data.changes?.critical_unread_count && (
                      <div className="warning sync-warning">
                        <span>{data.changes.critical_unread_count} important task changes</span>
                        <button onClick={() => setPage('Changes')}>
                          Review changes <ChevronRight size={14} />
                        </button>
                      </div>
                    )}
                    {courseFilter !== 'all' && (
                      <div className="course-filter-bar">
                        <button
                          onClick={() => {
                            setCourseFilter('all');
                            setProvider('all');
                          }}
                        >
                          All courses
                          <X size={13} />
                        </button>
                        {data.courses.find((c) => c.id === courseFilter)?.needs_binding && (
                          <button
                            onClick={() => {
                              setBindingCourse(data.courses.find((c) => c.id === courseFilter)!);
                              setCourseDialog(true);
                            }}
                          >
                            Link source course
                            <Link2 size={13} />
                          </button>
                        )}
                      </div>
                    )}
                    {data.sources.some(
                      (s) => s.last_outcome && !['success', 'partial'].includes(s.last_outcome),
                    ) && (
                      <div className="warning sync-warning">
                        <span>Sync failed</span>
                        <button onClick={() => setPage('Sources')}>
                          Review sources
                          <ChevronRight size={14} />
                        </button>
                      </div>
                    )}
                    <div className="stats">
                      <Stat
                        label="To do"
                        value={scopedActive.length}
                        icon={<LayoutList size={20} />}
                      />
                      <Stat
                        label="Due today"
                        value={
                          scopedActive.filter(
                            (t) =>
                              t.due_at && dayKey(new Date(t.due_at), zone) === dayKey(clock, zone),
                          ).length
                        }
                        icon={<Clock3 size={20} />}
                      />
                      <Stat
                        label="Next 7 days"
                        value={
                          scopedActive.filter(
                            (t) =>
                              t.due_at &&
                              new Date(t.due_at) > clock &&
                              dayNumber(new Date(t.due_at), zone) - dayNumber(clock, zone) < 7,
                          ).length
                        }
                        icon={<BookOpen size={20} />}
                      />
                    </div>
                    <div className="list-heading">
                      <div className="views" role="group" aria-label="Assignment views">
                        {[
                          ['active', 'To do'],
                          ['done', 'Done'],
                          ['all', 'All tasks'],
                          ['hidden', 'Hidden'],
                          ['archive', 'Archive'],
                        ].map(([key, name]) => (
                          <button
                            key={key}
                            className={view === key ? 'active' : ''}
                            aria-pressed={view === key}
                            onClick={() => setView(key)}
                          >
                            {name}
                            {view === key && <span className="view-count">{filtered.length}</span>}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="filters">
                      <label className="search">
                        <Search size={17} />
                        <input
                          aria-label="Search assignments"
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                          placeholder="Search assignments…"
                        />
                      </label>
                      <select
                        aria-label="Filter by source"
                        value={provider}
                        onChange={(e) => {
                          setProvider(e.target.value);
                          setCourseFilter('all');
                        }}
                      >
                        <option value="all">All sources</option>
                        <option value="custom">Custom</option>
                        {data.sources.map((s) => (
                          <option key={s.key} value={s.key}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                    </div>
                    {filtered.length === 0 ? (
                      <div className="empty">
                        <span className="empty-icon">
                          <CheckCheck size={30} />
                        </span>
                        <h2>
                          {data.tasks.length ? 'No matching assignments' : 'No assignments yet'}
                        </h2>
                        {!data.tasks.length && (
                          <button className="primary" onClick={() => setPage('Sources')}>
                            Connect a source
                            <ArrowUpRight size={16} />
                          </button>
                        )}
                      </div>
                    ) : (
                      ['Overdue', 'Today', 'Tomorrow', 'Next 7 days', 'Later', 'No due date'].map(
                        (group) => {
                          const rows = filtered.filter((t) => bucket(t, zone, clock) === group);
                          return (
                            rows.length > 0 && (
                              <section className="task-group" key={group}>
                                <h3 className={group === 'Overdue' ? 'overdue' : ''}>
                                  <span className="group-dot" />
                                  {group}
                                  <span className="group-count">{rows.length}</span>
                                  <span className="group-line" />
                                </h3>
                                {rows.map((t) => (
                                  <div
                                    className={`task-row ${completed(t) ? 'done' : ''} ${sourceWarning(t) ? 'uncertain' : ''} ${completion?.task.id === t.id ? `completing ${completion.phase}` : ''}`}
                                    key={t.id}
                                    aria-busy={completion?.task.id === t.id}
                                  >
                                    <button
                                      className={`checkbox ${completed(t) || completion?.task.id === t.id ? 'checked' : ''}`}
                                      aria-label={`${t.local.dismissed || t.local.completion_override === 'done' ? 'Restore' : completed(t) ? 'Completed at source:' : 'Complete'} ${t.title}`}
                                      aria-pressed={completed(t) || completion?.task.id === t.id}
                                      disabled={
                                        busy ||
                                        (completed(t) &&
                                          !t.local.dismissed &&
                                          t.local.completion_override !== 'done')
                                      }
                                      onClick={() => void toggleComplete(t)}
                                    >
                                      {(completed(t) || completion?.task.id === t.id) && (
                                        <Check size={13} />
                                      )}
                                    </button>
                                    <button className="task-main" onClick={() => setSelected(t.id)}>
                                      <span className="task-title">
                                        <span className="task-title-copy">
                                          <span className="task-title-text">{t.title}</span>
                                        </span>
                                        {t.local.pinned && <Pin size={13} />}{' '}
                                        {t.local.priority > 0 && (
                                          <span className="priority">P{t.local.priority}</span>
                                        )}
                                      </span>
                                      <span className="task-meta">
                                        {course(t) && (
                                          <span className="task-course">
                                            <i
                                              className="dot"
                                              style={{ backgroundColor: courseColor(course(t)) }}
                                            />
                                            {course(t)?.name}
                                          </span>
                                        )}
                                        <span className="task-provider">
                                          {names[t.provider] ?? t.provider}
                                        </span>
                                        {t.local.note && <FileText size={12} />}
                                        {t.email_id && <Mail size={12} aria-label="Linked email" />}
                                      </span>
                                      {sourceWarning(t) && (
                                        <span className="task-source-warning">
                                          {sourceWarning(t)}
                                        </span>
                                      )}
                                    </button>
                                    {!sourceWarning(t) && (
                                      <span className={`status ${t.submission_status}`}>
                                        {t.submission_status.replaceAll('_', ' ')}
                                      </span>
                                    )}
                                    <div
                                      className={`due ${group === 'Overdue' && !completed(t) ? 'overdue' : ''}`}
                                    >
                                      <Clock3 size={13} />
                                      {date(t.due_at)}
                                    </div>
                                    {safeUrl(t.url) ? (
                                      <a
                                        className="source-link"
                                        href={safeUrl(t.url)}
                                        target="_blank"
                                        rel="noreferrer"
                                        aria-label={`Open ${t.title} at source`}
                                      >
                                        <ArrowUpRight size={18} />
                                      </a>
                                    ) : (
                                      <span className="source-link" />
                                    )}
                                  </div>
                                ))}
                              </section>
                            )
                          );
                        },
                      )
                    )}
                  </>
                )}
                {page === 'Sources' && (
                  <>
                    <div className="source-grid">
                      {data.sources.map((s) => (
                        <SourceCard
                          key={s.key}
                          source={s}
                          busy={busy}
                          date={date}
                          action={action}
                        />
                      ))}
                    </div>
                    <div className="section-heading">
                      <h2>Sync history</h2>
                    </div>
                    <div className="history">
                      {logs.length ? (
                        logs.slice(0, 12).map((log) => (
                          <div key={log.id}>
                            <i className={`dot ${log.provider}`} />
                            <b>{names[log.provider] ?? log.provider}</b>
                            <span>{date(log.attempted_at)}</span>
                            <span className="status">{log.outcome.replaceAll('_', ' ')}</span>
                            <span>{log.task_count} tasks</span>
                            {log.warnings.length > 0 && <small>{log.warnings.join(' ')}</small>}
                          </div>
                        ))
                      ) : (
                        <p>No syncs yet</p>
                      )}
                    </div>
                  </>
                )}
                {page === 'Courses' && (
                  <>
                    <div className="filters">
                      <label className="search">
                        <Search size={17} />
                        <input
                          aria-label="Search courses"
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                          placeholder="Find a course…"
                        />
                      </label>
                      <select
                        aria-label="Filter courses by source"
                        value={provider}
                        onChange={(e) => setProvider(e.target.value)}
                      >
                        <option value="all">All sources</option>
                        {data.sources.map((s) => (
                          <option key={s.key} value={s.key}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                    </div>
                    <label className="course-deleted-filter">
                      <input
                        type="checkbox"
                        checked={showDeletedCourses}
                        onChange={(e) => setShowDeletedCourses(e.target.checked)}
                      />
                      Show deleted
                    </label>
                    <div className="course-grid">
                      {data.courses
                        .filter(
                          (c) =>
                            (showDeletedCourses || !c.deleted) &&
                            (provider === 'all' ||
                              (c.providers ?? [c.provider]).includes(provider)) &&
                            c.name.toLowerCase().includes(query.toLowerCase()),
                        )
                        .map((c: Course) => (
                          <article
                            className={`course-card${c.disabled ? ' disabled' : ''}`}
                            key={c.id}
                          >
                            <div className="course-markers">
                              {(c.deleted || c.disabled) && (
                                <span>{c.deleted ? 'Deleted' : 'Disabled'}</span>
                              )}
                              {c.alias && (
                                <span
                                  className="course-alias-marker"
                                  aria-label="Alias enabled"
                                  title={`Original name: ${c.original_name}`}
                                >
                                  *
                                </span>
                              )}
                            </div>
                            <span
                              className="course-icon"
                              style={{
                                color: courseColor(c),
                                backgroundColor: `color-mix(in srgb, ${courseColor(c)} 16%, transparent)`,
                              }}
                            >
                              <BookOpen size={22} />
                            </span>
                            <small>
                              {(c.providers ?? [c.provider]).map((p) => names[p]).join(' · ')}
                            </small>
                            <h2>{c.name}</h2>
                            <p className="course-code" aria-hidden={!c.section && !c.teacher}>
                              {[c.section, c.teacher].filter(Boolean).join(' · ')}
                            </p>
                            <footer>
                              <div className="course-actions">
                                {c.deleted ? (
                                  <button
                                    disabled={busy}
                                    onClick={() =>
                                      action(
                                        `/courses/${encodeURIComponent(c.workspace_id ?? c.id)}`,
                                        'PATCH',
                                        { deleted: false },
                                      )
                                    }
                                  >
                                    Restore course
                                  </button>
                                ) : (
                                  <>
                                    <button
                                      onClick={() => {
                                        setBindingCourse(c);
                                        setCourseDialog(true);
                                      }}
                                    >
                                      Edit course
                                      <Link2 size={13} />
                                    </button>
                                    <button
                                      disabled={busy}
                                      onClick={() =>
                                        action(
                                          `/courses/${encodeURIComponent(c.workspace_id ?? c.id)}`,
                                          'PATCH',
                                          { disabled: !c.disabled },
                                        )
                                      }
                                    >
                                      {c.disabled ? 'Enable' : 'Disable'}
                                    </button>
                                    <button
                                      disabled={busy}
                                      onClick={() =>
                                        action(
                                          `/courses/${encodeURIComponent(c.workspace_id ?? c.id)}`,
                                          'DELETE',
                                        )
                                      }
                                    >
                                      Delete
                                    </button>
                                  </>
                                )}
                              </div>
                              <span>
                                {
                                  data.tasks.filter(
                                    (t) =>
                                      t.course_id === c.id &&
                                      !t.archived &&
                                      !t.local.hidden &&
                                      !completed(t),
                                  ).length
                                }{' '}
                                tasks to do
                              </span>
                              {safeUrl(c.source_url) && (
                                <a href={safeUrl(c.source_url)} target="_blank" rel="noreferrer">
                                  Open course
                                  <ArrowUpRight size={15} />
                                </a>
                              )}
                            </footer>
                          </article>
                        ))}
                    </div>
                    {!data.courses.some((c) => showDeletedCourses || !c.deleted) && (
                      <div className="empty">
                        <BookOpen />
                        <h2>No courses yet</h2>
                        <button className="primary" onClick={() => setPage('Sources')}>
                          Connect a source
                        </button>
                      </div>
                    )}
                  </>
                )}
                {page === 'Settings' && (
                  <SettingsPage settings={data.settings} busy={busy} action={action} />
                )}
              </>
            )}
          </div>
          {page === 'Todo' && data && (
            <div className="inbox-column">
              <button
                className="inbox-close"
                aria-label="Close inbox"
                onClick={() => setInboxVisible(false)}
              >
                <X size={18} />
              </button>
              <Inbox
                courses={data.courses}
                addTask={(email) => setTaskDialog({ email })}
                openId={emailToOpen}
                opened={() => setEmailToOpen(null)}
              />
            </div>
          )}
        </div>
      </main>
      {taskDialog && data && (
        <TaskDialog
          courses={activeCourses}
          {...taskDialog}
          initialCourse={courseFilter !== 'all' ? courseFilter : undefined}
          close={() => setTaskDialog(null)}
          saved={() => {
            void load();
          }}
        />
      )}
      {courseDialog && data && (
        <CourseDialog
          data={data}
          binding={bindingCourse}
          busy={busy}
          action={action}
          close={() => {
            setCourseDialog(false);
            setBindingCourse(null);
          }}
        />
      )}
      {task && (
        <div className="overlay" onClick={() => setSelected(null)}>
          <aside
            className="detail"
            role="dialog"
            aria-modal="true"
            aria-label="Assignment details"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="detail-top">
              <span>ASSIGNMENT DETAILS</span>
              <button aria-label="Close details" autoFocus onClick={() => setSelected(null)}>
                <X size={21} />
              </button>
            </div>
            <span className={`provider-tag ${task.provider}`}>{names[task.provider]}</span>
            <h2>{task.title}</h2>
            <p>{course(task)?.name}</p>
            {task.provider === 'custom' && (
              <button
                onClick={() => {
                  setTaskDialog({ task });
                  setSelected(null);
                }}
              >
                Edit task
              </button>
            )}
            {task.email_id && (
              <button
                onClick={() => {
                  setEmailToOpen(task.email_id!);
                  setSelected(null);
                  setPage('Todo');
                  setInboxVisible(true);
                }}
              >
                <Mail size={16} />
                Open email
              </button>
            )}
            {task.raw_data?.unavailable_fields?.includes('due_at') && (
              <p className="warning">Due date not refreshed</p>
            )}
            <dl>
              <dt>{task.local.due_override ? 'Local due date' : 'Due date'}</dt>
              <dd>{date(task.due_at, true)}</dd>
              {task.local.due_override && (
                <>
                  <dt>Source due date</dt>
                  <dd>{date(task.source_due_at ?? null, true)}</dd>
                </>
              )}
              {task.local.completion_override && (
                <>
                  <dt>Local status</dt>
                  <dd>{task.local.completion_override}</dd>
                </>
              )}
              {task.provider !== 'custom' && (
                <>
                  <dt>Submission closes</dt>
                  <dd>{task.closes_at ? date(task.closes_at, true) : '—'}</dd>
                  <dt>Available from</dt>
                  <dd>{task.available_at ? date(task.available_at, true) : '—'}</dd>
                  <dt>Source status</dt>
                  <dd>{sourceWarning(task) || task.submission_status.replaceAll('_', ' ')}</dd>
                  <dt>Grade</dt>
                  <dd>
                    {task.score ?? '—'} / {task.points_possible ?? '—'}
                    {task.graded ? ' · Graded' : ''}
                  </dd>
                  <dt>Last seen</dt>
                  <dd>{date(task.last_seen_at)}</dd>
                </>
              )}
            </dl>
            {task.missing_count > 0 && (
              <p className="warning">Not found in the last {task.missing_count} syncs</p>
            )}
            {task.raw_data?.unavailable_fields?.includes('description') && (
              <p className="warning">Description could not be fully read.</p>
            )}
            <button
              onClick={() => {
                setChatTarget({ courseId: task.course_id ?? undefined, taskId: task.id });
                setSelected(null);
                setPage('Chat');
              }}
            >
              <MessageSquare size={16} /> Ask about task
            </button>
            <TaskRelations
              key={task.id}
              task={task}
              onOpenTask={(id) => setSelected(id)}
              onOpenMail={(id) => {
                setEmailToOpen(id);
                setSelected(null);
                setPage('Todo');
                setInboxVisible(true);
              }}
              onOpenDocument={(id) => {
                setChatTarget({ courseId: task.course_id ?? undefined, documentId: id });
                setSelected(null);
                setPage('Chat');
              }}
            />
            <div className="local-controls">
              <div className="button-row">
                <button
                  disabled={busy}
                  onClick={() => update(task, { pinned: !task.local.pinned })}
                >
                  <Pin size={15} />
                  {task.local.pinned ? 'Unpin' : 'Pin'}
                </button>
                <button
                  disabled={busy}
                  onClick={() => update(task, { hidden: !task.local.hidden })}
                >
                  {task.local.hidden ? <Eye size={15} /> : <EyeOff size={15} />}
                  {task.local.hidden ? 'Unhide' : 'Hide'}
                </button>
                <button
                  disabled={busy}
                  onClick={() => update(task, { dismissed: !task.local.dismissed })}
                >
                  <Archive size={15} />
                  {task.local.dismissed ? 'Restore' : 'Dismiss'}
                </button>
              </div>
              <label>
                Priority
                <select
                  value={task.local.priority}
                  disabled={busy}
                  onChange={(e) => update(task, { priority: Number(e.target.value) })}
                >
                  <option value={0}>None</option>
                  <option value={1}>1 · Low</option>
                  <option value={2}>2 · Medium</option>
                  <option value={3}>3 · High</option>
                </select>
              </label>
              <NoteEditor
                key={task.id}
                task={task}
                busy={busy}
                save={(note) => update(task, { note })}
              />
              <TaskHistory key={task.id + '-history'} task={task} reload={load} />
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
function Stat({ label, value, icon }: { label: string; value: number; icon: React.ReactNode }) {
  return (
    <article className="stat">
      <div>
        <span>{label}</span>
        {icon}
      </div>
      <strong>{value}</strong>
    </article>
  );
}
function NoteEditor({
  task,
  busy,
  save,
}: {
  task: Task;
  busy: boolean;
  save: (note: string) => void;
}) {
  const [note, setNote] = useState(task.local.note);
  const previous = useRef(task.local.note);
  useEffect(() => {
    const saved = previous.current;
    setNote((draft) => (draft === saved ? task.local.note : draft));
    previous.current = task.local.note;
  }, [task.local.note]);
  return (
    <>
      <label>
        Note
        <textarea
          value={note}
          maxLength={10000}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Add a note…"
        />
      </label>
      <button disabled={busy || note === task.local.note} onClick={() => save(note)}>
        Save note
      </button>
    </>
  );
}
function SettingsPage({
  settings,
  busy,
  action,
}: {
  settings: Settings;
  busy: boolean;
  action: Action;
}) {
  const [zone, setZone] = useState(settings.timezone);
  return (
    <div className="settings-panel">
      <AISettings />
      <section>
        <h2>Preferences</h2>
        <label className="setting-row">
          <div>
            <b>Sync on startup</b>
          </div>
          <input
            type="checkbox"
            checked={settings.startup_sync}
            onChange={(e) =>
              action('/settings', 'PUT', { ...settings, startup_sync: e.target.checked })
            }
          />
        </label>
        <label className="setting-row">
          <div>
            <b>Show completed assignments</b>
          </div>
          <input
            type="checkbox"
            checked={settings.show_completed}
            onChange={(e) =>
              action('/settings', 'PUT', { ...settings, show_completed: e.target.checked })
            }
          />
        </label>
        <div className="setting-row">
          <div>
            <b>Display timezone</b>
          </div>
          <div className="button-row">
            <input
              aria-label="Display timezone"
              value={zone}
              onChange={(e) => setZone(e.target.value)}
            />
            <button
              disabled={busy || zone === settings.timezone}
              onClick={() => action('/settings', 'PUT', { ...settings, timezone: zone })}
            >
              Save
            </button>
          </div>
        </div>
      </section>
      <RecoverySettings onRestored={() => window.location.reload()} />
      <section>
        <h2>Diagnostics</h2>
        <div className="setting-row">
          <a href="/api/debug" target="_blank" rel="noreferrer">
            Open diagnostics
            <ArrowUpRight size={15} />
          </a>
        </div>
      </section>
    </div>
  );
}
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
