import React, { useEffect, useRef, useState } from 'react';
import {
  AgentAskResponse,
  api,
  formatValue,
  MemorySearchHit,
} from '../api';

interface Message {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  facts?: MemorySearchHit[];
  usedFallback?: boolean;
}

/**
 * Chat over the symbolic memory. Each question goes to POST /agent/ask, which
 * retrieves relevant facts first and shows them alongside the answer so the
 * grounding is visible rather than implied.
 */
export function LLMChat({ onFactStored }: { onFactStored?: () => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [topK, setTopK] = useState(5);
  const [pending, setPending] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const nextId = useRef(1);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const submit = async () => {
    const prompt = input.trim();
    if (!prompt || pending) return;

    const userMessage: Message = { id: nextId.current++, role: 'user', text: prompt };
    setMessages((prev) => [...prev, userMessage]);
    setInput('');
    setPending(true);

    try {
      const result: AgentAskResponse = await api.askAgent({ prompt, top_k: topK });
      setMessages((prev) => [
        ...prev,
        {
          id: nextId.current++,
          role: 'assistant',
          text: result.response ?? '',
          facts: result.retrieved_facts,
          usedFallback: result.used_fallback,
        },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: nextId.current++,
          role: 'assistant',
          text: err instanceof Error ? err.message : String(err),
        },
      ]);
    } finally {
      setPending(false);
    }
  };

  const send = (event: React.FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const saveFact = async (fact: MemorySearchHit) => {
    try {
      await api.postFact({
        subject: fact.subject,
        predicate: fact.predicate,
        value: fact.value,
        owner: 'llm-chat',
      });
      onFactStored?.();
    } catch {
      // Already stored, or the value changed since it was retrieved. Either way
      // the fact is in memory; nothing to do here.
    }
  };

  return (
    <div className="panel chat-panel">
      <div className="panel-header">
        <h2>Agent Chat</h2>
        <label className="inline-label">
          facts in context
          <input
            type="number"
            min={1}
            max={50}
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value))}
          />
        </label>
      </div>

      <div className="panel-content chat-scroll">
        {messages.length === 0 && (
          <div className="empty-state">
            <p>
              Ask a question. The agent looks up relevant facts in symbolic memory and
              answers from them.
            </p>
          </div>
        )}

        {messages.map((message) => (
          <div key={message.id} className={`chat-message ${message.role}`}>
            <div className="chat-role">{message.role === 'user' ? 'You' : 'Agent'}</div>
            <div className="chat-body">
              {message.usedFallback ? (
                <div className="alert warning">
                  The LLM is unavailable, so there is no answer — only the facts retrieved
                  above. Use the Memory tab to keep working without it.
                </div>
              ) : (
                <div className="chat-text">{message.text}</div>
              )}

              {message.facts && message.facts.length > 0 && (
                <div className="chat-facts">
                  <span className="dim">
                    {message.facts.length} fact{message.facts.length === 1 ? '' : 's'} used:
                  </span>
                  {message.facts.map((fact) => (
                    <div key={`${fact.subject}/${fact.predicate}`} className="chat-fact">
                      <span className="mono">
                        {fact.subject}.{fact.predicate} = {formatValue(fact.value)}
                      </span>
                      {fact.similarity !== undefined && (
                        <span className="badge badge-action dim">
                          {fact.similarity.toFixed(2)}
                        </span>
                      )}
                      <button
                        className="btn btn-ghost"
                        onClick={() => saveFact(fact)}
                        title="Store this fact in memory"
                      >
                        Save
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}

        {pending && <div className="chat-pending"><span className="spinner" /> Thinking...</div>}
        <div ref={bottomRef} />
      </div>

      <form className="chat-input" onSubmit={send}>
        <textarea
          value={input}
          rows={2}
          placeholder="Ask about anything in memory..."
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <button className="btn btn-primary" type="submit" disabled={pending || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}