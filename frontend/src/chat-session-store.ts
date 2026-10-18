import { useSyncExternalStore } from 'react';
import { api } from './api';
import { readChatStream, mergeActivity, type ChatActivity } from './chat-stream';
import type {
  ChatAttachment,
  ChatMessage,
  ChatResponse,
  Conversation,
  DraftAttachment,
} from './chat-types';

export const MAX_CHAT_FILES = 10;
export const MAX_CHAT_FILE_SIZE = 50 * 1024 * 1024;
export const CHAT_FILE_ACCEPT = '.pdf,.txt,.md,.csv,.json';
const FILE_ONLY_PROMPT = 'Summarize the attached files.';

export interface ChatSession {
  key: string;
  id: string | null;
  title: string;
  courseId: string;
  taskId: string | null;
  messages: ChatMessage[];
  draft: string;
  draftAttachments: DraftAttachment[];
  fetchMaterials: boolean;
  busy: boolean;
  loading: boolean;
  loaded: boolean;
  deleting: boolean;
  error: string;
  pendingMessage: string;
  pendingAttachments: ChatAttachment[];
  streamText: string;
  activity: ChatActivity[];
}

class ChatError extends Error {
  constructor(
    message: string,
    public conversationId?: string,
  ) {
    super(message);
  }
}

// Streams belong to sessions, so navigating away from Chat does not cancel a reply.
export class ChatSessionStore {
  private listeners = new Set<() => void>();
  private requests = new Map<string, { controller: AbortController; stopped: boolean }>();
  private uploads = new Map<string, AbortController>();
  private deleted = new Set<string>();
  private deleting = new Set<string>();
  private refreshSequence = 0;
  state: {
    selected: string;
    sessions: Record<string, ChatSession>;
    conversations: Conversation[];
    historyError: string;
  } = {
    selected: '',
    sessions: {},
    conversations: [],
    historyError: '',
  };

