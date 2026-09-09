import { useEffect, useState } from 'react';

export function useHeartbeat() {
  const [status, setStatus] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    const ping = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 2500);
      try {
        const response = await fetch('/api/heartbeat', {
          cache: 'no-store',
          signal: controller.signal,
        });
        const body = await response.json();
        if (!response.ok || body.service !== 'coursedeck' || body.status !== 'connected')
          throw new Error('Invalid heartbeat');
        if (!stopped) setStatus('connected');
      } catch {
        if (!stopped) setStatus('disconnected');
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(ping, 3000);
      }
    };
    void ping();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
    };
  }, []);
  return status;
}
