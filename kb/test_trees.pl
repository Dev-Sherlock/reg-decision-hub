:- begin_tests(trees).

% Tests for the four example domains and the engine that walks them.
%
% The load-bearing claim here is that decide_in/4 is parameterised by module: the
% same engine, the same condition evaluator and the same walk reach four
% different trees, one of which uses an atom comparison and two of which the
% energy tree's tests never exercise. If the module argument were being ignored,
% every domain would answer with the energy tree's leaves and most of this file
% would fail.
%
% Paths are relative to this file's own directory, as in test_decision.pl.

:- use_module(tree_registry).
:- use_module(tree_engine, [
    decide_in/4,
    root_in/2,
    eval_condition/2,
    input_keys_in/2,
    collect_nodes_in/2,
    is_leaf_in/2
]).
:- use_module(air_traffic).
:- use_module(manufacturing).
:- use_module(cyber).
:- use_module(decision_tree).

% Helper: every node id in a module.
module_node(Module, ID) :-
    Module:node(ID, _, _, _, _, _).

% Helper: which children of Node have a condition that holds for Input. More than
% one solution means the siblings overlap, which would make the walk's answer
% depend on clause order rather than on the data.
matching_children(Module, Node, Input, Children) :-
    findall(Child,
            ( Module:node(Child, Node, Cond, _, _, _),
              eval_condition(Cond, Input)
            ),
            Children).

% ---------------------------------------------------------------------------
% Registry
% ---------------------------------------------------------------------------

test(registry_four_static_domains) :-
    findall(Id, tree_registry:domain(Id, _, _), Ids),
    sort(Ids, Sorted),
    Sorted = [air_traffic, cyber, energy, manufacturing].

test(registry_labels_are_present) :-
    tree_registry:domain(energy, module(decision_tree), Label),
    atom(Label),
    nonvar(Label).

test(every_static_domain_is_active) :-
    findall(Id, tree_registry:domain(Id, _, _), Ids),
    forall(member(Id, Ids), tree_registry:domain_active(Id)).

test(no_runtime_domains_at_startup) :-
    findall(Id, tree_registry:domain_enabled(Id), Ids),
    Ids == [].

test(registry_reports_source) :-
    tree_registry:domain_source(energy, kb),
    tree_registry:domain_source(cyber, kb).

% ---------------------------------------------------------------------------
% Structural invariants every tree must hold
% ---------------------------------------------------------------------------

test(node_ids_are_globally_unique) :-
    findall(ID, module_node(decision_tree, ID), Energy),
    findall(ID, module_node(air_traffic, ID), Air),
    findall(ID, module_node(manufacturing, ID), Qa),
    findall(ID, module_node(cyber, ID), Sec),
    append([Energy, Air, Qa, Sec], All),
    sort(All, Unique),
    length(All, Total),
    length(Unique, Total).

test(each_tree_has_exactly_one_root) :-
    forall(member(M, [decision_tree, air_traffic, manufacturing, cyber]),
           root_in(module(M), _)).

test(root_is_the_only_node_with_no_parent) :-
    root_in(module(air_traffic), at_root),
    air_traffic:node(at_root, none, _, _, _, _),
    findall(ID, air_traffic:node(ID, none, _, _, _, _), Roots),
    Roots = [at_root].

test(input_keys_are_declared_for_every_domain) :-
    % forall/2 undoes its bindings, so the shape checks have to live inside it —
    % testing Keys after the fact would inspect an unbound variable.
    forall(member(M, [decision_tree, air_traffic, manufacturing, cyber]),
           ( input_keys_in(module(M), Keys),
             Keys \== [],
             is_list(Keys)
           )).

% ---------------------------------------------------------------------------
% The module argument is real
% ---------------------------------------------------------------------------

test(decide_in_uses_the_module_argument) :-
    decide_in(module(decision_tree), _{demand:90, temperature:35, humidity:80}, Energy, _),
    Energy = n7,
    decide_in(module(cyber), _{blast_radius_pct:40, data_exposed:true}, Cyber, _),
    Cyber = sec4.

test(the_same_input_reaches_different_trees) :-
    % Neither input contains a key from the other domain, so if the handle were
    % ignored both calls would land on the energy root.
    decide_in(module(air_traffic), _{region:south, brake_temp_c:300}, Air, _),
    Air = at9,
    decide_in(module(manufacturing), _{defect_rate_pct:2, hardness_hrc:40}, Qa, _),
    Qa = qa3.

% ---------------------------------------------------------------------------
% air_traffic
% ---------------------------------------------------------------------------

test(air_traffic_north_icing_denies_takeoff) :-
    air_traffic:decide(_{region:north, ice_on_wing_pct:15}, Leaf, _),
    Leaf = at3.

