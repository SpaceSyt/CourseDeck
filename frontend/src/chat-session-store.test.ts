import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { ChatSessionStore } from './chat-session-store';

beforeEach(() => vi.stubGlobal('window', { dispatchEvent: vi.fn() }));
afterEach(() => vi.unstubAllGlobals());
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function stream(signal: AbortSignal) {
  let source!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      source = controller;
    },
  });
  signal.addEventListener('abort', () => source.error(new DOMException('Aborted', 'AbortError')));
  return {
    response: new Response(body),
    emit(event: object) {
      source.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
    },
    close() {
      source.close();
    },
  };
}

test('concurrent streams keep messages, drafts, stop and errors in their own session without subscribers', async () => {
  const streams: ReturnType<typeof stream>[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, options) => {
      if (url.endsWith('/stream')) {
        const next = stream(options.signal);
        streams.push(next);
        return next.response;
      }
      return json({ conversations: [] });
    }),
  );
  const store = new ChatSessionStore();
  const a = store.state.selected;
  store.patch(a, { draft: 'question A' });
  const first = store.send(a);
  streams[0].emit({
    type: 'session',
    conversation_id: 'a',
    message: { id: 'ua', role: 'user', content: 'question A' },
  });
  streams[0].emit({ type: 'delta', text: 'answer A' });
  const b = store.newConversation();
  store.patch(b, { draft: 'question B' });
  const second = store.send(b);
  streams[1].emit({
    type: 'session',
    conversation_id: 'b',
    message: { id: 'ub', role: 'user', content: 'question B' },
  });
  streams[1].emit({ type: 'delta', text: 'partial B' });
  await vi.waitFor(() => expect(store.state.sessions[b].streamText).toBe('partial B'));
  store.patch(a, { draft: 'next A' });
  store.patch(b, { draft: 'next B' });
  await store.select('a');
  await store.remove('a');
  expect(store.state.sessions[a].busy).toBe(true);
  store.stop(b);
  await second;
  expect(store.state.sessions[b].error).toBe('Stopped.');
  expect(store.state.sessions[a].busy).toBe(true);
  streams[0].emit({ type: 'done', message: { id: 'aa', role: 'assistant', content: 'answer A' } });
  streams[0].close();
  await first;
  expect(store.state.selected).toBe(a);
  expect(store.state.sessions[a].messages.map((item) => item.content)).toEqual([
    'question A',
    'answer A',
  ]);
  expect(store.state.sessions[a].error).toBe('');
  expect(store.state.sessions[a].draft).toBe('next A');
  expect(store.state.sessions[b].draft).toBe('next B');
  expect(store.state.sessions[b].messages.at(-1)?.warnings).toEqual(['Reply interrupted.']);
});

test.each(['network', 'stop'])(
  'existing conversation preserves an unaccepted input on %s failure',
  async (failure) => {
    const store = new ChatSessionStore();
    const key = store.state.selected;
    store.patch(key, {
      id: 'existing',
      draft: 'unsent',
      messages: [{ id: 'old', role: 'user', content: 'old message' }],
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url, options) => {
        if (url.endsWith('/stream')) {
          if (failure === 'network') throw new Error('Offline');
          return new Promise((_, reject) =>
            options.signal.addEventListener('abort', () =>
              reject(new DOMException('Aborted', 'AbortError')),
            ),
          );
        }
        if (url.endsWith('/existing'))
          return json({
            id: 'existing',
            title: 'Old chat',
            course_id: null,
            messages: [{ id: 'old', role: 'user', content: 'old message' }],
          });
        return json({ conversations: [] });
      }),
    );
    const sending = store.send(key);
    if (failure === 'stop') store.stop(key);
    await sending;
    expect(store.state.sessions[key].draft).toBe('unsent');
    expect(store.state.sessions[key].messages).toHaveLength(1);
  },
);

test('recovery of a saved user message does not duplicate the draft', async () => {
  const store = new ChatSessionStore();
  const key = store.state.selected;
  store.patch(key, { draft: 'saved question' });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) => {
      if (url.endsWith('/stream'))
        return json({ detail: { message: 'Model failed', conversation_id: 'saved' } }, 502);
      if (url.endsWith('/saved'))
        return json({
          id: 'saved',
          title: 'Saved',
          messages: [{ id: 'user', role: 'user', content: 'saved question' }],
        });
      return json({ conversations: [] });
    }),
  );
  await store.send(key);
  expect(store.state.sessions[key].draft).toBe('');
  expect(store.state.sessions[key].messages).toHaveLength(1);
  expect(store.state.sessions[key].error).toBe('Model failed');
});

test('reopening an interrupted session recovers server receipts using its saved ID', async () => {
  const store = new ChatSessionStore();
  const key = store.state.selected;
  store.patch(key, { draft: 'Read a source' });
  let active!: ReturnType<typeof stream>;
  const fetch = vi.fn(async (url, options) => {
    if (url.endsWith('/stream')) {
      active = stream(options.signal);
      return active.response;
    }
    if (url.endsWith('/saved'))
      return json({
        id: 'saved',
        title: 'Read a source',
        messages: [
          { id: 'user', role: 'user', content: 'Read a source' },
          {
            id: 'receipt',
            role: 'assistant',
            content: '',
            activity: [{ id: 'read', label: 'Source read', status: 'done' }],
          },
        ],
      });
    return json({ conversations: [] });
  });
  vi.stubGlobal('fetch', fetch);
  const sending = store.send(key);
  active.emit({
    type: 'session',
    conversation_id: 'saved',
    message: { id: 'user', role: 'user', content: 'Read a source' },
  });
  await vi.waitFor(() => expect(store.state.sessions[key].id).toBe('saved'));
  store.stop(key);
  await sending;
  expect(store.state.sessions[key].loaded).toBe(false);
  await store.select(key);
  expect(store.state.sessions[key].messages.at(-1)?.activity?.[0].status).toBe('done');
  expect(store.state.sessions[key].loaded).toBe(true);
});

test('cached history switches immediately and current deletion always publishes a valid selection', async () => {
  const fetch = vi.fn(async (url, options) => {
    if (options.method === 'DELETE') return json({ id: 'saved' });
    if (url.endsWith('/saved'))
      return json({ id: 'saved', title: 'Saved', course_id: null, messages: [] });
    return json({ conversations: [{ id: 'saved', title: 'Saved', course_id: null }] });
  });
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  await store.refresh();
  await store.select('saved');
  store.newConversation();
  await store.select('saved');
  expect(
    fetch.mock.calls.filter(([url, options]) => url.endsWith('/saved') && options.method === 'GET'),
  ).toHaveLength(1);
  store.subscribe(() => expect(store.state.sessions[store.state.selected]).toBeDefined());
  await store.remove('saved');
  expect(store.state.conversations).toHaveLength(0);
  expect(store.state.sessions[store.state.selected].id).toBeNull();
  await store.refresh();
  expect(store.state.conversations).toHaveLength(0);
});
