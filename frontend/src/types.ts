export interface LocalState {
  hidden: boolean;
  dismissed: boolean;
  pinned: boolean;
  note: string;
  priority: number;
}
export interface Task {
  raw_data?: { unavailable_fields?: string[] };
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
  graded: boolean;
  score: number | null;
  points_possible: number | null;
  archived: boolean;
  missing_count: number;
  last_seen_at: string;
  local: LocalState;
}
export interface Course {
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
  id: string;
  field: 'sender' | 'subject' | 'body' | 'any';
  contains: string;
  action: 'course' | 'category' | 'ignore' | 'none';
  value: string;
}
