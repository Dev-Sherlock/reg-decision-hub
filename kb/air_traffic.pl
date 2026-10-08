:- module(air_traffic, [
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

% Air-traffic go/no-go — example domain.
%
% Deliberately exercises the atom-comparison path in eval_condition/2
% (`region == north`), which the energy tree never reaches because all three of
% its conditions are numeric. A KB that only ever compared numbers would pass
% test_decision.pl and then get this tree wrong.
%
% Every sibling pair below is mutually exclusive, so clause order never decides
% an answer. That matters: decide_in/4 takes the first solution Prolog finds, so
% an overlapping pair would make the result depend on file layout rather than on
% the data. Check a new tree against this before trusting it.

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
% with the module that owns the predicate — so a caller naming air_traffic and asking
% for eval_condition/2 has to get it. Without this the goal dies with an existence
% error while the same query against energy works, which looks like a routing bug.
:- reexport(tree_engine, [eval_condition/2, node_timestamp/1]).

:- dynamic node/6.

node(at_root, none, true, none, system, 1).
node(at1, at_root, region == north, evaluate_icing, safety_officer, 1).
node(at2, at_root, \+ (region == north), standard_procedure, dispatcher, 1).
node(at3, at1, ice_on_wing_pct > 10, takeoff_denied_deice, safety_officer, 1).
node(at4, at1, ice_on_wing_pct =< 10, crosswind_check, dispatcher, 1).
node(at5, at4, crosswind_kts > 25, takeoff_denied_wind, dispatcher, 1).
node(at6, at4, crosswind_kts =< 25, takeoff_cleared, dispatcher, 1).
node(at7, at2, engine_out, emergency_return, safety_officer, 1).
% The parentheses are load-bearing. `\+ engine_out, brake_temp_c > 400` unbracketed
% is seven arguments, not six: `,` is an argument separator, so the clause would
% silently define node/7 and at8 would vanish from the tree. Nothing errors — the
% walk just stops at at2 and the branch becomes unreachable.
node(at8, at2, (\+ engine_out, brake_temp_c > 400), hold_short, dispatcher, 1).
node(at9, at2, (\+ engine_out, \+ (brake_temp_c > 400)), line_await_clearance, dispatcher, 1).

% at7/at8/at9 rely on `\+ engine_out` succeeding when the key is absent as well as
% when it is false: eval_flag/2 fails on a missing key, and negation of a failure
% succeeds. Omitting engine_out therefore takes the standard branch rather than
% dead-ending, which is what an operator expects from a partial report.
input_keys([region, ice_on_wing_pct, crosswind_kts, engine_out, brake_temp_c]).

can_write(Agent, NodeID) :-
    can_write_in(module(air_traffic), Agent, NodeID).

provenance(NodeID, Agent, Timestamp, Entry) :-
    provenance_in(module(air_traffic), NodeID, Agent, Timestamp, Entry).

trace(NodeID, Proof) :-
    trace_in(module(air_traffic), NodeID, Proof).

decide(Input, Leaf, Proof) :-
    decide_in(module(air_traffic), Input, Leaf, Proof).

get_all_nodes(Nodes) :-
    collect_nodes_in(module(air_traffic), Nodes).

get_leaves(Leaves) :-
    leaves_in(module(air_traffic), Leaves).

is_leaf(NodeID) :-
    is_leaf_in(module(air_traffic), NodeID).
