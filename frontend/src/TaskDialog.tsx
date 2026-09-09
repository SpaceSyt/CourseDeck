import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import type { Course, MailMessage, MailPage, Task } from './types';
import { api } from './api';

function localDate(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function TaskDialog({
  courses,
  email,
  task,
  initialCourse,
  close,
  saved,
}: {
  courses: Course[];
  email?: MailMessage;
  task?: Task;
  initialCourse?: string;
  close: () => void;
  saved: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(task?.title ?? email?.subject ?? '');
  const [description, setDescription] = useState(task?.description ?? '');
  const [due, setDue] = useState(localDate(task?.due_at));
  const suggestedCourse = email?.course_id ?? initialCourse;
  const [course, setCourse] = useState(
    task?.course_id ?? (courses.some((c) => c.id === suggestedCourse) ? suggestedCourse! : ''),
  );
  const [emailId, setEmailId] = useState(task?.email_id ?? email?.id ?? '');
  const [messages, setMessages] = useState<MailMessage[]>(email ? [email] : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    dialog.current?.showModal();
    let active = true;
    void api<MailPage>('/mail?limit=100')
      .then(async (page) => {
        const list = page.messages;
        const linkedId = task?.email_id ?? email?.id;
        if (linkedId && !list.some((m) => m.id === linkedId)) {
          list.unshift(await api<MailMessage>(`/mail/messages/${encodeURIComponent(linkedId)}`));
        }
        if (active) setMessages(list);
      })
      .catch(() => {
        if (active) setError('Email list unavailable. You can still save this task.');
      });
    return () => {
      active = false;
    };
  }, [task?.email_id, email?.id]);
  return (
    <dialog ref={dialog} className="course-dialog task-dialog" onCancel={close}>
      <div className="dialog-heading">
        <h2>{task ? 'Edit task' : 'Add task'}</h2>
        <button aria-label="Close task dialog" onClick={close}>
          <X size={18} />
        </button>
      </div>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            await api(
              task ? `/custom-tasks/${encodeURIComponent(task.id)}` : '/custom-tasks',
              task ? 'PUT' : 'POST',
              {
                title: title.trim(),
                description,
                due_at: due ? new Date(due).toISOString() : null,
                course_id: course || null,
                email_id: emailId || null,
              },
            );
            saved();
            close();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Title
          <input
            autoFocus
            required
            maxLength={500}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          Due date ({Intl.DateTimeFormat().resolvedOptions().timeZone})
          <input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} />
        </label>
        <label>
          Course
          <select aria-label="Course" value={course} onChange={(e) => setCourse(e.target.value)}>
            <option value="">No course</option>
            {courses.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Email
          <select aria-label="Email" value={emailId} onChange={(e) => setEmailId(e.target.value)}>
            <option value="">No email</option>
            {messages.map((m) => (
              <option key={m.id} value={m.id}>
                {m.subject || '(No subject)'}
              </option>
            ))}
          </select>
        </label>
        <label>
          Details
          <textarea
            value={description}
            maxLength={50000}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="warning">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" onClick={close}>
            Cancel
          </button>
          <button className="primary" disabled={busy || !title.trim()}>
            {task ? 'Save' : 'Add task'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
