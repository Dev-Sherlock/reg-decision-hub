// API client for the Universal Symbolic Memory Hub.

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// Decision trees (one per domain)
// ---------------------------------------------------------------------------

export interface Node {
  id: string;
  parent: string;
  condition: string;
  action: string;
  owner: string;
  version: number;
  /** Which domain's tree this node belongs to. */
  domain: string;
  is_leaf?: boolean;
  children?: Node[];
}

export interface DomainSummary {
  id: string;
  label: string;
  root: string;
  node_count: number;
  input_keys: string[];
  active: boolean;
  source: string;
}

export interface TreeResponse {
  nodes: Node[];
  root: string;
  domains: DomainSummary[];
}

export interface ProofStep {
  node_id: string;
  condition: string;
  action: string;
  owner: string;
}

export interface DecideRequest {
  inputs: Record<string, number | string | boolean>;
}

export interface DecideResponse {
  /** The domain that answered. May differ from what the UI has selected. */
  domain: string;
  leaf_node: string;
  action: string;
  owner: string;
  proof_trace: ProofStep[];
}

export interface OverrideResponse {
  success: boolean;
  node_id: string;
  old_action: string;
  new_action: string;
  owner: string;
  timestamp: string;
  diff_hash: string;
}

// ---------------------------------------------------------------------------
// Symbolic memory
// ---------------------------------------------------------------------------

export type FactValue = string | number | boolean | null | FactValue[] | { [k: string]: FactValue };

export interface Fact {
  subject: string;
  predicate: string;
  value: FactValue;
  version: number;
  created_at?: string;
  updated_at?: string;
}

export interface FactRequest {
  subject: string;
  predicate: string;
  value: FactValue;
  owner?: string;
  reindex?: boolean;
}

export interface FactListResponse {
  facts: Fact[];
  count: number;
}

export interface Rule {
  name: string;
  head: string;
  body: string;
  created_at?: string;
  updated_at?: string;
}

export interface RuleRequest {
  name: string;
  head: string;
  body: string;
}

export interface DerivedFact {
  subject: string;
  predicate: string;
  value: FactValue;
}

export interface MemorySearchHit extends Fact {
  similarity?: number;
}

export interface MemorySearchResponse {
  query: string;
  hits: MemorySearchHit[];
  count: number;
  semantic: boolean;
}

export interface QueryResponse {
  query: string;
  bindings: Record<string, unknown>[];
  count: number;
}

export interface ProvenanceEntry {
  timestamp: string;
  change_type: string;
  old_value: FactValue;
  new_value: FactValue;
}

export interface AuditEntry {
  id: number;
  entity_type: string;
  subject: string | null;
  predicate: string | null;
  rule_name: string | null;
  operation: string;
  old_value: FactValue;
  new_value: FactValue;
  owner: string | null;
  timestamp: string;
  diff_hash: string | null;
}

// ---------------------------------------------------------------------------
// LLM / agent
// ---------------------------------------------------------------------------

export interface AgentAskRequest {
  prompt: string;
  top_k?: number;
}

export interface AgentAskResponse {
  response: string | null;
  retrieved_facts: MemorySearchHit[];
  used_fallback: boolean;
  error?: string;
}

export interface LLMAskRequest {
  prompt: string;
  context_facts?: Fact[];
}

export interface LLMAskResponse {
  response: string | null;
  context_facts: MemorySearchHit[];
  error?: string;
}

