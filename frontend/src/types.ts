import type {
  FieldAvailability,
  SourceAvailability,
  SourceState,
  TaskField,
} from './task-contract-types';

export interface LocalState {
  due_override?: boolean;
  due_at_override?: string | null;
  completion_override?: 'done' | 'open' | null;
  hidden: boolean;
  dismissed: boolean;
  pinned: boolean;
  note: string;
  priority: number;
}
export interface Task {
  source_state?: SourceState;
  field_availability?: Partial<Record<TaskField, FieldAvailability>>;
  is_completed?: boolean;
  source_due_at?: string | null;
  raw_data?: {
    unavailable_fields?: string[];
    field_availability?: Partial<Record<TaskField, FieldAvailability>>;
  };
  id: string;
  provider: string;
  course_id: string | null;
  email_id?: string | null;
  title: string;
  description: string;
  due_at: string | null;
  closes_at: string | null;
  available_at: string | null;
  url: string | null;
  submission_status: string;
  source_availability?: SourceAvailability;
  source_status_known?: boolean;
  graded: boolean;
  score: number | null;
  points_possible: number | null;
  archived: boolean;
  missing_count: number;
  last_seen_at: string;
  local: LocalState;
}
export interface Course {
  color?: string | null;
  original_name?: string;
  alias?: string | null;
  disabled?: boolean;
  deleted?: boolean;
  providers?: string[];
  source_course_ids?: string[];
  source_names?: string[];
  workspace_id?: string;
  needs_binding?: boolean;
  id: string;
  provider: string;
  name: string;
  section: string | null;
  teacher: string | null;
  source_url: string | null;
}
export interface Source {
  metadata?: {
    diagnostics?: {
      stage?: string;
      category?: string;
      retry_attempt?: number;
      retry_limit?: number;
      next_retry_at?: string | null;
      action?: string;
    };
    [key: string]: unknown;
  };
  manual_login: boolean;
  configuration_fields?: {
    key: string;
    label: string;
    type: string;
    placeholder?: string;
    help?: string;
    options?: string[];
  }[];
  configuration_values?: Record<string, unknown>;
  key: string;
  name: string;
  description: string;
  status: string;
  syncing: boolean;
  last_outcome?: string;
  last_successful_sync?: string;
  last_attempted_sync?: string;
  warnings?: string[];
}
export interface Settings {
  startup_sync: boolean;
  timezone: string;
  show_completed: boolean;
}
export interface Snapshot {
  changes?: { unread_count: number; critical_unread_count: number };
  source_courses: Course[];
  tasks: Task[];
  courses: Course[];
  sources: Source[];
  settings: Settings;
  revision: number;
}
export interface SyncLog {
  id: number;
  provider: string;
  attempted_at: string;
  outcome: string;
  task_count: number;
  warnings: string[];
}

export interface MailMessage {
  attention?: boolean;
  attention_reasons?: string[];
  id: string;
  sender: string;
  sender_email: string;
  subject: string;
  snippet: string;
  body?: string;
  body_complete: boolean;
  received_at: string | null;
  date_label: string;
  date_text: string;
  url: string;
  unread: boolean;
  starred: boolean;
  deleted: boolean;
  ignored: boolean;
  classification: 'classified' | 'none' | 'unclassifiable';
  classification_reason: string;
  matched_rules: string[];
  categories: string[];
  course_id: string | null;
  course_override: string;
  task_ids?: string[];
}

export interface MailConnection {
  error_code?: string | null;
  status: string;
  syncing: boolean;
  account: string | null;
  last_sync: string | null;
  error: string | null;
  warning: string | null;
}

export interface MailPage {
  messages: MailMessage[];
  total: number;
  has_more: boolean;
  connection: MailConnection;
}

export interface MailRule {
  priority?: boolean;
  enabled: boolean;
  id: string;
  field: 'sender' | 'subject' | 'body' | 'any';
  contains: string;
  action: 'course' | 'category' | 'ignore' | 'none';
  value: string;
}
