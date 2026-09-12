/** Incrementally decode SSE, including UTF-8 characters split across network chunks. */
export async function readChatStream(
  response: Response,
  onEvent: (event: Record<string, unknown>) => void,
) {
  if (!response.body) throw new Error('Streaming response unavailable.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed = false;
  const parse = () => {
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const frame = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n');
      if (!data) continue;
      const event = JSON.parse(data) as Record<string, unknown>;
      if (event.type === 'done') completed = true;
      onEvent(event);
    }
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (buffer.length > 1_000_000) throw new Error('Chat stream event exceeds limit.');
      parse();
      if (done) break;
    }
    if (!completed) throw new Error('Reply interrupted. Check this conversation before retrying.');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export interface ChatActivity {
  id?: string;
  label: string;
  status?: 'running' | 'done' | 'error';
  detail?: string;
  browser_visit?: { url: string; title: string; checked_at: string; warnings: string[] };
  mail_rule_preview?: { changed: number; newly_ignored: number; new_conflicts: number };
  mail_rule_change?: {
    changed: boolean;
    rule_id: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
  };
  change?: {
    changed: boolean;
    task_id: string;
    change_id?: string;
    undone?: string;
    before?: Record<string, unknown>;
    after?: Record<string, unknown>;
  };
}

export function mergeActivity(items: ChatActivity[], next: ChatActivity) {
  const index = next.id ? items.findIndex((item) => item.id === next.id) : -1;
  return index < 0 ? [...items, next] : items.map((item, at) => (at === index ? next : item));
}
