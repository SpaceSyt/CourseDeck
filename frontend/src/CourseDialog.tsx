import { useRef, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import type { Course, Snapshot } from './types';
import type { Action } from './SourceCard';
import { courseColor } from './colors';

export function CourseDialog({
  data,
  binding,
  busy,
  action,
  close,
}: {
  data: Snapshot;
  binding: Course | null;
  busy: boolean;
  action: Action;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(binding?.name ?? '');
  const [alias, setAlias] = useState(
    binding?.alias ?? (binding?.needs_binding ? binding.name : ''),
  );
  const [provider, setProvider] = useState(binding?.provider ?? 'google_classroom');
  const [remote, setRemote] = useState('');
  const [color, setColor] = useState(binding?.color ?? '');
  const [mergeTarget, setMergeTarget] = useState('');
  const [links, setLinks] = useState<string[]>([
    ...new Set(
      binding?.source_course_ids ?? (binding && !binding.needs_binding ? [binding.id] : []),
    ),
  ]);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const candidates = data.source_courses.filter(
    (c) =>
      c.provider === provider &&
      !links.includes(c.id) &&
      !data.courses.some(
        (local) => local.deleted && (local.source_course_ids ?? [local.id]).includes(c.id),
      ) &&
      !data.courses.some(
        (local) =>
          local.workspace_id &&
          local.workspace_id !== binding?.workspace_id &&
          (local.source_course_ids ?? [local.id]).includes(c.id),
      ),
  );
  const mergeCandidates = data.courses.filter(
    (course) =>
      !course.deleted &&
      (course.workspace_id ?? course.id) !== (binding?.workspace_id ?? binding?.id),
  );
  return (
    <dialog
      className="course-dialog"
      ref={dialog}
      onCancel={close}
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="dialog-heading">
        <h2>{binding ? 'Edit course' : 'Add course'}</h2>
        <button type="button" aria-label="Close course dialog" onClick={close}>
          <X size={18} />
        </button>
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const source_course_ids = [...new Set([...links, ...(remote ? [remote] : [])])];
          const saved = binding
            ? await action(
                `/courses/${encodeURIComponent(binding.workspace_id ?? binding.id)}/sources`,
                'PUT',
                {
                  name: binding.original_name ?? binding.name,
                  alias: alias.trim() || null,
                  source_course_ids,
                  color: color || null,
                },
              )
            : await action('/courses', 'POST', {
                name: name.trim(),
                provider,
                source_course_ids,
                color: color || null,
              });
          if (saved) close();
        }}
      >
        {binding ? (
          <>
            <p className="course-original">{binding.original_name ?? binding.name}</p>
            <label>
              Alias
              <input
                autoFocus
                maxLength={200}
                value={alias}
                placeholder="Use original name"
                onChange={(e) => setAlias(e.target.value)}
              />
            </label>
            {alias && (
              <button type="button" className="add-course-link" onClick={() => setAlias('')}>
                Clear alias
              </button>
            )}
          </>
        ) : (
          <label>
            Course name
            <input
              autoFocus
              required
              maxLength={200}
              value={name}
              placeholder="e.g. Academic Writing"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
        )}
        <label>
          Color
          <input
            type="color"
            aria-label="Course color"
            value={color || courseColor(binding ?? undefined)}
            onChange={(event) => setColor(event.target.value)}
          />
        </label>
        {color && (
          <button type="button" className="add-course-link" onClick={() => setColor('')}>
            Use default color
          </button>
        )}
        <label>
          Source
          <select
            aria-label="Source"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              setRemote('');
            }}
          >
            {data.sources.map((s) => (
              <option key={s.key} value={s.key}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Source course
          <select
            aria-label="Source course"
            value={remote}
            onChange={(e) => setRemote(e.target.value)}
          >
            <option value="">
              {candidates.length ? 'Choose a synced course' : 'No available courses'}
            </option>
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.section ? ` · ${c.section}` : ''}
              </option>
            ))}
          </select>
        </label>
        {remote && (
          <button
            type="button"
            className="add-course-link"
            onClick={() => {
              setLinks([...links, remote]);
              setRemote('');
            }}
          >
            Add link
          </button>
        )}
        {!!links.length && (
          <div className="course-link-list">
            {links.map((id) => {
              const linked = data.source_courses.find((c) => c.id === id);
              return (
                <div key={id}>
                  <span>
                    <small>{data.sources.find((s) => s.key === linked?.provider)?.name}</small>
                    {linked?.name ?? id}
                  </span>
                  <button
                    type="button"
                    aria-label={`Remove link ${linked?.name ?? id}`}
                    onClick={() => setLinks(links.filter((key) => key !== id))}
                  >
                    <X size={15} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" onClick={close}>
            Cancel
          </button>
          <button className="primary" disabled={busy || (!binding && !name.trim())}>
            {binding ? (binding.needs_binding ? 'Link course' : 'Save') : 'Add course'}
          </button>
        </div>
      </form>
      {binding && !!mergeCandidates.length && !binding.deleted && (
        <details>
          <summary>Merge course</summary>
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              if (!mergeTarget) return;
              const saved = await action(
                `/courses/${encodeURIComponent(mergeTarget)}/merge`,
                'POST',
                { course_ids: [binding.workspace_id ?? binding.id] },
              );
              if (saved) close();
            }}
          >
            <label>
              Into
              <select
                aria-label="Merge into course"
                value={mergeTarget}
                onChange={(event) => setMergeTarget(event.target.value)}
              >
                <option value="">Choose a course</option>
                {mergeCandidates.map((course) => (
                  <option key={course.id} value={course.workspace_id ?? course.id}>
                    {course.name}
                    {course.disabled ? ' (disabled)' : ''}
                  </option>
                ))}
              </select>
            </label>
            {mergeTarget && (
              <p className="course-original">The destination keeps its name and color.</p>
            )}
            <div className="dialog-actions">
              <button className="primary" disabled={busy || !mergeTarget}>
                Merge courses
              </button>
            </div>
          </form>
        </details>
      )}
    </dialog>
  );
}
