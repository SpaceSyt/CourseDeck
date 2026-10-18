import { afterEach, expect, it, vi } from 'vitest';
import { WorkspaceSnapshotReader } from './useWorkspaceSnapshot';

afterEach(() => vi.unstubAllGlobals());
const response = (etag: string, value?: object) =>
  new Response(value ? JSON.stringify(value) : null, {
    status: value ? 200 : 304,
    headers: { ETag: etag },
  });

it('reuses ETag without clearing cached data and preserves data on failure', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(response('a', { tasks: [1] }))
    .mockResolvedValueOnce(response('a'))
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(new Response(JSON.stringify({ recovery_required: true })));
  vi.stubGlobal('fetch', fetch);
  const publish = vi.fn();
  const reader = new WorkspaceSnapshotReader(publish);
  await reader.load();
  await reader.load();
  expect(fetch.mock.calls[1][1].headers).toEqual({ 'If-None-Match': 'a' });
  expect(publish.mock.calls[1][0]).toEqual({
    data: undefined,
    offline: false,
    recoveryRequired: false,
  });
  await reader.load();
  expect(publish.mock.calls[2][0]).toEqual({ offline: true, recoveryRequired: true });
  reader.close();
});

it('serializes reloads and fetches again after a mutation during an in-flight read', async () => {
  let finish!: (response: Response) => void;
  const fetch = vi
    .fn()
    .mockReturnValueOnce(new Promise<Response>((resolve) => (finish = resolve)))
    .mockResolvedValueOnce(response('b', { tasks: [2] }));
  vi.stubGlobal('fetch', fetch);
  const publish = vi.fn();
  const reader = new WorkspaceSnapshotReader(publish);
  const first = reader.load();
  const second = reader.load();
  expect(fetch).toHaveBeenCalledTimes(1);
  finish(response('a', { tasks: [1] }));
  await Promise.all([first, second]);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(publish).toHaveBeenCalledTimes(1);
  expect(publish.mock.calls[0][0].data.tasks).toEqual([2]);
  reader.close();
});

it('closing a reader prevents a late response from updating a remounted view', async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>((resolve) => (finish = resolve))),
  );
  const publish = vi.fn();
  const reader = new WorkspaceSnapshotReader(publish);
  const read = reader.load();
  reader.close();
  finish(response('a', { tasks: [1] }));
  await read;
  expect(publish).not.toHaveBeenCalled();
});
