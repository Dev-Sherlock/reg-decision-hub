:- begin_tests(decision_tree).

% Load the module under test. The path is relative to this file's own
% directory; '../decision_tree' points outside /kb and silently imports nothing.
:- use_module(decision_tree).

% Helper: standard input dict
test_input(Demand, Temp, Humidity, _{demand:Demand, temperature:Temp, humidity:Humidity}).

% Helper: was a node visited on the way to the leaf?
% trace/2 returns [NodeID, Condition, Action, Owner] steps, so membership has
% to look inside each step rather than match the node id directly.
visited(NodeID, Proof) :-
    member(Step, Proof),
    Step = [NodeID, _, _, _].

% --- decide/3 tests for each leaf ---

% n3: demand >= 80, temperature > 30. n3 has children, so it is an internal
% node — the walk continues to n7/n8 depending on humidity.
test(decide_n3_high_demand_high_temp) :-
    test_input(90, 35, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n8,
    visited(n3, Proof).

% n4: demand >= 80, temperature =< 30. Likewise internal; humidity decides
% between n9 and n10.
test(decide_n4_high_demand_low_temp) :-
    test_input(85, 25, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n10,
    visited(n4, Proof).

% n5: demand < 80, temperature > 25
test(decide_n5_low_demand_high_temp) :-
    test_input(50, 30, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n5,
    visited(n5, Proof).

% n6: demand < 80, temperature =< 25
test(decide_n6_low_demand_low_temp) :-
    test_input(30, 20, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n6,
    visited(n6, Proof).

% n7: demand >= 80, temperature > 30, humidity > 70
test(decide_n7_high_demand_high_temp_high_humidity) :-
    test_input(90, 35, 80, Input),
    decide(Input, Leaf, Proof),
    Leaf = n7,
    visited(n7, Proof).

% n8: demand >= 80, temperature > 30, humidity =< 70
test(decide_n8_high_demand_high_temp_low_humidity) :-
    test_input(90, 35, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n8,
    visited(n8, Proof).

% n9: demand >= 80, temperature =< 30, humidity > 60
test(decide_n9_high_demand_low_temp_high_humidity) :-
    test_input(85, 25, 70, Input),
    decide(Input, Leaf, Proof),
    Leaf = n9,
    visited(n9, Proof).

% n10: demand >= 80, temperature =< 30, humidity =< 60
test(decide_n10_high_demand_low_temp_low_humidity) :-
    test_input(85, 25, 50, Input),
    decide(Input, Leaf, Proof),
    Leaf = n10,
    visited(n10, Proof).

% --- trace/2 tests ---

test(trace_root) :-
    trace(root, Proof),
    Proof = [[root, true, none, system]].

test(trace_n1) :-
    trace(n1, Proof),
    Proof = [
        [root, true, none, system],
        [n1, demand >= 80, dispatch_energy, forecaster]
    ].

test(trace_n3) :-
    trace(n3, Proof),
    Proof = [
        [root, true, none, system],
        [n1, demand >= 80, dispatch_energy, forecaster],
        [n3, temperature > 30, increase_cooling, optimizer]
    ].

% --- can_write/2 tests ---

test(can_write_forecaster_n1) :-
    can_write(forecaster, n1).

test(can_write_forecaster_n2) :-
    can_write(forecaster, n2).

test(can_write_optimizer_n3) :-
    can_write(optimizer, n3).

test(can_write_optimizer_n4) :-
    can_write(optimizer, n4).

test(cannot_write_forecaster_n3) :-
    \+ can_write(forecaster, n3).

test(cannot_write_optimizer_n1) :-
    \+ can_write(optimizer, n1).

test(cannot_write_system_any) :-
    \+ can_write(system, n1).

% --- eval_condition/2 tests ---

test(eval_true) :-
    eval_condition(true, _{}).

test(eval_demand_gt_80_true) :-
    eval_condition(demand > 80, _{demand:90}).

test(eval_demand_gt_80_false) :-
    \+ eval_condition(demand > 80, _{demand:50}).

test(eval_demand_le_80_true) :-
    eval_condition(demand =< 80, _{demand:50}).

test(eval_demand_le_80_false) :-
    \+ eval_condition(demand =< 80, _{demand:90}).

test(eval_temp_gt_30_true) :-
    eval_condition(temperature > 30, _{temperature:35}).

test(eval_temp_le_30_true) :-
    eval_condition(temperature =< 30, _{temperature:25}).

test(eval_humidity_gt_70_true) :-
    eval_condition(humidity > 70, _{humidity:80}).

test(eval_humidity_le_70_true) :-
    eval_condition(humidity =< 70, _{humidity:50}).

% --- generic condition evaluator (not energy-specific) ---

test(eval_unknown_key_fails) :-
    \+ eval_condition(risk_score > 5, _{demand:50}).

test(eval_string_comparison) :-
    eval_condition(region == north, _{region:north}).

test(eval_string_comparison_false) :-
    \+ eval_condition(region == north, _{region:south}).

test(eval_conjunction) :-
    eval_condition((demand > 80, humidity > 70), _{demand:90, humidity:80}).

test(eval_conjunction_false) :-
    \+ eval_condition((demand > 80, humidity > 70), _{demand:90, humidity:10}).

test(eval_negation) :-
    eval_condition(\+ (demand > 80), _{demand:50}).

test(eval_negation_false) :-
    \+ eval_condition(\+ (demand > 80), _{demand:90}).

test(eval_bare_flag_true) :-
    eval_condition(emergency_active, _{emergency_active:true}).

test(eval_bare_flag_false) :-
    \+ eval_condition(emergency_active, _{emergency_active:false}).

% --- decide/3 with a partial input dict ---
% n1 needs demand; without humidity it cannot choose between n7 and n8, so
% the walk stops at the deepest node the tree can justify.

test(decide_partial_input_stops_at_internal_node) :-
    decide(_{demand:90, temperature:35}, Leaf, Proof),
    Leaf = n3,
    visited(n3, Proof).

test(decide_demand_only) :-
    decide(_{demand:50}, Leaf, _),
    Leaf = n2.

% --- provenance/4 ---

test(provenance_snapshot_for_owner) :-
    provenance(n3, optimizer, Timestamp, Entry),
    atom(Timestamp),
    atom_length(Timestamp, 20),
    Entry = node{id:n3, action:increase_cooling, owner:optimizer, version:1}.

test(provenance_snapshot_for_non_owner) :-
    \+ provenance(n3, forecaster, _, _).

% --- get_all_nodes/1 test ---

test(get_all_nodes_count) :-
    get_all_nodes(Nodes),
    length(Nodes, 11).  % root + n1..n10

% --- is_leaf/1 tests ---
% n3 and n4 are internal (n7/n8 and n9/n10 hang off them); the leaves are the
% n5..n10 that terminate the walk.

test(is_leaf_n7) :-
    is_leaf(n7).

test(is_leaf_n10) :-
    is_leaf(n10).

test(not_leaf_root) :-
    \+ is_leaf(root).

test(not_leaf_n1) :-
    \+ is_leaf(n1).

test(not_leaf_n3) :-
    \+ is_leaf(n3).

test(not_leaf_n4) :-
    \+ is_leaf(n4).

% --- get_leaves/1 test ---

test(get_leaves_count) :-
    get_leaves(Leaves),
    length(Leaves, 6).  % n5, n6, n7, n8, n9, n10

test(get_leaves_membership) :-
    get_leaves(Leaves),
    sort(Leaves, Sorted),
    sort([n5, n6, n7, n8, n9, n10], Expected),
    Sorted == Expected.

:- end_tests(decision_tree).