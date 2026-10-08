import React from 'react';
import { ProofStep } from '../api';

interface ExplanationModalProps {
  isOpen: boolean;
  onClose: () => void;
  proofTrace: ProofStep[];
  explanation?: string;
  fallbackUsed?: boolean;
  onOverride?: (action: string, owner: string) => void;
  overrideMode?: boolean;
  selectedNodeId?: string;
}

export const ExplanationModal: React.FC<ExplanationModalProps> = ({
  isOpen,
  onClose,
  proofTrace,
  explanation,
  fallbackUsed,
  onOverride,
  overrideMode = false,
  selectedNodeId,
}) => {
  const [overrideAction, setOverrideAction] = React.useState('');
  const [overrideOwner, setOverrideOwner] = React.useState('forecaster');

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (onOverride && overrideAction.trim() && selectedNodeId) {
      onOverride(overrideAction.trim(), overrideOwner);
      onClose();
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{overrideMode ? 'Override Node Action' : 'Decision Explanation'}</h3>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div className="modal-body">
          {overrideMode ? (
            <form id="override-form" onSubmit={handleSubmit} className="override-form">
              <div className="form-group">
                <label htmlFor="override-action">New Action</label>
                <input
                  id="override-action"
                  type="text"
                  value={overrideAction}
                  onChange={e => setOverrideAction(e.target.value)}
                  placeholder="Enter new action..."
                  required
                  autoFocus
                />
              </div>
              <div className="form-group">
                <label htmlFor="override-owner">Owner Agent</label>
                <select
                  id="override-owner"
                  value={overrideOwner}
                  onChange={e => setOverrideOwner(e.target.value)}
                >
                  <option value="forecaster">Forecaster</option>
                  <option value="optimizer">Optimizer</option>
                </select>
              </div>
            </form>
          ) : (
            <>
              <div className="proof-trace">
                {proofTrace.map((step, index) => (
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
              {explanation && (
                <div className={`explanation-box ${fallbackUsed ? 'fallback' : ''}`}>
                  <strong>Explanation:</strong> {explanation}
                  {fallbackUsed && (
                    <p style={{ marginTop: '0.5rem', fontSize: '0.75rem', color: '#92400e' }}>
                      ⚠️ LLM service unavailable - showing fallback explanation
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
        <div className="modal-footer">
          {overrideMode ? (
            <>
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" form="override-form" className="btn btn-primary">
                Apply Override
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Close
            </button>
          )}
        </div>
      </div>
    </div>
  );
};