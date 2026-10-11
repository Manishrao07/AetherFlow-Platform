import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Cpu,
  FileCheck2,
  Filter,
  Globe,
  Loader2,
  Lock,
  RefreshCw,
  Shield,
  ShieldAlert,
  User,
} from 'lucide-react';
import { api } from '../services/api';
import { AuditLogEntry, User as UserType } from '../types/api';

interface AuditLogsViewProps {
  currentUser: UserType | null;
  onSwitchToAdmin: () => void;
}

export const AuditLogsView: React.FC<AuditLogsViewProps> = ({ currentUser, onSwitchToAdmin }) => {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [actionFilter, setActionFilter] = useState('');
  const [expandedLogId, setExpandedLogId] = useState<number | string | null>(null);

  const isAdmin = currentUser?.roles?.includes('admin');

  const actions = [
    { label: 'All Actions', value: '' },
    { label: 'approval.approved', value: 'approval.approved' },
    { label: 'approval.rejected', value: 'approval.rejected' },
    { label: 'approval.requested', value: 'approval.requested' },
    { label: 'workflow.created', value: 'workflow.created' },
    { label: 'access.denied', value: 'access.denied' },
    { label: 'auth.login', value: 'auth.login' },
  ];

  const loadLogs = async () => {
    if (!isAdmin) {
      setErrorMsg('Audit logs are restricted to the Admin role (Least Privilege RBAC).');
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await api.listAuditLogs(actionFilter || undefined, 60);
      setLogs(res.items);
    } catch (err: unknown) {
      console.error('Failed to load audit logs', err);
      setErrorMsg((err as Error).message || 'Failed to load audit logs');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadLogs();
  }, [actionFilter, currentUser]);

  const getActionColor = (action: string) => {
    if (action.includes('denied') || action.includes('rejected') || action.includes('failed')) {
      return '#fb7185';
    }
    if (action.includes('approved') || action.includes('success')) {
      return '#34d399';
    }
    if (action.includes('requested') || action.includes('pending')) {
      return '#fbbf24';
    }
    return 'var(--accent-purple)';
  };

  return (
    <div style={{ maxWidth: '1280px', margin: '0 auto', padding: '28px' }}>
      
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                width: '36px',
                height: '36px',
                borderRadius: '10px',
                background: 'rgba(99, 102, 241, 0.2)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <FileCheck2 size={20} color="#818cf8" />
            </div>
            <h2 style={{ fontSize: '1.4rem', fontWeight: 700 }}>Append-Only Security Audit Log</h2>
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Tamper-proof compliance stream tracking user decisions, human approvals, access violations, and system events.
          </p>
        </div>

        {isAdmin && (
          <button
            onClick={loadLogs}
            className="btn btn-secondary btn-icon"
            title="Refresh"
          >
            <RefreshCw size={16} />
          </button>
        )}
      </div>

      {/* Non-Admin RBAC Notice Banner */}
      {!isAdmin && (
        <div
          className="glass-card"
          style={{
            padding: '28px',
            marginBottom: '24px',
            border: '1px solid rgba(245, 158, 11, 0.4)',
            background: 'rgba(245, 158, 11, 0.08)',
            textAlign: 'center',
          }}
        >
          <div
            style={{
              width: '48px',
              height: '48px',
              borderRadius: '14px',
              background: 'rgba(245, 158, 11, 0.2)',
              margin: '0 auto 14px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Lock size={26} color="#fbbf24" />
          </div>

          <h3 style={{ fontSize: '1.2rem', fontWeight: 700, color: '#fff', marginBottom: '8px' }}>
            Admin Privileges Required (Least Privilege RBAC)
          </h3>

          <p style={{ fontSize: '0.88rem', color: '#cbd5e1', maxWidth: '640px', margin: '0 auto 20px', lineHeight: 1.5 }}>
            Security compliance rules prevent regular <strong>Operator</strong> and <strong>Approver</strong> roles from inspecting full organization audit logs. Only users with the <strong>Admin</strong> role (<code>admin@demo.local</code>) can access the audit trail.
          </p>

          <button
            onClick={onSwitchToAdmin}
            className="btn btn-primary"
            style={{ padding: '10px 24px', fontSize: '0.9rem', margin: '0 auto', gap: '10px' }}
          >
            <Cpu size={18} />
            <span>Switch to Admin Role & View Audit Trail</span>
          </button>
        </div>
      )}

      {/* Filter Row (Only when Admin) */}
      {isAdmin && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.82rem', color: 'var(--text-muted)' }}>
              <Filter size={15} />
              <span>Filter Action:</span>
            </div>

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              {actions.map((act) => (
                <button
                  key={act.value}
                  onClick={() => setActionFilter(act.value)}
                  className={`btn ${actionFilter === act.value ? 'btn-primary' : 'btn-secondary'}`}
                  style={{ padding: '5px 12px', fontSize: '0.78rem' }}
                >
                  {act.label}
                </button>
              ))}
            </div>
          </div>

          {/* Logs Table / List */}
          {loading ? (
            <div className="glass-card" style={{ padding: '50px', textAlign: 'center', color: 'var(--text-muted)' }}>
              <Loader2 size={28} className="spin" style={{ margin: '0 auto 10px', color: 'var(--accent-purple)' }} />
              Loading audit trail...
            </div>
          ) : logs.length === 0 ? (
            <div className="glass-card" style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
              <FileCheck2 size={40} style={{ margin: '0 auto 12px', opacity: 0.3 }} />
              <h3>No Audit Records Found</h3>
              <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)' }}>
                {actionFilter ? `No records matching "${actionFilter}". Try selecting "All Actions".` : 'Audit trail is empty.'}
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {logs.map((log) => {
                const isExpanded = expandedLogId === log.id;
                return (
                  <div
                    key={log.id}
                    className="glass-card"
                    style={{
                      padding: '14px 18px',
                      borderRadius: '10px',
                      border: isExpanded ? '1px solid var(--border-accent)' : '1px solid var(--border-subtle)',
                    }}
                  >
                    <div
                      onClick={() => setExpandedLogId(isExpanded ? null : log.id)}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        cursor: 'pointer',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
                        {isExpanded ? <ChevronDown size={16} color="var(--text-muted)" /> : <ChevronRight size={16} color="var(--text-muted)" />}
                        
                        <span
                          style={{
                            padding: '4px 10px',
                            borderRadius: '6px',
                            background: 'rgba(255, 255, 255, 0.04)',
                            border: `1px solid ${getActionColor(log.action)}`,
                            color: getActionColor(log.action),
                            fontSize: '0.78rem',
                            fontWeight: 600,
                            fontFamily: 'var(--font-mono)',
                          }}
                        >
                          {log.action}
                        </span>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85rem' }}>
                          <User size={14} color="var(--text-muted)" />
                          <span style={{ color: '#f1f5f9', fontWeight: 500 }}>{log.actor_email || '(system/anonymous)'}</span>
                        </div>

                        {log.resource_type && (
                          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                            Resource: <strong>{log.resource_type}</strong>
                            {log.resource_id && ` (${String(log.resource_id).slice(0, 8)})`}
                          </span>
                        )}
                      </div>

                      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', fontSize: '0.75rem', color: 'var(--text-dim)' }}>
                        {log.ip_address && (
                          <span style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                            <Globe size={13} />
                            {log.ip_address}
                          </span>
                        )}
                        <span>{new Date(log.created_at).toLocaleString()}</span>
                      </div>
                    </div>

                    {isExpanded && (
                      <div style={{ marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--border-subtle)' }}>
                        <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '6px', textTransform: 'uppercase' }}>
                          Audit Details & Payload
                        </div>
                        <pre style={{ background: 'rgba(0, 0, 0, 0.4)', padding: '12px', borderRadius: '8px', overflowX: 'auto', fontSize: '0.8rem' }}>
                          <code>{JSON.stringify(log.details, null, 2)}</code>
                        </pre>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
};
