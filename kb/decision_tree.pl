:- module(decision_tree, [
    node/6,
    can_write/2,
    provenance/4,
    node_timestamp/1,
    trace/2,
    deepest/3,
    decide/3,
    eval_condition/2,
    input_keys/1,
    get_all_nodes/1,
    get_leaves/1,
    is_leaf/1
]).

% Energy dispatch — the first example domain.
%
% The walk, the condition evaluator and the governance rules all live in
% kb/tree_engine.pl. What is left here is the tree itself and thin wrappers that
% bind it to this module, which is what keeps /decide able to consult several
% domains while test_decision.pl keeps calling decide/3 directly.

:- use_module(tree_engine, [
    root_in/2,
    input_keys_in/2,
    decide_in/4,
    deepest_in/4,
    trace_in/3,
    can_write_in/3,
    collect_nodes_in/2,
    is_leaf_in/2,
    leaves_in/2,
    provenance_in/5,
    eval_condition/2,
    node_timestamp/1
]).

% Re-exported rather than wrapped: test_decision.pl evaluates conditions straight
% through this module, and eval_condition/2 is the same predicate either way.
:- reexport(tree_engine, [eval_condition/2, node_timestamp/1]).

% node/6 is dynamic because a governed override retracts the old node and
% asserts the new one. Facts in a consulted file are static by default, and
% retract/1 on a static predicate fails with a permission_error — which would
% make override_node/3 report success in PostgreSQL while silently leaving the
% decision tree stale in Prolog. The facts below still seed it at load time.
:- dynamic node/6.

% node(ID, ParentID, Condition, Action, Owner, Version).
node(root, none, true, none, system, 1).
node(n1, root, demand >= 80, dispatch_energy, forecaster, 1).
node(n2, root, demand < 80, do_nothing, forecaster, 1).
node(n3, n1, temperature > 30, increase_cooling, optimizer, 1).
node(n4, n1, temperature =< 30, keep_cooling, optimizer, 1).

% Additional nodes for more realistic tree
node(n5, n2, temperature > 25, monitor_only, forecaster, 1).
node(n6, n2, temperature =< 25, do_nothing, forecaster, 1).
node(n7, n3, humidity > 70, activate_dehumidifier, optimizer, 1).
node(n8, n3, humidity =< 70, increase_cooling_only, optimizer, 1).
node(n9, n4, humidity > 60, adjust_airflow, optimizer, 1).
node(n10, n4, humidity =< 60, keep_cooling_only, optimizer, 1).

% The keys /decide routes on. Not inferable from the nodes, because a node that
% never tests a key may still be reachable when it is present in the input.
input_keys([demand, temperature, humidity]).

can_write(Agent, NodeID) :-
    can_write_in(module(decision_tree), Agent, NodeID).

provenance(NodeID, Agent, Timestamp, Entry) :-
    provenance_in(module(decision_tree), NodeID, Agent, Timestamp, Entry).

trace(NodeID, Proof) :-
    trace_in(module(decision_tree), NodeID, Proof).

deepest(Node, Input, Leaf) :-
    deepest_in(module(decision_tree), Node, Input, Leaf).

decide(Input, Leaf, Proof) :-
    decide_in(module(decision_tree), Input, Leaf, Proof).

get_all_nodes(Nodes) :-
    collect_nodes_in(module(decision_tree), Nodes).

get_leaves(Leaves) :-
    leaves_in(module(decision_tree), Leaves).

is_leaf(NodeID) :-
    is_leaf_in(module(decision_tree), NodeID).
