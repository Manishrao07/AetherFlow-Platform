import {
  ApprovalOut,
  AuditLogEntry,
  ChatMessage,
  Conversation,
  HealthResponse,
  LoginResponse,
  Role,
  TimelineOut,
  User,
  WorkflowRun,
} from '../types/api';
import {
  getMockTimeline,
  initialMockApprovals,
  initialMockWorkflows,
  mockAuditLogs,
  mockConversations,
  mockHealth,
  mockMessages,
  mockUsers,
} from './mockData';

const TOKEN_KEY = 'aetherflow_token';
const API_BASE_KEY = 'aetherflow_api_base';
const DEMO_MODE_KEY = 'aetherflow_demo_mode';
const CURRENT_ROLE_KEY = 'aetherflow_demo_role';

export const DEFAULT_LIVE_API = 'https://radical-womens-brought-burning.trycloudflare.com';

export const isDemoMode = (): boolean => {
  return localStorage.getItem(DEMO_MODE_KEY) === 'true';
};

export const setDemoMode = (enabled: boolean) => {
  if (enabled) {
    localStorage.setItem(DEMO_MODE_KEY, 'true');
  } else {
    localStorage.removeItem(DEMO_MODE_KEY);
  }
};

export const getApiBase = (): string => {
  const custom = localStorage.getItem(API_BASE_KEY);
  if (custom && custom.trim() && !custom.includes('lime-commissioner')) {
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
  localStorage.removeItem(CURRENT_ROLE_KEY);
};

// Local mock state for interactive demo experience
let localWorkflows = [...initialMockWorkflows];
let localApprovals = [...initialMockApprovals];
let localConversations = [...mockConversations];
const localMessagesMap = { ...mockMessages };

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

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 6000);
  const signal = options.signal || controller.signal;

  try {
    const response = await fetch(url, {
      ...options,
      headers,
      signal,
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
  } finally {
    clearTimeout(timeoutId);
  }
}

export const api = {
  // Auth
  async login(email: string, password: string): Promise<LoginResponse> {
    if (isDemoMode()) {
      const role = (email.split('@')[0] || 'operator') as Role;
      localStorage.setItem(CURRENT_ROLE_KEY, role);
      setToken(`mock-demo-jwt-token-${role}`);
      return {
        access_token: `mock-demo-jwt-token-${role}`,
        token_type: 'bearer',
        user_id: `u-${role}-01`,
        roles: [role],
      };
    }

    const res = await request<LoginResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    setToken(res.access_token);
    return res;
  },

  async getMe(): Promise<User> {
    if (isDemoMode()) {
      const role = (localStorage.getItem(CURRENT_ROLE_KEY) || 'operator') as Role;
      return (
        mockUsers[role] || {
          id: 'u-custom',
          email: `${role}@demo.local`,
          roles: [role],
          created_at: new Date().toISOString(),
        }
      );
    }
    return request<User>('/api/v1/auth/me');
  },

  logout(): void {
    clearToken();
  },

  // Health
  async getHealth(): Promise<HealthResponse> {
    if (isDemoMode()) {
      return mockHealth;
    }
    return request<HealthResponse>('/healthz');
  },

  async getReady(): Promise<HealthResponse> {
    if (isDemoMode()) {
      return mockHealth;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3500);
    try {
      return await request<HealthResponse>('/readyz', { signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  },

  // Workflows
  async listWorkflows(limit = 20): Promise<{ items: WorkflowRun[]; total: number }> {
    if (isDemoMode()) {
      return { items: localWorkflows.slice(0, limit), total: localWorkflows.length };
    }
    return request<{ items: WorkflowRun[]; total: number }>(`/api/v1/workflows?limit=${limit}`);
  },

  async getWorkflow(id: string): Promise<WorkflowRun> {
    if (isDemoMode()) {
      const found = localWorkflows.find((w) => w.id === id);
      return found || localWorkflows[0];
    }
    return request<WorkflowRun>(`/api/v1/workflows/${id}`);
  },

  async createWorkflow(
    goal: string,
    mode: 'standard' | 'deep' = 'standard',
    conversation_id?: string
  ): Promise<WorkflowRun> {
    if (isDemoMode()) {
      const newRun: WorkflowRun = {
        id: `wf-sim-${Math.floor(1000 + Math.random() * 9000)}`,
        user_id: 'u-operator-01',
        conversation_id: conversation_id || null,
        goal,
        mode,
        status: 'COMPLETED',
        plan: null,
        result: {
          summary: 'Simulated workflow completed successfully.',
          answer: 'All multi-agent execution steps verified.',
        },
        error: null,
        revision_count: 0,
        max_revisions: 2,
        review_verdict: 'APPROVED',
        review_score: 0.95,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        started_at: new Date().toISOString(),
        finished_at: new Date(Date.now() + 1500).toISOString(),
      };
      localWorkflows = [newRun, ...localWorkflows];
      return newRun;
    }

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
    if (isDemoMode()) {
      return getMockTimeline(id);
    }
    return request<TimelineOut>(`/api/v1/workflows/${id}/timeline`);
  },

  // Approvals
  async listApprovals(status = 'PENDING'): Promise<{ items: ApprovalOut[]; total: number }> {
    if (isDemoMode()) {
      const filtered = localApprovals.filter((a) => a.status === status);
      return { items: filtered, total: filtered.length };
    }
    return request<{ items: ApprovalOut[]; total: number }>(
      `/api/v1/approvals?status=${status}`
    );
  },

  async approve(id: string, comment?: string): Promise<ApprovalOut> {
    if (isDemoMode()) {
      localApprovals = localApprovals.map((a) =>
        a.id === id ? { ...a, status: 'APPROVED' as const } : a
      );
      return localApprovals.find((a) => a.id === id) || localApprovals[0];
    }
    return request<ApprovalOut>(`/api/v1/approvals/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ comment: comment || 'Approved via Web Console' }),
    });
  },

  async reject(id: string, comment: string): Promise<ApprovalOut> {
    if (isDemoMode()) {
      localApprovals = localApprovals.map((a) =>
        a.id === id ? { ...a, status: 'REJECTED' as const } : a
      );
      return localApprovals.find((a) => a.id === id) || localApprovals[0];
    }
    return request<ApprovalOut>(`/api/v1/approvals/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ comment }),
    });
  },

  // Conversations & Chat
  async listConversations(): Promise<{ items: Conversation[]; total: number }> {
    if (isDemoMode()) {
      return { items: localConversations, total: localConversations.length };
    }
    return request<{ items: Conversation[]; total: number }>('/api/v1/conversations');
  },

  async createConversation(title: string): Promise<Conversation> {
    if (isDemoMode()) {
      const newConv: Conversation = {
        id: `conv-sim-${Date.now()}`,
        title,
        status: 'active',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      localConversations = [newConv, ...localConversations];
      return newConv;
    }
    return request<Conversation>('/api/v1/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
  },

  async listMessages(conversationId: string): Promise<{ items: ChatMessage[]; total: number }> {
    if (isDemoMode()) {
      const msgs: ChatMessage[] = localMessagesMap[conversationId] || [
        {
          id: 'sim-msg-1',
          role: 'assistant',
          content: 'Hello! I am your Multi-Agent Workflow Assistant running in Interactive Demo Mode. You can ask me to run analysis, check reconciliation, or create workflows.',
          token_count: 32,
          run_id: null,
          created_at: new Date().toISOString(),
        },
      ];
      return { items: msgs, total: msgs.length };
    }
    return request<{ items: ChatMessage[]; total: number }>(
      `/api/v1/conversations/${conversationId}/messages`
    );
  },

  // Audit Logs
  async listAuditLogs(action?: string, limit = 50): Promise<{ items: AuditLogEntry[]; total: number }> {
    if (isDemoMode()) {
      const logs = action ? mockAuditLogs.filter((l) => l.action === action) : mockAuditLogs;
      return { items: logs.slice(0, limit), total: logs.length };
    }
    const q = action ? `&action=${encodeURIComponent(action)}` : '';
    return request<{ items: AuditLogEntry[]; total: number }>(
      `/api/v1/audit-logs?limit=${limit}${q}`
    );
  },
};
