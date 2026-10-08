% ---------------------------------------------------------------------------
% symbolic_memory.pl — universal symbolic memory for the API.
%
% This module is domain-agnostic. It knows how to store, query, derive and
% audit arbitrary subject/predicate/value triples and user-defined rules. The
% energy dispatch tree in decision_tree.pl is just one example domain that
% happens to live next to this file.
%
%   fact(Subject, Predicate, Value)
%       Subject   - atom, identifies the thing (room_101, person_john, n1)
%       Predicate - atom, names the property (temperature, works_at, action)
%       Value     - any Prolog term. The Python layer restricts this to
%                   JSON-representable values: atom/string, number, boolean,
%                   list, dict, or null.
%
%   rule(Name, Head, Body)
%       Head - a goal template, conventionally fact(Subject, Predicate, Value)
%       Body - list of goal templates evaluated against the current KB
%       Variables shared between Head and Body are the rule's parameters, so
%         rule(spreads, fact(X, knows, Y), [fact(X, works_at, Y)])
%       derives new knowledge by copying the pair before solving, which keeps
%       each derivation independent.
%
% Uniqueness: a fact is uniquely identified by (Subject, Predicate), matching
% the symbolic_facts primary key in PostgreSQL. Asserting an existing pair is
% an UPDATE, not a duplicate insert.
%
% All predicates are intended to be called qualified from Python, e.g.
%   symbolic_memory:assert_fact('room_101', temperature, 22)
% ---------------------------------------------------------------------------

:- module(symbolic_memory, [
    fact/3,
    rule/3,
    provenance_entry/6,

    assert_fact/3,
    retract_fact/3,
    query_facts/3,
    all_facts/4,
    get_fact/3,
    add_rule/3,
    get_rule/3,
    all_rules/3,
    run_rule/2,
    derive/1,
    load_fact/3,
    add_rule_from_text/3,
    retract_rule/1,

    log_provenance/5,
    provenance/6,
    provenance_for/3,

    clear_facts/0,
    clear_rules/0,
    clear_provenance/0,
    fact_count/1
]).

% These three hold the memory itself and are never given static clauses — every
% fact arrives from PostgreSQL at startup or from the API at runtime. Declaring
% them dynamic is what makes them exist: without it SWI-Prolog refuses to load
% the module, because an exported predicate with no clauses is undefined.
:- dynamic fact/3.
:- dynamic rule/3.
:- dynamic provenance_entry/6.

% ---------------------------------------------------------------------------
% Storage
% ---------------------------------------------------------------------------

% fact(Subject, Predicate, Value).
% Asserted facts live in this module. Nothing is written here at load time:
% the API bootstraps them from PostgreSQL on startup (see PrologClient.load_from_db).

% rule(Name, Head, Body).
% User-defined inference rules, also bootstrapped from PostgreSQL.

% provenance_entry(Subject, Predicate, Timestamp, ChangeType, OldValue, NewValue).
% Append-only audit trail inside the Prolog KB. PostgreSQL keeps the durable copy.

% ---------------------------------------------------------------------------
% Writing facts
% ---------------------------------------------------------------------------

% assert_fact(+Subject, +Predicate, +Value) is det.
% Upsert semantics: if a fact with the same (Subject, Predicate) already
% exists it is replaced and the change is audited as UPDATE, otherwise the
% change is audited as INSERT.
assert_fact(Subject, Predicate, Value) :-
    nonvar(Subject),
    nonvar(Predicate),
    nonvar(Value),
    (   fact(Subject, Predicate, OldValue)
    ->  ChangeType = update
    ;   ChangeType = insert,
        OldValue = none
    ),
    % Silent replacement. retract_fact/3 would append its own DELETE entry and
    % make every update look like a delete followed by an insert.
    retract_all_facts(Subject, Predicate),
    assertz(fact(Subject, Predicate, Value)),
    log_provenance(Subject, Predicate, ChangeType, OldValue, Value).

% retract_fact(+Subject, +Predicate, -Value) is nondet.
% Value may be unbound, in which case every value for the pair is retracted.
% Returns one solution per removed fact. Audits a delete for each one.
retract_fact(Subject, Predicate, Value) :-
    nonvar(Subject),
    nonvar(Predicate),
    fact(Subject, Predicate, Value),
    retract(fact(Subject, Predicate, Value)),
    log_provenance(Subject, Predicate, delete, Value, none).

% ---------------------------------------------------------------------------
% Reading facts
% ---------------------------------------------------------------------------

% query_facts(?Subject, ?Predicate, ?Value) is nondet.
% The generic query interface. Leave any argument unbound to wildcard it —
%   query_facts(room_101, temperature, V)   % every temperature in room_101
%   query_facts(S, _, _)                      % every fact, every subject
query_facts(Subject, Predicate, Value) :-
    fact(Subject, Predicate, Value).

% all_facts(?Subject, ?Predicate, ?Value, ?Facts) is det.
% Deterministic wrapper around query_facts/3 that returns the whole result set
% as a list of [Subject, Predicate, Value] triples in a single solution.
% This is what the API layer binds against, so a caller never has to page
% through the interpreter's solution-by-solution iteration.
% The pattern arguments FILTER but do not bind: they are unified inside a
% findall, which discards bindings made within it.
all_facts(Subject, Predicate, Value, Facts) :-
    copy_term(Subject-Predicate-Value, Subject0-Predicate0-Value0),
    findall([S, P, V],
            ( query_facts(S, P, V),
              unify_if_ground(Subject0, S),
              unify_if_ground(Predicate0, P),
              unify_if_ground(Value0, V)
            ),
            Facts).

% unify_if_ground(+Pattern, ?Term): binds Term to Pattern, or succeeds
% unchanged when Pattern is unbound (wildcard).
unify_if_ground(Pattern, Term) :-
    (   var(Pattern)
    ->  true
    ;   Pattern = Term
    ).