  constructor() {
    this.newConversation();
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  getSnapshot = () => this.state;
  private publish(next: Partial<typeof this.state>) {
    this.state = { ...this.state, ...next };
    this.listeners.forEach((listener) => listener());
  }
  patch = (key: string, patch: Partial<ChatSession>) => {
    const session = this.state.sessions[key];
    if (session)
      this.publish({ sessions: { ...this.state.sessions, [key]: { ...session, ...patch } } });
  };
  newConversation = (courseId = '', taskId: string | null = null) => {
    const key = crypto.randomUUID();
    const session: ChatSession = {
      key,
      id: null,
      title: 'New chat',
      courseId,
      taskId,
      messages: [],
      draft: '',
      draftAttachments: [],
      fetchMaterials: false,
      busy: false,
      loading: false,
      loaded: true,
      deleting: false,
      error: '',
      pendingMessage: '',
      pendingAttachments: [],
      streamText: '',
      activity: [],
    };
    // An untouched empty tab has no user content to retain.
    const sessions = Object.fromEntries(
      Object.entries(this.state.sessions).filter(
        ([, item]) =>
          item.id ||
          item.busy ||
          item.draft ||
          item.draftAttachments.length ||
          item.messages.length ||
          item.error,
      ),
    );
    this.publish({ selected: key, sessions: { ...sessions, [key]: session } });
    return key;
  };
  refresh = async () => {
    const sequence = ++this.refreshSequence;
    try {
      const result = await api<{ conversations: Conversation[] }>('/chat/conversations');
      if (sequence === this.refreshSequence)
        this.publish({
          conversations: result.conversations.filter((item) => !this.deleted.has(item.id)),
          historyError: '',
        });
    } catch (error) {
      if (sequence === this.refreshSequence)
        this.publish({ historyError: (error as Error).message });
    }
  };
  rename = (id: string, title: string) => {
    const session = Object.values(this.state.sessions).find((item) => item.id === id);
    if (session) this.patch(session.key, { title });
    this.publish({
      conversations: this.state.conversations.map((item) =>
        item.id === id ? { ...item, title } : item,
      ),
    });
  };
  select = async (id: string) => {
    let session = Object.values(this.state.sessions).find(
      (item) => item.id === id || item.key === id,
    );
    if (!session) {
      const conversation = this.state.conversations.find((item) => item.id === id);
      if (!conversation) return;
      const key = crypto.randomUUID();
      session = {
        ...this.state.sessions[this.state.selected],
        key,
        id,
        title: conversation.title,
        courseId: conversation.course_id ?? '',
        taskId: conversation.task_id ?? null,
        messages: [],
        draft: '',
        draftAttachments: [],
        fetchMaterials: false,
        busy: false,
        loading: false,
        loaded: false,
        deleting: false,
        error: '',
        pendingMessage: '',
        pendingAttachments: [],
        streamText: '',
        activity: [],
      };
      this.publish({ sessions: { ...this.state.sessions, [key]: session } });
    }
    this.publish({ selected: session.key });
    if (session.loaded || session.loading || session.busy) return;
    this.patch(session.key, { loading: true, error: '' });
    try {
      await this.restore(session.key, session.id ?? id);
    } catch (error) {
      this.patch(session.key, { error: (error as Error).message });
    } finally {
      this.patch(session.key, { loading: false });
    }
  };
  private restore = async (key: string, id: string) => {
    const result = await api<Conversation & { messages: ChatMessage[] }>(
      `/chat/conversations/${encodeURIComponent(id)}`,
    );
    this.patch(key, {
      id: result.id,
      title: result.title,
      courseId: result.course_id ?? '',
      taskId: result.task_id ?? null,
      messages: result.messages,
      loaded: true,
    });
  };
  upload = async (key: string, files: File[]) => {
    const session = this.state.sessions[key];
    if (!session || session.loading || session.deleting || session.busy) return;
    const available = MAX_CHAT_FILES - session.draftAttachments.length;
    const selected = files.slice(0, available);
    const entries: DraftAttachment[] = selected.map((file) => {
      const extension = `.${file.name.split('.').at(-1)?.toLowerCase()}`;
      const error = !CHAT_FILE_ACCEPT.split(',').includes(extension)
        ? 'Use PDF, TXT, MD, CSV or JSON.'
        : file.size > MAX_CHAT_FILE_SIZE
          ? 'File exceeds 50 MB.'
          : file.size === 0
            ? 'File is empty.'
            : undefined;
      return {
        key: crypto.randomUUID(),
        name: file.name,
        size: file.size,
        status: error ? 'error' : 'uploading',
        error,
      };
    });
    this.patch(key, {
      draftAttachments: [...session.draftAttachments, ...entries],
      error: files.length > available ? 'Attach up to 10 files per message.' : '',
    });
    const uploadEntry = async (entry: DraftAttachment, index: number) => {
      if (
        entry.status === 'error' ||
        !this.state.sessions[key]?.draftAttachments.some((item) => item.key === entry.key)
      )
        return;
      const controller = new AbortController();
      this.uploads.set(entry.key, controller);
      const timeout = setTimeout(() => controller.abort(), 300000);
      try {
        const response = await fetch(
          `/api/chat/attachments?filename=${encodeURIComponent(entry.name)}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/octet-stream', 'X-CourseDeck': '1' },
            body: selected[index],
            signal: controller.signal,
          },
        );
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(
            typeof result.detail === 'string'
              ? result.detail
              : `Upload failed (${response.status}).`,
          );
        }
        const attachment = (await response.json()) as ChatAttachment;
        const current = this.state.sessions[key];
        if (!current?.draftAttachments.some((item) => item.key === entry.key)) {
          await api(`/chat/attachments/${encodeURIComponent(attachment.id)}`, 'DELETE');
          return;
        }
        this.patch(key, {
          draftAttachments: current.draftAttachments.map((item) =>
            item.key === entry.key
              ? { ...item, attachment, status: 'ready', error: undefined }
              : item,
          ),
        });
      } catch (error) {
        const current = this.state.sessions[key];
        if (current)
          this.patch(key, {
            draftAttachments: current.draftAttachments.map((item) =>
              item.key === entry.key
                ? {
                    ...item,
                    status: 'error',
                    error: controller.signal.aborted
                      ? 'Upload interrupted. Remove the file and try again.'
                      : (error as Error).message,
                  }
                : item,
            ),
          });
      } finally {
        clearTimeout(timeout);
        this.uploads.delete(entry.key);
      }
    };
    let next = 0;
    await Promise.all(
      Array.from({ length: Math.min(2, entries.length) }, async () => {
        while (next < entries.length) {
          const index = next++;
          await uploadEntry(entries[index], index);
        }
      }),
    );
  };
  removeAttachment = async (key: string, attachmentKey: string) => {
    const session = this.state.sessions[key];
    const entry = session?.draftAttachments.find((item) => item.key === attachmentKey);
    if (!entry || entry.removing || session.busy || session.deleting) return;
    this.uploads.get(attachmentKey)?.abort();
    if (entry.attachment) {
      this.patch(key, {
        draftAttachments: session.draftAttachments.map((item) =>
          item.key === attachmentKey ? { ...item, removing: true } : item,
        ),
      });
      try {
        await api(`/chat/attachments/${encodeURIComponent(entry.attachment.id)}`, 'DELETE');
      } catch (error) {
        const current = this.state.sessions[key];
        if (current)
          this.patch(key, {
            draftAttachments: current.draftAttachments.map((item) =>
              item.key === attachmentKey
                ? { ...item, removing: false, error: (error as Error).message }
                : item,
            ),
          });
        return;
      }
    }
    const current = this.state.sessions[key];
    if (current)
      this.patch(key, {
        draftAttachments: current.draftAttachments.filter((item) => item.key !== attachmentKey),
      });
  };
  remove = async (keyOrId: string) => {
    if (this.deleting.has(keyOrId)) return;
    const session = Object.values(this.state.sessions).find(
      (item) => item.key === keyOrId || item.id === keyOrId,
    );
    if (
      session?.busy ||
      session?.deleting ||
      session?.draftAttachments.some((item) => item.status === 'uploading' || item.removing)
    )
      return;
    this.deleting.add(keyOrId);
    const id = session ? session.id : keyOrId;
    if (session) this.patch(session.key, { deleting: true, error: '' });
    try {
      for (const entry of session?.draftAttachments ?? []) {
        if (entry.attachment)
          await api(`/chat/attachments/${encodeURIComponent(entry.attachment.id)}`, 'DELETE');
        const current = session && this.state.sessions[session.key];
        if (current)
          this.patch(session.key, {
            draftAttachments: current.draftAttachments.filter((item) => item.key !== entry.key),
          });
      }
      if (id) await api(`/chat/conversations/${encodeURIComponent(id)}`, 'DELETE');
      if (id) this.deleted.add(id);
      if (session?.key === this.state.selected) this.newConversation(session.courseId);
      const sessions = { ...this.state.sessions };
      if (session) delete sessions[session.key];
      this.publish({
        sessions,
        conversations: this.state.conversations.filter((item) => item.id !== id),
        historyError: '',
      });
    } catch (error) {
      if (session) this.patch(session.key, { deleting: false });
      this.publish({ historyError: (error as Error).message });
    } finally {
      this.deleting.delete(keyOrId);
    }
  };
  stop = (key: string) => {
    const request = this.requests.get(key);
    if (request) {
      request.stopped = true;
      request.controller.abort();
    }
  };
  send = async (key: string) => {
    const session = this.state.sessions[key];
    if (
      !session ||
      session.busy ||
      session.loading ||
      session.deleting ||
      session.draftAttachments.some((item) => item.status !== 'ready' || item.removing)
    )
      return;
    const attachments = session.draftAttachments.flatMap((item) =>
      item.attachment ? [item.attachment] : [],
    );
    const content = session.draft.trim() || (attachments.length ? FILE_ONLY_PROMPT : '');
    if (!content) return;
    const request = { controller: new AbortController(), stopped: false };
    this.requests.set(key, request);
    this.patch(key, {
      busy: true,
      error: '',
      draft: '',
      draftAttachments: [],
      pendingMessage: content,
      pendingAttachments: attachments,
      streamText: '',
      activity: [],
      title: session.id ? session.title : content.slice(0, 80),
    });
    const timeout = setTimeout(() => request.controller.abort(), 660000);
    let savedId = session.id;
    let completed = false;
    let accepted = false;
    let receivedText = '';
    let receivedActivity: ChatActivity[] = [];
    try {
      const response = await fetch('/api/chat/messages/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CourseDeck': '1' },
        body: JSON.stringify({
          message: content,
          conversation_id: session.id ?? undefined,
          course_id: session.courseId || undefined,
          task_id: session.taskId ?? undefined,
          fetch_materials: session.fetchMaterials,
          attachment_ids: attachments.map((item) => item.id),
        }),
        signal: request.controller.signal,
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        const detail = result.detail;
        throw new ChatError(
          typeof detail === 'string'
            ? detail
            : detail?.message || `Request failed (${response.status}).`,
          detail?.conversation_id,
        );
      }
      await readChatStream(response, (event) => {
        const current = this.state.sessions[key];
        if (!current) return;
        if (event.type === 'session') {
          accepted = true;
          savedId = String(event.conversation_id);
          this.patch(key, {
            id: savedId,
            messages: [...current.messages, event.message as ChatMessage],
            pendingMessage: '',
            pendingAttachments: [],
          });
        } else if (event.type === 'answer_start') {
          receivedText = '';
          this.patch(key, { streamText: '' });
        } else if (event.type === 'delta') {
          receivedText += String(event.text);
          this.patch(key, { streamText: receivedText });
        } else if (event.type === 'activity') {
          receivedActivity = mergeActivity(receivedActivity, event as unknown as ChatActivity);
          this.patch(key, { activity: receivedActivity });
          if (event.inbox_change || event.mail_rule_change)
            window.dispatchEvent(new Event('coursedeck:inbox-changed'));
        } else if (event.type === 'done') {
          completed = true;
          this.patch(key, {
            messages: [...current.messages, (event as unknown as ChatResponse).message],
            streamText: '',
            activity: [],
          });
        } else if (event.type === 'error') {
          throw new ChatError(String(event.message), event.conversation_id as string | undefined);
        }
      });
    } catch (error) {
      const failure = error as ChatError;
      savedId = failure.conversationId ?? savedId;
      let restored = false;
      if (savedId && (!request.controller.signal.aborted || attachments.length)) {
        this.patch(key, { id: savedId });
        try {
          await this.restore(key, savedId);
          restored = true;
          accepted ||= this.state.sessions[key].messages.some(
            (message) =>
              message.role === 'user' &&
              message.content === content &&
              attachments.every((file) =>
                message.attachments?.some((item) => item.id === file.id),
              ) &&
              !session.messages.some((previous) => previous.id === message.id),
          );
        } catch {
          /* Preserve the visible reply when recovery is unavailable. */
        }
      }
      const current = this.state.sessions[key];
      if (!completed && !restored && (receivedText || receivedActivity.length)) {
        this.patch(key, {
          messages: [
            ...current.messages,
            {
              id: `interrupted-${crypto.randomUUID()}`,
              role: 'assistant',
              content: receivedText,
              activity: receivedActivity,
              warnings: ['Reply interrupted.'],
            },
          ],
        });
      }
      this.patch(key, {
        streamText: '',
        activity: [],
        loaded: restored || (!savedId && !accepted),
        draft: accepted ? current.draft : [content, current.draft].filter(Boolean).join('\n\n'),
        draftAttachments: accepted
          ? current.draftAttachments
          : [...session.draftAttachments, ...current.draftAttachments],
        error: request.controller.signal.aborted
          ? request.stopped
            ? 'Stopped.'
            : 'Response timed out. Check this conversation before sending again.'
          : failure.message,
      });
    } finally {
      clearTimeout(timeout);
      this.requests.delete(key);
      this.patch(key, { busy: false, pendingMessage: '', pendingAttachments: [] });
      window.dispatchEvent(new Event('coursedeck:inbox-changed'));
      void this.refresh();
    }
  };
}

export const chatSessions = new ChatSessionStore();
export function useChatSessions() {
  return useSyncExternalStore(chatSessions.subscribe, chatSessions.getSnapshot);
}
