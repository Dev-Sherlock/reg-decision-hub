%% ---------------------------------------------------------------------------
%% test_symbolic_memory.pl — plunit tests for the universal symbolic memory.
%%
%% Run from the project root:
%%   swipl -g "consult('kb/test_symbolic_memory.pl')" -t "run_tests"
%% ---------------------------------------------------------------------------
:- begin_tests(symbolic_memory).

:- use_module(symbolic_memory).

% ---------------------------------------------------------------------------
% Fixtures
% ---------------------------------------------------------------------------

setup :-
    clear_facts,
    clear_rules,
    clear_provenance.

cleanup :-
    clear_facts,
    clear_rules,
    clear_provenance.

% ---------------------------------------------------------------------------
% Fact CRUD
% ---------------------------------------------------------------------------

test(assert_fact, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    fact(room_101, temperature, 22),
    fact_count(1).

test(assert_fact_is_upsert, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    assert_fact(room_101, temperature, 25),
    fact_count(1),
    fact(room_101, temperature, 25),
    \+ fact(room_101, temperature, 22).

test(query_fact, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    query_facts(room_101, temperature, V),
    V == 22.

test(retract_fact, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    retract_fact(room_101, temperature, _),
    \+ fact(room_101, temperature, _),
    fact_count(0).

test(retract_fact_wrong_value_is_noop, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    \+ retract_fact(room_101, temperature, 99),
    fact(room_101, temperature, 22).

test(structured_values, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(person_john, works_at, "Acme Corp"),
    assert_fact(person_john, knows, [english, spanish]),
    assert_fact(person_john, active, true),
    fact(person_john, works_at, "Acme Corp"),
    fact(person_john, knows, [english, spanish]),
    fact(person_john, active, true),
    fact_count(3).

% JSON null is carried in Prolog as the reserved atom '@none', which cannot
% collide with a real string value because strings are escaped on the way in.
test(null_value_is_a_value, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, occupant, '@none'),
    fact(room_101, occupant, '@none'),
    fact_count(1).

% ---------------------------------------------------------------------------
% Wildcard queries
% ---------------------------------------------------------------------------

fact_fixture :-
    assert_fact(room_101, temperature, 22),
    assert_fact(room_101, humidity, 45),
    assert_fact(person_john, works_at, "Acme Corp").

test(wildcard_subject, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    findall(P-V, query_facts(room_101, P, V), Pairs),
    sort(Pairs, Expected),
    sort([temperature-22, humidity-45], Expected).

test(wildcard_predicate, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    findall(S, query_facts(S, temperature, _), Subjects),
    sort(Subjects, [room_101]).

test(wildcard_value, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    findall(S, query_facts(S, _, _), Subjects),
    sort(Subjects, [room_101]).

test(wildcard_all, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    findall(S-P, query_facts(S, P, _), Pairs),
    sort(Pairs, Sorted),
    sort([
        room_101-temperature,
        room_101-humidity,
        person_john-works_at
    ], Expected),
    Sorted == Expected.

test(all_facts_exact_match, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    all_facts(room_101, temperature, _, Facts),
    Facts = [[room_101, temperature, 22]].

test(all_facts_wildcard, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    all_facts(_, _, _, Facts),
    length(Facts, 3).

test(all_facts_value_pattern, [setup(setup), cleanup(cleanup)]) :-
    fact_fixture,
    all_facts(_, _, "Acme Corp", Facts),
    Facts = [[person_john, works_at, "Acme Corp"]].

% ---------------------------------------------------------------------------
% Rules
% ---------------------------------------------------------------------------

test(add_and_get_rule, [setup(setup), cleanup(cleanup)]) :-
    add_rule(spreads, fact(X, knows, Y), [fact(X, works_at, Y)]),
    get_rule(spreads, Head, Body),
    Head = fact(X, knows, Y),
    Body = [fact(_, works_at, _)].

test(redefine_rule_replaces, [setup(setup), cleanup(cleanup)]) :-
    add_rule(spreads, fact(X, knows, Y), [fact(X, works_at, Y)]),
    add_rule(spreads, fact(X, colleague_of, Y), [fact(X, works_at, Y)]),
    \+ get_rule(spreads, fact(_, knows, _), _),
    get_rule(spreads, fact(_, colleague_of, _), _).

test(run_rule_derives_facts, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(person_john, works_at, "Acme Corp"),
    assert_fact(person_jane, works_at, "Acme Corp"),
    add_rule(colleagues, fact(X, colleague_of, Y), [fact(X, works_at, Y)]),
    findall(Derived, run_rule(colleagues, Derived), Derived),
    % Sort both sides: sort/2 unifies its output with the sorted list, so the
    % expected literal has to be in standard term order too or the goal fails.
    sort(Derived, Sorted),
    sort([
        [person_john, colleague_of, "Acme Corp"],
        [person_jane, colleague_of, "Acme Corp"]
    ], Expected),
    Sorted == Expected.

test(run_rule_does_not_persist, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(person_john, works_at, "Acme Corp"),
    add_rule(colleagues, fact(X, colleague_of, Y), [fact(X, works_at, Y)]),
    run_rule(colleagues, _),
    \+ fact(_, colleague_of, _),
    fact_count(1).

test(run_rule_transitive, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(a, parent_of, b),
    assert_fact(b, parent_of, c),
    add_rule(grandparent, fact(X, grandparent_of, Z),
             [fact(X, parent_of, Y), fact(Y, parent_of, Z)]),
    findall(Derived, run_rule(grandparent, Derived), Derived),
    Derived = [[a, grandparent_of, c]].

test(run_rule_conditions, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(a, parent_of, b),
    assert_fact(b, parent_of, c),
    assert_fact(x, parent_of, y),
    assert_fact(y, parent_of, z),
    add_rule(grandparent, fact(X, grandparent_of, Z),
             [fact(X, parent_of, Y), fact(Y, parent_of, Z)]),
    findall(Subject-Z, run_rule(grandparent, [Subject, grandparent_of, Z]), Pairs),
    sort(Pairs, [a-c, x-z]).

test(run_rule_unknown_name_has_no_solutions, [setup(setup), cleanup(cleanup)]) :-
    \+ run_rule(no_such_rule, _).

test(rule_with_no_derivation, [setup(setup), cleanup(cleanup)]) :-
    add_rule(needs_thing, fact(X, derived, Y), [fact(X, missing, Y)]),
    \+ run_rule(needs_thing, _).

% ---------------------------------------------------------------------------
% Provenance / audit trail
% ---------------------------------------------------------------------------

test(provenance_insert, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    provenance(room_101, temperature, Timestamp, insert, none, 22),
    Timestamp \== none,
    provenance_for(room_101, temperature, [[_, insert, none, 22]]).

test(provenance_update_keeps_old_value, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    assert_fact(room_101, temperature, 25),
    provenance_for(room_101, temperature, Entries),
    length(Entries, 2),
    Entries = [_, [_, update, 22, 25]].

test(provenance_delete, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    retract_fact(room_101, temperature, _),
    provenance_for(room_101, temperature, [[_, insert, none, 22], [_, delete, 22, none]]).

test(provenance_is_per_fact, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    assert_fact(room_101, humidity, 45),
    provenance_for(room_101, humidity, Entries),
    Entries = [[_, insert, none, 45]].

test(provenance_wildcard, [setup(setup), cleanup(cleanup)]) :-
    assert_fact(room_101, temperature, 22),
    assert_fact(room_101, humidity, 45),
    findall(P, provenance(room_101, P, _, _, _, _), Predicates),
    sort(Predicates, [humidity, temperature]).

test(provenance_empty_for_unknown_fact, [setup(setup), cleanup(cleanup)]) :-
    provenance_for(nobody, nothing, Entries),
    Entries == [].

:- end_tests(symbolic_memory).