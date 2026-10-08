:- module(cyber, [
    node/6,
    can_write/2,
    provenance/4,
    trace/2,
    decide/3,
    input_keys/1,
    get_all_nodes/1,
    get_leaves/1,
    is_leaf/1,
    eval_condition/2,
    node_timestamp/1
]).

% Cyber incident severity — example domain.
%
% sec2 and sec3 test two keys in conjunction, so a report carrying only a blast
% radius stops at sec1's parent rather than being escalated on half the evidence.
%
% sec1's three children are the one place in this KB where sibling overlap would
% have been tempting and was avoided by making the exclusions explicit:
% data_exposed wins outright, and only its negation reaches the ransomware test.
% Writing sec6 as `ransomware_flag` instead would let both sec5 and sec6 match the
% same incident, and decide_in/4 would then return whichever clause SWI happened
% to try first. The overlap is documented rather than hidden.

:- use_module(tree_engine, [
    decide_in/4,
    trace_in/3,
    can_write_in/3,
    collect_nodes_in/2,
    is_leaf_in/2,
    leaves_in/2,
    provenance_in/5
]).

% Re-exported so all four domain modules present the same interface. The registry
% dispatches on node/6, decide/3 and input_keys/1, but /memory/query qualifies a goal
% with the module that owns the predicate — so a caller naming cyber and asking for
% eval_condition/2 has to get it. Without this the goal dies with an existence error
% while the same query against energy works, which looks like a routing bug.
:- reexport(tree_engine, [eval_condition/2, node_timestamp/1]).

:- dynamic node/6.

node(sec_root, none, true, none, system, 1).
node(sec1, sec_root, blast_radius_pct > 25, declare_sev1, soc_lead, 1).
node(sec2, sec_root, (blast_radius_pct =< 25, affected_users > 1000), escalate_to_exec, ciso, 1).
node(sec3, sec_root, (blast_radius_pct =< 25, affected_users =< 1000), triage_containment, soc_lead, 1).
node(sec4, sec1, data_exposed, notify_ciso_and_regulator, ciso, 1).
node(sec5, sec1, (\+ data_exposed, ransomware_flag), isolate_subnet, soc_lead, 1).
node(sec6, sec1, (\+ data_exposed, \+ ransomware_flag), escalate_to_exec, ciso, 1).

input_keys([blast_radius_pct, affected_users, data_exposed, ransomware_flag]).

can_write(Agent, NodeID) :-
    can_write_in(module(cyber), Agent, NodeID).

provenance(NodeID, Agent, Timestamp, Entry) :-
    provenance_in(module(cyber), NodeID, Agent, Timestamp, Entry).

trace(NodeID, Proof) :-
    trace_in(module(cyber), NodeID, Proof).

decide(Input, Leaf, Proof) :-
    decide_in(module(cyber), Input, Leaf, Proof).

get_all_nodes(Nodes) :-
    collect_nodes_in(module(cyber), Nodes).

get_leaves(Leaves) :-
    leaves_in(module(cyber), Leaves).

is_leaf(NodeID) :-
    is_leaf_in(module(cyber), NodeID).
