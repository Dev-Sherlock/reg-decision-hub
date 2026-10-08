:- module(tree_registry, [
    domain/3,
    domains/1,
    active_domain_ids/1,
    domain_active/1,
    domain_input_keys/2,
    domain_module/2,
    domain_node/7,
    domain_label/2,
    domain_source/2,
    set_domain_node/7,
    register_domain/4,
    clear_runtime_domain/1,
    clear_runtime_domains/0,
    activate_domain/1,
    deactivate_domain/1
]).

% Single source of truth for which decision domains exist.
%
% Every string that reaches a Prolog goal in *module position* comes from domain/3
% below and from nowhere else. Nothing in the API may take a module name from a
% request: this file is the trust boundary, and a domain that is not registered
% here does not exist as far as the engine is concerned.
%
% The four static domains are the examples. They live in their own .pl modules
% because they are code, and code is not something a request may generate. A
% domain created through POST /memory/domain is different in kind: its nodes are
% domain_node/7 facts here, so it is auditable, reversible and reloadable.
%
% Node ids are globally unique across every domain, which is what lets
% /tree/{node_id} and the audit lookup resolve a bare id without being told which
% domain it belongs to. A new domain that reuses `root` or `n*` silently breaks
% both. Prefix every id.

% ---------------------------------------------------------------------------
% Static domains
% ---------------------------------------------------------------------------

% domain(+Id, -Handle, -Label)
%
% Handle is the tree handle tree_engine understands: module(PrologModule) for a
% static domain, runtime(Id) for one registered at runtime.
domain(energy,        module(decision_tree), 'Energy dispatch').
domain(air_traffic,  module(air_traffic),    'Air-traffic go/no-go').
domain(manufacturing, module(manufacturing),  'Manufacturing lot release').
domain(cyber,         module(cyber),          'Cyber incident severity').

% tree_registry:assert_node/2 needs maplist/3.
:- use_module(library(apply)).

% ---------------------------------------------------------------------------
% Runtime domains
%
% POST /memory/domain validates a node spec and writes these facts; it does not
% activate them. POST /memory/domain/{id}/activate does, and both are audited.
% ---------------------------------------------------------------------------

:- dynamic domain_node_fact/7.
:- dynamic domain_input_keys_fact/2.
:- dynamic domain_meta/3.
:- dynamic domain_enabled/1.

% domain_node(+DomainId, +NodeId, +Parent, +Cond, +Action, +Owner, +Version)
domain_node(Domain, ID, Parent, Condition, Action, Owner, Version) :-
    domain_node_fact(Domain, ID, Parent, Condition, Action, Owner, Version).

% domain_input_keys(+DomainId, -Keys)
%
% The keys a domain declares, used to route an incoming input map to a tree. This
% is the one thing about a domain that cannot be inferred from its nodes, so it is
% stated rather than derived.
domain_input_keys(Domain, Keys) :-
    domain_input_keys_fact(Domain, Keys).

% register_domain(+Id, +Label, +Keys, +Nodes)
%
% Writes a runtime domain inactive. Each node spec is six fields —
% [NodeId, Parent, Condition, Action, Owner, Version] — which assert_node/2 unpacks
% into the seven-argument fact, with the domain id coming from here so it cannot be
% spelled differently in two places.
%
% Any previous registration of the same id is dropped first, so a re-registration
% cannot inherit stale node ids. That means a failure part-way leaves no domain
% rather than a partial one, which is why the client treats "no solution" as an error.
register_domain(Id, Label, Keys, Nodes) :-
    clear_runtime_domain(Id),
    assert_nodes(Id, Nodes),
    assertz(domain_input_keys_fact(Id, Keys)),
    assertz(domain_meta(Id, label, Label)).

% maplist/3 rather than forall/2: forall/2 succeeds whether or not the goal did, so a
% spec of the wrong arity would be silently skipped and the domain would come back
% registered but short a node — which then answers nothing for the branch that node
% was supposed to decide. Here a bad spec fails the whole registration.
assert_nodes(Domain, Specs) :-
    maplist(assert_node(Domain), Specs).