test(air_traffic_north_light_ice_checks_crosswind) :-
    air_traffic:decide(_{region:north, ice_on_wing_pct:5, crosswind_kts:30}, Leaf, _),
    Leaf = at5.

test(air_traffic_north_calm_clears_takeoff) :-
    air_traffic:decide(_{region:north, ice_on_wing_pct:5, crosswind_kts:10}, Leaf, _),
    Leaf = at6.

test(air_traffic_engine_out_triggers_emergency_return) :-
    air_traffic:decide(_{region:south, engine_out:true}, Leaf, _),
    Leaf = at7.

test(air_traffic_hot_brakes_hold_short) :-
    air_traffic:decide(_{region:south, brake_temp_c:450}, Leaf, _),
    Leaf = at8.

test(air_traffic_normal_south_branch_awaits_clearance) :-
    air_traffic:decide(_{region:south, brake_temp_c:300}, Leaf, _),
    Leaf = at9.

% The atom comparison the energy tree never exercises: north and south must land
% on different sides of the root.
test(air_traffic_region_is_compared_as_an_atom) :-
    air_traffic:decide(_{region:north, ice_on_wing_pct:15}, North, _),
    air_traffic:decide(_{region:south, ice_on_wing_pct:15}, South, _),
    North \== South.

test(air_traffic_a_missing_flag_still_resolves) :-
    % engine_out is absent rather than false. eval_flag/2 fails on a missing key
    % and \+ turns that into success, so the branch degrades to at8/at9 instead of
    % dead-ending at at2.
    air_traffic:decide(_{region:south, brake_temp_c:300}, Leaf, _),
    Leaf = at9.

test(air_traffic_partial_input_stops_at_the_deepest_justified_node) :-
    % Enough to pick the northern branch, not enough to clear or deny takeoff.
    air_traffic:decide(_{region:north, ice_on_wing_pct:5}, Leaf, _),
    Leaf = at4.

test(air_traffic_trace_reaches_the_root) :-
    air_traffic:trace(at3, Proof),
    Proof = [
        [at_root, true, none, system],
        [at1, region == north, evaluate_icing, safety_officer],
        [at3, ice_on_wing_pct > 10, takeoff_denied_deice, safety_officer]
    ].

test(air_traffic_owner_may_write_its_own_node) :-
    air_traffic:can_write(safety_officer, at3),
    air_traffic:can_write(dispatcher, at6).

test(air_traffic_non_owner_cannot_write) :-
    \+ air_traffic:can_write(dispatcher, at3),
    \+ air_traffic:can_write(system, at1).

test(air_traffic_leaves) :-
    air_traffic:get_leaves(Leaves),
    sort(Leaves, Sorted),
    sort([at3, at5, at6, at7, at8, at9], Expected),
    Sorted == Expected.

test(air_traffic_siblings_do_not_overlap) :-
    matching_children(air_traffic, at_root, _{region:north}, [at1]),
    matching_children(air_traffic, at_root, _{region:south}, [at2]),
    matching_children(air_traffic, at1, _{ice_on_wing_pct:10}, [at4]),
    matching_children(air_traffic, at2, _{engine_out:true}, [at7]),
    % Two unrelated engine states at once: exactly one child may answer.
    matching_children(air_traffic, at2,
                      _{engine_out:true, brake_temp_c:450}, [at7]).

% ---------------------------------------------------------------------------
% manufacturing
% ---------------------------------------------------------------------------

test(manufacturing_high_defects_quarantine) :-
    manufacturing:decide(_{defect_rate_pct:8}, Leaf, _),
    Leaf = qa1.

test(manufacturing_soft_lot_is_reworked) :-
    manufacturing:decide(_{defect_rate_pct:2, hardness_hrc:40}, Leaf, _),
    Leaf = qa3.

test(manufacturing_thin_coating_releases) :-
    manufacturing:decide(_{defect_rate_pct:2, hardness_hrc:50, coating_um:20}, Leaf, _),
    Leaf = qa5.

test(manufacturing_thick_coating_with_ftir_releases_with_deviation) :-
    manufacturing:decide(
        _{defect_rate_pct:2, hardness_hrc:50, coating_um:30, ftir_flag:true},
        Leaf, _),
    Leaf = qa7.

test(manufacturing_thick_coating_without_ftir_quarantines) :-
    manufacturing:decide(
        _{defect_rate_pct:2, hardness_hrc:50, coating_um:30, ftir_flag:false},
        Leaf, _),
    Leaf = qa8.

