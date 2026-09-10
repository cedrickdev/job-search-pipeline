"""The company discovery providers this deployment ships.

Three, and the boundary they share is the one §7 draws: **none of them touches the
network.** `configured_ats` reads V1's own `config/companies.yaml`, `stored_opportunities`
reads postings already in the database, and `manual_seed` reads what an operator handed
the process. "Do not implement arbitrary internet crawling in this phase" is not a rule
these modules follow carefully — there is no HTTP client in the package, so there is no
code path that could.

Registered by `backend.app.companies.bootstrap` and nowhere else. Nothing in
`registry.py` or `orchestrator.py` imports this package, which is what makes §18's
"no hard-coded provider list" checkable rather than aspirational.
"""
