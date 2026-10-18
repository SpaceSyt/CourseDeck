import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { Action } from './SourceCard';
import type { Settings } from './types';

export type ReminderPreferences = {
  enabled: boolean;
  lead_24h: boolean;
  lead_2h: boolean;
  deadline_earlier: boolean;
  quiet_enabled: boolean;
  quiet_start: string;
  quiet_end: string;
};

type ReminderStatus = {
  snoozed_until: string | null;
  last_batch: {
    count: number;
    items: { task_id: string; title: string; due_at: string; warning: string | null }[];
  } | null;
};

const defaults: ReminderPreferences = {
  enabled: false,
  lead_24h: true,
  lead_2h: true,
  deadline_earlier: true,
  quiet_enabled: true,
  quiet_start: '22:00',
  quiet_end: '08:00',
};

export function DeadlineReminders({
  settings,
  busy,
  action,
}: {
  settings: Settings;
  busy: boolean;
  action: Action;
}) {
  const [draft, setDraft] = useState(settings.reminders ?? defaults);
  const [status, setStatus] = useState<ReminderStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const locked = useRef(false);
  const active = useRef(false);
  const changed = JSON.stringify(draft) !== JSON.stringify(settings.reminders ?? defaults);
  const date = (value: string) =>
    new Intl.DateTimeFormat('en-US', {
      timeZone: settings.timezone,
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZoneName: 'short',
    }).format(new Date(value));

  useEffect(() => {
    active.current = true;
    let current = true;
    const load = () =>
      api<ReminderStatus>('/reminders/status')
        .then((value) => {
          if (current) setStatus(value);
        })
        .catch(() => {
          if (current) setError('Could not read reminder status.');
        });
    void load();
    const timer = setInterval(() => void load(), 30000);
    return () => {
      current = false;
      active.current = false;
      clearInterval(timer);
    };
  }, []);

  const snooze = async () => {
    if (locked.current) return;
    locked.current = true;
    setPending(true);
    setError('');
    try {
      await api('/reminders/snooze', 'POST');
      const value = await api<ReminderStatus>('/reminders/status');
      if (active.current) setStatus(value);
    } catch (cause) {
      if (active.current) setError((cause as Error).message);
    } finally {
      locked.current = false;
      if (active.current) setPending(false);
    }
  };

  return (
    <section aria-label="Deadline reminders">
      <h2>Deadline reminders</h2>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (locked.current) return;
          locked.current = true;
          setPending(true);
          try {
            await action('/settings', 'PUT', { ...settings, reminders: draft });
          } finally {
            locked.current = false;
            if (active.current) setPending(false);
          }
        }}
      >
        {(
          [
            ['enabled', 'Enable deadline reminders'],
            ['lead_24h', '24 hours before'],
            ['lead_2h', '2 hours before'],
            ['deadline_earlier', 'Deadline moved earlier'],
            ['quiet_enabled', 'Quiet hours'],
          ] as const
        ).map(([key, label]) => (
          <label className="setting-row" key={key}>
            <b>{label}</b>
            <input
              type="checkbox"
              checked={draft[key]}
              disabled={busy || pending}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.checked })}
            />
          </label>
        ))}
        {draft.quiet_enabled && (
          <div className="setting-row">
            <b>{settings.timezone}</b>
            <div className="button-row">
              <input
                type="time"
                aria-label="Quiet hours start"
                required
                value={draft.quiet_start}
                disabled={busy || pending}
                onChange={(event) => setDraft({ ...draft, quiet_start: event.target.value })}
              />
              <span>to</span>
              <input
                type="time"
                aria-label="Quiet hours end"
                required
                value={draft.quiet_end}
                disabled={busy || pending}
                onChange={(event) => setDraft({ ...draft, quiet_end: event.target.value })}
              />
            </div>
          </div>
        )}
        <div className="setting-row">
          <div>
            <p>Notifications require CourseDeck running in the Windows tray.</p>
          </div>
          <button type="submit" disabled={busy || pending || !changed}>
            Save reminders
          </button>
        </div>
      </form>
      {status?.snoozed_until && new Date(status.snoozed_until).getTime() > Date.now() && (
        <p role="status">Snoozed until {date(status.snoozed_until)}</p>
      )}
      <button
        disabled={busy || pending || !settings.reminders?.enabled || !status}
        onClick={() => void snooze()}
      >
        Snooze 30 minutes
      </button>
      {status?.last_batch && (
        <details>
          <summary>Recent reminder ({status.last_batch.count})</summary>
          <ul>
            {status.last_batch.items.map((item) => (
              <li key={item.task_id}>
                {item.title} · {date(item.due_at)}
                {item.warning && <p className="warning">{item.warning}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