test(manufacturing_absent_ftir_flag_quarantines) :-
    % A bare flag test must not release a lot on absent evidence.
    manufacturing:decide(
        _{defect_rate_pct:2, hardness_hrc:50, coating_um:30},
        Leaf, _),
    Leaf = qa8.

test(manufacturing_partial_input_stops_at_the_deepest_justified_node) :-
    manufacturing:decide(_{defect_rate_pct:2, hardness_hrc:50}, Leaf, _),
    Leaf = qa4.

test(manufacturing_owner_may_write_its_own_node) :-
    manufacturing:can_write(qe_engineer, qa1),
    manufacturing:can_write(process_engineer, qa4).

test(manufacturing_non_owner_cannot_write) :-
    \+ manufacturing:can_write(process_engineer, qa1),
    \+ manufacturing:can_write(qe_engineer, qa4).

test(manufacturing_leaves) :-
    manufacturing:get_leaves(Leaves),
    sort(Leaves, Sorted),
    sort([qa1, qa3, qa5, qa7, qa8], Expected),
    Sorted == Expected.

test(manufacturing_siblings_do_not_overlap) :-
    matching_children(manufacturing, qa_root, _{defect_rate_pct:5}, [qa2]),
    matching_children(manufacturing, qa_root, _{defect_rate_pct:5.1}, [qa1]),
    matching_children(manufacturing, qa2, _{hardness_hrc:45}, [qa4]),
    matching_children(manufacturing, qa6, _{ftir_flag:false}, [qa8]),
    matching_children(manufacturing, qa6, _{ftir_flag:true}, [qa7]).

% ---------------------------------------------------------------------------
% cyber
% ---------------------------------------------------------------------------

% sec1 is an internal node, exactly as n3 and n4 are in the energy tree: its three
% children always answer, so the walk never stops there. A wide blast with neither
% flag set reaches sec6.
test(cyber_wide_blast_reaches_the_sev1_branch) :-
    % once/1 around the pair, not just the trace: trace_in/3 genuinely is a search
    % predicate — it enumerates the paths to a node — so it is left nondet on purpose.
    % This test wants one of those paths and one matching step, not every alternative.
    once((
        cyber:trace(sec6, Proof),
        member([sec1, blast_radius_pct > 25, declare_sev1, soc_lead], Proof)
    )).

test(cyber_wide_blast_without_flags_escalates) :-
    cyber:decide(_{blast_radius_pct:40}, Leaf, _),
    Leaf = sec6.

test(cyber_data_exposure_outranks_ransomware) :-
    % Both flags true. Data exposure wins outright; had sec6 been written as a
    % bare ransomware test it would also match and the walk would be ambiguous.
    cyber:decide(_{blast_radius_pct:40, data_exposed:true, ransomware_flag:true}, Leaf, _),
    Leaf = sec4,
    matching_children(cyber, sec1,
        _{data_exposed:true, ransomware_flag:true}, [sec4]).

test(cyber_ransomware_without_exposure_isolates) :-
    cyber:decide(_{blast_radius_pct:40, data_exposed:false, ransomware_flag:true}, Leaf, _),
    Leaf = sec5.

test(cyber_wide_blast_without_flags_escalates) :-
    cyber:decide(_{blast_radius_pct:40, data_exposed:false, ransomware_flag:false}, Leaf, _),
    Leaf = sec6.

test(cyber_many_users_escalates) :-
    cyber:decide(_{blast_radius_pct:10, affected_users:5000}, Leaf, _),
    Leaf = sec2.

test(cyber_few_users_triages) :-
    cyber:decide(_{blast_radius_pct:10, affected_users:50}, Leaf, _),
    Leaf = sec3.

test(cyber_conjunction_needs_both_inputs) :-
    % A blast radius on its own cannot classify the incident: sec2 and sec3 both
    % require affected_users, so neither holds and the walk stops at the root.
    % Escalating on half the evidence is exactly what the conjunction prevents.
    cyber:decide(_{blast_radius_pct:10}, Leaf, _),
    Leaf = sec_root,
    cyber:decide(_{blast_radius_pct:10, affected_users:5000}, Classified, _),
    Classified = sec2.

test(cyber_owner_may_write_its_own_node) :-
    cyber:can_write(ciso, sec4),
    cyber:can_write(soc_lead, sec1).

test(cyber_non_owner_cannot_write) :-
    % The case governance needs to get right: sec1 belongs to soc_lead, so the
    % ciso may not rewrite it even though the ciso owns sec4 below it.
    \+ cyber:can_write(ciso, sec1),
    \+ cyber:can_write(soc_lead, sec4).

test(cyber_leaves) :-
    cyber:get_leaves(Leaves),
    sort(Leaves, Sorted),
    sort([sec2, sec3, sec4, sec5, sec6], Expected),
    Sorted == Expected.

