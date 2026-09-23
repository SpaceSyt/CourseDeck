import { useEffect, useState } from 'react';
import { api } from './api';

type Startup = { supported: boolean; enabled: boolean };

export function DesktopStartup() {
  const [state, setState] = useState<Startup | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void api<Startup>('/desktop/autostart')
      .then((value) => {
        if (active) setState(value);
      })
      .catch(() => {
        if (active) setError('Could not read the Windows startup setting.');
      });
    return () => {
      active = false;
    };
  }, []);

  const update = async (enabled: boolean) => {
    setBusy(true);
    setError('');
    try {
      setState(await api<Startup>('/desktop/autostart', 'PUT', { enabled }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change the startup setting.');
    } finally {
      setBusy(false);
    }
  };

  if (state && !state.supported) return null;
  return (
    <>
      <label className="setting-row">
        <div>
          <b>Start at Windows sign-in</b>
        </div>
        <input
          type="checkbox"
          checked={state?.enabled ?? false}
          disabled={!state || busy}
          onChange={(event) => void update(event.target.checked)}
        />
      </label>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
