import type { Citation } from './ChatMarkdown';
import type { ChatActivity } from './chat-stream';

export interface ChatAttachment {
  id: string;
  name: string;
  size: number;
  media_type: string;
  pages: number | null;
  characters: number;
  warnings: string[];
  complete: boolean;
}

export interface DraftAttachment {
  key: string;
  name: string;
  size: number;
  status: 'uploading' | 'ready' | 'error';
  attachment?: ChatAttachment;
  removing?: boolean;
  error?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  warnings?: string[];
  activity?: ChatActivity[];
  attachments?: ChatAttachment[];
}
export interface Conversation {
  id: string;
  title: string;
  course_id: string | null;
  task_id?: string | null;
  updated_at?: string;
}
export interface ChatConfig {
  base_url: string;
  model: string;
  has_api_key: boolean;
  builtin_prompt?: string;
  credential_error?: string | null;
}
export interface ChatResponse {
  conversation_id: string;
  message: ChatMessage;
  warnings: string[];
}
export interface Material {
  id: string;
  title: string;
  body: string;
  body_truncated?: boolean;
  complete?: boolean;
  warnings?: string[];
  fetched_at?: string | null;
  checked_at?: string | null;
  source_modified_at?: string | null;
  url: string | null;
  provider: string;
  course_id: string | null;
  kind: string;
  updated_at?: string;
}
export interface MaterialPage {
  documents: Material[];
  total: number;
  warnings?: string[];
}
export interface TaskContext {
  task: { id: string; title: string; course_id: string | null };
  documents: (Material & { related_via: string })[];
  warnings: string[];
}
