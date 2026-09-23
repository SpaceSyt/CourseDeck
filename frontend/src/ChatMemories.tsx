import { useEffect, useRef, useState } from 'react';
import { Download, Plus } from 'lucide-react';
import { api } from './api';
import type { Course } from './types';

interface Memory {
  id: string;
  text: string;
  course_id: string | null;
  version: string | number;
  status: 'active' | 'pending';
  origin: 'manual' | 'chat';
  source_quote?: string | null;
  conversation_id?: string | null;
  message_id?: string | null;
  created_at: string;
  updated_at: string;
}
type MemoryMode = 'automatic' | 'confirm';
interface MemoriesResponse {
  mode: MemoryMode;
  memories: Memory[];
}
interface MemoryDraft {
  id?: string;
  version?: Memory['version'];
  text: string;
  courseId: string;
}
const draftFrom = (memory: Memory): MemoryDraft => ({
  id: memory.id,
  version: memory.version,
  text: memory.text,
  courseId: memory.course_id ?? '',
});

export function ChatMemories({
  courses,
  initialCourseId = '',
}: {
  courses: Course[];
  initialCourseId?: string;
}) {
  const [data, setData] = useState<MemoriesResponse | null>(null);
  const [draft, setDraft] = useState<MemoryDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const writing = useRef(false);
  const mounted = useRef(true);
  const request = useRef(0);
  const latest = draft?.id ? data?.memories.find((memory) => memory.id === draft.id) : undefined;
  const stale = !!draft?.id && (!latest || latest.version !== draft.version);
  const scopeName = (id: string | null) =>
    !id
      ? 'All courses'
      : (courses.find((course) => course.id === id)?.name ?? 'Unavailable course');
  const load = async () => {
    const sequence = ++request.current;
    const result = await api<MemoriesResponse>('/chat/memories');
    if (mounted.current && sequence === request.current) setData(result);
  };
  useEffect(() => {
    mounted.current = true;
    void load()
      .catch((failure: Error) => {
        if (mounted.current) setError(failure.message);
      })
      .finally(() => {
        if (mounted.current) setLoading(false);
      });
    return () => {
      mounted.current = false;
      request.current += 1;
    };
  }, []);
  const perform = async (action: () => Promise<unknown>, saved?: () => void) => {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    setError('');
    try {
      await action();
      if (mounted.current) saved?.();
      try {
        await load();
      } catch (failure) {
        if (mounted.current)
          setError(`Saved, but memories could not be refreshed: ${(failure as Error).message}`);
      }
    } catch (failure) {
      let message = (failure as Error).message;
      try {
        await load();
      } catch {
        message += ' Could not refresh memories; the cached list is still shown.';
      }
      if (mounted.current) setError(message);
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const save = () => {
    if (!draft?.text.trim() || stale) return;
    void perform(
      () =>
        draft.id
          ? api(`/chat/memories/${encodeURIComponent(draft.id)}`, 'PUT', {
              text: draft.text.trim(),
              course_id: draft.courseId || null,
              expected_version: draft.version,
            })
          : api('/chat/memories', 'POST', {
              text: draft.text.trim(),
              course_id: draft.courseId || null,
            }),
      () => setDraft(null),
    );
  };
  const exportMemories = async () => {
    setError('');
    try {
      const response = await fetch('/api/chat/memories/export', {
        headers: { 'X-CourseDeck': '1' },
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error(`Could not export memories (${response.status}).`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = 'coursedeck-memories.md';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      if (mounted.current) setError((failure as Error).message);
    }
  };
  return (
    <section className="chat-memories" aria-label="Memories" aria-busy={loading || busy}>
      <div className="chat-memory-toolbar">
        <label>
          Remember
          <select
            aria-label="Memory mode"
            value={data?.mode ?? 'automatic'}
            disabled={!data || busy}
            onChange={(event) => {
              const mode = event.target.value as MemoryMode;
              void perform(
                () => api('/chat/memories/settings', 'PUT', { mode }),
                () => setData((current) => current && { ...current, mode }),
              );
            }}
          >
            <option value="automatic">Automatic</option>
            <option value="confirm">Ask first</option>
          </select>
        </label>
        <div className="chat-memory-actions">
          <button disabled={!data || busy} onClick={() => void exportMemories()}>
            <Download size={15} /> Export
          </button>
          {!draft && (
            <button
              disabled={!data || busy}
              onClick={() => setDraft({ text: '', courseId: initialCourseId })}
            >
              <Plus size={15} /> Add memory
            </button>
          )}
        </div>
      </div>
      {data && (
        <p className="chat-memory-mode-note">
          {data.mode === 'automatic'
            ? 'Explicit long-term information can be saved automatically. Other memories need approval.'
            : 'Memories suggested in Chat need approval before use.'}
        </p>
      )}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {!data && !loading && (
        <button
          onClick={() => {
            setLoading(true);
            setError('');
            void load()
              .catch((failure: Error) => setError(failure.message))
              .finally(() => setLoading(false));
          }}
        >
          Reload
        </button>
      )}
      {loading && (
        <p className="chat-placeholder" role="status">
          Loading memories…
        </p>
      )}
      {draft && (
        <form
          className="chat-memory-editor"
          aria-label={draft.id ? 'Edit memory' : 'Add memory'}
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <label>
            Memory
            <textarea
              autoFocus
              aria-label="Memory text"
              value={draft.text}
              maxLength={1000}
              rows={3}
              disabled={busy}
              onChange={(event) => setDraft({ ...draft, text: event.target.value })}
            />
          </label>
          <label>
            Scope
            <select
              aria-label="Memory scope"
              value={draft.courseId}
              disabled={busy}
              onChange={(event) => setDraft({ ...draft, courseId: event.target.value })}
            >
              <option value="">All courses</option>
              {draft.courseId && !courses.some((course) => course.id === draft.courseId) && (
                <option value={draft.courseId}>Unavailable course</option>
              )}
              {courses.map((course) => (
                <option key={course.id} value={course.id}>
                  {course.name}
                </option>
              ))}
            </select>
          </label>
          {stale && (
            <p className="warning" role="alert">
              {latest
                ? 'This memory changed. Load its latest text before editing again.'
                : 'This memory was deleted. Your draft has been kept.'}
            </p>
          )}
          <div className="chat-memory-actions">
            {stale && latest && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setDraft(draftFrom(latest));
                  setError('');
                }}
              >
                Load latest
              </button>
            )}
            <button type="button" disabled={busy} onClick={() => setDraft(null)}>
              Cancel
            </button>
            <button type="submit" disabled={busy || stale || !draft.text.trim()}>
              {draft.id ? 'Save' : 'Add'}
            </button>
          </div>
        </form>
      )}
      {data && !data.memories.length && !draft && (
        <p className="chat-placeholder">No memories yet.</p>
      )}
      <div className="chat-memory-list">
        {data?.memories.map((memory) => (
          <article className="chat-memory" key={memory.id} aria-label={memory.text}>
            <div className="chat-memory-meta">
              <span>{scopeName(memory.course_id)}</span>
              {memory.status === 'pending' && <strong>Needs approval</strong>}
            </div>
            <p className="chat-memory-text">{memory.text}</p>
            {memory.source_quote && (
              <details>
                <summary>Source</summary>
                <blockquote>{memory.source_quote}</blockquote>
              </details>
            )}
            <div className="chat-memory-actions">
              {memory.status === 'pending' && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void perform(() =>
                      api(`/chat/memories/${encodeURIComponent(memory.id)}/approve`, 'POST', {
                        expected_version: memory.version,
                      }),
                    )
                  }
                >
                  Approve
                </button>
              )}
              <button
                disabled={busy}
                onClick={() => {
                  setDraft(draftFrom(memory));
                  setError('');
                }}
              >
                Edit
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  if (window.confirm('Delete this memory?'))
                    void perform(
                      () =>
                        api(
                          `/chat/memories/${encodeURIComponent(memory.id)}?version=${encodeURIComponent(memory.version)}`,
                          'DELETE',
                        ),
                      () => {
                        if (draft?.id === memory.id) setDraft(null);
                      },
                    );
                }}
              >
                Delete
              </button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
