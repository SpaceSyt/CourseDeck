import { useRef, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import type { Course, Snapshot } from './types';
import type { Action } from './SourceCard';

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
  const [links, setLinks] = useState<string[]>(
    binding?.source_course_ids ?? (binding && !binding.needs_binding ? [binding.id] : []),
  );
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
                },
              )
            : await action('/courses', 'POST', {
                name: name.trim(),
                provider,
                source_course_ids,
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
    </dialog>
  );
}
