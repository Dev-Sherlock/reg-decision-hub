import { useCallback, useEffect, useMemo, useState } from 'react';
import { DecisionTree } from './components/DecisionTree';
import { ExplanationModal } from './components/ExplanationModal';
import { LLMChat } from './components/LLMChat';
import { MemoryPanel } from './components/MemoryPanel';
import { api, DecideResponse, DomainSummary, HealthStatus, Node } from './api';

type Tab = 'memory' | 'chat' | 'tree';

const TABS: { id: Tab; label: string }[] = [
  { id: 'memory', label: 'Memory' },
  { id: 'chat', label: 'LLM Chat' },
  { id: 'tree', label: 'Decision Tree' },
];

interface InputRow {
  id: number;
  key: string;
  value: string;
}

function App() {
  const [tab, setTab] = useState<Tab>('memory');
  const [health, setHealth] = useState<HealthStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [memoryVersion, setMemoryVersion] = useState(0);
  // Bumped after a governed override so the tree panel re-reads the node it changed.
  const [treeVersion, setTreeVersion] = useState(0);

  const refreshMemory = useCallback(() => setMemoryVersion((v) => v + 1), []);
  const refreshTree = useCallback(() => setTreeVersion((v) => v + 1), []);

  useEffect(() => {
    const load = async () => {
      try {
        const data = await api.health();
        setHealth(data.services);
      } catch (err) {
        console.error('Health check failed:', err);
      }
    };
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, []);

  const isTreeTab = tab === 'tree';

  return (
    <div className="app">
      <header className="header">
        <h1>Universal Symbolic Memory Hub</h1>
        <nav className="tabs">
          {TABS.map(({ id, label }) => (
            <button
              key={id}
              className={`tab ${tab === id ? 'active' : ''}`}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="status">
          {health.map((service) => (
            <span key={service.service} className={`status-badge ${service.status}`}>
              {service.service}
              {service.latency_ms ? ` ${service.latency_ms.toFixed(0)}ms` : ''}
            </span>
          ))}
        </div>
      </header>

      <main className={`main ${isTreeTab ? 'two-column' : ''}`}>
        {tab === 'memory' && <MemoryPanel key={memoryVersion} />}
        {tab === 'chat' && <LLMChat key={memoryVersion} onFactStored={refreshMemory} />}
        {tab === 'tree' && (
          <>
            <div className="panel">
              <div className="panel-header">
                <h2>Decision Tree</h2>
              </div>
              <div className="panel-content tree-panel">
                <DecisionTabInner
                  onError={setError}
                  treeVersion={treeVersion}
                />
              </div>
            </div>

            <div className="panel">
              <div className="panel-header">
                <h2>Decision Result</h2>
              </div>
              <div className="panel-content">
                <DecisionResultPanel
                  onError={setError}
                  onTreeVersionChange={refreshTree}
                />
              </div>
            </div>
          </>
        )}
      </main>

      {error && <div className="alert error floating">{error}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decision tree tab — split into two components for two-column layout
// ---------------------------------------------------------------------------

function DecisionTabInner({
  onError,
  treeVersion,
}: {
  onError: (message: string | null) => void;
  treeVersion: number;
}) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [domains, setDomains] = useState<DomainSummary[]>([]);
  const [domainId, setDomainId] = useState<string>('');
  const [rows, setRows] = useState<InputRow[]>([]);
  const [nextRowId, setNextRowId] = useState(1);

  // The dropdown is view state only. /decide is never told which domain to use —
  // it routes on the input keys and reports back what it chose. Showing one tree at
  // a time is about being able to read it, not about steering the decision.
  const selectedDomain = useMemo(
    () => domains.find((d) => d.id === domainId),
    [domains, domainId],
  );

  const loadTree = useCallback(async () => {
    try {
      const [tree, registry] = await Promise.all([api.getTree(), api.getDomains()]);
      setNodes(tree.nodes);
      // /trees is the authority on which domains exist, /tree on their nodes; the
      // two are fetched together so the picker and the forest cannot disagree.
      setDomains(registry.filter((d) => d.active));
      setDomainId((current) =>
        current && registry.some((d) => d.id === current && d.active) ? current : registry[0]?.id ?? '',
      );
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Failed to load decision tree');
      setNodes([]);
    }
  }, [onError]);

  useEffect(() => {
    loadTree();
  }, [loadTree, treeVersion]);

  const visibleNodes = useMemo(
    () => (domainId ? nodes.filter((node) => node.domain === domainId) : nodes),
    [nodes, domainId],
  );

  // Seed input rows from the domain's declared keys.
  //
  // The previous version scraped the tree's condition strings with a regex, which
  // turned `region == north` into an input key called `north` — a key no domain
  // declares, so the request came back 400. The registry states the keys outright.
  useEffect(() => {
    const keys = selectedDomain?.input_keys ?? [];
    setRows((prev) => {
      const previous = new Map(prev.map((row) => [row.key.trim(), row.value]));
      const seeded = keys.map((key, index) => ({
        id: index + 1,
        key,
        value: previous.get(key) ?? '',
      }));
      setNextRowId(seeded.length + 1);
      return seeded;
    });
  }, [selectedDomain]);

  const [selectedNode, setSelectedNode] = useState<Node | null>(null);
  const [loading, setLoading] = useState(false);

  const inputs = useMemo(() => {
    const collected: Record<string, number | string | boolean> = {};
    for (const row of rows) {
      const key = row.key.trim();
      const raw = row.value.trim();
      if (!key || raw === '') continue;
      const numeric = Number(raw);
      collected[key] = Number.isNaN(numeric) ? raw : numeric;
    }
    return collected;
  }, [rows]);

  const runDecision = useCallback(async () => {
    setLoading(true);
    onError(null);
    try {
      const result = await api.decide({ inputs });
      // The domain the inputs were seeded from travels with the result so the panel
      // can say whether routing agreed with the selection. It is not sent to the API.
      window.dispatchEvent(
        new CustomEvent('decision-result', { detail: { result, seededFor: domainId } }),
      );
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Decision failed');
    } finally {
      setLoading(false);
    }
  }, [inputs, domainId, onError]);

  const addRow = () => {
    setRows((prev) => [...prev, { id: nextRowId, key: '', value: '' }]);
    setNextRowId((id) => id + 1);
  };

  return (
    <>
      <div className="panel-header panel-header-split">
        <h2>Domain</h2>
      </div>
      <div className="panel-content">
        <div className="domain-picker">
          <select
            value={domainId}
            onChange={(e) => {
              setDomainId(e.target.value);
              setSelectedNode(null);
            }}
            aria-label="Decision domain"
          >
            {domains.map((domain) => (
              <option key={domain.id} value={domain.id}>
                {domain.label} ({domain.node_count} nodes)
              </option>
            ))}
          </select>
          <p className="hint">
            Inputs below are seeded from this domain&rsquo;s declared keys. The decision itself
            is routed by the API on the keys you send, not by this selection.
          </p>
        </div>
      </div>

      <DecisionTree
        nodes={visibleNodes}
        rootId={selectedDomain?.root ?? ''}
        selectedNodeId={selectedNode?.id}
        onNodeClick={setSelectedNode}
        highlightedPath={[]}
      />

      <div className="panel-header panel-header-split">
        <h2>Inputs</h2>
        <button className="btn btn-ghost" onClick={addRow}>
          + Add field
        </button>
      </div>
      <div className="panel-content">
        <div className="input-builder">
          {rows.map((row) => (
            <div key={row.id} className="input-row">
              <input
                value={row.key}
                placeholder="name"
                onChange={(e) =>
                  setRows((prev) =>
                    prev.map((r) => (r.id === row.id ? { ...r, key: e.target.value } : r)),
                  )
                }
              />
              <input
                value={row.value}
                placeholder="value"
                onChange={(e) =>
                  setRows((prev) =>
                    prev.map((r) => (r.id === row.id ? { ...r, value: e.target.value } : r)),
                  )
                }
              />
              <button
                className="btn btn-ghost danger"
                onClick={() => setRows((prev) => prev.filter((r) => r.id !== row.id))}
                title="Remove this field"
              >
                &times;
              </button>
            </div>
          ))}
          <div className="btn-group">
            <button
              className="btn btn-primary"
              onClick={runDecision}
              disabled={loading || Object.keys(inputs).length === 0}
            >
              {loading ? (
                <>
                  <span className="spinner" /> Evaluating...
                </>
              ) : (
                'Evaluate Decision'
              )}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

function DecisionResultPanel({
  onError,
  onTreeVersionChange,
}: {
  onError: (message: string | null) => void;
  onTreeVersionChange: () => void;
}) {
  const [result, setResult] = useState<DecideResponse | null>(null);
  const [seededFor, setSeededFor] = useState<string>('');
  const [selectedNode, _setSelectedNode] = useState<Node | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [overrideMode, setOverrideMode] = useState(false);

  useEffect(() => {
    const handler = (event: CustomEvent<{ result: DecideResponse; seededFor: string }>) => {
      setResult(event.detail.result);
      setSeededFor(event.detail.seededFor);
      setModalOpen(true);
      setOverrideMode(false);
    };
    window.addEventListener('decision-result', handler as EventListener);
    return () => window.removeEventListener('decision-result', handler as EventListener);
  }, []);

  const handleOverride = async (action: string, owner: string) => {
    if (!selectedNode) return;
    try {
      await api.overrideNode(selectedNode.id, action, owner);
      onTreeVersionChange();
      // Re-run decision by re-dispatching
      if (result) {
        window.dispatchEvent(
          new CustomEvent('decision-result', { detail: { result, seededFor } }),
        );
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : 'Override failed');
    }
  };

  // Routing on the input keys can legitimately disagree with what is selected here:
  // the user may have edited the keys, or a domain may simply claim more of them.
  // Worth saying out loud rather than showing a result from an unnamed tree.
  const routedElsewhere = Boolean(result && seededFor && result.domain !== seededFor);

  return (
    <>
      {result ? (
        <div className="result-panel">
          <div className="result-header">
            <div className="result-title">{result.action}</div>
            <div className="result-meta">
              <span className="badge badge-action">{result.action}</span>
              <span className="badge badge-owner">{result.owner}</span>
              <span className="badge">{result.leaf_node}</span>
              <span className="badge">{result.domain}</span>
            </div>
          </div>

          {routedElsewhere && (
            <div className="alert warning">
              The API routed these inputs to <strong>{result.domain}</strong>, not{' '}
              {seededFor}. Check the input keys.
            </div>
          )}

          <div className="proof-trace">
            {result.proof_trace.map((step, index) => (
              <div key={step.node_id} className={`proof-step ${index === 0 ? 'root' : ''}`}>
                <div className="proof-step-header">
                  <span className="proof-step-node">{step.node_id}</span>
                  <span className="proof-step-owner">{step.owner}</span>
                </div>
                <div className="proof-step-condition">
                  IF {step.condition} THEN {step.action}
                </div>
              </div>
            ))}
          </div>

          <div className="explanation-box">
            <strong>Status:</strong> Decision evaluated successfully.
          </div>

          <div className="btn-group">
            <button
              className="btn btn-secondary"
              onClick={() => {
                setOverrideMode(true);
                setModalOpen(true);
              }}
            >
              Override Action
            </button>
            <button
              className="btn btn-primary"
              onClick={() => {
                setOverrideMode(false);
                setModalOpen(true);
              }}
            >
              View Proof Trace
            </button>
          </div>
        </div>
      ) : (
        <div className="empty-state">
          <p>Set some inputs and click "Evaluate Decision" in the left panel.</p>
        </div>
      )}

      <ExplanationModal
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        proofTrace={result?.proof_trace || []}
        onOverride={handleOverride}
        overrideMode={overrideMode}
        selectedNodeId={selectedNode?.id}
      />
    </>
  );
}

export default App;