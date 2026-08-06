import React from "react";
import { MessageCircle, Plus, Trash2, Cpu, Search, FileSearch, Zap } from "lucide-react";
import { api, request } from "./api";

// ── Types ──

interface ChatSession {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  locale: string;
  report_language: string;
  linked_investigation_ids: string[];
  status: string;
}

interface ChatMessage {
  message_id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  status: string;
  turn_id: string;
}

interface SSEEvent {
  event_id: string;
  event_type: string;
  timestamp: number;
  turn_id: string;
  [key: string]: any;
}

// ── SSE Hook ──

function useChatSSE(sessionId: string | null) {
  const [events, setEvents] = React.useState<SSEEvent[]>([]);
  const [streaming, setStreaming] = React.useState(false);
  const seenRef = React.useRef<Set<string>>(new Set());
  const readerRef = React.useRef<ReadableStreamDefaultReader | null>(null);
  const abortedRef = React.useRef(false);

  React.useEffect(() => {
    if (!sessionId) return;
    abortedRef.current = false;
    seenRef.current.clear();
    setEvents([]);
    // streaming turns on when a turn actually starts (turn_started event or send()),
    // never on mere session open — otherwise a fresh session locks the composer forever.
    setStreaming(false);

    const connect = async () => {
      try {
        const resp = await fetch(`/api/v1/conversation/sessions/${sessionId}/events`);
        if (!resp.body) return;
        const reader = resp.body.getReader();
        readerRef.current = reader;
        const decoder = new TextDecoder();
        let buffer = "";

        while (!abortedRef.current) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const part of parts) {
            const lines = part.split("\n");
            let id = "", eventType = "", data = "";
            for (const line of lines) {
              if (line.startsWith("id: ")) id = line.slice(4);
              else if (line.startsWith("event: ")) eventType = line.slice(7);
              else if (line.startsWith("data: ")) data = line.slice(6);
            }
            if (data && id) {
              if (seenRef.current.has(id)) continue;
              seenRef.current.add(id);
              try {
                const evt: SSEEvent = JSON.parse(data);
                setEvents(prev => [...prev, evt]);
                if (evt.event_type === "turn_started") {
                  setStreaming(true);
                } else if (evt.event_type === "response_completed" || evt.event_type === "error" || evt.event_type === "turn_cancelled") {
                  setStreaming(false);
                }
              } catch { /* skip bad JSON */ }
            }
          }
        }
      } catch {
        setStreaming(false);
      }
    };

    connect();
    return () => { abortedRef.current = true; readerRef.current?.cancel(); };
  }, [sessionId]);

  return { events, streaming, setEvents, setStreaming };
}

// ── Chat API helpers ──

