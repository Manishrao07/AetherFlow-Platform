import React, { useEffect, useState } from 'react';
import {
  AlertCircle,
  Bot,
  Cpu,
  FileCheck2,
  GitBranch,
  KeyRound,
  Loader2,
  Lock,
  MessageSquare,
  Shield,
  ShieldAlert,
  Sparkles,
  User as UserIcon,
} from 'lucide-react';
import { Header } from './components/Header';
import { ChatView } from './components/ChatView';
import { WorkflowsView } from './components/WorkflowsView';
import { ApprovalsView } from './components/ApprovalsView';
import { AuditLogsView } from './components/AuditLogsView';
import { api, getApiBase, getToken, setApiBase } from './services/api';
import { Role, User } from './types/api';

export const App: React.FC = () => {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [activeTab, setActiveTab] = useState<'chat' | 'workflows' | 'approvals' | 'audit' | 'settings'>('chat');
  const [backendOnline, setBackendOnline] = useState<boolean>(true);
  const [pendingApprovalsCount, setPendingApprovalsCount] = useState<number>(0);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState<boolean>(true);

  // Manual login form state
  const [loginEmail, setLoginEmail] = useState('operator@demo.local');
  const [loginPassword, setLoginPassword] = useState('Demo#Pass123');
  const [loginError, setLoginError] = useState<string | null>(null);
  const [showApiConfig, setShowApiConfig] = useState(false);
  const [customApiUrl, setCustomApiUrl] = useState(getApiBase());

  const checkHealthAndApprovals = async () => {
    try {
      await api.getReady();
      setBackendOnline(true);
    } catch {
      setBackendOnline(false);
    }

    try {
      const appr = await api.listApprovals('PENDING');
      setPendingApprovalsCount(appr.total);
    } catch {
      // ignore
    }
  };

  const initAuth = async () => {
    setAuthLoading(true);
    const token = getToken();
    if (token) {
      try {
        const user = await api.getMe();
        setCurrentUser(user);
      } catch {
        // Token invalid, try auto-logging in as operator
        await handleRoleSwitch('operator');
      }
    } else {
      // Auto-login as default operator for seamless demo experience
      await handleRoleSwitch('operator');
    }
    setAuthLoading(false);
  };

  useEffect(() => {
    initAuth();
    checkHealthAndApprovals();
    const interval = setInterval(checkHealthAndApprovals, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleRoleSwitch = async (role: Role) => {
    try {
      setAuthLoading(true);
      await api.login(`${role}@demo.local`, 'Demo#Pass123');
      const user = await api.getMe();
      setCurrentUser(user);
      setLoginError(null);
      await checkHealthAndApprovals();
    } catch (err: unknown) {
      setLoginError((err as Error).message || 'Failed to switch role');
    } finally {
      setAuthLoading(false);
    }
  };

  const handleManualLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthLoading(true);
    setLoginError(null);
    try {
      await api.login(loginEmail, loginPassword);
      const user = await api.getMe();
      setCurrentUser(user);
    } catch (err: unknown) {
      setLoginError((err as Error).message || 'Invalid credentials');
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = () => {
    api.logout();
    setCurrentUser(null);
  };

  const handleOpenWorkflow = (runId: string) => {
    setSelectedWorkflowId(runId);
    setActiveTab('workflows');
  };

  const handleGoToApprovals = () => {
    setActiveTab('approvals');
  };

  // If no user and not auto-logged in, show gorgeous login modal
  if (!currentUser && !authLoading) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '20px',
        }}
      >
        <div
          className="glass-card"
          style={{
            width: '100%',
            maxWidth: '440px',
            padding: '36px',
            border: '1px solid var(--border-accent)',
            boxShadow: 'var(--shadow-glow)',
            textAlign: 'center',
          }}
        >
          <div
            style={{
              width: '56px',
              height: '56px',
              borderRadius: '16px',
              background: 'var(--gradient-brand)',
              margin: '0 auto 20px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxShadow: '0 0 25px rgba(139, 92, 246, 0.5)',
            }}
          >
            <Bot size={30} color="#fff" />
          </div>

          <h2 style={{ fontSize: '1.6rem', fontWeight: 800, marginBottom: '6px' }}>
            AETHERFLOW
          </h2>
          <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: '24px' }}>
            Multi-Agent Workflow Platform & Governance
          </p>

          {/* Quick Demo 1-Click Role Logins */}
          <div style={{ marginBottom: '24px', textAlign: 'left' }}>
            <span style={{ display: 'block', fontSize: '0.72rem', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '8px', fontWeight: 600 }}>
              Quick 1-Click Demo Login
            </span>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              <button
                onClick={() => handleRoleSwitch('operator')}
                className="btn btn-secondary"
                style={{ fontSize: '0.78rem', justifyContent: 'flex-start', padding: '8px 10px' }}
              >
                <Bot size={14} color="#818cf8" />
                <span>Operator</span>
              </button>
              <button
                onClick={() => handleRoleSwitch('approver')}
                className="btn btn-secondary"
                style={{ fontSize: '0.78rem', justifyContent: 'flex-start', padding: '8px 10px' }}
              >
                <Shield size={14} color="#fbbf24" />
                <span>Approver</span>
              </button>
              <button
                onClick={() => handleRoleSwitch('admin')}
                className="btn btn-secondary"
                style={{ fontSize: '0.78rem', justifyContent: 'flex-start', padding: '8px 10px' }}
              >
                <Cpu size={14} color="#c084fc" />
                <span>Admin</span>
              </button>
              <button
                onClick={() => handleRoleSwitch('viewer')}
                className="btn btn-secondary"
                style={{ fontSize: '0.78rem', justifyContent: 'flex-start', padding: '8px 10px' }}
              >
                <UserIcon size={14} color="#34d399" />
                <span>Viewer</span>
              </button>
            </div>
          </div>

          <div style={{ position: 'relative', marginBottom: '24px' }}>
            <div style={{ borderTop: '1px solid var(--border-subtle)' }} />
            <span
              style={{
                position: 'absolute',
                top: '-10px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: '#0e1526',
                padding: '0 10px',
                fontSize: '0.72rem',
                color: 'var(--text-muted)',
              }}
            >
              or enter credentials
            </span>
          </div>

          <form onSubmit={handleManualLogin} style={{ textAlign: 'left' }}>
            <div style={{ marginBottom: '14px' }}>
              <label style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '4px' }}>
                Email
              </label>
              <input
                type="email"
                className="input"
                value={loginEmail}
                onChange={(e) => setLoginEmail(e.target.value)}
                required
              />
            </div>

            <div style={{ marginBottom: '18px' }}>
              <label style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '4px' }}>
                Password
              </label>
              <input
                type="password"
                className="input"
                value={loginPassword}
                onChange={(e) => setLoginPassword(e.target.value)}
                required
              />
            </div>

            {loginError && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#fb7185', fontSize: '0.78rem', marginBottom: '14px' }}>
                <AlertCircle size={14} />
                <span>{loginError}</span>
              </div>
            )}

            <button
              type="submit"
              className="btn btn-primary"
              style={{ width: '100%', padding: '10px' }}
            >
              Sign In
            </button>
          </form>

          {/* API Server status & config */}
          <div style={{ marginTop: '20px', paddingTop: '16px', borderTop: '1px solid var(--border-subtle)' }}>
            {!showApiConfig ? (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
                  <span
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: '50%',
                      backgroundColor: backendOnline ? '#34d399' : '#fb7185',
                      display: 'inline-block',
                      boxShadow: backendOnline ? '0 0 8px rgba(52, 211, 153, 0.6)' : 'none',
                    }}
                  />
                  <span>Server: {backendOnline ? 'Online' : 'Connecting...'}</span>
                </span>
                <button
                  type="button"
                  onClick={() => setShowApiConfig(true)}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#818cf8',
                    cursor: 'pointer',
                    fontSize: '0.72rem',
                    textDecoration: 'underline',
                  }}
                >
                  Configure Endpoint
                </button>
              </div>
            ) : (
              <div style={{ textAlign: 'left', fontSize: '0.74rem' }}>
                <label style={{ display: 'block', color: 'var(--text-muted)', marginBottom: '4px', fontSize: '0.7rem' }}>
                  Backend API URL (Tunnel / Server)
                </label>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <input
                    type="url"
                    className="input"
                    value={customApiUrl}
                    onChange={(e) => setCustomApiUrl(e.target.value)}
                    placeholder="https://..."
                    style={{ fontSize: '0.72rem', padding: '6px 8px' }}
                  />
                  <button
                    type="button"
                    className="btn btn-primary"
                    style={{ fontSize: '0.72rem', padding: '6px 12px' }}
                    onClick={() => {
                      setApiBase(customApiUrl);
                      setShowApiConfig(false);
                      checkHealthAndApprovals();
                    }}
                  >
                    Save
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-container">
      {/* Header bar */}
      <Header
        currentUser={currentUser}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        onRoleSwitch={handleRoleSwitch}
        onLogout={handleLogout}
        backendOnline={backendOnline}
        pendingApprovalsCount={pendingApprovalsCount}
      />

      {/* Main Content Body */}
      <main style={{ flex: 1 }}>
        {activeTab === 'chat' && (
          <ChatView
            onOpenWorkflow={handleOpenWorkflow}
            onGoToApprovals={handleGoToApprovals}
          />
        )}

        {activeTab === 'workflows' && (
          <WorkflowsView
            initialRunId={selectedWorkflowId}
            onGoToApprovals={handleGoToApprovals}
          />
        )}

        {activeTab === 'approvals' && (
          <ApprovalsView
            currentUser={currentUser}
            onOpenWorkflow={handleOpenWorkflow}
          />
        )}

        {activeTab === 'audit' && (
          <AuditLogsView
            currentUser={currentUser}
            onSwitchToAdmin={() => handleRoleSwitch('admin')}
          />
        )}
      </main>
    </div>
  );
};
export default App;
