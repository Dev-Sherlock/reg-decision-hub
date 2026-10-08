:- module(tree_engine, [
    eval_condition/2,
    node_in/7,
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
    set_action_in/5,
    condition_readable/2,
    conditions_readable/2,
    node_timestamp/1
]).

% Domain-agnostic half of the decision-tree machinery.
%
% Nothing here knows a feature name, a threshold or an action. Every tree-bound
% predicate takes a *tree handle* and reaches its nodes through node_in/7, so the
% four example domains are data rather than four copies of this file.
%
% A tree handle is one of:
%
%   module(PrologModule)  one of the static kb/*.pl domains
%   runtime(DomainId)     a domain registered at runtime through
%                         POST /memory/domain, whose nodes are
%                         tree_registry:domain_node/7 facts
%
% node_in/7 is the ONLY goal in this module that is qualified by a runtime-bound
% module, and that is load-bearing. SWI is transparent inside a qualified goal
% when the qualifier is a variable at call time, so
%
%   Module:helper(Arg)
%
% would resolve `helper/1` against *this* module rather than the domain's. That
% is harmless for node/6, a bare dynamic fact, and would silently misbehave the
% moment node_in/7 grew a helper call. Do not add one.
%
% The condition evaluator below is unchanged from decision_tree.pl: it moved here
% verbatim so that the four domains inherit exactly the semantics test_decision.pl
% already pins down.

% trace_in/3 uses append/3, which lives in library(lists) and is not imported
% into this module by default.
:- use_module(library(lists)).
% conditions_readable/2 uses maplist/3.
:- use_module(library(apply)).

% ---------------------------------------------------------------------------
% Node access
% ---------------------------------------------------------------------------

% node_in(+Handle, ?ID, ?Parent, ?Cond, ?Action, ?Owner, ?Version)
node_in(module(Module), ID, Parent, Condition, Action, Owner, Version) :-
    Module:node(ID, Parent, Condition, Action, Owner, Version).
node_in(runtime(Domain), ID, Parent, Condition, Action, Owner, Version) :-
    tree_registry:domain_node(Domain, ID, Parent, Condition, Action, Owner, Version).

% root_in(+Handle, -Root)
%
% Derived rather than declared, so a tree cannot claim one root and be built with
% another. Exactly one node must have parent `none`; a tree with none or with two
% fails here rather than silently picking one.
root_in(Handle, Root) :-
    findall(ID, node_in(Handle, ID, none, _, _, _, _), Roots),
    Roots = [Root].

% input_keys_in(+Handle, -Keys)
%
% The keys a domain declares, used to route an incoming input map to a tree. This
% is the one thing about a domain that cannot be inferred from its nodes, so each
% domain states it.
input_keys_in(module(Module), Keys) :-
    Module:input_keys(Keys).
input_keys_in(runtime(Domain), Keys) :-
    tree_registry:domain_input_keys(Domain, Keys).

% ---------------------------------------------------------------------------
% Condition evaluator — generic, not domain-specific.
%
% eval_condition(+Condition, +Input) is nondet.
% Input is a Prolog dict (SWI-Prolog 7+ / dict library) whose keys are the
% feature names. Accepted condition forms:
%
%   true                          always succeeds
%   Key                           succeeds unless Input[Key] is false or missing
%   Key >  Value   Key <  Value   numeric or term-order comparison
%   Key =< Value   Key >= Value
%   Key =:= Value  Key =\= Value  numeric-only comparison
%   Key == Value   Key \== Value  strict unification (no type coercion)
%   Key =  Value   Key \= Value   unification
%   Cond1, Cond2                  conjunction
%   \+ Cond                       negation
%
% That makes the tree reusable: add a node whose condition mentions a new key
% and the /decide endpoint works with it without touching this file.
% ---------------------------------------------------------------------------

eval_condition(Condition, Input) :-
    is_dict(Input),
    eval_conditions(Condition, Input).

eval_conditions(true, _).
eval_conditions((CondA, CondB), Input) :-
    eval_conditions(CondA, Input),
    eval_conditions(CondB, Input).
eval_conditions(\+ Cond, Input) :-
    \+ eval_conditions(Cond, Input).

% Bare feature test: e.g. `emergency_active`
eval_conditions(Flag, Input) :-
    atom(Flag),
    eval_flag(Flag, Input).

% Operator comparison: e.g. `demand > 80`
% The functor is checked against the known comparison operators. Without that
% guard this clause also matches a conjunction, and `=..` would hand get_dict/3
% the whole `(demand > 80, humidity > 70)` term as a key — which is a type
% error, not a plain failure, and only shows up on backtracking.
eval_conditions(Constraint, Input) :-
    Constraint =.. [Op, Key, Value],
    comparison_op(Op),
    eval_comparison(Op, Key, Value, Input).

% Deterministic on purpose. numeric_comparison/1 and term_comparison/1 overlap on
% >, <, >= and =<, so a plain disjunction succeeds twice for those operators and
% every comparison condition yields duplicate solutions. That is invisible while
% callers only ask "does it hold" — the walk takes the first answer either way —
% but it re-solves each node twice per level, and it makes "which children match"
% unanswerable, which is exactly the property decide_in/4 depends on staying
% unambiguous.
comparison_op(Op) :-
    (   numeric_comparison(Op)
    ->  true
    ;   term_comparison(Op)
    ).

eval_flag(Flag, Input) :-
    get_dict(Flag, Input, Actual),
    Actual \== false.

eval_comparison(Op, Key, Value, Input) :-
    get_dict(Key, Input, Actual),
    compare_values(Op, Actual, Value).

% Compare only when both sides agree on type. A numeric threshold never
% matches a string — `demand > 80` with demand = "high" simply does not hold —
% and two strings fall back to Prolog term order. Mixed types fail the
% condition rather than raising, so a wrong-typed input yields "no decision"
% instead of a confidently wrong one.
compare_values(Op, Actual, Value) :-
    (   number(Actual), number(Value),
        numeric_comparison(Op)
    ->  apply_numeric(Op, Actual, Value)
    ;   \+ number(Actual), \+ number(Value),
        term_comparison(Op)
    ->  apply_term(Op, Actual, Value)
    ).

numeric_comparison((>)).
numeric_comparison((<)).
numeric_comparison((>=)).
numeric_comparison((=<)).
numeric_comparison((=:=)).
numeric_comparison((=\=)).

term_comparison((>)).
term_comparison((<)).
term_comparison((>=)).
term_comparison((=<)).
term_comparison((==)).
term_comparison((\==)).
term_comparison((=)).
term_comparison((\=)).

apply_numeric((>),  A, B) :- A > B.
apply_numeric((<),  A, B) :- A < B.
apply_numeric((>=), A, B) :- A >= B.
apply_numeric((=<), A, B) :- A =< B.
apply_numeric((=:=), A, B) :- A =:= B.
apply_numeric((=\=), A, B) :- A =\= B.

apply_term((>),  A, B) :- A @> B.
apply_term((<),  A, B) :- A @< B.
apply_term((>=), A, B) :- A @>= B.
apply_term((=<), A, B) :- A @=< B.
apply_term((==), A, B) :- A == B.
apply_term((\==), A, B) :- A \== B.
apply_term((=),  A, B) :- A = B.
apply_term((\=), A, B) :- A \= B.

% Numeric values are stored in JSONB and round-trip through Prolog as Prolog
% numbers, so arithmetic comparison is the expected case; the @</@> variants
% only apply when both sides are non-numeric.

% ---------------------------------------------------------------------------
% Walk
% ---------------------------------------------------------------------------

% Main decision predicate
% decide_in(+Handle, +InputDict, -LeafNode, -ProofTrace)
% InputDict is a Prolog dict whose keys are the features referenced by the
% node conditions, e.g. _{demand:90, temperature:35, humidity:50}.
%
% The walk stops at the deepest node whose condition holds. A node with
% children where no child condition holds is itself returned as the leaf, so
% a partial input dict yields the most specific answer the tree can justify
% instead of failing.
%
% once/1 is deliberate. deepest_in/4 is nondet — a node with two matching
% children has two answers — but every caller wants exactly one: the API takes
% results[0], and the domain wrappers declare decide/3 as a single answer. Leaving
% the alternatives on the choice stack changes nothing observable and makes every
% test that calls this report "succeeded with choicepoint", which reads like a
% failure and buries the real ones.
decide_in(Handle, Input, Leaf, Proof) :-
    once((
        root_in(Handle, Root),
        deepest_in(Handle, Root, Input, Leaf)
    )),
    trace_in(Handle, Leaf, Proof).

% deepest_in(+Handle, +Node, +Input, -Leaf) is nondet.
% Only genuine children are considered. Recursing with an unbound variable would
% re-enter every node — including root — and never terminate.
deepest_in(Handle, Node, Input, Leaf) :-
    node_in(Handle, Node, _, Condition, _, _, _),
    eval_condition(Condition, Input),
    (   child_leaf_in(Handle, Node, Input, Leaf)
    ->  true
    ;   Leaf = Node
    ).

% child_leaf_in(+Handle, +Node, +Input, -Leaf) is nondet.
% One solution per child whose own condition holds, resolving that child's
% deepest node. Node being bound is what bounds the search.
child_leaf_in(Handle, Node, Input, Leaf) :-
    node_in(Handle, Child, Node, ChildCondition, _, _, _),
    eval_condition(ChildCondition, Input),
    deepest_in(Handle, Child, Input, Leaf).

% ---------------------------------------------------------------------------
% Proof trace
% ---------------------------------------------------------------------------

% Proof-trace generator
% trace_in(+Handle, +NodeID, -Proof)
% Proof = [[NodeID, Condition, Action, Owner], ...] from root to NodeID.
% Each step is a 4-element LIST, not a `,`/4 term: the API client converts Prolog
% lists into JSON arrays and steps over them by index.
trace_in(Handle, NodeID, Proof) :-
    node_in(Handle, NodeID, Parent, Cond, Action, Owner, _),
    (   Parent = none
    ->  Proof = [[NodeID, Cond, Action, Owner]]
    ;   trace_in(Handle, Parent, SubProof),
        append(SubProof, [[NodeID, Cond, Action, Owner]], Proof)
    ).

% ---------------------------------------------------------------------------
% Condition parsing
% ---------------------------------------------------------------------------

% condition_readable(+Text, -Condition) is semidet.
%
% Parses condition source into a term without evaluating it. This is the gate
% POST /memory/domain puts in front of a runtime tree: a condition the evaluator
% cannot match would leave a node that silently never fires, which reads exactly
% like "the tree is correct and the inputs were wrong".
%
% The second read is the part that is easy to leave out. read_term/2 stops at the
% first term, so `demand > 80, garbage(` would parse as a usable `demand > 80` and
% the typo would be discarded rather than reported.
%
% syntax_errors(dec10), not error: with error a genuine syntax problem raises and
% takes the whole request down, when what is wanted is a validation failure the
% caller can read. dec10 reports it as a message and returns an unbound term, which
% the nonvar/1 check below rejects.
condition_readable(Text, Condition) :-
    % read_term/3 wants a terminating full stop. open_string/2 hands it a stream with
    % none, so the dot is added here; a text that already ends in one just gets two,
    % which reads the same.
    atom_concat(Text, '.', Source),
    open_string(Source, Stream),
    read_term(Stream, Condition, [syntax_errors(dec10)]),
    read_term(Stream, Rest, [syntax_errors(dec10)]),
    nonvar(Condition),
    Condition \= end_of_file,
    Rest == end_of_file.

% conditions_readable(+Texts, -Conditions) is semidet.
%
% All-or-nothing on purpose: a domain is rejected as a whole rather than half
% loaded, because a tree with one unreadable condition is a tree that will give a
% wrong answer later instead of failing now.
conditions_readable(Texts, Conditions) :-
    maplist(condition_readable, Texts, Conditions).

% ---------------------------------------------------------------------------
% Governance
% ---------------------------------------------------------------------------

% Permission rule: only the owner agent can write to their nodes
can_write_in(Handle, Agent, NodeID) :-
    node_in(Handle, NodeID, _, _, _, Owner, _),
    Owner = Agent.

% set_action_in(+Handle, +NodeID, +Action, +Owner, -Version)
%
% The only write path a governed override takes, so the retract/assert pair that
% replaces a node clause is written once here rather than once per domain module.
% Fails when the node does not exist, which is what makes the API's 404 honest
% instead of dependent on a read it did not do.
set_action_in(Handle, NodeID, Action, Owner, Version) :-
    node_in(Handle, NodeID, Parent, Condition, _, _, Current),
    Version is Current + 1,
    replace_node(Handle, NodeID, Parent, Condition, Action, Owner, Version).

% retractall rather than retract: an override must leave exactly one live clause
% for the id even if an earlier write somehow left two.
replace_node(module(Module), NodeID, Parent, Condition, Action, Owner, Version) :-
    retractall((Module:node(NodeID, _, _, _, _, _))),
    assertz((Module:node(NodeID, Parent, Condition, Action, Owner, Version))).
replace_node(runtime(Domain), NodeID, Parent, Condition, Action, Owner, Version) :-
    tree_registry:set_domain_node(Domain, NodeID, Parent, Condition, Action, Owner, Version).

% Provenance (audit view)
% provenance_in(+Handle, +NodeID, +Agent, -Timestamp, -Entry) is nondet.
% The durable audit trail lives in PostgreSQL (audit_log table, written by the
% API's override endpoint). This predicate produces the entry that gets
% written there: a snapshot of the node as its owning agent sees it.
%   Entry = node{id:NodeID, action:Action, owner:Owner, version:Version}
provenance_in(Handle, NodeID, Agent, Timestamp, Entry) :-
    node_in(Handle, NodeID, _, _, Action, Owner, Version),
    Owner = Agent,
    node_timestamp(Timestamp),
    Entry = node{id:NodeID, action:Action, owner:Owner, version:Version}.

% node_timestamp(-Timestamp) is det. UTC ISO-8601.
% format_time/4's fourth argument is a locale, not a timezone, so passing `utc`
% there raises a domain error. The container sets TZ=UTC instead.
node_timestamp(Timestamp) :-
    get_time(Stamp),
    format_time(atom(Timestamp), '%Y-%m-%dT%H:%M:%SZ', Stamp).

% ---------------------------------------------------------------------------
% Serialization helpers
% ---------------------------------------------------------------------------

% Utility: get all nodes as list of dicts for API serialization
collect_nodes_in(Handle, Nodes) :-
    findall(
        node{id:ID, parent:Parent, condition:Cond, action:Action, owner:Owner, version:Ver},
        node_in(Handle, ID, Parent, Cond, Action, Owner, Ver),
        Nodes
    ).

% Check if node is a leaf
is_leaf_in(Handle, NodeID) :-
    node_in(Handle, NodeID, _, _, _, _, _),
    \+ node_in(Handle, _, NodeID, _, _, _, _).

% Get leaf nodes
leaves_in(Handle, Leaves) :-
    findall(Leaf, (node_in(Handle, Leaf, _, _, _, _, _), is_leaf_in(Handle, Leaf)), Leaves).
