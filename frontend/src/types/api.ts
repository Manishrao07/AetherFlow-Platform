export type Role = 'admin' | 'approver' | 'operator' | 'viewer';

export interface User {
  id: string;
  email: string;
  roles: Role[];
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  roles: Role[];
}

export type RunStatus =
  | 'CREATED'
  | 'PLANNING'
  | 'AWAITING_APPROVAL'
  | 'EXECUTING'
  | 'REVIEWING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED';

export type StepStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'AWAITING_APPROVAL'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'SKIPPED';

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH';

export type ReviewVerdict = 'APPROVED' | 'NEEDS_REVISION' | 'REJECTED';

export interface WorkflowRun {
  id: string;
  user_id: string;
  conversation_id: string | null;
  goal: string;
  mode: 'standard' | 'deep';
  status: RunStatus;
  plan: {
    summary?: string;
    max_risk?: RiskLevel;
    needs_approval?: boolean;
    steps?: Array<{
      key: string;
      description: string;
      tool?: string;
      agent?: string;
      risk_level: RiskLevel;
      requires_approval: boolean;
      dependencies: string[];
    }>;
  } | null;
  result: {
    summary?: string;
    answer?: string;
    sources?: Array<{ n: number; tool: string; citation: string; url?: string }>;
    [key: string]: unknown;
  } | null;
  error: string | null;
  revision_count: number;
  max_revisions: number;
  review_verdict: ReviewVerdict | null;
  review_score: string | number | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface StepOut {
  id: string;
  step_key: string;
  revision: number;
  position: number;
  agent: string;
  tool: string | null;
  inputs: Record<string, unknown>;
  output: Record<string, unknown> | null;
  status: StepStatus;
  risk_level: RiskLevel;
  requires_approval: boolean;
  attempts: number;
  max_attempts: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface LLMCallOut {
  id: string;
  step_id: string | null;
  agent: string;
  provider: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  cost_usd: string | number;
  cached: boolean;
  status: string;
  error: string | null;
  created_at: string;
}

export interface ToolExecutionOut {
  id: string;
  step_id: string;
  tool_name: string;
  attempt: number;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  status: string;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface TimelineTotals {
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: string | number;
  tool_executions: number;
  revisions: number;
}

export interface TimelineOut {
  run: WorkflowRun;
  steps: StepOut[];
  tool_executions: ToolExecutionOut[];
  llm_calls: LLMCallOut[];
  approvals: ApprovalOut[];
  events: Array<{ id: number; type: string; data: Record<string, unknown>; created_at: string }>;
  totals: TimelineTotals;
}

export interface ApprovalOut {
  id: string;
  run_id: string;
  kind: string;
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'EXPIRED';
  risk_level: RiskLevel;
  reason: string;
  requested_by: string;
  requester_email: string;
  decided_by: string | null;
  comment: string | null;
  expires_at: string;
  decided_at: string | null;
  created_at: string;
  goal?: string | null;
  plan?: {
    summary?: string;
    max_risk?: RiskLevel;
    steps?: Array<{
      key: string;
      description: string;
      tool?: string;
      risk_level: RiskLevel;
      requires_approval: boolean;
    }>;
  } | null;
}

export interface Conversation {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  token_count: number;
  run_id: string | null;
  created_at: string;
}

export interface AuditLogEntry {
  id: number;
  action: string;
  actor_id: string | null;
  actor_email: string | null;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  checks?: {
    database?: boolean;
    llm?: boolean;
    redis?: boolean;
  };
}