test(cyber_siblings_do_not_overlap) :-
    matching_children(cyber, sec_root, _{blast_radius_pct:25, affected_users:1000}, [sec3]),
    matching_children(cyber, sec_root, _{blast_radius_pct:25, affected_users:1001}, [sec2]),
    matching_children(cyber, sec_root, _{blast_radius_pct:25.1}, [sec1]).

% ---------------------------------------------------------------------------
% Cross-domain
% ---------------------------------------------------------------------------

test(a_wrong_typed_input_never_matches_a_numeric_threshold) :-
    % The type-safety rule is what stops a confidently wrong decision, and it has
    % to hold in every domain, not just the energy one.
    \+ eval_condition(blast_radius_pct > 25, _{blast_radius_pct:"wide"}),
    \+ eval_condition(ice_on_wing_pct > 10, _{ice_on_wing_pct:"heavy"}),
    \+ eval_condition(defect_rate_pct > 5, _{defect_rate_pct:"high"}).

test(the_energy_tree_still_answers_through_the_engine) :-
    decide_in(module(decision_tree), _{demand:90, temperature:35, humidity:80}, n7, _),
    decide_in(module(decision_tree), _{demand:30, temperature:20, humidity:50}, n6, _).

test(every_domain_collects_its_own_nodes) :-
    collect_nodes_in(module(decision_tree), Energy),
    collect_nodes_in(module(air_traffic), Air),
    collect_nodes_in(module(manufacturing), Qa),
    collect_nodes_in(module(cyber), Sec),
    length(Energy, 11),
    length(Air, 10),
    length(Qa, 9),
    length(Sec, 7).

test(is_leaf_distinguishes_internal_from_terminal) :-
    is_leaf_in(module(cyber), sec4),
    \+ is_leaf_in(module(cyber), sec1),
    is_leaf_in(module(air_traffic), at3),
    \+ is_leaf_in(module(air_traffic), at4).

% ---------------------------------------------------------------------------
% One interface, four modules
% ---------------------------------------------------------------------------

% exports_everything_the_registry_and_the_query_endpoint_reach(Module)
%
% The predicates the registry dispatches on, plus the two every module has to
% re-export from tree_engine. The re-export is not cosmetic: /memory/query
% qualifies a goal with the module that owns the predicate, so a module missing
% eval_condition/2 answers one admin query with an existence error and the
% equivalent query against another domain — which reads as a routing fault
% rather than as a missing export.
exports_everything_the_registry_and_the_query_endpoint_reach(Module) :-
    Module:node(_, _, _, _, _, _),
    Module:input_keys(_),
    % A concrete input, not an unbound one: eval_comparison/4 on an unbound feature is
    % not a meaningful call, and a goal that raises here would fail the export check
    % for the wrong reason.
    Module:decide(_{probe:1}, _, _),
    Module:node(Root, none, _, _, _, _),
    Module:trace(Root, _),
    Module:can_write(_, _),
    Module:get_all_nodes(_),
    Module:get_leaves(_),
    Module:is_leaf(_),
    % A concrete dict: eval_condition/2 guards its input, so an unbound one fails and
    % would make this an export check that fails for the wrong reason.
    Module:eval_condition(true, _{probe:1}),
    Module:node_timestamp(_).

test(every_domain_module_exports_the_same_interface) :-
    % once/1 because the helper enumerates on purpose — node/6, input_keys/1 and
    % can_write/2 all leave alternatives when asked with unbound arguments. Existence
    % is the claim under test, not enumeration.
    once(exports_everything_the_registry_and_the_query_endpoint_reach(decision_tree)),
    once(exports_everything_the_registry_and_the_query_endpoint_reach(air_traffic)),
    once(exports_everything_the_registry_and_the_query_endpoint_reach(manufacturing)),
    once(exports_everything_the_registry_and_the_query_endpoint_reach(cyber)).

test(a_condition_evaluates_the_same_way_under_any_domain_module) :-
    % Same predicate, same answer, whichever module it is reached through. If a
    % module wrapped it instead of re-exporting it, this would still pass, so it
    % complements rather than replaces the export test above.
    air_traffic:eval_condition(ice_on_wing_pct > 10, _{ice_on_wing_pct:15}),
    cyber:eval_condition(blast_radius_pct > 25, _{blast_radius_pct:40}),
    manufacturing:eval_condition(defect_rate_pct > 5, _{defect_rate_pct:8}),
    \+ cyber:eval_condition(blast_radius_pct > 25, _{blast_radius_pct:"wide"}).

:- end_tests(trees).
