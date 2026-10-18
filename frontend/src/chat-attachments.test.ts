import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { ChatSessionStore, MAX_CHAT_FILE_SIZE } from './chat-session-store';
import type { ChatAttachment } from './chat-types';

beforeEach(() => vi.stubGlobal('window', { dispatchEvent: vi.fn() }));
afterEach(() => vi.unstubAllGlobals());
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const attachment = (id: string): ChatAttachment => ({
  id,
  name: `${id}.pdf`,
  size: 40,
  media_type: 'application/pdf',
  pages: 2,
  characters: 50,
  complete: false,
  warnings: ['Page 2 contains no readable text.'],
});
const file = (name = 'notes.pdf') => new File(['%PDF-fixture'], name, { type: 'application/pdf' });

test('uploads finish in their original sessions and cannot send before extraction completes', async () => {
  const uploads: ((response: Response) => void)[] = [];
  const fetch = vi.fn(async (url: string) => {
    if (url.startsWith('/api/chat/attachments?'))
      return new Promise<Response>((resolve) => uploads.push(resolve));
    return json({ conversations: [] });
  });
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const a = store.state.selected;
  const first = store.upload(a, [file('a.pdf')]);
  const b = store.newConversation();
  const second = store.upload(b, [file('b.pdf')]);
  store.patch(a, { draft: 'Explain this' });
  await store.send(a);
  expect(fetch).toHaveBeenCalledTimes(2);
  uploads[1](json(attachment('b')));
  await second;
  uploads[0](json(attachment('a')));
  await first;
  expect(store.state.selected).toBe(b);
  expect(store.state.sessions[a].draftAttachments[0].attachment?.id).toBe('a');
  expect(store.state.sessions[b].draftAttachments[0].attachment?.id).toBe('b');
  await store.select(a);
  expect(store.state.sessions[a].draft).toBe('Explain this');
});

test('file-only send persists attachments once and stopping a saved reply does not restore them as drafts', async () => {
  const saved = {
    id: 'user',
    role: 'user',
    content: 'Summarize the attached files.',
    attachments: [attachment('a')],
  };
  let source!: ReadableStreamDefaultController<Uint8Array>;
  let payload: Record<string, unknown> = {};
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options) => {
      if (url.startsWith('/api/chat/attachments?')) return json(attachment('a'));
      if (url.endsWith('/stream')) {
        payload = JSON.parse(options.body);
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            source = controller;
          },
        });
        options.signal.addEventListener('abort', () =>
          source.error(new DOMException('Aborted', 'AbortError')),
        );
        return new Response(body);
      }
      if (url.endsWith('/saved')) return json({ id: 'saved', title: 'Files', messages: [saved] });
      return json({ conversations: [] });
    }),
  );
  const store = new ChatSessionStore();
  const key = store.state.selected;
  await store.upload(key, [file()]);
  const sending = store.send(key);
  expect(payload.attachment_ids).toEqual(['a']);
  expect(payload.message).toBe(saved.content);
  expect(store.state.sessions[key].pendingAttachments[0].id).toBe('a');
  source.enqueue(
    new TextEncoder().encode(
      `data: ${JSON.stringify({ type: 'session', conversation_id: 'saved', message: saved })}\n\n`,
    ),
  );
  await vi.waitFor(() => expect(store.state.sessions[key].id).toBe('saved'));
  store.stop(key);
  await sending;
  expect(store.state.sessions[key].messages).toEqual([saved]);
  expect(store.state.sessions[key].draftAttachments).toEqual([]);
  expect(store.state.sessions[key].pendingAttachments).toEqual([]);
  expect(store.state.sessions[key].draft).toBe('');
});

test('rejected send keeps uploaded files and can retry without uploading or duplicating them', async () => {
  let attempts = 0;
  const requests: Record<string, unknown>[] = [];
  const fetch = vi.fn(async (url: string, options) => {
    if (url.startsWith('/api/chat/attachments?')) return json(attachment('a'));
    if (url.endsWith('/stream')) {
      requests.push(JSON.parse(options.body));
      if (++attempts === 1) return json({ detail: 'Configure a model first.' }, 400);
      const events = [
        {
          type: 'session',
          conversation_id: 'saved',
          message: {
            id: 'user',
            role: 'user',
            content: 'Read file',
            attachments: [attachment('a')],
          },
        },
        { type: 'done', message: { id: 'answer', role: 'assistant', content: 'Read it.' } },
      ];
      return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''));
    }
    return json({ conversations: [] });
  });
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const key = store.state.selected;
  await store.upload(key, [file()]);
  store.patch(key, { draft: 'Read file' });
  await store.send(key);
  expect(store.state.sessions[key].draftAttachments).toHaveLength(1);
  expect(store.state.sessions[key].draft).toBe('Read file');
  await store.send(key);
  expect(requests.map((item) => item.attachment_ids)).toEqual([['a'], ['a']]);
  expect(fetch.mock.calls.filter(([url]) => url.startsWith('/api/chat/attachments?'))).toHaveLength(
    1,
  );
  expect(store.state.sessions[key].messages).toHaveLength(2);
  expect(store.state.sessions[key].draftAttachments).toHaveLength(0);
});

