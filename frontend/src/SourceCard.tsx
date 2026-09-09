import { useState } from 'react';
import { ArrowUpRight, Link2, Settings2 } from 'lucide-react';
import type { Source } from './types';

export type Action = (path: string, method?: string, body?: unknown) => Promise<boolean>;

export function SourceCard({
  source: s,
  busy,
  date,
  action,
}: {
  source: Source;
  busy: boolean;
  date: (value: string | null) => string;
  action: Action;
}) {
  const [config, setConfig] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [configError, setConfigError] = useState('');
  const openConfig = () => {
    setConfig(!config);
    setFields(
      Object.fromEntries(
        Object.entries(s.configuration_values ?? {}).map(([key, value]) => [
          key,
          typeof value === 'string' ? value : JSON.stringify(value),
        ]),
      ),
    );
  };
  const save = async () => {
    try {
      const value = Object.fromEntries(
        (s.configuration_fields ?? [])
          .filter((f) => fields[f.key]?.trim())
          .map((f) => [
            f.key,
            f.type === 'json'
              ? JSON.parse(fields[f.key])
              : f.type === 'number'
                ? Number(fields[f.key])
                : fields[f.key].trim(),
          ]),
      );
      setConfigError('');
      if (await action(`/sources/${s.key}/configure`, 'PUT', value)) {
        setFields({});
        setConfig(false);
      }
    } catch {
      setConfigError('Check the field formats. URL lists must be a valid JSON array.');
    }
  };
  const status = s.syncing
    ? 'Syncing'
    : s.status === 'connected' && s.last_outcome && s.last_outcome !== 'success'
      ? s.last_outcome.replaceAll('_', ' ')
      : s.status.replaceAll('_', ' ');
  return (
    <article className="source-card" id={`source-${s.key}`}>
      <div className="source-title">
        <span className={`course-icon ${s.key}`}>
          <Link2 size={21} />
        </span>
        <span className={`status ${s.status}`}>{status}</span>
      </div>
      <h2>{s.name}</h2>
      {s.last_attempted_sync && <small>Last attempt · {date(s.last_attempted_sync)}</small>}
      {s.warnings?.map((warning, i) => (
        <div className="warning" key={i}>
          {warning}
        </div>
      ))}
      <div className="button-row">
        <button
          className="primary"
          disabled={busy || s.syncing}
          onClick={() => {
            if (s.status === 'not_configured') {
              if (!config) openConfig();
              return;
            }
            void action(
              `/sources/${s.key}/${s.status === 'connected' && s.last_outcome !== 'auth_required' ? 'sync' : 'connect'}`,
            );
          }}
        >
          {s.status === 'not_configured'
            ? 'Set up'
            : s.status === 'connected'
              ? s.last_outcome === 'auth_required'
                ? 'Reconnect'
                : 'Sync'
              : s.status === 'login_pending'
                ? 'Reopen login'
                : 'Connect'}
          <ArrowUpRight size={14} />
        </button>
        {s.status === 'login_pending' && s.manual_login && (
          <button disabled={busy} onClick={() => action(`/sources/${s.key}/finish-login`)}>
            Finish login
          </button>
        )}
        {!['not_connected', 'not_configured'].includes(s.status) && (
          <button
            disabled={busy || s.syncing}
            onClick={() => action(`/sources/${s.key}/disconnect`)}
          >
            Disconnect
          </button>
        )}
        {s.status === 'connected' && s.manual_login && (
          <button disabled={busy || s.syncing} onClick={() => action(`/sources/${s.key}/connect`)}>
            Reconnect
          </button>
        )}
        {!!s.configuration_fields?.length && (
          <button onClick={openConfig} aria-label={`Configure ${s.name}`}>
            <Settings2 size={16} />
          </button>
        )}
      </div>
      {config && (
        <form
          className="source-config"
          autoComplete="off"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          {s.configuration_fields?.map((field) => (
            <label key={field.key}>
              {field.label}
              {field.type === 'json' ? (
                <textarea
                  value={fields[field.key] ?? ''}
                  placeholder={field.placeholder}
                  onChange={(e) => setFields({ ...fields, [field.key]: e.target.value })}
                />
              ) : field.type === 'select' ? (
                <select
                  aria-label={field.label}
                  value={fields[field.key] ?? field.options?.[0]}
                  onChange={(e) => setFields({ ...fields, [field.key]: e.target.value })}
                >
                  {field.options?.map((option) => <option key={option}>{option}</option>)}
                </select>
              ) : (
                <input
                  type={field.type}
                  value={fields[field.key] ?? ''}
                  placeholder={
                    field.placeholder ??
                    (field.type === 'password' ? 'Enter to replace saved credential' : '')
                  }
                  onChange={(e) => setFields({ ...fields, [field.key]: e.target.value })}
                />
              )}
              {field.help && <small>{field.help}</small>}
            </label>
          ))}
          {configError && <p role="alert">{configError}</p>}
          <button type="submit" disabled={busy}>
            Save configuration
          </button>
        </form>
      )}
    </article>
  );
}
