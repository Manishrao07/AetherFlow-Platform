import React, { useEffect, useRef, useState } from 'react';
import {
  Bot,
  Check,
  ChevronDown,
  Cpu,
  FileCheck2,
  GitBranch,
  LogOut,
  MessageSquare,
  Radio,
  Server,
  Settings as SettingsIcon,
  Shield,
  ShieldAlert,
  Sparkles,
  User as UserIcon,
} from 'lucide-react';
import { Role, User } from '../types/api';
import { getApiBase, setApiBase } from '../services/api';

interface HeaderProps {
  currentUser: User | null;
  activeTab: 'chat' | 'workflows' | 'approvals' | 'audit' | 'settings';
  setActiveTab: (tab: 'chat' | 'workflows' | 'approvals' | 'audit' | 'settings') => void;
  onRoleSwitch: (role: Role) => void;
  onLogout: () => void;
  backendOnline: boolean;
  pendingApprovalsCount: number;
}

export const Header: React.FC<HeaderProps> = ({
  currentUser,
  activeTab,
  setActiveTab,
  onRoleSwitch,
  onLogout,
  backendOnline,
  pendingApprovalsCount,
}) => {
  const [showSettings, setShowSettings] = useState(false);
  const [showRoleDropdown, setShowRoleDropdown] = useState(false);
  const [customApiUrl, setCustomApiUrl] = useState(getApiBase());
  const [savedMsg, setSavedMsg] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const demoRoles: Array<{ role: Role; label: string; email: string; icon: typeof UserIcon; desc: string; color: string }> = [
    { role: 'operator', label: 'Operator', email: 'operator@demo.local', icon: Bot, desc: 'Execute workflows & chat', color: '#818cf8' },
    { role: 'approver', label: 'Approver', email: 'approver@demo.local', icon: Shield, desc: 'Decide high-risk gated actions', color: '#fbbf24' },
    { role: 'admin', label: 'Admin', email: 'admin@demo.local', icon: Cpu, desc: 'Full privileges & audit logs', color: '#c084fc' },
    { role: 'viewer', label: 'Viewer', email: 'viewer@demo.local', icon: UserIcon, desc: 'Read-only audit & run viewer', color: '#34d399' },
  ];

  const currentRole = currentUser?.roles?.[0] || 'operator';
  const activeRoleConfig = demoRoles.find((r) => r.role === currentRole) || demoRoles[0];
  const ActiveIcon = activeRoleConfig.icon;

  // Close dropdown on click outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setShowRoleDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleSaveApiUrl = () => {
    setApiBase(customApiUrl);
    setSavedMsg(true);
    setTimeout(() => {
      setSavedMsg(false);
      setShowSettings(false);
      window.location.reload();
    }, 800);
  };

  return (
    <>
      <header
        style={{
          background: 'rgba(8, 12, 22, 0.92)',
          backdropFilter: 'blur(24px)',
          WebkitBackdropFilter: 'blur(24px)',
          borderBottom: '1px solid rgba(255, 255, 255, 0.07)',
          position: 'sticky',
          top: 0,
          zIndex: 50,
          height: '60px',
        }}
        className="px-6 flex items-center"
      >
        <div
          style={{
            width: '100%',
            maxWidth: '1520px',
            margin: '0 auto',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '16px',
          }}
        >
          {/* 1. Left: Sleek Brand Logo */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
            <div
              style={{
                width: '34px',
                height: '34px',
                borderRadius: '10px',
                background: 'linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #ec4899 100%)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                boxShadow: '0 0 16px rgba(139, 92, 246, 0.45)',
              }}
            >
              <Bot size={19} color="#ffffff" />
            </div>
            
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span
                style={{
                  fontSize: '1.15rem',
                  fontWeight: 800,
                  letterSpacing: '-0.02em',
                  background: 'linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%)',
                  WebkitBackgroundClip: 'text',
                  WebkitTextFillColor: 'transparent',
                }}
              >
                AetherFlow
              </span>
              <span
                style={{
                  fontSize: '0.65rem',
                  fontWeight: 600,
                  padding: '2px 6px',
                  borderRadius: '6px',
                  background: 'rgba(139, 92, 246, 0.15)',
                  color: '#c084fc',
                  border: '1px solid rgba(139, 92, 246, 0.25)',
                }}
              >
                v1.0
              </span>
            </div>
          </div>

          {/* 2. Center: Navigation Tabs */}
          <nav
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              background: 'rgba(255, 255, 255, 0.03)',
              padding: '3px 4px',
              borderRadius: '10px',
              border: '1px solid rgba(255, 255, 255, 0.06)',
            }}
          >
            <button
              onClick={() => setActiveTab('chat')}
              className={`btn ${activeTab === 'chat' ? 'btn-primary' : 'btn-secondary'}`}
              style={{
                padding: '6px 14px',
                fontSize: '0.82rem',
                border: activeTab === 'chat' ? 'none' : 'transparent',
                background: activeTab === 'chat' ? 'var(--gradient-brand)' : 'transparent',
                borderRadius: '8px',
              }}
            >
              <MessageSquare size={15} />
              <span>Grounded Chat</span>
            </button>

            <button
              onClick={() => setActiveTab('workflows')}
              className={`btn ${activeTab === 'workflows' ? 'btn-primary' : 'btn-secondary'}`}
              style={{
                padding: '6px 14px',
                fontSize: '0.82rem',
                border: activeTab === 'workflows' ? 'none' : 'transparent',
                background: activeTab === 'workflows' ? 'var(--gradient-brand)' : 'transparent',
                borderRadius: '8px',
              }}
            >
              <GitBranch size={15} />
              <span>Workflows & DAG</span>
            </button>

            <button
              onClick={() => setActiveTab('approvals')}
              className={`btn ${activeTab === 'approvals' ? 'btn-primary' : 'btn-secondary'}`}
              style={{
                padding: '6px 14px',
                fontSize: '0.82rem',
                position: 'relative',
                border: activeTab === 'approvals' ? 'none' : 'transparent',
                background: activeTab === 'approvals' ? 'var(--gradient-brand)' : 'transparent',
                borderRadius: '8px',
              }}
            >
              <ShieldAlert size={15} />
              <span>Approvals</span>
              {pendingApprovalsCount > 0 && (
                <span
                  style={{
                    position: 'absolute',
                    top: '-3px',
                    right: '-3px',
                    background: '#f43f5e',
                    color: '#fff',
                    borderRadius: '999px',
                    fontSize: '0.62rem',
                    fontWeight: 700,
                    width: '17px',
                    height: '17px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    boxShadow: '0 0 10px rgba(244, 63, 94, 0.7)',
                  }}
                >
                  {pendingApprovalsCount}
                </span>
              )}
            </button>

            <button
              onClick={() => setActiveTab('audit')}
              className={`btn ${activeTab === 'audit' ? 'btn-primary' : 'btn-secondary'}`}
              style={{
                padding: '6px 14px',
                fontSize: '0.82rem',
                border: activeTab === 'audit' ? 'none' : 'transparent',
                background: activeTab === 'audit' ? 'var(--gradient-brand)' : 'transparent',
                borderRadius: '8px',
              }}
            >
              <FileCheck2 size={15} />
              <span>Audit Logs</span>
            </button>
          </nav>

          {/* 3. Right: Clean Controls */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
            
            {/* Minimal Pulse Status */}
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '5px 10px',
                background: backendOnline ? 'rgba(16, 185, 129, 0.08)' : 'rgba(239, 68, 68, 0.08)',
                border: `1px solid ${backendOnline ? 'rgba(16, 185, 129, 0.2)' : 'rgba(239, 68, 68, 0.2)'}`,
                borderRadius: '8px',
                fontSize: '0.72rem',
                color: backendOnline ? '#34d399' : '#f87171',
              }}
              title={backendOnline ? 'Backend API connected' : 'Connecting to API...'}
            >
              <div
                style={{
                  width: '6px',
                  height: '6px',
                  borderRadius: '50%',
                  background: backendOnline ? '#10b981' : '#ef4444',
                  boxShadow: backendOnline ? '0 0 8px #10b981' : '0 0 8px #ef4444',
                }}
              />
              <span>{backendOnline ? 'Online' : 'Offline'}</span>
            </div>

            {/* Role Selector Dropdown */}
            <div style={{ position: 'relative' }} ref={dropdownRef}>
              <button
                onClick={() => setShowRoleDropdown(!showRoleDropdown)}
                className="btn btn-secondary"
                style={{
                  padding: '5px 10px',
                  fontSize: '0.78rem',
                  gap: '7px',
                  borderRadius: '8px',
                  borderColor: 'rgba(255, 255, 255, 0.12)',
                }}
              >
                <ActiveIcon size={14} color={activeRoleConfig.color} />
                <span style={{ fontWeight: 600, color: '#f1f5f9' }}>
                  {activeRoleConfig.label}
                </span>
                <ChevronDown size={13} color="var(--text-muted)" />
              </button>

              {/* Dropdown Menu */}
              {showRoleDropdown && (
                <div
                  className="glass-card"
                  style={{
                    position: 'absolute',
                    top: 'calc(100% + 8px)',
                    right: 0,
                    width: '240px',
                    padding: '6px',
                    borderRadius: '12px',
                    border: '1px solid var(--border-light)',
                    boxShadow: '0 12px 30px rgba(0, 0, 0, 0.7)',
                    background: 'rgba(13, 19, 33, 0.96)',
                    backdropFilter: 'blur(20px)',
                    zIndex: 60,
                  }}
                >
                  <div
                    style={{
                      fontSize: '0.68rem',
                      fontWeight: 600,
                      color: 'var(--text-muted)',
                      textTransform: 'uppercase',
                      letterSpacing: '0.05em',
                      padding: '6px 10px 4px',
                    }}
                  >
                    Switch Demo Role
                  </div>

                  {demoRoles.map((dr) => {
                    const isSelected = dr.role === currentRole;
                    const IconComp = dr.icon;
                    return (
                      <div
                        key={dr.role}
                        onClick={() => {
                          onRoleSwitch(dr.role);
                          setShowRoleDropdown(false);
                        }}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          padding: '8px 10px',
                          borderRadius: '8px',
                          cursor: 'pointer',
                          background: isSelected ? 'rgba(139, 92, 246, 0.15)' : 'transparent',
                          transition: 'background 0.15s ease',
                        }}
                        onMouseEnter={(e) => {
                          if (!isSelected) e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
                        }}
                        onMouseLeave={(e) => {
                          if (!isSelected) e.currentTarget.style.background = 'transparent';
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                          <div
                            style={{
                              width: '28px',
                              height: '28px',
                              borderRadius: '7px',
                              background: 'rgba(255, 255, 255, 0.04)',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                            }}
                          >
                            <IconComp size={15} color={dr.color} />
                          </div>
                          <div>
                            <div style={{ fontSize: '0.82rem', fontWeight: 600, color: '#f8fafc' }}>
                              {dr.label}
                            </div>
                            <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
                              {dr.desc}
                            </div>
                          </div>
                        </div>

                        {isSelected && <Check size={14} color="var(--accent-purple)" />}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Divider */}
            <div style={{ width: '1px', height: '20px', background: 'rgba(255, 255, 255, 0.1)' }} />

            {/* User chip */}
            {currentUser && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '4px 8px',
                  borderRadius: '8px',
                  background: 'rgba(255, 255, 255, 0.03)',
                  border: '1px solid rgba(255, 255, 255, 0.06)',
                  fontSize: '0.78rem',
                  color: 'var(--text-secondary)',
                }}
              >
                <div
                  style={{
                    width: '20px',
                    height: '20px',
                    borderRadius: '50%',
                    background: 'rgba(139, 92, 246, 0.25)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: '#c084fc',
                  }}
                >
                  <UserIcon size={12} />
                </div>
                <span>{currentUser.email.split('@')[0]}</span>
              </div>
            )}

            {/* Logout button */}
            {currentUser && (
              <button
                onClick={onLogout}
                className="btn btn-secondary btn-icon"
                style={{ width: '32px', height: '32px' }}
                title="Logout"
              >
                <LogOut size={14} />
              </button>
            )}

            {/* Settings button */}
            <button
              onClick={() => setShowSettings(true)}
              className="btn btn-secondary btn-icon"
              style={{ width: '32px', height: '32px' }}
              title="Configure Backend Connection"
            >
              <SettingsIcon size={14} />
            </button>
          </div>
        </div>
      </header>

      {/* Backend Settings Modal */}
      {showSettings && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            backdropFilter: 'blur(8px)',
            zIndex: 100,
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
              maxWidth: '520px',
              padding: '24px',
              border: '1px solid var(--border-accent)',
              boxShadow: 'var(--shadow-glow)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
              <Server size={22} color="var(--accent-purple)" />
              <h3 style={{ fontSize: '1.2rem', fontWeight: 700 }}>Backend Connection Settings</h3>
            </div>

            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              When deployed to <strong>Vercel</strong> or another host, configure the live FastAPI backend URL (e.g. <code>https://api.yourdomain.com</code> or <code>http://localhost:8080</code>).
            </p>

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '6px' }}>
                API Base URL
              </label>
              <input
                type="text"
                className="input"
                value={customApiUrl}
                onChange={(e) => setCustomApiUrl(e.target.value)}
                placeholder="http://localhost:8080"
              />
            </div>

            {savedMsg && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#34d399', fontSize: '0.85rem', marginBottom: '12px' }}>
                <span>Saved! Reloading interface...</span>
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                className="btn btn-secondary"
                onClick={() => setShowSettings(false)}
              >
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={handleSaveApiUrl}
              >
                Save & Apply
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};
