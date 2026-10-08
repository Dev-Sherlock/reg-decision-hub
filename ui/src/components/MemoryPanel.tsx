import React, { useCallback, useEffect, useState } from 'react';
import {
  api,
  Fact,
  FactValue,
  formatValue,
  MemorySearchHit,
  Rule,
} from '../api';

type Tab = 'facts' | 'rules' | 'search';

const TABS: { id: Tab; label: string }[] = [
  { id: 'facts', label: 'Facts' },
  { id: 'rules', label: 'Rules' },
  { id: 'search', label: 'Search' },
];

/**
 * Browser for the symbolic memory: stored facts, inference rules, and
 * semantic search over the pgvector index.
 */
export function MemoryPanel({ onFactsChanged }: { onFactsChanged?: () => void }) {
  const [tab, setTab] = useState<Tab>('facts');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const report = (err: unknown) => {
    setNotice(null);
    // Callers pass null to clear a previous error, so nullish means "reset".
    setError(err == null ? null : err instanceof Error ? err.message : String(err));
  };

  const notify = (message: string) => {
    setError(null);
    setNotice(message);
  };

  return (
    <div className="panel">
      <div className="panel-header tabs-header">
        <h2>Symbolic Memory</h2>
        <div className="tabs">
          {TABS.map(({ id, label }) => (
            <button
              key={id}
              className={`tab ${tab === id ? 'active' : ''}`}
              onClick={() => {
                setTab(id);
                setError(null);
                setNotice(null);
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="panel-content">
        {error && <div className="alert error">{error}</div>}
        {notice && <div className="alert success">{notice}</div>}

        {tab === 'facts' && (
          <FactsTab onError={report} onNotice={notify} onChanged={onFactsChanged} />
        )}
        {tab === 'rules' && (
          <RulesTab onError={report} onNotice={notify} onChanged={onFactsChanged} />
        )}
        {tab === 'search' && <SearchTab onError={report} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Facts
// ---------------------------------------------------------------------------

function FactsTab({
  onError,
  onNotice,
  onChanged,
}: {
  onError: (err: unknown) => void;
  onNotice: (message: string) => void;
  onChanged?: () => void;
}) {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [subject, setSubject] = useState('*');
  const [predicate, setPredicate] = useState('*');
  const [loading, setLoading] = useState(false);

  const [subjectInput, setSubjectInput] = useState('');
  const [predicateInput, setPredicateInput] = useState('');
  const [valueInput, setValueInput] = useState('');
  const [ownerInput, setOwnerInput] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getFacts(subject, predicate);
      setFacts(data.facts);
      onError(null);
    } catch (err) {
      onError(err);
      setFacts([]);
    } finally {
      setLoading(false);
    }
  }, [subject, predicate, onError]);

  useEffect(() => {
    load();
  }, [load]);

  const addFact = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      const created = await api.postFact({
        subject: subjectInput.trim(),
        predicate: predicateInput.trim(),
        value: coerce(valueInput),
        owner: ownerInput.trim() || undefined,
      });
      onNotice(`Stored ${created.subject}.${created.predicate}`);
      setSubjectInput('');
      setPredicateInput('');
      setValueInput('');
      await load();
      onChanged?.();
    } catch (err) {
      onError(err);
    }
  };

  const removeFact = async (fact: Fact) => {
    try {
      await api.deleteFact(fact.subject, fact.predicate);
      onNotice(`Deleted ${fact.subject}.${fact.predicate}`);
      await load();
      onChanged?.();
    } catch (err) {
      onError(err);
    }
  };

  return (
    <>
      <div className="filter-row">
        <label>
          Subject
          <input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="* for any"
          />
        </label>
        <label>
          Predicate
          <input
            value={predicate}
            onChange={(e) => setPredicate(e.target.value)}
            placeholder="* for any"
          />
        </label>
        <button className="btn btn-secondary" onClick={load} disabled={loading}>
          {loading ? 'Loading...' : 'Apply'}
        </button>
      </div>

      <form className="inline-form" onSubmit={addFact}>
        <input
          required
          value={subjectInput}
          onChange={(e) => setSubjectInput(e.target.value)}
          placeholder="subject"
        />
        <input
          required
          value={predicateInput}
          onChange={(e) => setPredicateInput(e.target.value)}
          placeholder="predicate"
        />
        <input
          required
          value={valueInput}
          onChange={(e) => setValueInput(e.target.value)}
          placeholder='value (22, "Acme Corp", [a, b])'
        />
        <input
          value={ownerInput}
          onChange={(e) => setOwnerInput(e.target.value)}
          placeholder="owner (optional)"
        />
        <button className="btn btn-primary" type="submit">
          Store fact
        </button>
      </form>

      {facts.length === 0 ? (
        <div className="empty-state">
          <p>No facts match this filter.</p>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Subject</th>
              <th>Predicate</th>
              <th>Value</th>
              <th>v</th>
              <th>Updated</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {facts.map((fact) => (
              <tr key={`${fact.subject}/${fact.predicate}`}>
                <td className="mono">{fact.subject}</td>
                <td className="mono">{fact.predicate}</td>
                <td>{formatValue(fact.value)}</td>
                <td className="dim">{fact.version}</td>
                <td className="dim">{fact.updated_at ? fact.updated_at.slice(0, 19) : '-'}</td>
                <td>
                  <button
                    className="btn btn-ghost danger"
                    onClick={() => removeFact(fact)}
                    title="Delete this fact"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------

function RulesTab({
  onError,
  onNotice,
  onChanged,
}: {
  onError: (err: unknown) => void;
  onNotice: (message: string) => void;
  onChanged?: () => void;
}) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [name, setName] = useState('');
  const [head, setHead] = useState('fact(X, colleague_of, Y)');
  const [body, setBody] = useState('[fact(X, works_at, Y)]');
  const [derived, setDerived] = useState<Record<string, Fact[]>>({});

  const load = useCallback(async () => {
    try {
      const data = await api.getRules();
      setRules(data.rules);
      onError(null);
    } catch (err) {
      onError(err);
      setRules([]);
    }
  }, [onError]);

  useEffect(() => {
    load();
  }, [load]);

  const addRule = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await api.postRule({ name: name.trim(), head: head.trim(), body: body.trim() });
      onNotice(`Rule '${name.trim()}' stored`);
      setName('');
      await load();
      onChanged?.();
    } catch (err) {
      onError(err);
    }
  };

  const testRule = async (rule: Rule) => {
    try {
      const result = await api.runRule(rule.name, false);
      setDerived((prev) => ({ ...prev, [rule.name]: result.derived }));
      onNotice(
        result.count === 0
          ? `'${rule.name}' derived nothing — its body goals matched no facts`
          : `'${rule.name}' derives ${result.count} fact(s) (dry run, nothing stored)`,
      );
    } catch (err) {
      onError(err);
    }
  };

  const storeRule = async (rule: Rule) => {
    try {
      const result = await api.runRule(rule.name, true);
      onNotice(`Stored ${result.count} fact(s) derived by '${rule.name}'`);
      await load();
      onChanged?.();
    } catch (err) {
      onError(err);
    }
  };

  const removeRule = async (rule: Rule) => {
    try {
      await api.deleteRule(rule.name);
      onNotice(`Deleted rule '${rule.name}'`);
      await load();
      onChanged?.();
    } catch (err) {
      onError(err);
    }
  };

  return (
    <>
      <form className="inline-form column" onSubmit={addRule}>
        <div className="inline-form">
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="rule name"
          />
          <button className="btn btn-primary" type="submit">
            Store rule
          </button>
        </div>
        <input
          required
          value={head}
          onChange={(e) => setHead(e.target.value)}
          placeholder="head, e.g. fact(X, colleague_of, Y)"
        />
        <input
          required
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="body, e.g. [fact(X, works_at, Y)]"
        />
      </form>

      {rules.length === 0 ? (
        <div className="empty-state">
          <p>No rules defined yet.</p>
        </div>
      ) : (
        <ul className="rule-list">
          {rules.map((rule) => (
            <li key={rule.name}>
              <div className="rule-header">
                <span className="mono rule-name">{rule.name}</span>
                <span className="rule-actions">
                  <button className="btn btn-ghost" onClick={() => testRule(rule)}>
                    Test
                  </button>
                  <button className="btn btn-ghost" onClick={() => storeRule(rule)}>
                    Derive &amp; store
                  </button>
                  <button className="btn btn-ghost danger" onClick={() => removeRule(rule)}>
                    Delete
                  </button>
                </span>
              </div>
              <div className="mono dim">IF {rule.body}</div>
              <div className="mono dim">THEN {rule.head}</div>
              {derived[rule.name] && (
                <ul className="derived-list">
                  {derived[rule.name].length === 0 ? (
                    <li className="dim">No derivations.</li>
                  ) : (
                    derived[rule.name].map((fact) => (
                      <li key={`${fact.subject}/${fact.predicate}`} className="mono">
                        {fact.subject}.{fact.predicate} = {formatValue(fact.value)}
                      </li>
                    ))
                  )}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

function SearchTab({ onError }: { onError: (err: unknown) => void }) {
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(5);
  const [hits, setHits] = useState<MemorySearchHit[]>([]);
  const [semantic, setSemantic] = useState(true);
  const [loading, setLoading] = useState(false);

  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    try {
      const result = await api.searchMemory(query, topK);
      setHits(result.hits);
      setSemantic(result.semantic);
      onError(null);
    } catch (err) {
      onError(err);
      setHits([]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <form className="inline-form" onSubmit={search}>
        <input
          required
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask in plain language, e.g. how hot is room 101"
        />
        <label className="inline-label">
          top-k
          <input
            type="number"
            min={1}
            max={50}
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value))}
          />
        </label>
        <button className="btn btn-primary" type="submit" disabled={loading}>
          {loading ? 'Searching...' : 'Search'}
        </button>
      </form>

      {!semantic && (
        <div className="alert warning">
          The embedding model is unavailable, so these results come from keyword
          matching rather than meaning. Stored facts and everything else still work.
        </div>
      )}

      {hits.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Score</th>
              <th>Subject</th>
              <th>Predicate</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((hit) => (
              <tr key={`${hit.subject}/${hit.predicate}`}>
                <td className="mono">
                  {hit.similarity !== undefined ? hit.similarity.toFixed(3) : '-'}
                </td>
                <td className="mono">{hit.subject}</td>
                <td className="mono">{hit.predicate}</td>
                <td>{formatValue(hit.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        query && !loading && <div className="empty-state"><p>No facts matched.</p></div>
      )}
    </>
  );
}

/**
 * Parse a typed value into JSON where it looks like JSON, otherwise keep it as
 * a string. Lets "22" store a number and '"Acme Corp"' or '["a"]' store
 * structure without the user needing to know the value schema.
 */
function coerce(raw: string): FactValue {
  const trimmed = raw.trim();
  if (trimmed === 'null') return null;
  if (trimmed === 'true') return true;
  if (trimmed === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  if (/^[[{]/.test(trimmed)) {
    try {
      return JSON.parse(trimmed) as FactValue;
    } catch {
      return raw;
    }
  }
  return raw;
}
