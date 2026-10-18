import { expect, test } from 'vitest';
import { readChatStream, mergeActivity } from './chat-stream';

test('delivers split UTF-8 SSE deltas before the stream completes', async () => {
  let source!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      source = controller;
    },
  });
  const events: Record<string, unknown>[] = [];
  let seen!: () => void;
  const first = new Promise<void>((resolve) => {
    seen = resolve;
  });
  const running = readChatStream(new Response(body), (event) => {
    events.push(event);
    seen();
  });
  const bytes = new TextEncoder().encode(
    ': heartbeat\r\n\r\ndata: {"type":"delta","text":"你好"}\r\n\r\n',
  );
  for (const byte of bytes) source.enqueue(new Uint8Array([byte]));
  await first;
  expect(events).toEqual([{ type: 'delta', text: '你好' }]);
  source.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n'));
  source.close();
  await running;
});

test('truncated replies fail instead of silently finishing', async () => {
  await expect(
    readChatStream(new Response('data: {"type":"delta","text":"partial"}\n\n'), () => {}),
  ).rejects.toThrow('interrupted');
});

test('tool start/end events collapse into one visible step', () => {
  expect(
    mergeActivity([{ id: 'a', label: 'Read link', status: 'running' }], {
      id: 'a',
      label: 'Read link',
      status: 'done',
    }),
  ).toEqual([{ id: 'a', label: 'Read link', status: 'done' }]);
});
