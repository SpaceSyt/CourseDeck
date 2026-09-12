import { useRef, useState } from 'react';
import { api } from './api';
import type { Conversation } from './chat-types';

export function ConversationTitleEditor({
  conversation,
  saved,
  cancel,
}: {
  conversation: Conversation;
  saved: (title: string) => void;
  cancel: () => void;
}) {
  const [title, setTitle] = useState(conversation.title);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const save = async () => {
    if (saving.current) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError('Enter a title.');
      return;
    }
    if (trimmed === conversation.title) {
      cancel();
      return;
    }
    saving.current = true;
    setBusy(true);
    setError('');
    try {
      const result = await api<{ title: string }>(
        `/chat/conversations/${encodeURIComponent(conversation.id)}`,
        'PATCH',
        { title: trimmed },
      );
      saved(result.title);
    } catch (failure) {
      setError((failure as Error).message);
      saving.current = false;
      setBusy(false);
    }
  };
  return (
    <div className="chat-title-editor">
      <input
        autoFocus
        aria-label="Conversation title"
        maxLength={100}
        value={title}
        disabled={busy}
        onFocus={(event) => event.target.select()}
        onChange={(event) => {
          setTitle(event.target.value);
          setError('');
        }}
        onBlur={() => {
          void save();
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === 'Enter') {
            event.preventDefault();
            void save();
          }
          if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            saving.current = true;
            cancel();
          }
        }}
      />
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