assert_node(Domain, [NodeID, Parent, Condition, Action, Owner, Version]) :-
    assertz(domain_node_fact(Domain, NodeID, Parent, Condition, Action, Owner, Version)).

clear_runtime_domain(Id) :-
    retractall(domain_node_fact(Id, _, _, _, _, _, _)),
    retractall(domain_input_keys_fact(Id, _)),
    retractall(domain_meta(Id, _, _)),
    retractall(domain_enabled(Id)).

% clear_runtime_domains — drop every runtime domain.
%
% For test isolation. The four static domains are code and are deliberately not
% touched: this predicate says "forget what callers registered", not "reset the KB".
% Without it a domain registered by one test stays routable for every test after it,
% because Prolog has no transactions to roll back with.
clear_runtime_domains :-
    findall(Id, domain_meta(Id, _, _), Ids),
    forall(member(Id, Ids), clear_runtime_domain(Id)).

activate_domain(Id) :-
    retractall(domain_enabled(Id)),
    assertz(domain_enabled(Id)).

% deactivate_domain(+Id)
%
% The inverse of activate_domain/1: the domain stays registered — its
% nodes, keys and label remain readable, and it still shows in /trees —
% but domain_enabled/1 is gone, so active_domain_ids/1 stops listing it
% and /decide will not route to it. Deactivating an already-inactive
% domain is a no-op, which is what makes the route idempotent.
deactivate_domain(Id) :-
    retractall(domain_enabled(Id)).

% set_domain_node(+DomainId, +NodeID, +Parent, +Condition, +Action, +Owner, +Version)
%
% Called by tree_engine:replace_node/7 for a runtime domain, so the retract/assert
% pair that replaces a node clause stays next to the facts it rewrites. Parent and
% Condition are passed in rather than read back because the clause is already gone
% by then.
set_domain_node(Domain, NodeID, Parent, Condition, Action, Owner, Version) :-
    retractall(domain_node_fact(Domain, NodeID, _, _, _, _, _)),
    assertz(domain_node_fact(Domain, NodeID, Parent, Condition, Action, Owner, Version)).

% ---------------------------------------------------------------------------
% Queries
% ---------------------------------------------------------------------------

% A static domain is active from startup because it is code that shipped in the
% image. A runtime domain is inert until someone activates it, which is what stops
% a half-validated POST from becoming routable.
domain_active(Id) :-
    domain(Id, _, _).
domain_active(Id) :-
    domain_enabled(Id).

% active_domain_ids(-Ids) — the domains /decide may route to.
active_domain_ids(Ids) :-
    findall(Id, domain_active(Id), All),
    sort(All, Ids).

% domains(-Summaries) — every known domain, active or not, for the UI picker.
%   Summary = domain{id:Id, label:Label, active:Active, source:Source}
%
% Two clauses rather than one guarded by an if-then-else: a KB normally has several
% static domains *and* possibly several runtime ones, and findall/3 needs a solution
% per domain. An if-then-else here would commit on the first static domain it found
% and silently report only that one.
domains(Summaries) :-
    findall(Summary, domain_summary(Summary), Raw),
    sort(Raw, Summaries).

domain_summary(domain{id:Id, label:Label, active:true, source:kb}) :-
    domain(Id, _, Label).

domain_summary(domain{id:Id, label:Label, active:Active, source:runtime}) :-
    domain_meta(Id, label, Label),
    (   domain_enabled(Id) -> Active = true ; Active = false ).

% domain_module(+Id, -PrologModule)
%
% The module a static domain's nodes live in. Fails for a runtime domain, whose
% nodes are domain_node/7 facts here instead — which is why callers must go
% through this rather than pattern-matching the handle in Python.
domain_module(Id, Module) :-
    domain(Id, module(Module), _).

domain_label(Id, Label) :-
    domain_meta(Id, label, Label), !.
domain_label(Id, Label) :-
    domain(Id, _, Label).

% domain_source(+Id, -Source)
%
% `kb` for the four modules that ship in the image, `runtime` for a domain
% registered through the API. Reported so a reader can tell shipped code from
% mutable state without guessing.
domain_source(Id, kb) :-
    domain(Id, _, _), !.
domain_source(Id, runtime) :-
    domain_meta(Id, _, _).