export interface LLMHealthResponse {
  reachable: boolean;
  status?: string;
  model?: string;
  reason?: string;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthStatus {
  service: string;
  status: string;
  latency_ms?: number;
  details?: Record<string, unknown>;
}

export interface HealthResponse {
  overall: string;
  services: HealthStatus[];
  timestamp: string;
}

/** Render a fact value compactly for table cells. */
export function formatValue(value: FactValue): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

export const api = {
  // Facts
  postFact: (data: FactRequest) => request<Fact>('/memory/fact', {
    method: 'POST',
    body: JSON.stringify(data),
  }),

  getFacts: (subject?: string, predicate?: string, limit = 100) => {
    const params = new URLSearchParams();
    if (subject) params.set('subject', subject);
    if (predicate) params.set('predicate', predicate);
    params.set('limit', String(limit));
    const query = params.toString();
    return request<FactListResponse>(`/memory/fact${query ? `?${query}` : ''}`);
  },

  getFact: (subject: string, predicate: string) =>
    request<Fact>(`/memory/fact/${encodeURIComponent(subject)}/${encodeURIComponent(predicate)}`),

  deleteFact: (subject: string, predicate: string) =>
    request<{ deleted: boolean; value: FactValue }>(
      `/memory/fact/${encodeURIComponent(subject)}/${encodeURIComponent(predicate)}`,
      { method: 'DELETE' },
    ),

  overrideFact: (subject: string, predicate: string, value: FactValue, owner: string) =>
    request<Fact>(
      `/memory/fact/${encodeURIComponent(subject)}/${encodeURIComponent(predicate)}/override`,
      { method: 'POST', body: JSON.stringify({ value, owner }) },
    ),

  getProvenance: (subject: string, predicate: string) =>
    request<ProvenanceEntry[]>(
      `/memory/fact/${encodeURIComponent(subject)}/${encodeURIComponent(predicate)}/provenance`,
    ),

  // Rules
  postRule: (data: RuleRequest) => request<Rule>('/memory/rule', {
    method: 'POST',
    body: JSON.stringify(data),
  }),

  getRules: () => request<{ rules: Rule[]; count: number }>('/memory/rule'),

  getRule: (name: string) => request<Rule>(`/memory/rule/${encodeURIComponent(name)}`),

  deleteRule: (name: string) =>
    request<{ deleted: boolean }>(`/memory/rule/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  runRule: (name: string, store = false) =>
    request<{ rule: string; derived: Fact[]; count: number }>(
      `/memory/rule/${encodeURIComponent(name)}/run?store=${store}`,
      { method: 'POST' },
    ),

  // Query and search
  queryMemory: (query: string, adminKey?: string, limit = 100) =>
    request<QueryResponse>('/memory/query', {
      method: 'POST',
      headers: adminKey ? { 'X-Admin-Key': adminKey } : {},
      body: JSON.stringify({ query, limit }),
    }),

  searchMemory: (query: string, topK = 5) =>
    request<MemorySearchResponse>(`/memory/search?query=${encodeURIComponent(query)}&top_k=${topK}`),

  getAudit: (subject?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (subject) params.set('subject', subject);
    return request<{ entries: AuditEntry[]; count: number }>(`/memory/audit?${params.toString()}`);
  },

  verifyFact: (fact: FactRequest) =>
    request<{
      stored: boolean;
      stored_value: FactValue;
      derivable_from_rules: string[];
      consistent: boolean;
    }>('/memory/verify', { method: 'POST', body: JSON.stringify(fact) }),

  // LLM and agent
  askAgent: (data: AgentAskRequest) => request<AgentAskResponse>('/agent/ask', {
    method: 'POST',
    body: JSON.stringify(data),
  }),

  askLLM: (data: LLMAskRequest) => request<LLMAskResponse>('/llm/ask', {
    method: 'POST',
    body: JSON.stringify(data),
  }),

  llmHealth: () => request<LLMHealthResponse>('/llm/health'),

  // Decision trees
  getTree: () => request<TreeResponse>('/tree'),
  getNode: (id: string) => request<Node>(`/tree/${encodeURIComponent(id)}`),
  getDomains: () => request<DomainSummary[]>('/trees'),
  // No domain is sent: the API routes on the input keys and reports back which
  // domain it used, which is the point of the exercise.
  decide: (data: DecideRequest) => request<DecideResponse>('/decide', {
    method: 'POST',
    body: JSON.stringify(data),
  }),

  overrideNode: (id: string, action: string, owner: string) =>
    request<OverrideResponse>(`/memory/node/${encodeURIComponent(id)}/override`, {
      method: 'POST',
      body: JSON.stringify({ action, owner }),
    }),

  getNodeAudit: (id: string) =>
    request<{ node_id: string; entries: any[]; count: number }>(
      `/memory/node/${encodeURIComponent(id)}/audit`,
    ),

  // Health
  health: () => request<HealthResponse>('/health'),
  healthAll: () => request<{ status: string }>('/health/all'),
};
