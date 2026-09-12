import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import type { Task } from './types';

type History = {
  version: string;
  changes: { id: string; fields: string[]; created_at: string; undone: boolean }[];
};

export function TaskHistory({ task, reload }: { task: Task; reload: () => Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const path = `/task-edits/${encodeURIComponent(task.id)}`;
  const local = JSON.stringify(task.local);
  const refresh = useCallback(async () => {
    try {
      setHistory(await api<History>(path));
      setError('');
    } catch (error) {
      setError((error as Error).message);
    }
  }, [path]);
  useEffect(() => {
    if (open) void refresh();
  }, [open, local, refresh]);
  const undo = async (id: string) => {
    if (!history) return;
    setBusy(true);
    try {
      await api(`${path}/undo`, 'POST', { change_id: id, expected_version: history.version });
      await refresh();
      await reload();
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const latest = history?.changes.find((change) => !change.undone)?.id;
  return (
    <details className="task-edit-history" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Local edits</summary>
      {error && (
        <div role="alert">
          <p>{error}</p>
          <button disabled={busy} onClick={() => void refresh()}>
            Refresh edits
          </button>
        </div>
      )}
      {open && !history && !error && <p role="status">Loading…</p>}
      {history?.changes.length === 0 && <p>No local edits</p>}
      {history?.changes.map((change) => (
        <div className="button-row" key={change.id}>
          <span>{change.fields.map((field) => field.replaceAll('_', ' ')).join(', ')}</span>
          <time dateTime={change.created_at}>{new Date(change.created_at).toLocaleString()}</time>
          {change.undone ? (
            <span>Undone</span>
          ) : (
            change.id === latest && (
              <button disabled={busy} onClick={() => void undo(change.id)}>
                Undo
              </button>
            )
          )}
        </div>
      ))}
    </details>
  );
}
