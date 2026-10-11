import React, { useEffect, useState } from 'react';
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Coins,
  Cpu,
  ExternalLink,
  Flame,
  GitBranch,
  Layers,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Shield,
  ShieldAlert,
  Sparkles,
  Terminal,
  XCircle,
} from 'lucide-react';
import { api } from '../services/api';
import { RiskLevel, RunStatus, TimelineOut, WorkflowRun } from '../types/api';

interface WorkflowsViewProps {
  initialRunId?: string | null;
  onGoToApprovals: () => void;
}

export const WorkflowsView: React.FC<WorkflowsViewProps> = ({ initialRunId, onGoToApprovals }) => {
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(initialRunId || null);
  const [timeline, setTimeline] = useState<TimelineOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingTimeline, setLoadingTimeline] = useState(false);
  const [showNewModal, setShowNewModal] = useState(false);

  // New run form state
  const [goal, setGoal] = useState('');
  const [mode, setMode] = useState<'standard' | 'deep'>('standard');
  const [creating, setCreating] = useState(false);

  // Expanded step key
  const [expandedStep, setExpandedStep] = useState<string | null>(null);

  const isTerminalStatus = (status?: string | null) => {
    return status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED';
  };

  const loadRuns = async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await api.listWorkflows(30);
      setRuns(res.items);
      if (!selectedRunId && res.items.length > 0) {
        setSelectedRunId(res.items[0].id);
      }
    } catch (err) {
      console.error('Failed to load runs', err);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const loadTimeline = async (id: string, silent = false) => {
    if (!silent) setLoadingTimeline(true);
    try {
      const res = await api.getTimeline(id);
      setTimeline(res);
      if (res.steps.length > 0 && !expandedStep) {
        setExpandedStep(res.steps[0].step_key);
      }
      // Keep the run item in sidebar in sync
      setRuns((prev) =>
        prev.map((r) => (r.id === id ? { ...r, ...res.run } : r))
      );
    } catch (err) {
      console.error('Failed to load timeline', err);
    } finally {
      if (!silent) setLoadingTimeline(false);
    }
  };

  useEffect(() => {
    loadRuns();
  }, []);

  useEffect(() => {
    if (initialRunId) {
      setSelectedRunId(initialRunId);
    }
  }, [initialRunId]);

  useEffect(() => {
    if (selectedRunId) {
      loadTimeline(selectedRunId, false);
    }
  }, [selectedRunId]);

  // Live Auto-Polling Effect: continuously refresh active runs until terminal state
  useEffect(() => {
    if (!selectedRunId) return;

    const currentRun = runs.find((r) => r.id === selectedRunId);
    const active = timeline ? !isTerminalStatus(timeline.run.status) : (currentRun ? !isTerminalStatus(currentRun.status) : true);

    if (!active) return;

    const interval = setInterval(async () => {
      try {
        const res = await api.getTimeline(selectedRunId);
        setTimeline(res);
        if (res.steps.length > 0 && !expandedStep) {
          setExpandedStep(res.steps[0].step_key);
        }
        setRuns((prev) =>
          prev.map((r) => (r.id === selectedRunId ? { ...r, ...res.run } : r))
        );

        if (isTerminalStatus(res.run.status)) {
          clearInterval(interval);
          // Refresh entire runs list once on completion to update any counters or ordering
          api.listWorkflows(30).then((r) => setRuns(r.items)).catch(() => {});
        }
      } catch (err) {
        console.error('Failed to poll timeline', err);
      }
    }, 1500);

    return () => clearInterval(interval);
  }, [selectedRunId, timeline?.run.status, expandedStep]);

  const handleCreateRun = async () => {
    if (!goal.trim() || creating) return;
    setCreating(true);
    try {
      const newRun = await api.createWorkflow(goal.trim(), mode);
      setRuns([newRun, ...runs]);
      setSelectedRunId(newRun.id);
      // Immediately populate initial empty timeline for instant visual feedback
      setTimeline({
        run: newRun,
        steps: [],
        tool_executions: [],
        llm_calls: [],
        approvals: [],
        events: [],
        totals: {
          llm_calls: 0,
          prompt_tokens: 0,
          completion_tokens: 0,
          cost_usd: 0,
          tool_executions: 0,
          revisions: 0,
        },
      });
      setShowNewModal(false);
      setGoal('');
      // Trigger initial fast fetch
      setTimeout(() => loadTimeline(newRun.id, true), 800);
    } catch (err: unknown) {
      alert((err as Error).message || 'Failed to start run');
    } finally {
      setCreating(false);
    }
  };

  const getRiskBadge = (risk: RiskLevel) => {
    switch (risk) {
      case 'HIGH':
        return <span className="badge badge-high">HIGH RISK</span>;
      case 'MEDIUM':
        return <span className="badge badge-medium">MED RISK</span>;
      default:
        return <span className="badge badge-low">LOW RISK</span>;
    }
  };

  const getStatusBadge = (status: RunStatus | string) => {
    const s = status.toLowerCase();
    return <span className={`badge badge-${s}`}>{status}</span>;
  };

  return (
    <div style={{ maxWidth: '1440px', margin: '0 auto', padding: '24px', display: 'grid', gridTemplateColumns: '320px minmax(0, 1fr)', gap: '24px' }}>
      
      {/* Left Column: Runs Directory */}
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
          <div>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>Workflow Runs</h2>
            <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>Historical and active executions</p>
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={() => loadRuns()}
              className="btn btn-secondary btn-icon"
              title="Refresh"
            >
              <RefreshCw size={15} />
            </button>
            <button
              onClick={() => setShowNewModal(true)}
              className="btn btn-primary"
              style={{ padding: '6px 12px', fontSize: '0.82rem' }}
            >
              <Plus size={15} />
              <span>New Run</span>
            </button>
          </div>
        </div>

        {/* Runs List */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', maxHeight: 'calc(100vh - 180px)', overflowY: 'auto' }}>
          {loading && runs.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
              <Loader2 size={24} className="spin" style={{ margin: '0 auto 8px' }} />
              Loading workflow runs...
            </div>
          ) : (
            runs.map((r) => {
              const isSelected = selectedRunId === r.id;
              return (
                <div
                  key={r.id}
                  onClick={() => setSelectedRunId(r.id)}
                  className={`glass-card ${isSelected ? '' : 'glass-card-interactive'}`}
                  style={{
                    padding: '14px',
                    cursor: 'pointer',
                    background: isSelected ? 'rgba(30, 44, 76, 0.7)' : 'rgba(15, 23, 42, 0.6)',
                    borderColor: isSelected ? 'var(--accent-purple)' : 'var(--border-subtle)',
                    boxShadow: isSelected ? 'var(--shadow-glow)' : 'none',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <span className="mono" style={{ fontSize: '0.75rem', color: 'var(--accent-purple)', fontWeight: 600 }}>
                      {r.id.slice(0, 8)}
                    </span>
                    {getStatusBadge(r.status)}
                  </div>

                  <div style={{ fontSize: '0.86rem', fontWeight: 500, color: '#f1f5f9', marginBottom: '8px', lineHeight: 1.4 }}>
                    {r.goal}
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    <span>Mode: <strong style={{ color: '#cbd5e1' }}>{r.mode}</strong></span>
                    <span>Rev: {r.revision_count}/{r.max_revisions}</span>
                    <span>{new Date(r.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Right Column: Run Detail & DAG Visualizer */}
      <div style={{ minWidth: 0 }}>
        {loadingTimeline ? (
          <div className="glass-card" style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
            <Loader2 size={32} className="spin" style={{ margin: '0 auto 12px', color: 'var(--accent-purple)' }} />
            <p>Decompiling DAG & telemetry data...</p>
          </div>
        ) : !timeline ? (
          <div className="glass-card" style={{ padding: '60px', textAlign: 'center', color: 'var(--text-muted)' }}>
            <GitBranch size={40} style={{ margin: '0 auto 16px', opacity: 0.3 }} />
            <h3>Select a Workflow Run to Inspect</h3>
            <p style={{ fontSize: '0.85rem' }}>View dependency graphs, step outputs, tool calls, and review verdicts.</p>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', minWidth: 0 }}>
            
            {/* Top Detail Card */}
            <div className="glass-card" style={{ padding: '22px' }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '20px', marginBottom: '14px' }}>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
                    <span className="mono" style={{ fontSize: '0.85rem', color: 'var(--accent-purple)', fontWeight: 700 }}>
                      RUN {timeline.run.id}
                    </span>
                    {getStatusBadge(timeline.run.status)}
                    {timeline.run.plan?.max_risk && getRiskBadge(timeline.run.plan.max_risk)}
                  </div>
                  <h3 style={{ fontSize: '1.2rem', fontWeight: 700, color: '#fff', lineHeight: 1.4 }}>
                    {timeline.run.goal}
                  </h3>
                </div>

                {timeline.run.status === 'AWAITING_APPROVAL' && (
                  <button
                    onClick={onGoToApprovals}
                    className="btn btn-primary"
                    style={{ background: '#f59e0b', color: '#000', fontWeight: 600, flexShrink: 0 }}
                  >
                    <ShieldAlert size={16} />
                    Needs Human Sign-off
                  </button>
                )}
              </div>

              {/* Metrics Grid */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: '10px', marginTop: '16px', paddingTop: '16px', borderTop: '1px solid var(--border-subtle)' }}>
                <div style={{ padding: '10px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.03)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    <Cpu size={14} color="var(--accent-purple)" />
                    LLM Invocations
                  </div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 700, marginTop: '4px' }}>
                    {timeline.totals.llm_calls} calls
                  </div>
                </div>

                <div style={{ padding: '10px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.03)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    <Layers size={14} color="var(--accent-cyan)" />
                    Tokens (In / Out)
                  </div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 700, marginTop: '4px' }}>
                    {timeline.totals.prompt_tokens} / {timeline.totals.completion_tokens}
                  </div>
                </div>

                <div style={{ padding: '10px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.03)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    <Coins size={14} color="#34d399" />
                    Est. Cost USD
                  </div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 700, marginTop: '4px', color: '#34d399' }}>
                    ${Number(timeline.totals.cost_usd || 0).toFixed(6)}
                  </div>
                </div>

                <div style={{ padding: '10px', borderRadius: '8px', background: 'rgba(255, 255, 255, 0.03)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    <Terminal size={14} color="var(--accent-amber)" />
                    Tool Executions
                  </div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 700, marginTop: '4px' }}>
                    {timeline.totals.tool_executions} calls
                  </div>
                </div>
              </div>
            </div>

            {/* Review Agent Verdict Card */}
            {timeline.run.review_verdict && (
              <div
                className="glass-card"
                style={{
                  padding: '18px',
                  border: `1px solid ${timeline.run.review_verdict === 'APPROVED' ? 'rgba(16, 185, 129, 0.4)' : 'rgba(245, 158, 11, 0.4)'}`,
                  background: timeline.run.review_verdict === 'APPROVED' ? 'rgba(16, 185, 129, 0.08)' : 'rgba(245, 158, 11, 0.08)',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Shield size={18} color={timeline.run.review_verdict === 'APPROVED' ? '#34d399' : '#fbbf24'} />
                    <span style={{ fontSize: '0.9rem', fontWeight: 700 }}>
                      Review Agent Verdict: {timeline.run.review_verdict}
                    </span>
                  </div>
                  {timeline.run.review_score !== null && (
                    <span className="badge badge-completed">
                      Score: {Number(timeline.run.review_score).toFixed(2)} / 1.00
                    </span>
                  )}
                </div>
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
                  {timeline.run.review_verdict === 'APPROVED'
                    ? 'The autonomous Review Agent checked compliance, accuracy, and tool inputs. No hallucinations or policy breaches detected.'
                    : 'The Review Agent triggered a revision loop to address unfulfilled goals or placeholder inputs.'}
                </p>
              </div>
            )}

            {/* Visual DAG Step Flow */}
            <div className="glass-card" style={{ padding: '22px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <GitBranch size={18} color="var(--accent-purple)" />
                  <h4 style={{ fontSize: '1rem', fontWeight: 700 }}>Execution DAG & Step Nodes</h4>
                </div>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  {timeline.steps.length} sequential & parallel nodes
                </span>
              </div>

              {timeline.steps.length === 0 ? (
                <div
                  style={{
                    textAlign: 'center',
                    padding: '36px 20px',
                    borderRadius: '12px',
                    background: 'rgba(255, 255, 255, 0.02)',
                    border: '1px dashed var(--border-subtle)',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '12px',
                  }}
                >
                  {!isTerminalStatus(timeline.run.status) ? (
                    <>
                      <Loader2 size={28} className="spin" style={{ color: 'var(--accent-purple)' }} />
                      <div style={{ fontSize: '0.92rem', fontWeight: 600, color: '#fff' }}>
                        Agent Pipeline Active ({timeline.run.status})...
                      </div>
                      <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', maxWidth: '440px', margin: 0, lineHeight: 1.5 }}>
                        Planner agent is decomposing your goal into DAG nodes and assigning guarded execution tools. Telemetry will update live automatically.
                      </p>
                    </>
                  ) : (
                    <>
                      <Layers size={28} style={{ opacity: 0.3, color: 'var(--text-muted)' }} />
                      <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                        No execution steps recorded for this workflow run.
                      </div>
                    </>
                  )}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                  {timeline.steps.map((st, index) => {
                    const isExpanded = expandedStep === st.step_key;
                    return (
                      <div
                        key={st.id}
                        style={{
                          borderRadius: '10px',
                          border: '1px solid var(--border-subtle)',
                          background: 'rgba(10, 15, 26, 0.6)',
                          overflow: 'hidden',
                          transition: 'all 0.2s ease',
                        }}
                      >
                        <div
                          onClick={() => setExpandedStep(isExpanded ? null : st.step_key)}
                          style={{
                            padding: '12px 16px',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            cursor: 'pointer',
                            background: isExpanded ? 'rgba(255, 255, 255, 0.03)' : 'transparent',
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                            {isExpanded ? <ChevronDown size={16} color="var(--text-muted)" /> : <ChevronRight size={16} color="var(--text-muted)" />}
                            
                            <div
                              style={{
                                width: '26px',
                                height: '26px',
                                borderRadius: '6px',
                                background: 'rgba(139, 92, 246, 0.15)',
                                color: 'var(--accent-purple)',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                fontSize: '0.75rem',
                                fontWeight: 700,
                              }}
                            >
                              {index + 1}
                            </div>

                            <div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span className="mono" style={{ fontSize: '0.85rem', fontWeight: 600, color: '#fff' }}>
                                  {st.step_key}
                                </span>
                                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                                  ({st.agent})
                                </span>
                                {st.tool && (
                                  <span className="badge badge-executing" style={{ fontSize: '0.65rem' }}>
                                    Tool: {st.tool}
                                  </span>
                                )}
                              </div>
                            </div>
                          </div>

                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            {getRiskBadge(st.risk_level)}
                            {getStatusBadge(st.status)}
                          </div>
                        </div>

                        {/* Collapsible Step Payload */}
                        {isExpanded && (
                          <div style={{ padding: '16px', borderTop: '1px solid var(--border-subtle)', background: 'rgba(0, 0, 0, 0.25)', fontSize: '0.8rem' }}>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px' }}>
                              <div>
                                <span style={{ display: 'block', fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase' }}>
                                  Inputs
                                </span>
                                <pre style={{ background: 'rgba(0, 0, 0, 0.4)', padding: '10px', borderRadius: '6px', overflowX: 'auto', maxHeight: '160px' }}>
                                  <code>{JSON.stringify(st.inputs, null, 2)}</code>
                                </pre>
                              </div>

                              <div>
                                <span style={{ display: 'block', fontSize: '0.72rem', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase' }}>
                                  Output Result
                                </span>
                                <pre style={{ background: 'rgba(0, 0, 0, 0.4)', padding: '10px', borderRadius: '6px', overflowX: 'auto', maxHeight: '160px' }}>
                                  <code>{st.output ? JSON.stringify(st.output, null, 2) : '(none)'}</code>
                                </pre>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* LLM Invocations Telemetry Breakdown */}
            {timeline.llm_calls && timeline.llm_calls.length > 0 && (
              <div className="glass-card" style={{ padding: '22px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Cpu size={18} color="var(--accent-purple)" />
                    <h4 style={{ fontSize: '1rem', fontWeight: 700, color: '#fff' }}>
                      LLM Calls Telemetry ({timeline.llm_calls.length} Invocations)
                    </h4>
                  </div>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    Total Tokens: {timeline.totals.prompt_tokens + timeline.totals.completion_tokens}
                  </span>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  {timeline.llm_calls.map((call, idx) => {
                    let agentTitle = call.agent;
                    let agentDesc = 'Autonomous agent reasoning step';
                    let agentColor = 'var(--accent-purple)';

                    if (call.agent.toLowerCase().includes('plan')) {
                      agentTitle = '🧠 Planner Agent';
                      agentDesc = 'Goal decomposition, tool selection, and risk assessment';
                      agentColor = '#818cf8';
                    } else if (call.agent.toLowerCase().includes('retriev') || call.agent.toLowerCase().includes('synth')) {
                      agentTitle = '🔍 Retrieval & Synthesizer Agent';
                      agentDesc = 'Grounded knowledge extraction and answer drafting';
                      agentColor = '#06b6d4';
                    } else if (call.agent.toLowerCase().includes('review')) {
                      agentTitle = '🛡️ Review Agent';
                      agentDesc = 'Hallucination check, policy compliance & quality audit';
                      agentColor = '#34d399';
                    }

                    return (
                      <div
                        key={call.id || idx}
                        style={{
                          padding: '14px 16px',
                          borderRadius: '10px',
                          background: 'rgba(10, 15, 26, 0.6)',
                          border: '1px solid var(--border-subtle)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: '16px',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                          <div
                            style={{
                              width: '28px',
                              height: '28px',
                              borderRadius: '7px',
                              background: 'rgba(255, 255, 255, 0.05)',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              fontSize: '0.75rem',
                              fontWeight: 700,
                              color: 'var(--text-muted)',
                            }}
                          >
                            #{idx + 1}
                          </div>

                          <div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                              <span style={{ fontSize: '0.88rem', fontWeight: 600, color: agentColor }}>
                                {agentTitle}
                              </span>
                              <span className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', padding: '2px 6px', background: 'rgba(255, 255, 255, 0.04)', borderRadius: '4px' }}>
                                {call.provider} / {call.model}
                              </span>
                            </div>
                            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                              {agentDesc}
                            </p>
                          </div>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '14px', fontSize: '0.78rem', flexShrink: 0 }}>
                          <div style={{ textAlign: 'right' }}>
                            <div style={{ color: '#cbd5e1', fontWeight: 600 }}>
                              {call.prompt_tokens} in / {call.completion_tokens} out
                            </div>
                            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
                              Tokens
                            </div>
                          </div>

                          <div style={{ textAlign: 'right', minWidth: '60px' }}>
                            <div style={{ color: '#cbd5e1', fontWeight: 600 }}>
                              {call.latency_ms} ms
                            </div>
                            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
                              Latency
                            </div>
                          </div>

                          <span className="badge badge-completed" style={{ fontSize: '0.65rem' }}>
                            {call.status}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Final Result / Answer Preview */}
            {timeline.run.result && (() => {
              const res = timeline.run.result as any;
              let directAnswer: string | null = null;
              let citations: any[] = [];
              if (res.steps) {
                for (const stepKey of Object.keys(res.steps)) {
                  const stepData = res.steps[stepKey];
                  if (stepData?.answer) {
                    directAnswer = stepData.answer;
                  }
                  if (stepData?.citations && Array.isArray(stepData.citations)) {
                    citations = [...citations, ...stepData.citations];
                  }
                }
              }

              return (
                <div className="glass-card" style={{ padding: '22px', maxWidth: '100%', overflow: 'hidden' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <Sparkles size={18} color="var(--accent-purple)" />
                      <h4 style={{ fontSize: '1rem', fontWeight: 700, color: '#fff' }}>
                        Verified Outcome & Report
                      </h4>
                    </div>
                    {timeline.run.status === 'COMPLETED' && (
                      <span className="badge badge-completed" style={{ fontSize: '0.7rem' }}>
                        <CheckCircle2 size={12} /> Execution Complete
                      </span>
                    )}
                  </div>

                  {/* Highlighted Direct Answer Box if found */}
                  {directAnswer && (
                    <div
                      style={{
                        background: 'rgba(16, 185, 129, 0.08)',
                        border: '1px solid rgba(16, 185, 129, 0.35)',
                        borderRadius: '10px',
                        padding: '16px',
                        marginBottom: '16px',
                      }}
                    >
                      <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#34d399', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: '6px' }}>
                        Grounded Knowledge Answer
                      </div>
                      <div style={{ fontSize: '0.95rem', lineHeight: 1.6, color: '#f8fafc', fontWeight: 500, wordBreak: 'break-word', overflowWrap: 'anywhere' }}>
                        {directAnswer}
                      </div>

                      {citations.length > 0 && (
                        <div style={{ marginTop: '12px', paddingTop: '10px', borderTop: '1px solid rgba(16, 185, 129, 0.2)', display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                          {citations.map((c: any, i: number) => (
                            <span key={i} className="badge badge-completed" style={{ fontSize: '0.72rem' }}>
                              📖 {c.title || c.citation || 'Source Citation'}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Full Synthesis / Report */}
                  <div style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-muted)', marginBottom: '8px', textTransform: 'uppercase' }}>
                    Workflow Execution Summary
                  </div>
                  <div
                    style={{
                      fontSize: '0.9rem',
                      lineHeight: 1.7,
                      color: 'var(--text-primary)',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      overflowWrap: 'anywhere',
                      background: 'rgba(0, 0, 0, 0.25)',
                      padding: '14px 16px',
                      borderRadius: '8px',
                      border: '1px solid var(--border-subtle)',
                    }}
                  >
                    {res.summary || res.answer || JSON.stringify(res, null, 2)}
                  </div>
                </div>
              );
            })()}
          </div>
        )}
      </div>

      {/* New Run Modal */}
      {showNewModal && (
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
              maxWidth: '560px',
              padding: '26px',
              border: '1px solid var(--border-accent)',
              boxShadow: 'var(--shadow-glow)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
              <Plus size={22} color="var(--accent-purple)" />
              <h3 style={{ fontSize: '1.25rem', fontWeight: 700 }}>Dispatch New Workflow Run</h3>
            </div>

            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '6px' }}>
                Objective / Goal
              </label>
              <textarea
                className="textarea"
                rows={4}
                placeholder="e.g. Check the SEV2 incident policy, open a ticket and email ops@example.com"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
              />
            </div>

            <div style={{ marginBottom: '20px' }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '8px' }}>
                Execution Paradigm
              </label>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                <div
                  onClick={() => setMode('standard')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    border: `1px solid ${mode === 'standard' ? 'var(--accent-purple)' : 'var(--border-subtle)'}`,
                    background: mode === 'standard' ? 'rgba(139, 92, 246, 0.15)' : 'rgba(255, 255, 255, 0.02)',
                    cursor: 'pointer',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: '0.85rem' }}>Standard DAG</div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                    Single-turn decompose, tool call, review loop
                  </div>
                </div>

                <div
                  onClick={() => setMode('deep')}
                  style={{
                    padding: '12px',
                    borderRadius: '8px',
                    border: `1px solid ${mode === 'deep' ? 'var(--accent-cyan)' : 'var(--border-subtle)'}`,
                    background: mode === 'deep' ? 'rgba(6, 182, 212, 0.15)' : 'rgba(255, 255, 255, 0.02)',
                    cursor: 'pointer',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--accent-cyan)' }}>Deep Research</div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                    Multi-question recursive web/doc synthesis
                  </div>
                </div>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                className="btn btn-secondary"
                onClick={() => setShowNewModal(false)}
                disabled={creating}
              >
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={handleCreateRun}
                disabled={!goal.trim() || creating}
              >
                {creating ? <Loader2 size={16} className="spin" /> : <Sparkles size={16} />}
                <span>Launch Autonomous Run</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
