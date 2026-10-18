import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { api } from './api';

const localHost = ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
const NetworkContext = createContext({ local: localHost });
export const useNetworkSession = () => useContext(NetworkContext);

export function NetworkGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ local: boolean; authenticated: boolean } | null>(
    localHost ? { local: true, authenticated: true } : null,
  );
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (localHost) return;
    let active = true;
    const check = () => {
      void api<{ local: boolean; authenticated: boolean }>('/network/session')
        .then((value) => {
          if (active) setSession(value);
        })
        .catch(() => {
          if (active) {
            setSession({ local: false, authenticated: false });
            setError('Could not verify LAN access. Check the connection and try again.');
          }
        });
    };
    check();
    const timer = window.setInterval(check, 15000);
    window.addEventListener('focus', check);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener('focus', check);
    };
  }, []);

  async function login() {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await api('/network/login', 'POST', { password });
      setPassword('');
      const value = await api<{ local: boolean; authenticated: boolean }>('/network/session');
      setSession(value);
      if (!value.authenticated)
        setError('Sign-in could not be verified. Check HTTPS and cookie settings.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in.');
    } finally {
      setPassword('');
      setBusy(false);
    }
  }

  if (session?.local || session?.authenticated) {
    return (
      <NetworkContext.Provider value={{ local: session.local }}>{children}</NetworkContext.Provider>
    );
  }
  return (
    <main className="settings-panel" style={{ maxWidth: 480, margin: '10vh auto', padding: 20 }}>
      <section className="ai-settings" aria-labelledby="network-login-heading">
        <h1 id="network-login-heading">CourseDeck</h1>
        {!session ? (
          <p role="status">Checking access…</p>
        ) : (
          <>
            <h2>LAN sign-in</h2>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void login();
              }}
            >
              <label>
                LAN password
                <input
                  type="password"
                  required
                  maxLength={256}
                  autoComplete="current-password"
                  value={password}
                  disabled={busy}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </label>
              {error && (
                <p className="warning" role="alert">
                  {error}
                </p>
              )}
              <button className="primary" disabled={busy || !password}>
                Sign in
              </button>
            </form>
            <p className="chat-config-note">
              Use the password set on the computer running CourseDeck. This opens its shared
              workspace.
            </p>
          </>
        )}
      </section>
    </main>
  );
}

export function NetworkLogout() {
  const { local } = useNetworkSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (local) return null;
  const logout = async () => {
    setBusy(true);
    setError('');
    try {
      await api('/network/logout', 'POST');
      window.location.reload();
    } catch {
      setError('Could not sign out. Check the connection and retry.');
      setBusy(false);
    }
  };
  return (
    <>
      <button className="nav-item" disabled={busy} onClick={() => void logout()}>
        Sign out
      </button>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