test('file validation and upload errors remain removable and block silent partial sends', async () => {
  const fetch = vi.fn(async () => json({ detail: 'PDF is password protected.' }, 422));
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const key = store.state.selected;
  const oversized = file('large.pdf');
  Object.defineProperty(oversized, 'size', { value: MAX_CHAT_FILE_SIZE + 1 });
  await store.upload(key, [file('wrong.exe'), oversized, file('locked.pdf')]);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(store.state.sessions[key].draftAttachments.map((entry) => entry.error)).toEqual([
    'Use PDF, TXT, MD, CSV or JSON.',
    'File exceeds 50 MB.',
    'PDF is password protected.',
  ]);
  store.patch(key, { draft: 'Read all three' });
  await store.send(key);
  expect(fetch).toHaveBeenCalledTimes(1);
  for (const entry of store.state.sessions[key].draftAttachments)
    await store.removeAttachment(key, entry.key);
  expect(store.state.sessions[key].draftAttachments).toEqual([]);
});

test('ten uploads above the old size limit queue in pairs and reject an eleventh', async () => {
  const finishes: (() => void)[] = [];
  let active = 0;
  let peak = 0;
  const fetch = vi.fn(async (url: string) => {
    if (!url.startsWith('/api/chat/attachments?')) return json({});
    active += 1;
    peak = Math.max(peak, active);
    return new Promise<Response>((resolve) => {
      const name = new URL(url, 'http://localhost').searchParams.get('filename')!;
      finishes.push(() => {
        active -= 1;
        resolve(json(attachment(name)));
      });
    });
  });
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const key = store.state.selected;
  const files = Array.from({ length: 11 }, (_, index) => {
    const selected = file(`notes-${index}.pdf`);
    Object.defineProperty(selected, 'size', { value: 12 * 1024 * 1024 });
    return selected;
  });
  const uploading = store.upload(key, files);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(store.state.sessions[key].draftAttachments).toHaveLength(10);
  expect(store.state.sessions[key].error).toBe('Attach up to 10 files per message.');
  for (let index = 0; index < 10; index += 1) {
    await vi.waitFor(() => expect(finishes.length).toBeGreaterThan(index));
    finishes[index]();
  }
  await uploading;
  expect(fetch).toHaveBeenCalledTimes(10);
  expect(peak).toBe(2);
  expect(
    store.state.sessions[key].draftAttachments.every((entry) => entry.status === 'ready'),
  ).toBe(true);
});

test('a removed queued file never starts an upload', async () => {
  const finishes: ((response: Response) => void)[] = [];
  const fetch = vi.fn(() => new Promise<Response>((resolve) => finishes.push(resolve)));
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const key = store.state.selected;
  const uploading = store.upload(key, [file('a.pdf'), file('b.pdf'), file('removed.pdf')]);
  await store.removeAttachment(key, store.state.sessions[key].draftAttachments[2].key);
  finishes[0](json(attachment('a')));
  finishes[1](json(attachment('b')));
  await uploading;
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(store.state.sessions[key].draftAttachments).toHaveLength(2);
});

test('removing an uploading file never resurrects its draft if upload completes concurrently', async () => {
  let finish!: (response: Response) => void;
  const fetch = vi.fn(async (url: string) => {
    if (url.startsWith('/api/chat/attachments?'))
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return json({});
  });
  vi.stubGlobal('fetch', fetch);
  const store = new ChatSessionStore();
  const key = store.state.selected;
  const uploading = store.upload(key, [file()]);
  await store.removeAttachment(key, store.state.sessions[key].draftAttachments[0].key);
  finish(json(attachment('a')));
  await uploading;
  expect(store.state.sessions[key].draftAttachments).toEqual([]);
  expect(fetch).toHaveBeenCalledWith(
    '/api/chat/attachments/a',
    expect.objectContaining({ method: 'DELETE' }),
  );
});

test('opening a saved conversation does not inherit another session’s attachments', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.startsWith('/api/chat/attachments?')) return json(attachment('a'));
      if (url.endsWith('/saved'))
        return json({ id: 'saved', title: 'Saved', messages: [], course_id: null });
      return json({ conversations: [{ id: 'saved', title: 'Saved', course_id: null }] });
    }),
  );
  const store = new ChatSessionStore();
  const key = store.state.selected;
  await store.upload(key, [file()]);
  await store.refresh();
  await store.select('saved');
  expect(store.state.sessions[store.state.selected].draftAttachments).toEqual([]);
  expect(store.state.sessions[key].draftAttachments[0].attachment?.id).toBe('a');
});

test('retry recovers a file message saved before the initial session event was lost', async () => {
  let attempts = 0;
  const saved = {
    id: 'user',
    role: 'user',
    content: 'Read file',
    attachments: [attachment('a')],
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.startsWith('/api/chat/attachments?')) return json(attachment('a'));
      if (url.endsWith('/stream')) {
        if (++attempts === 1) throw new Error('Connection lost');
        return json(
          {
            detail: {
              message: 'File already sent. Conversation restored.',
              conversation_id: 'saved',
            },
          },
          409,
        );
      }
      if (url.endsWith('/saved')) return json({ id: 'saved', title: 'Files', messages: [saved] });
      return json({ conversations: [] });
    }),
  );
  const store = new ChatSessionStore();
  const key = store.state.selected;
  await store.upload(key, [file()]);
  store.patch(key, { draft: 'Read file' });
  await store.send(key);
  expect(store.state.sessions[key].draftAttachments).toHaveLength(1);
  expect(store.state.sessions[key].id).toBeNull();
  await store.send(key);
  expect(store.state.sessions[key].id).toBe('saved');
  expect(store.state.sessions[key].draftAttachments).toEqual([]);
  expect(store.state.sessions[key].draft).toBe('');
  expect(store.state.sessions[key].messages).toEqual([saved]);
});
