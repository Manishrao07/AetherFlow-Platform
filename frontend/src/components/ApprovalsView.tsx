import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  ExternalLink,
  Flame,
  Info,
  Loader2,
  Lock,
  RefreshCw,
  Shield,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  XCircle,
} from 'lucide-react';
import { api } from '../services/api';
import { ApprovalOut, User } from '../types/api';

interface ApprovalsViewProps {
  currentUser: User | null;
  onOpenWorkflow: (runId: string) => void;
}

export const ApprovalsView: React.FC<ApprovalsViewProps> = ({ currentUser, onOpenWorkflow }) => {
  const [approvals, setApprovals] = useState<ApprovalOut[]>([]);
  const [filter, setFilter] = useState<'PENDING' | 'APPROVED' | 'REJECTED'>('PENDING');
  const [loading, setLoading] = useState(false);
  const [actionModal, setActionModal] = useState<{
    approval: ApprovalOut;
    type: 'approve' | 'reject';
  } | null>(null);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const loadApprovals = async () => {
    setLoading(true);
    try {
      const res = await api.listApprovals(filter);
      setApprovals(res.items);
    } catch (err) {
      console.error('Failed to load approvals', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadApprovals();
  }, [filter]);

  const handleDecision = async () => {
    if (!actionModal) return;
    const { approval, type } = actionModal;

    if (type === 'reject' && !comment.trim()) {
      alert('A comment is strictly required when rejecting a plan.');
      return;
    }

    setSubmitting(true);
    try {
      if (type === 'approve') {
        await api.approve(approval.id, comment);
      } else {
        await api.reject(approval.id, comment.trim());
      }
      setActionModal(null);
      setComment('');
      await loadApprovals();
    } catch (err: unknown) {
      alert((err as Error).message || 'Failed to submit decision');
    } finally {
      setSubmitting(false);
    }
  };

  const isRequester = (approval: ApprovalOut) => {
    return currentUser?.id === approval.requested_by;
  };

  const canDecide = currentUser?.roles?.some((r) => r === 'admin' || r === 'approver');

  return (
    <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '28px' }}>
      
      {/* Header banner */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                width: '36px',
                height: '36px',
                borderRadius: '10px',
                background: 'rgba(245, 158, 11, 0.2)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <ShieldAlert size={20} color="#fbbf24" />
            </div>
            <h2 style={{ fontSize: '1.4rem', fontWeight: 700 }}>Human-in-the-Loop Approvals</h2>
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
            Enforce compliance, separation of duties, and security sign-off before executing high-risk autonomous tools.
          </p>
        </div>

        <button
          onClick={loadApprovals}
          className="btn btn-secondary btn-icon"
          title="Refresh"
        >
          <RefreshCw size={16} />
        </button>
      </div>

      {/* Separation of duties rule reminder */}
      <div
        className="glass-card"
        style={{
          padding: '14px 18px',
          marginBottom: '20px',
          border: '1px solid rgba(99, 102, 241, 0.25)',
          background: 'rgba(99, 102, 241, 0.08)',
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
        }}
      >
        <Lock size={18} color="#818cf8" />
        <span style={{ fontSize: '0.82rem', color: '#cbd5e1' }}>
          <strong>Separation of Duties Policy:</strong> Requesters cannot approve their own workflow runs, even if they possess the <code>admin</code> role. Decisions require a separate <code>approver</code>.
        </span>
      </div>

      {/* Filter Tabs */}
      <div style={{ display: 'flex', gap: '10px', marginBottom: '20px' }}>
        {(['PENDING', 'APPROVED', 'REJECTED'] as const).map((st) => (
          <button
            key={st}
            onClick={() => setFilter(st)}
            className={`btn ${filter === st ? 'btn-primary' : 'btn-secondary'}`}
            style={{ padding: '6px 16px', fontSize: '0.82rem' }}
          >
            {st === 'PENDING' && <Clock size={14} />}
            {st === 'APPROVED' && <CheckCircle2 size={14} />}
            {st === 'REJECTED' && <XCircle size={14} />}
            <span>{st}</span>
          </button>
        ))}
      </div>

      {/* Approvals List */}
      {loading ? (
        <div className="glass-card" style={{ padding: '50px', textAlign: 'center', color: 'var(--text-muted)' }}>
          <Loader2 size={28} className="spin" style={{ margin: '0 auto 10px', color: 'var(--accent-purple)' }} />
          Loading approvals...
        </div>
      ) : approvals.length === 0 ? (
        <div className="glass-card" style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
          <ShieldCheck size={44} style={{ margin: '0 auto 14px', opacity: 0.3 }} />
          <h3 style={{ fontSize: '1.1rem', fontWeight: 600 }}>No {filter} Approvals</h3>
          <p style={{ fontSize: '0.82rem' }}>All autonomous workflows are currently in compliance.</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {approvals.map((appr) => {
            const selfConflict = isRequester(appr);
            return (
              <div
                key={appr.id}
                className="glass-card"
                style={{
                  padding: '20px',
                  border: appr.status === 'PENDING' ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid var(--border-subtle)',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '16px', marginBottom: '14px' }}>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
                      <span className="mono" style={{ fontSize: '0.8rem', color: 'var(--accent-purple)', fontWeight: 600 }}>
                        APPROVAL {appr.id.slice(0, 8)}
                      </span>
                      <span className={`badge badge-${appr.status.toLowerCase()}`}>
                        {appr.status}
                      </span>
                      <span className={`badge badge-${appr.risk_level.toLowerCase()}`}>
                        {appr.risk_level} RISK
                      </span>
                    </div>

                    <h4 style={{ fontSize: '1.05rem', fontWeight: 600, color: '#fff', marginBottom: '4px' }}>
                      {appr.goal || appr.reason.split('\n')[0]}
                    </h4>

                    <div style={{ display: 'flex', alignItems: 'center', gap: '14px', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                      <span>Requested by: <strong style={{ color: '#cbd5e1' }}>{appr.requester_email}</strong></span>
                      <span>Expires: {new Date(appr.expires_at).toLocaleString()}</span>
                    </div>
                  </div>

                  <button
                    onClick={() => onOpenWorkflow(appr.run_id)}
                    className="btn btn-secondary"
                    style={{ padding: '6px 12px', fontSize: '0.78rem' }}
                  >
                    <ExternalLink size={13} />
                    Inspect Run
                  </button>
                </div>

                {/* Plan Breakdown / Gated Steps */}
                <div
                  style={{
                    background: 'rgba(0, 0, 0, 0.35)',
                    padding: '12px 16px',
                    borderRadius: '8px',
                    fontSize: '0.82rem',
                    marginBottom: '16px',
                    border: '1px solid var(--border-subtle)',
                  }}
                >
                  <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '6px', textTransform: 'uppercase' }}>
                    Reason & Gated Actions
                  </div>
                  <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit', color: '#e2e8f0', margin: 0 }}>
                    {appr.reason}
                  </pre>
                </div>

                {/* Decided Comment (if approved/rejected) */}
                {appr.comment && (
                  <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: '14px' }}>
                    <strong>Decision Note:</strong> <em>"{appr.comment}"</em>
                  </div>
                )}

                {/* Action Buttons */}
                {appr.status === 'PENDING' && (
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: '12px', borderTop: '1px solid var(--border-subtle)' }}>
                    <div>
                      {selfConflict && (
                        <span style={{ fontSize: '0.78rem', color: '#fb7185', display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <AlertTriangle size={14} />
                          Separation of duties: You cannot approve your own request.
                        </span>
                      )}
                      {!canDecide && (
                        <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                          Your role does not have approval decision privileges.
                        </span>
                      )}
                    </div>

                    <div style={{ display: 'flex', gap: '10px' }}>
                      <button
                        onClick={() => {
                          setActionModal({ approval: appr, type: 'reject' });
                          setComment('');
                        }}
                        disabled={selfConflict || !canDecide}
                        className="btn btn-danger"
                        style={{ padding: '6px 14px', fontSize: '0.82rem', opacity: selfConflict || !canDecide ? 0.4 : 1 }}
                      >
                        <XCircle size={15} />
                        Reject Plan
                      </button>

                      <button
                        onClick={() => {
                          setActionModal({ approval: appr, type: 'approve' });
                          setComment('Approved via Web Console');
                        }}
                        disabled={selfConflict || !canDecide}
                        className="btn btn-success"
                        style={{ padding: '6px 16px', fontSize: '0.82rem', opacity: selfConflict || !canDecide ? 0.4 : 1 }}
                      >
                        <CheckCircle2 size={15} />
                        Approve & Resume
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Approve / Reject Modal */}
      {actionModal && (
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
              border: `1px solid ${actionModal.type === 'approve' ? 'var(--accent-emerald)' : 'var(--accent-rose)'}`,
              boxShadow: 'var(--shadow-glow)',
            }}
          >
            <h3 style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: '10px', color: '#fff' }}>
              {actionModal.type === 'approve' ? 'Approve Plan Execution' : 'Reject Workflow Plan'}
            </h3>

            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              {actionModal.type === 'approve'
                ? 'Approving will immediately resume the workflow engine to execute the gated actions.'
                : 'Rejecting will cancel the run and inform the requester of the reason.'}
            </p>

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '6px' }}>
                {actionModal.type === 'approve' ? 'Comment (Optional)' : 'Rejection Reason (Required)'}
              </label>
              <textarea
                className="textarea"
                rows={3}
                placeholder={actionModal.type === 'approve' ? 'Looks good to proceed...' : 'Explain why the gated action was rejected...'}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
              />
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                className="btn btn-secondary"
                onClick={() => setActionModal(null)}
                disabled={submitting}
              >
                Cancel
              </button>
              <button
                className={`btn ${actionModal.type === 'approve' ? 'btn-success' : 'btn-danger'}`}
                onClick={handleDecision}
                disabled={submitting || (actionModal.type === 'reject' && !comment.trim())}
              >
                {submitting ? <Loader2 size={16} className="spin" /> : null}
                <span>Confirm {actionModal.type === 'approve' ? 'Approval' : 'Rejection'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
