import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { api } from './api';
import type { ChatConfig } from './chat-types';
import './chat.css';

export function AISettings() {
  const [config, setConfig] = useState<ChatConfig | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let active = true;
    void api<ChatConfig>('/chat/config')
      .then((value) => {
        if (active) {
          setConfig(value);
          setBaseUrl(value.base_url);
          setModel(value.model);
        }
      })
      .catch((e: Error) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, []);
  const endpointChanged =
    config && baseUrl.trim().replace(/\/$/, '') !== config.base_url.replace(/\/$/, '');
  const dirty = config && (endpointChanged || model.trim() !== config.model || !!key || clearKey);
  const save = async () => {
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const value = await api<ChatConfig>('/chat/config', 'PUT', {
        base_url: baseUrl.trim(),
        model: model.trim(),
        ...(key ? { api_key: key } : {}),
        ...(clearKey ? { clear_api_key: true } : {}),
      });
      setConfig(value);
      setBaseUrl(value.base_url);
      setModel(value.model);
      setKey('');
      setClearKey(false);
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="ai-settings">
      <h2>Chat API</h2>
      {config?.credential_error && (
        <p className="warning" role="alert">
          {config.credential_error}
        </p>
      )}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {!config ? (
        <p className="chat-placeholder">{error ? 'Configuration unavailable' : 'Loading…'}</p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label>
            Base URL
            <input
              type="url"
              required
              value={baseUrl}
              disabled={busy}
              placeholder="https://api.example.com/v1"
              onChange={(e) => {
                setBaseUrl(e.target.value);
                setSaved(false);
              }}
            />
          </label>
          <label>
            Model
            <input
              required
              value={model}
              disabled={busy}
              placeholder="Model ID"
              onChange={(e) => {
                setModel(e.target.value);
                setSaved(false);
              }}
            />
          </label>
          <label>
            API key
            <input
              type="password"
              autoComplete="new-password"
              value={key}
              disabled={busy || clearKey}
              placeholder={
                config.has_api_key && !endpointChanged
                  ? 'Saved · leave blank to keep'
                  : 'Optional for local providers'
              }
              onChange={(e) => {
                setKey(e.target.value);
                setSaved(false);
              }}
            />
          </label>
          {config.has_api_key && (
            <label className="chat-checkbox">
              <input
                type="checkbox"
                checked={clearKey}
                disabled={busy}
                onChange={(e) => {
                  setClearKey(e.target.checked);
                  setKey('');
                  setSaved(false);
                }}
              />{' '}
              Remove saved key
            </label>
          )}
          {endpointChanged && config.has_api_key && (
            <p className="warning">Changing the endpoint removes the saved key.</p>
          )}
          <p className="chat-config-note">
            Chat sends relevant course content to this API endpoint.
          </p>
          {config.builtin_prompt && (
            <details>
              <summary>Built-in prompt</summary>
              <pre>{config.builtin_prompt}</pre>
            </details>
          )}
          <div className="chat-settings-actions">
            {saved && <span role="status">Saved</span>}
            <button className="primary" disabled={busy || !dirty}>
              {busy && <Loader2 size={16} className="spin" />} Save
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
