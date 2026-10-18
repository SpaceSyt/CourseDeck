import { useEffect, useState } from 'react';
import { api } from './api';

type NetworkConfig = {
  enabled: boolean;
  host: string;
  port: number;
  certfile: string;
  keyfile: string;
  password_set: boolean;
  active: boolean;
  active_url: string | null;
  restart_required: boolean;
  error: string | null;
};

export function NetworkSettings() {
  const [config, setConfig] = useState<NetworkConfig | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [host, setHost] = useState('');
  const [port, setPort] = useState('48322');
  const [certfile, setCertfile] = useState('');
  const [keyfile, setKeyfile] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  function apply(value: NetworkConfig) {
    setConfig(value);
    setEnabled(value.enabled);
    setHost(value.host);
    setPort(String(value.port));
    setCertfile(value.certfile);
    setKeyfile(value.keyfile);
    setPassword('');
  }

  useEffect(() => {
    let active = true;
    void api<NetworkConfig>('/network')
      .then((value) => {
        if (active) apply(value);
      })
      .catch((err: Error) => {
        if (active) setError(err.message);
      });
    return () => {
      active = false;
    };
  }, []);

  async function save(disable = false) {
    if (busy) return;
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const value = await api<NetworkConfig>(
        '/network',
        'PUT',
        disable || !enabled
          ? { enabled: false }
          : {
              enabled: true,
              host: host.trim(),
              port: Number(port),
              certfile: certfile.trim(),
              keyfile: keyfile.trim(),
              ...(password ? { password } : {}),
            },
      );
      apply(value);
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save LAN access.');
    } finally {
      setPassword('');
      setBusy(false);
    }
  }

  const dirty =
    config &&
    (!!config.error ||
      enabled !== config.enabled ||
      (enabled &&
        (host.trim() !== config.host ||
          Number(port) !== config.port ||
          certfile.trim() !== config.certfile ||
          keyfile.trim() !== config.keyfile ||
          !!password)));

  return (
    <section className="ai-settings" aria-labelledby="network-heading">
      <h2 id="network-heading">LAN access</h2>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {config?.error && (
        <p className="warning" role="alert">
          {config.error}
        </p>
      )}
      {!config ? (
        <p>{error ? 'LAN settings unavailable' : 'Loading…'}</p>
      ) : (
        <>
          <p role="status">
            {config.active ? `Active: ${config.active_url}` : 'Local access only'}
            {config.restart_required &&
              (config.enabled
                ? ' · Restart CourseDeck to apply LAN access.'
                : ' · Restart CourseDeck to apply network changes.')}
          </p>
          {config.enabled && (
            <p className="chat-config-note">
              Configured address: https://{config.host}:{config.port}
            </p>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
            onChange={() => setSaved(false)}
          >
            <label className="chat-checkbox">
              <input
                type="checkbox"
                checked={enabled}
                disabled={busy}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enable LAN access after restart
            </label>
            {enabled && (
              <>
                <label>
                  LAN IPv4 address
                  <input
                    required
                    value={host}
                    disabled={busy}
                    placeholder="192.168.1.10"
                    autoComplete="off"
                    onChange={(event) => setHost(event.target.value)}
                  />
                </label>
                <label>
                  HTTPS port
                  <input
                    type="number"
                    required
                    min={1024}
                    max={65535}
                    step={1}
                    value={port}
                    disabled={busy}
                    onChange={(event) => setPort(event.target.value)}
                  />
                </label>
                <label>
                  TLS certificate path
                  <input
                    required
                    value={certfile}
                    disabled={busy}
                    placeholder="Absolute path on this computer"
                    autoComplete="off"
                    onChange={(event) => setCertfile(event.target.value)}
                  />
                </label>
                <label>
                  TLS private key path
                  <input
                    required
                    value={keyfile}
                    disabled={busy}
                    placeholder="Absolute path on this computer"
                    autoComplete="off"
                    onChange={(event) => setKeyfile(event.target.value)}
                  />
                </label>
                <label>
                  LAN password
                  <input
                    type="password"
                    required={!config.password_set}
                    minLength={16}
                    maxLength={256}
                    value={password}
                    disabled={busy}
                    autoComplete="new-password"
                    placeholder={
                      config.password_set ? 'Saved — leave blank to keep' : 'At least 16 characters'
                    }
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </label>
                <p className="chat-config-note">
                  Anyone with this password can read and change this workspace, including course
                  materials, mail and attachments. Administration stays on this computer.
                </p>
                <details>
                  <summary>Connection and security</summary>
                  <p className="chat-config-note">
                    Use this computer’s private IPv4 address and a certificate valid for that
                    address, trusted by each connecting device. Keep the private key on this
                    computer. Use only a trusted network; do not forward the port on your router.
                    CourseDeck does not change firewall rules.
                  </p>
                </details>
              </>
            )}
            <p className="chat-config-note">
              Save, then restart CourseDeck to enable or change the address. Disable here on this
              computer to revoke LAN sessions and close the listener immediately.
            </p>
            <div className="chat-settings-actions">
              {saved && <span role="status">Saved</span>}
              {(config.enabled || config.active) && (
                <button type="button" disabled={busy} onClick={() => void save(true)}>
                  Disable LAN access
                </button>
              )}
              <button className="primary" disabled={busy || !dirty}>
                Save LAN access
              </button>
            </div>
          </form>
        </>
      )}
    </section>
  );
}
