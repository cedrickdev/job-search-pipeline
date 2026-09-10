"""Company discovery: who employs people, independently of who is hiring today.

The package Phase 6 adds beside `backend.app.discovery`. The two are deliberately
separate abstractions (§6): a `CompanyDiscoveryProvider` answers "which employers
exist?" and an `OpportunitySource` answers "what is open right now?". Forcing one
protocol to do both would mean every provider had to invent postings it has never
seen, and every source had to promise a company identity it cannot resolve.

Modules, in dependency order — each imports only the ones before it:

- `identity` — deterministic name and domain normalization, and the evidence-based
  comparison that decides whether two records describe one employer (§2, §3).
- `ats` — detecting Greenhouse/Lever/Ashby from URLs and configuration, with the
  typed confidence §10 requires.
- `contracts` — `CompanyDiscoveryProvider`, seeds, candidates, requests, results.
- `registry` — which providers exist, and which a country may use (§18).
- `resolution` — `Opportunity.company_name` → a canonical `company_id` (§13, §14).
- `orchestrator` — running providers concurrently, isolating their failures (§26).
- `providers/` — the implementations. None of them makes a network request.
- `bootstrap` — the one place that names a provider (§18).

Persistence lives outside: adapters discover, application services decide what to
store (§17). Nothing in this package opens a session.
"""
