import React, { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle,
  Clock,
  Compass,
  CornerDownLeft,
  ExternalLink,
  FileText,
  Loader2,
  MessageSquare,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldAlert,
  Sparkles,
  User as UserIcon,
} from 'lucide-react';
import { api } from '../services/api';
import { ChatMessage, Conversation, WorkflowRun } from '../types/api';

interface ChatViewProps {
  onOpenWorkflow: (runId: string) => void;
  onGoToApprovals: () => void;
}

export const ChatView: React.FC<ChatViewProps> = ({ onOpenWorkflow, onGoToApprovals }) => {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConv, setActiveConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [newTitle, setNewTitle] = useState('');
  const [inputPrompt, setInputPrompt] = useState('');
  const [deepResearch, setDeepResearch] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [activeRunStatus, setActiveRunStatus] = useState<{ id: string; status: string; goal: string } | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const samplePrompts = [
    { title: 'Refund Window Policy', text: 'What is the refund window for digital goods?' },
    { title: 'Incident Response', text: 'Check the SEV2 incident policy, open a ticket and email ops@example.com' },
    { title: 'Fee Calculation', text: 'Calculate 1200 * 0.18 for the refund fee' },
  ];

  const loadConversations = async () => {
    try {
      const res = await api.listConversations();
      setConversations(res.items);
      if (res.items.length > 0 && !activeConv) {
        setActiveConv(res.items[0]);
      }
    } catch (err) {
      console.error('Failed to load conversations', err);
    }
  };

  const loadMessages = async (convId: string) => {
    setLoadingHistory(true);
    try {
      const res = await api.listMessages(convId);
      setMessages(res.items);
    } catch (err) {
      console.error('Failed to load messages', err);
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    loadConversations();
  }, []);

  useEffect(() => {
    if (activeConv) {
      loadMessages(activeConv.id);
    } else {
      setMessages([]);
    }
  }, [activeConv]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, activeRunStatus]);

  const handleCreateConversation = async () => {
    if (!newTitle.trim()) return;
    try {
      const created = await api.createConversation(newTitle.trim());
      setConversations([created, ...conversations]);
      setActiveConv(created);
      setNewTitle('');
    } catch (err) {
      console.error('Failed to create conversation', err);
    }
  };

  const handleSendMessage = async (textToSend?: string) => {
    const text = textToSend || inputPrompt;
    if (!text.trim() || isSubmitting) return;

    let conv = activeConv;
    if (!conv) {
      // Auto-create thread
      try {
        conv = await api.createConversation(text.slice(0, 30) + '...');
        setConversations([conv, ...conversations]);
        setActiveConv(conv);
      } catch (err) {
        console.error(err);
        return;
      }
    }

    setInputPrompt('');
    setIsSubmitting(true);

    // Optimistically render user prompt immediately so the stream never appears empty
    const optimisticMessage: ChatMessage = {
      id: 'temp-' + Date.now(),
      role: 'user',
      content: text,
      token_count: 0,
      run_id: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimisticMessage]);

    try {
      // Create Workflow run attached to conversation
      const run = await api.createWorkflow(
        text,
        deepResearch ? 'deep' : 'standard',
        conv.id
      );

      setActiveRunStatus({ id: run.id, status: run.status, goal: text });

      // Poll until finished or paused
      const pollInterval = setInterval(async () => {
        try {
          const updatedRun = await api.getWorkflow(run.id);
          setActiveRunStatus({ id: updatedRun.id, status: updatedRun.status, goal: text });

          if (
            updatedRun.status === 'COMPLETED' ||
            updatedRun.status === 'FAILED' ||
            updatedRun.status === 'CANCELLED' ||
            updatedRun.status === 'AWAITING_APPROVAL'
          ) {
            clearInterval(pollInterval);
            setIsSubmitting(false);
            if (conv) {
              await loadMessages(conv.id);
            }
          }
        } catch {
          clearInterval(pollInterval);
          setIsSubmitting(false);
        }
      }, 1000);
    } catch (err: unknown) {
      alert((err as Error).message || 'Failed to trigger agent workflow');
      setIsSubmitting(false);
      setActiveRunStatus(null);
    }
  };

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', height: 'calc(100vh - 60px)', overflow: 'hidden' }}>
      
      {/* Left Sidebar: Threads */}
      <div
        style={{
          borderRight: '1px solid var(--border-subtle)',
          background: 'rgba(11, 16, 29, 0.4)',
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
        }}
      >
        <div style={{ padding: '16px', borderBottom: '1px solid var(--border-subtle)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)' }}>
              Conversations
            </span>
            <button
              onClick={loadConversations}
              className="btn btn-secondary btn-icon"
              style={{ width: '28px', height: '28px' }}
              title="Refresh"
            >
              <RefreshCw size={13} />
            </button>
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <input
              type="text"
              className="input"
              style={{ padding: '6px 10px', fontSize: '0.82rem' }}
              placeholder="New thread title..."
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreateConversation()}
            />
            <button
              onClick={handleCreateConversation}
              className="btn btn-primary"
              style={{ padding: '6px 12px' }}
              title="Create"
            >
              <Plus size={16} />
            </button>
          </div>
        </div>

        {/* Conversation List */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px' }}>
          {conversations.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '30px 10px', color: 'var(--text-muted)', fontSize: '0.82rem' }}>
              No conversations yet. Create one or start asking a question!
            </div>
          ) : (
            conversations.map((c) => {
              const isSelected = activeConv?.id === c.id;
              return (
                <div
                  key={c.id}
                  onClick={() => setActiveConv(c)}
                  style={{
                    padding: '10px 12px',
                    borderRadius: '9px',
                    marginBottom: '6px',
                    cursor: 'pointer',
                    background: isSelected ? 'rgba(139, 92, 246, 0.15)' : 'transparent',
                    border: `1px solid ${isSelected ? 'rgba(139, 92, 246, 0.4)' : 'transparent'}`,
                    transition: 'all 0.15s ease',
                  }}
                  className={isSelected ? '' : 'glass-card-interactive'}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <MessageSquare size={15} color={isSelected ? 'var(--accent-purple)' : 'var(--text-muted)'} />
                    <span
                      style={{
                        fontSize: '0.85rem',
                        fontWeight: isSelected ? 600 : 400,
                        color: isSelected ? '#ffffff' : 'var(--text-secondary)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {c.title}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', marginTop: '4px', paddingLeft: '23px' }}>
                    {new Date(c.created_at).toLocaleDateString()}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Main Chat Stream */}
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: 'transparent' }}>
        
        {/* Top Chat Bar: Deep Research Toggle */}
        <div
          style={{
            padding: '12px 24px',
            borderBottom: '1px solid var(--border-subtle)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            background: 'rgba(15, 23, 42, 0.3)',
          }}
        >
          <div>
            <h2 style={{ fontSize: '1.05rem', fontWeight: 700, color: '#fff' }}>
              {activeConv ? activeConv.title : 'Interactive Multi-Agent Session'}
            </h2>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Planner, Retrieval (RAG), Execution, and Review agents collaborating autonomously
            </p>
          </div>

          {/* Deep Research Mode Toggle */}
          <div
            onClick={() => setDeepResearch(!deepResearch)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              cursor: 'pointer',
              padding: '6px 14px',
              borderRadius: '999px',
              background: deepResearch ? 'rgba(6, 182, 212, 0.15)' : 'rgba(255, 255, 255, 0.05)',
              border: `1px solid ${deepResearch ? 'rgba(6, 182, 212, 0.4)' : 'var(--border-subtle)'}`,
              transition: 'all 0.2s ease',
            }}
          >
            <Compass size={16} color={deepResearch ? 'var(--accent-cyan)' : 'var(--text-muted)'} />
            <div style={{ textAlign: 'left' }}>
              <div style={{ fontSize: '0.78rem', fontWeight: 600, color: deepResearch ? 'var(--accent-cyan)' : 'var(--text-secondary)' }}>
                Deep Research Mode
              </div>
            </div>
            <div
              style={{
                width: '32px',
                height: '18px',
                borderRadius: '999px',
                background: deepResearch ? 'var(--accent-cyan)' : 'rgba(255, 255, 255, 0.2)',
                position: 'relative',
                transition: 'background 0.2s ease',
              }}
            >
              <div
                style={{
                  width: '14px',
                  height: '14px',
                  borderRadius: '50%',
                  background: '#fff',
                  position: 'absolute',
                  top: '2px',
                  left: deepResearch ? '16px' : '2px',
                  transition: 'left 0.2s ease',
                }}
              />
            </div>
          </div>
        </div>

        {/* Messages Scroll Area */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '24px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
          
          {messages.length === 0 && !isSubmitting && (
            <div style={{ maxWidth: '680px', margin: '40px auto', textAlign: 'center' }}>
              <div
                style={{
                  width: '64px',
                  height: '64px',
                  borderRadius: '20px',
                  background: 'var(--gradient-brand)',
                  margin: '0 auto 20px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: 'var(--shadow-glow)',
                }}
              >
                <Sparkles size={32} color="#fff" />
              </div>
              <h3 style={{ fontSize: '1.4rem', fontWeight: 700, marginBottom: '8px' }}>
                How can the autonomous agents assist you?
              </h3>
              <p style={{ fontSize: '0.88rem', color: 'var(--text-secondary)', marginBottom: '28px' }}>
                Ask policy questions grounded in your PostgreSQL Knowledge Base, trigger complex multi-step workflows with human-in-the-loop governance, or synthesize multi-turn research.
              </p>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px', textAlign: 'left' }}>
                {samplePrompts.map((sp, idx) => (
                  <div
                    key={idx}
                    onClick={() => handleSendMessage(sp.text)}
                    className="glass-card glass-card-interactive"
                    style={{ padding: '14px', cursor: 'pointer' }}
                  >
                    <div style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--accent-purple)', marginBottom: '4px' }}>
                      {sp.title}
                    </div>
                    <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                      {sp.text}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Render Messages */}
          {messages.map((m) => {
            const isUser = m.role === 'user';
            return (
              <div
                key={m.id}
                style={{
                  display: 'flex',
                  gap: '14px',
                  alignSelf: isUser ? 'flex-end' : 'flex-start',
                  maxWidth: isUser ? '80%' : '88%',
                }}
              >
                {!isUser && (
                  <div
                    style={{
                      width: '36px',
                      height: '36px',
                      borderRadius: '10px',
                      background: 'rgba(139, 92, 246, 0.2)',
                      border: '1px solid rgba(139, 92, 246, 0.4)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <Bot size={20} color="var(--accent-purple)" />
                  </div>
                )}

                <div
                  className="glass-card"
                  style={{
                    padding: '16px 20px',
                    background: isUser ? 'rgba(99, 102, 241, 0.18)' : 'rgba(15, 23, 42, 0.75)',
                    border: isUser ? '1px solid rgba(99, 102, 241, 0.35)' : '1px solid var(--border-subtle)',
                    borderRadius: '14px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px', gap: '16px' }}>
                    <span style={{ fontSize: '0.75rem', fontWeight: 600, color: isUser ? '#818cf8' : 'var(--accent-purple)' }}>
                      {isUser ? 'Operator / Requester' : 'AetherFlow Orchestrator'}
                    </span>
                    <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
                      {new Date(m.created_at).toLocaleTimeString()}
                    </span>
                  </div>

                  <div style={{ fontSize: '0.92rem', lineHeight: 1.6, whiteSpace: 'pre-wrap', color: 'var(--text-primary)' }}>
                    {m.content}
                  </div>

                  {/* Attached Run Inspector Link */}
                  {m.run_id && (
                    <div style={{ marginTop: '14px', paddingTop: '10px', borderTop: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span className="badge badge-completed" style={{ fontSize: '0.7rem' }}>
                        <CheckCircle size={12} />
                        Run Approved & Executed
                      </span>

                      <button
                        onClick={() => onOpenWorkflow(m.run_id!)}
                        className="btn btn-secondary"
                        style={{ padding: '4px 10px', fontSize: '0.75rem' }}
                      >
                        <ExternalLink size={12} />
                        View DAG & Metrics
                      </button>
                    </div>
                  )}
                </div>

                {isUser && (
                  <div
                    style={{
                      width: '36px',
                      height: '36px',
                      borderRadius: '10px',
                      background: 'rgba(99, 102, 241, 0.2)',
                      border: '1px solid rgba(99, 102, 241, 0.4)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <UserIcon size={18} color="#818cf8" />
                  </div>
                )}
              </div>
            );
          })}

          {/* Active Run Live Orchestration Status Card */}
          {activeRunStatus && isSubmitting && (
            <div
              className="glass-card"
              style={{
                padding: '16px 20px',
                border: '1px solid var(--border-accent)',
                boxShadow: 'var(--shadow-glow)',
                maxWidth: '85%',
                display: 'flex',
                alignItems: 'center',
                gap: '16px',
              }}
            >
              <div
                style={{
                  width: '40px',
                  height: '40px',
                  borderRadius: '10px',
                  background: 'rgba(139, 92, 246, 0.2)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <Loader2 size={22} className="spin" color="var(--accent-purple)" />
              </div>

              <div style={{ flex: 1 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 700 }}>
                    Executing Workflow Run {activeRunStatus.id.slice(0, 8)}...
                  </span>
                  <span className={`badge badge-${activeRunStatus.status.toLowerCase()}`}>
                    {activeRunStatus.status}
                  </span>
                </div>
                <p style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                  {activeRunStatus.status === 'PLANNING' && 'Planner agent is decomposing goal into dependency graph...'}
                  {activeRunStatus.status === 'EXECUTING' && 'Running tools and retrieving semantic chunks...'}
                  {activeRunStatus.status === 'REVIEWING' && 'Review agent is evaluating answer accuracy and compliance...'}
                  {activeRunStatus.status === 'AWAITING_APPROVAL' && 'Paused: High-risk tool gated by Human Approval policy!'}
                </p>
              </div>

              {activeRunStatus.status === 'AWAITING_APPROVAL' ? (
                <button
                  onClick={onGoToApprovals}
                  className="btn btn-primary"
                  style={{ padding: '6px 14px', fontSize: '0.8rem', background: '#f59e0b', color: '#000' }}
                >
                  <ShieldAlert size={14} />
                  Sign-off Required
                </button>
              ) : (
                <button
                  onClick={() => onOpenWorkflow(activeRunStatus.id)}
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', fontSize: '0.78rem' }}
                >
                  Inspect Live
                </button>
              )}
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Bottom Input Area */}
        <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border-subtle)', background: 'rgba(11, 16, 29, 0.7)' }}>
          <div style={{ maxWidth: '960px', margin: '0 auto', display: 'flex', gap: '10px', alignItems: 'center' }}>
            {/* Deep Research Toggle Button next to input */}
            <button
              type="button"
              onClick={() => setDeepResearch(!deepResearch)}
              className="btn"
              style={{
                padding: '12px 14px',
                borderRadius: '12px',
                background: deepResearch ? 'rgba(6, 182, 212, 0.2)' : 'rgba(255, 255, 255, 0.04)',
                border: `1px solid ${deepResearch ? 'var(--accent-cyan)' : 'var(--border-subtle)'}`,
                color: deepResearch ? 'var(--accent-cyan)' : 'var(--text-muted)',
                fontWeight: deepResearch ? 700 : 500,
                fontSize: '0.85rem',
                gap: '8px',
                boxShadow: deepResearch ? '0 0 16px rgba(6, 182, 212, 0.35)' : 'none',
                transition: 'all 0.2s ease',
                flexShrink: 0,
              }}
              title="Toggle Deep Research Mode"
            >
              <Compass size={17} />
              <span>Deep Research {deepResearch ? 'ON' : 'OFF'}</span>
            </button>

            <input
              type="text"
              className="input"
              style={{ padding: '12px 18px', fontSize: '0.95rem', borderRadius: '12px' }}
              placeholder={
                deepResearch
                  ? '⚡ [Deep Research Mode Active] Enter topic for multi-question synthesis report...'
                  : 'Ask question or give autonomous instructions (e.g. check refund policy, trigger SEV2 notification)...'
              }
              value={inputPrompt}
              onChange={(e) => setInputPrompt(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSendMessage()}
              disabled={isSubmitting}
            />

            <button
              onClick={() => handleSendMessage()}
              disabled={!inputPrompt.trim() || isSubmitting}
              className="btn btn-primary"
              style={{
                padding: '12px 20px',
                borderRadius: '12px',
                opacity: !inputPrompt.trim() || isSubmitting ? 0.5 : 1,
              }}
            >
              {isSubmitting ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
              <span>Send</span>
            </button>
          </div>
          <div style={{ display: 'flex', justifyContent: 'center', marginTop: '6px' }}>
            <span style={{ fontSize: '0.72rem', color: 'var(--text-dim)' }}>
              Powered by Groq LLM · Vector Knowledge Base · Human-in-the-Loop Safeguards
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};