const chatApi = {
  sessions: () => request<{ items: ChatSession[] }>("/api/v1/conversation/sessions"),
  createSession: (locale: string, linked?: string[]) =>
    request<ChatSession>("/api/v1/conversation/sessions", {
      method: "POST",
      body: JSON.stringify({ locale, linked_investigation_ids: linked || [] }),
    }),
  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/conversation/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) =>
    request<{ items: ChatMessage[] }>(`/api/v1/conversation/sessions/${id}/messages`),
  sendMessage: (id: string, content: string, linked?: { investigation_id?: string; claim_id?: string; evidence_id?: string }) =>
    request<{ ok: boolean; message: ChatMessage }>(`/api/v1/conversation/sessions/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, ...linked }),
    }),
  cancel: (id: string) =>
    request<{ ok: boolean }>(`/api/v1/conversation/sessions/${id}/cancel`, { method: "POST" }),
};

// ── Main Chat Page ──

export function ChatPage({ linkedInvestigationId, linkedClaimId, linkedEvidenceId, lang }: {
  linkedInvestigationId?: string;
  linkedClaimId?: string;
  linkedEvidenceId?: string;
  lang: string;
}) {
  const [sessions, setSessions] = React.useState<ChatSession[]>([]);
  const [activeSid, setActiveSid] = React.useState<string | null>(null);
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [input, setInput] = React.useState("");
  const { events: sseEvents, streaming, setEvents: setSSEEvents, setStreaming } = useChatSSE(activeSid);
  const messagesEndRef = React.useRef<HTMLDivElement>(null);

  // Load sessions
  React.useEffect(() => {
    chatApi.sessions().then(d => setSessions(d.items)).catch(() => {});
  }, [activeSid]);

  // Auto-create session if linked from investigation
  React.useEffect(() => {
    if (linkedInvestigationId && !activeSid) {
      chatApi.createSession(lang, [linkedInvestigationId]).then(s => {
        setActiveSid(s.session_id);
        setSessions(prev => [s, ...prev]);
      });
    }
  }, [linkedInvestigationId]);

  // Load messages when session changes
  React.useEffect(() => {
    if (!activeSid) { setMessages([]); setSSEEvents([]); return; }
    chatApi.messages(activeSid).then(d => setMessages(d.items)).catch(() => {});
  }, [activeSid]);

  // Build streaming assistant message from SSE events
  const streamContent = React.useMemo(() => {
    const toolCalls: { tool: string; status: string; message: string; symbol?: string }[] = [];
    let responseText = "";
    let status = "idle";
    let planMessage = "";

    for (const evt of sseEvents) {
      switch (evt.event_type) {
        case "turn_started": status = "planning"; break;
        case "plan_created": status = "planning"; planMessage = evt.message || ""; break;
        case "investigation_started": status = "investigating"; break;
        case "tool_call_started":
          toolCalls.push({ tool: evt.tool || "", status: "running", message: evt.message || "", symbol: evt.symbol });
          status = "executing";
          break;
        case "investigation_progress": status = "investigating"; break;
        case "response_completed":
          responseText = evt.content || "";
          status = "completed";
          break;
        case "error":
          responseText = `❌ ${evt.message || "Error"}`;
          status = "error";
          break;
      }
    }
    return { toolCalls, responseText, status, planMessage };
  }, [sseEvents]);

  React.useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, streamContent]);

  // Reload persisted messages as soon as the turn finishes (success or failure)
  React.useEffect(() => {
    if (!activeSid) return;
    if (streamContent.status === "completed" || streamContent.status === "error") {
      chatApi.messages(activeSid).then(d => setMessages(d.items)).catch(() => {});
    }
  }, [streamContent.status, activeSid]);

  const send = async () => {
    if (!input.trim() || !activeSid) return;
    const content = input;
    setInput("");
    setSSEEvents([]);

    // Optimistic user message
    const userMsg: ChatMessage = {
      message_id: "local-" + Date.now(),
      session_id: activeSid,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      status: "completed",
      turn_id: "",
    };
    setMessages(prev => [...prev, userMsg]);

    setStreaming(true);
    try {
      if (linkedInvestigationId || linkedClaimId || linkedEvidenceId) {
        await chatApi.sendMessage(activeSid, content, {
          investigation_id: linkedInvestigationId,
          claim_id: linkedClaimId,
          evidence_id: linkedEvidenceId,
        });
      } else {
        await chatApi.sendMessage(activeSid, content);
      }
    } catch {
      setStreaming(false);
    }
  };

  const newSession = async () => {
    const s = await chatApi.createSession(lang);
    setSessions(prev => [s, ...prev]);
    setActiveSid(s.session_id);
  };

  return (
    <div className="chat-layout">
      {/* Session List */}
      <aside className="chat-sessions">
        <button className="primary" onClick={newSession} style={{ width: "100%", marginBottom: 12 }}>
          <Plus size={16} /> {lang === "zh-CN" ? "新对话" : "New Chat"}
        </button>
        <div className="session-list">
          {sessions.map(s => (
            <button
              key={s.session_id}
              className={`session-item ${activeSid === s.session_id ? "active" : ""}`}
              onClick={() => setActiveSid(s.session_id)}
            >
              <MessageCircle size={14} />
              <span className="session-title">{s.title}</span>
              <Trash2 size={12} className="session-delete" onClick={(e) => {
                e.stopPropagation();
                chatApi.deleteSession(s.session_id).then(() => {
                  setSessions(prev => prev.filter(x => x.session_id !== s.session_id));
                  if (activeSid === s.session_id) setActiveSid(null);
                });
              }} />
            </button>
          ))}
        </div>
      </aside>

      {/* Chat Area */}
      <main className="chat-main">
        {!activeSid ? (
          <div className="chat-empty">
            <MessageCircle size={48} />
            <h2>{lang === "zh-CN" ? "选择或创建对话" : "Select or create a chat"}</h2>
            <p>{lang === "zh-CN" ? "左侧创建新对话开始研究" : "Create a new chat from the sidebar to start researching"}</p>
          </div>
        ) : (
          <>
            <div className="chat-messages">
              {messages.map(m => (
                <div key={m.message_id} className={`chat-bubble ${m.role}`}>
                  <div className="bubble-content">
                    {m.role === "user" ? m.content : renderMarkdownContent(m.content)}
                  </div>
                </div>
              ))}

              {/* Streaming content */}
              {streaming && (
                <div className="chat-bubble assistant streaming">
                  <div className="bubble-content">
                    {/* Tool Calls */}
                    {streamContent.toolCalls.map((tc, i) => (
                      <div key={i} className="tool-call-card">
                        <div className="tool-call-header">
                          {tc.tool === "investigate_stock" ? <Search size={14} /> : <Cpu size={14} />}
                          <span>{tc.tool}</span>
                          <span className={`badge provider ${tc.status}`}>{tc.status}</span>
                        </div>
                        <p>{tc.message}</p>
                        {tc.symbol && <code>{tc.symbol}</code>}
                      </div>
                    ))}

                    {/* Thinking indicator — shown for all pre-response states */}
                    {(streamContent.status === "idle" || streamContent.status === "planning" || streamContent.status === "executing") && (
                      <div className="thinking-banner">
                        <Cpu size={16} className="spin" />
                        <span>{streamContent.planMessage || (lang === "zh-CN" ? "思考中..." : "Thinking...")}</span>
                      </div>
                    )}

                    {/* Investigation status */}
                    {streamContent.status === "investigating" && (
                      <div className="investigation-running-banner">
                        <Zap size={16} className="spin" />
                        <span>{lang === "zh-CN" ? "正在调查中..." : "Investigating..."}</span>
                      </div>
                    )}

                    {/* Response text */}
                    {streamContent.responseText && (
                      <div className="response-text">{renderMarkdownContent(streamContent.responseText)}</div>
                    )}
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>

            {/* Input */}
            <div className="chat-input-bar">
              <input
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
                placeholder={lang === "zh-CN" ? "输入消息... (Enter 发送)" : "Type a message... (Enter to send)"}
                disabled={streaming}
              />
              <button onClick={send} disabled={streaming || !input.trim()}>
                {lang === "zh-CN" ? "发送" : "Send"}
              </button>
            </div>
          </>
        )}
      </main>

      {/* Context Panel */}
      {activeSid && linkedInvestigationId && (
        <aside className="chat-context">
          <div className="context-card">
            <FileSearch size={14} />
            <span>{lang === "zh-CN" ? "关联调查" : "Linked Investigation"}</span>
            <code>{linkedInvestigationId.slice(0, 12)}...</code>
          </div>
        </aside>
      )}
    </div>
  );
}

// Simple markdown-ish rendering
function renderMarkdownContent(content: string): React.ReactNode {
  const lines = content.split("\n");
  return lines.map((line, i) => {
    if (line.startsWith("## ")) return <h3 key={i}>{line.slice(3)}</h3>;
    if (line.startsWith("### ")) return <h4 key={i}>{line.slice(4)}</h4>;
    if (line.startsWith("- ")) return <li key={i}>{line.slice(2)}</li>;
    if (line.startsWith("**") && line.includes("**")) {
      const parts = line.split("**");
      return <p key={i}>{parts.map((p, j) => j % 2 === 1 ? <strong key={j}>{p}</strong> : p)}</p>;
    }
    if (line.trim() === "") return <br key={i} />;
    return <p key={i}>{line}</p>;
  });
}
