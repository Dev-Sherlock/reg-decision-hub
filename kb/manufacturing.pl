:- module(manufacturing, [
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

% Manufacturing lot release — example domain.
%
% The only one of the four whose deepest branch is a pair of *flags* rather than a
% comparison: qa7 tests ftir_flag bare and qa8 negates it, so a lot with no FTIR
% reading stored falls through to quarantine rather than being released on absent
% evidence.
%
% Every sibling pair is mutually exclusive; see air_traffic.pl for why that is
% load-bearing.

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
% with the module that owns the predicate — so a caller naming manufacturing and asking
% for eval_condition/2 has to get it. Without this the goal dies with an existence
% error while the same query against energy works, which looks like a routing bug.
:- reexport(tree_engine, [eval_condition/2, node_timestamp/1]).

:- dynamic node/6.

node(qa_root, none, true, none, system, 1).
node(qa1, qa_root, defect_rate_pct > 5, quarantine_lot, qe_engineer, 1).
node(qa2, qa_root, defect_rate_pct =< 5, check_hardness, process_engineer, 1).
node(qa3, qa2, hardness_hrc < 45, rework_lot, process_engineer, 1).
node(qa4, qa2, hardness_hrc >= 45, check_coating, process_engineer, 1).
node(qa5, qa4, coating_um =< 25, release_lot, qe_engineer, 1).
node(qa6, qa4, coating_um > 25, hold_for_ftir, process_engineer, 1).
node(qa7, qa6, ftir_flag, release_with_deviation, qe_engineer, 1).
node(qa8, qa6, \+ ftir_flag, quarantine_lot, qe_engineer, 1).

input_keys([defect_rate_pct, hardness_hrc, coating_um, ftir_flag]).

can_write(Agent, NodeID) :-
    can_write_in(module(manufacturing), Agent, NodeID).

provenance(NodeID, Agent, Timestamp, Entry) :-
    provenance_in(module(manufacturing), NodeID, Agent, Timestamp, Entry).

trace(NodeID, Proof) :-
    trace_in(module(manufacturing), NodeID, Proof).

decide(Input, Leaf, Proof) :-
    decide_in(module(manufacturing), Input, Leaf, Proof).

get_all_nodes(Nodes) :-
    collect_nodes_in(module(manufacturing), Nodes).

get_leaves(Leaves) :-
    leaves_in(module(manufacturing), Leaves).

is_leaf(NodeID) :-
    is_leaf_in(module(manufacturing), NodeID).
