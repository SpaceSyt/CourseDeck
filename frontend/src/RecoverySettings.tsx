import { useEffect, useState } from 'react';
import './RecoverySettings.css';

type Backup = {
  id: string;
  created_at?: string;
  valid: boolean;
  reason?: string;
  error?: string;
  legacy?: boolean;
  importable?: boolean;
  upgradable?: boolean;
};
type Preflight = Backup & { replaces: string[]; excludes: string[] };

export function RecoverySettings({ onRestored }: { onRestored?: () => void }) {
  const [backups, setBackups] = useState<Backup[]>([]);
  const [selected, setSelected] = useState('');
  const [preview, setPreview] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [interrupted, setInterrupted] = useState(false);
  const unavailable = backups.filter(
    (backup) => !backup.valid && !backup.importable && !backup.upgradable,
  );

  async function request(path: string, body?: unknown) {
    const response = await fetch(`/api/recovery/${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CourseDeck': '1' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(
        typeof result.detail === 'string' ? result.detail : 'Recovery operation failed.',
      );
    return result;
  }

  async function load() {
    const result = await request('backups');
    setBackups(result.backups);
    setInterrupted(result.interrupted_restore);
  }

  useEffect(() => {
    void load().catch((cause: Error) => setError(cause.message));
  }, []);

  async function run(operation: 'backup' | 'preflight' | 'restore' | 'import' | 'prepare') {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      if (operation === 'backup') {
        await request('backups', {});
        setMessage('Paired backup saved.');
      } else if (operation === 'preflight') {
        setPreview(await request('preflight', { id: selected }));
      } else if (operation === 'import' || operation === 'prepare') {
        const result = await request(operation, { id: selected });
        setSelected(result.id);
        setPreview(result);
        setMessage('Backup copy prepared. Original files retained.');
      } else if (preview?.id === selected) {
        const result = await request('restore', { id: selected });
        setPreview(null);
        setMessage(
          result.pre_restore_backup
            ? `Restored. Recovery backup: ${result.pre_restore_backup}.`
            : 'Restored.',
        );
        onRestored?.();
      }
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Recovery operation failed.');
      if (operation === 'preflight') setPreview(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="source-card recovery-settings">
      <h2>Backup &amp; restore</h2>
      {interrupted && (
        <p className="warning" role="alert">
          A restore was interrupted. Restore a validated backup before continuing.
        </p>
      )}
      <button disabled={busy || interrupted} onClick={() => void run('backup')}>
        Back up now
      </button>
      <label>
        Backup
        <select
          aria-label="Backup"
          value={selected}
          disabled={busy}
          onChange={(event) => {
            setSelected(event.target.value);
            setPreview(null);
          }}
        >
          <option value="">Select backup</option>
          {backups.map((backup) => (
            <option
              key={backup.id}
              value={backup.id}
              disabled={!backup.valid && !backup.importable && !backup.upgradable}
            >
              {backup.created_at ? new Date(backup.created_at).toLocaleString() : backup.id}
              {backup.reason === 'pre_restore' ? ' · Before restore' : ''}
              {backup.legacy ? ' · Legacy' : ''}
              {backup.upgradable ? ' · Upgrade needed' : ''}
              {!backup.valid && !backup.importable && !backup.upgradable ? ' · Invalid' : ''}
            </option>
          ))}
        </select>
      </label>
      {!!unavailable.length && (
        <details>
          <summary>{unavailable.length} unavailable backups</summary>
          {unavailable.map((backup) => (
            <p className="warning" key={backup.id}>
              {backup.id}: {backup.error}
            </p>
          ))}
        </details>
      )}
      <button
        disabled={busy || !selected}
        onClick={() =>
          void run(
            backups.find((backup) => backup.id === selected)?.importable
              ? 'import'
              : backups.find((backup) => backup.id === selected)?.upgradable
                ? 'prepare'
                : 'preflight',
          )
        }
      >
        {backups.find((backup) => backup.id === selected)?.importable
          ? 'Import backup'
          : backups.find((backup) => backup.id === selected)?.upgradable
            ? 'Prepare upgraded copy'
            : 'Check backup'}
      </button>
      {preview && (
        <div>
          <p>Replace current {preview.replaces.join('; ').toLowerCase()}.</p>
          <p>
            {preview.excludes.join(', ')} are excluded.{' '}
            {interrupted
              ? 'Existing backups will be retained.'
              : 'Current data will be backed up first.'}
          </p>
          <button disabled={busy} onClick={() => void run('restore')}>
            Restore this backup
          </button>
        </div>
      )}
      {message && <p role="status">{message}</p>}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
