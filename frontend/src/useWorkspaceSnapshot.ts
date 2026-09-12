import { useCallback, useEffect, useRef, useState } from 'react';
import type { Snapshot } from './types';

type Observation = { data?: Snapshot; offline: boolean; recoveryRequired: boolean };

export class WorkspaceSnapshotReader {
  private etag: string | null = null;
  private pending: Promise<void> | null = null;
  private again = false;
  private closed = false;
  private abort: AbortController | null = null;

  constructor(private publish: (value: Observation) => void) {}

  load(): Promise<void> {
    if (this.closed) return Promise.resolve();
    this.again = true;
    if (!this.pending) this.pending = this.drain().finally(() => (this.pending = null));
    return this.pending;
  }

  close() {
    this.closed = true;
    this.abort?.abort();
  }

  private async drain() {
    while (this.again && !this.closed) {
      this.again = false;
      this.abort = new AbortController();
      const signal = AbortSignal.any([this.abort.signal, AbortSignal.timeout(10000)]);
      try {
        const response = await fetch('/api/snapshot', {
          headers: this.etag ? { 'If-None-Match': this.etag } : {},
          signal,
        });
        if (!response.ok && response.status !== 304) throw new Error('Snapshot unavailable');
        const data = response.status === 304 ? undefined : ((await response.json()) as Snapshot);
        // Do not remember an ETag for a response superseded by a user mutation.
        if (!this.closed && !this.again) {
          this.etag = response.headers.get('ETag');
          this.publish({ data, offline: false, recoveryRequired: false });
        }
      } catch {
        let recoveryRequired = false;
        if (!this.closed) {
          try {
            const response = await fetch('/api/recovery/status', { signal });
            if (response.ok) recoveryRequired = (await response.json()).recovery_required === true;
          } catch {
            // A failed status request does not clear cached content.
          }
          if (!this.closed && !this.again) this.publish({ offline: true, recoveryRequired });
        }
      }
    }
  }
}

export function useWorkspaceSnapshot() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [offline, setOffline] = useState(false);
  const [recoveryRequired, setRecoveryRequired] = useState(false);
  const reader = useRef<WorkspaceSnapshotReader | null>(null);
  const load = useCallback(async () => reader.current?.load(), []);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const current = new WorkspaceSnapshotReader((value) => {
      if (value.data) setData(value.data);
      setOffline(value.offline);
      setRecoveryRequired(value.recoveryRequired);
    });
    reader.current = current;
    const poll = async () => {
      await current.load();
      if (!stopped) timer = setTimeout(() => void poll(), 3000);
    };
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      current.close();
      if (reader.current === current) reader.current = null;
    };
  }, []);
  return { data, setData, offline, recoveryRequired, load };
}
