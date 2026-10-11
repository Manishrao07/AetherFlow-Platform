import {
  ApprovalOut,
  AuditLogEntry,
  ChatMessage,
  Conversation,
  HealthResponse,
  LoginResponse,
  TimelineOut,
  User,
  WorkflowRun,
} from '../types/api';

const TOKEN_KEY = 'aetherflow_token';
const API_BASE_KEY = 'aetherflow_api_base';

export const DEFAULT_LIVE_API = 'https://lime-commissioner-anniversary-los.trycloudflare.com';

export const getApiBase = (): string => {
  const custom = localStorage.getItem(API_BASE_KEY);
  if (custom && custom.trim()) {
    return custom.trim().replace(/\/$/, '');
  }
  const envUrl = import.meta.env.VITE_API_URL;
  if (envUrl && envUrl.trim()) {
    return envUrl.trim().replace(/\/$/, '');
  }
  if (
    typeof window !== 'undefined' &&
    window.location.hostname !== 'localhost' &&
    window.location.hostname !== '127.0.0.1'
  ) {
    return DEFAULT_LIVE_API;
  }
  return 'http://localhost:8080';
};

export const setApiBase = (url: string) => {
  if (!url || !url.trim()) {
    localStorage.removeItem(API_BASE_KEY);
  } else {
    localStorage.setItem(API_BASE_KEY, url.trim().replace(/\/$/, ''));
  }
};

export const getToken = (): string | null => {
  return localStorage.getItem(TOKEN_KEY);
};

export const setToken = (token: string) => {
  localStorage.setItem(TOKEN_KEY, token);
};

export const clearToken = () => {
  localStorage.removeItem(TOKEN_KEY);
};

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const base = getApiBase();
  const url = `${base}${path}`;
  const token = getToken();

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> || {}),
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let errorDetail = `HTTP ${response.status}: ${response.statusText}`;
    try {
      const errJson = await response.json();
      if (errJson.detail) {
        errorDetail = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail);
      } else if (errJson.error && errJson.error.message) {
        errorDetail = errJson.error.message;
      }
    } catch {
      // fallback to status text
    }
    throw new Error(errorDetail);
  }

  if (response.status === 204) {
    return {} as T;
  }

  return response.json();
}

export const api = {
  // Auth
  async login(email: string, password: string): Promise<LoginResponse> {
    const res = await request<LoginResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    setToken(res.access_token);
    return res;
  },

  async getMe(): Promise<User> {
    return request<User>('/api/v1/auth/me');
  },

  logout() {
    clearToken();
  },

  // Health
  async getHealth(): Promise<HealthResponse> {
    return request<HealthResponse>('/healthz');
  },

  async getReady(): Promise<HealthResponse> {
    return request<HealthResponse>('/readyz');
  },

  // Workflows
  async listWorkflows(limit = 20): Promise<{ items: WorkflowRun[]; total: number }> {
    return request<{ items: WorkflowRun[]; total: number }>(`/api/v1/workflows?limit=${limit}`);
  },

  async getWorkflow(id: string): Promise<WorkflowRun> {
    return request<WorkflowRun>(`/api/v1/workflows/${id}`);
  },

  async createWorkflow(
    goal: string,
    mode: 'standard' | 'deep' = 'standard',
    conversation_id?: string
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>('/api/v1/workflows', {
      method: 'POST',
      body: JSON.stringify({
        goal,
        mode,
        conversation_id: conversation_id || null,
      }),
    });
  },

  async getTimeline(id: string): Promise<TimelineOut> {
    return request<TimelineOut>(`/api/v1/workflows/${id}/timeline`);
  },

  // Approvals
  async listApprovals(status = 'PENDING'): Promise<{ items: ApprovalOut[]; total: number }> {
    return request<{ items: ApprovalOut[]; total: number }>(
      `/api/v1/approvals?status=${status}`
    );
  },

  async approve(id: string, comment?: string): Promise<ApprovalOut> {
    return request<ApprovalOut>(`/api/v1/approvals/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ comment: comment || 'Approved via Web Console' }),
    });
  },

  async reject(id: string, comment: string): Promise<ApprovalOut> {
    return request<ApprovalOut>(`/api/v1/approvals/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ comment }),
    });
  },

  // Conversations & Chat
  async listConversations(): Promise<{ items: Conversation[]; total: number }> {
    return request<{ items: Conversation[]; total: number }>('/api/v1/conversations');
  },

  async createConversation(title: string): Promise<Conversation> {
    return request<Conversation>('/api/v1/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
  },

  async listMessages(conversationId: string): Promise<{ items: ChatMessage[]; total: number }> {
    return request<{ items: ChatMessage[]; total: number }>(
      `/api/v1/conversations/${conversationId}/messages`
    );
  },

  // Audit Logs
  async listAuditLogs(action?: string, limit = 50): Promise<{ items: AuditLogEntry[]; total: number }> {
    const q = action ? `&action=${encodeURIComponent(action)}` : '';
    return request<{ items: AuditLogEntry[]; total: number }>(
      `/api/v1/audit-logs?limit=${limit}${q}`
    );
  },
};