% get_fact(+Subject, +Predicate, ?Value) is nondet.
get_fact(Subject, Predicate, Value) :-
    fact(Subject, Predicate, Value).

% fact_count(-Count) is det.
fact_count(Count) :-
    findall(S-P-V, fact(S, P, V), Facts),
    length(Facts, Count).

% ---------------------------------------------------------------------------
% Rules
% ---------------------------------------------------------------------------

% add_rule(+Name, +Head, +Body) is det.
% Head is a goal template, Body is a list of goal templates. Redefining an
% existing name replaces the old definition.
add_rule(Name, Head, Body) :-
    nonvar(Name),
    nonvar(Head),
    is_list(Body),
    retract_all_rules(Name),
    assertz(rule(Name, Head, Body)).

retract_all_rules(Name) :-
    forall(retract(rule(Name, _, _)), true).

% get_rule(+Name, ?Head, ?Body) is nondet.
get_rule(Name, Head, Body) :-
    rule(Name, Head, Body).

% all_rules(?Name, ?Head, ?Body) is nondet.
all_rules(Name, Head, Body) :-
    rule(Name, Head, Body).

% load_fact(+Subject, +Predicate, +Value) is det.
% Asserts a fact without touching the audit trail. This is the bootstrap path:
% reloading a hundred facts from PostgreSQL on every start must not append a
% hundred spurious INSERT entries to provenance. The PostgreSQL row is the
% record of when the fact was first written.
load_fact(Subject, Predicate, Value) :-
    nonvar(Subject),
    nonvar(Predicate),
    nonvar(Value),
    retract_all_facts(Subject, Predicate),
    assertz(fact(Subject, Predicate, Value)).

retract_all_facts(Subject, Predicate) :-
    forall(retract(fact(Subject, Predicate, _)), true).

% add_rule_from_text(+Name, +HeadText, +BodyText) is det.
% Builds the Head and Body terms by reading them as Prolog source, so callers
% can pass rule definitions as text:
%   add_rule_from_text(colleagues,
%                      "fact(X, colleague_of, Y)",
%                      "[fact(X, works_at, Y)]")
% read_term_from_atom/3 keeps the variables in Head and Body consistent because
% they are read into one term, so the shared variables survive.
add_rule_from_text(Name, HeadText, BodyText) :-
    format(atom(Combined), '((~w) :- ~w)', [HeadText, BodyText]),
    read_term_from_atom(Combined, (Head :- Body), []),
    add_rule(Name, Head, Body).

% retract_rule(+Name) is det.
retract_rule(Name) :-
    retract_all_rules(Name).

% run_rule(+Name, ?Triple) is nondet.
% Applies a named rule and yields one ground derived fact per solution as
% [Subject, Predicate, Value] — the same shape all_facts/4 uses, so a caller
% handles stored and derived facts with one representation. The rule head is
% written as fact(Subject, Predicate, Value), which is what the text form in
% add_rule_from_text/3 documents, and is unpacked here.
% The derived facts are NOT stored — the caller decides whether to persist
% them, so a dry run stays side-effect free.
run_rule(Name, Triple) :-
    rule(Name, Head, Body),
    copy_term(Head-Body, Head1-Body1),
    derive(Body1),
    % The head is unified with a fresh term, never called: calling it would
    % only succeed if the conclusion were already a stored fact, which is exactly
    % what the rule is being asked to derive.
    Head1 = fact(Subject, Predicate, Value),
    Triple = [Subject, Predicate, Value],
    term_variables(Triple, []).

% derive(+Goals) is nondet.
% Solves a list of goal templates against the current KB.
derive([]).
derive([Goal|Goals]) :-
    call(Goal),
    derive(Goals).

% ---------------------------------------------------------------------------
% Provenance / audit trail
% ---------------------------------------------------------------------------

% log_provenance(+Subject, +Predicate, +ChangeType, +OldValue, +NewValue) is det.
log_provenance(Subject, Predicate, ChangeType, OldValue, NewValue) :-
    timestamp(Timestamp),
    assertz(provenance_entry(
        Subject, Predicate, Timestamp, ChangeType, OldValue, NewValue
    )).

% provenance(?Subject, ?Predicate, ?Timestamp, ?ChangeType, ?OldValue, ?NewValue)
% is nondet. Generic access to the audit trail; wildcard anything unbound.
provenance(S, P, T, C, O, N) :-
    provenance_entry(S, P, T, C, O, N).

% provenance_for(+Subject, +Predicate, ?Entries) is det.
% Chronological audit history for one fact, newest last. Entries are
% [Timestamp, ChangeType, OldValue, NewValue] lists.
provenance_for(Subject, Predicate, Entries) :-
    findall([T, C, O, N],
            provenance_entry(Subject, Predicate, T, C, O, N),
            Entries).

% timestamp(-Timestamp) is det. UTC ISO-8601, e.g. '2026-10-02T09:15:00Z'.
% The 'Z' is only truthful if the container runs in UTC, which Dockerfile.api
% sets via TZ=UTC: format_time/3 formats local time and SWI 9.x has no
% stamp_date_time/4 timezone argument to convert it.
timestamp(Timestamp) :-
    get_time(Stamp),
    format_time(atom(Timestamp), '%Y-%m-%dT%H:%M:%SZ', Stamp).

% ---------------------------------------------------------------------------
% Test / bootstrap helpers
% ---------------------------------------------------------------------------

clear_facts :-
    forall(retract(fact(_, _, _)), true).

clear_rules :-
    forall(retract(rule(_, _, _)), true).

clear_provenance :-
    forall(retract(provenance_entry(_, _, _, _, _, _)), true).