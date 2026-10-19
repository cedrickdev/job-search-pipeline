# Phase 17 — Switzerland Country Pack verification

Date: 19 October 2026

## Decision

Switzerland is the only active country. This delivery validates CH as the first
Phase 17 Country Pack and keeps the architecture ready for a separately gated
future country. No other country is registered or selectable.

## Acceptance evidence

| Phase 17 requirement | Evidence |
| --- | --- |
| Source review | `docs/SWISS_SOURCE_REVIEW_2026-10-12.md` records each reviewed source, access decision, rate-limit gap and activation restriction. |
| Terminology | `country_packs/ch/terminology.yaml` covers Swiss workplace, contract, language and legal-form vocabulary; `tests/test_v2_country_packs.py` verifies representative FR/DE/IT terms. |
| Opportunity types | `country_packs/ch/opportunity_types.yaml` maps local terms onto the universal enum and declares the supported categories. |
| Geographic validation | `country_packs/ch/metadata.yaml` owns a WGS84 centre, MapLibre zoom and Swiss bounds. `CountryMapViewport` validates coordinate ranges, zoom and centre containment. |
| Eligibility metadata | `country_packs/ch/eligibility.yaml` remains typed, country-scoped and absent from the public catalogue response. |
| Adapter tests | Phase 5 discovery adapter, registry, normalization and orchestration suites remain part of the Phase 17 regression set. |
| Documented limitations | The source review keeps unapproved live collection disabled. `docs/COUNTRY_PACKS.md` records workload, source and transport limits. |

## Delivered wiring

- `GET /api/v2/country-packs` requires an authenticated session.
- The response contains public CH presentation metadata only.
- Profile and saved-search country controls list active packs instead of free text.
- Map and company filters use the same active-country catalogue.
- The map fallback camera comes from the CH pack rather than a country constant in
  the page.
- OpenAPI and generated TypeScript types include the catalogue contract.

## Safety invariants

- No credential, source environment variable, raw eligibility rule or permit rule
  appears in the catalogue response.
- No source is activated by the existence of a Country Pack. Runtime source access
  remains constrained by the operator allow-list and the source review.
- No live job-board request or real application submission occurs in tests.
- CAPTCHA, MFA, login and ambiguous-form handling remain unchanged.

## Migrations

None. Country Pack metadata and the catalogue are configuration/API changes only.

## Remaining limitation

Phase 17 validates the CH pack and its product wiring. It does not override the
October 2026 access review: live multi-source collection remains unapproved until
authorized feeds and request budgets are recorded. A later country must pass its
own gate before registration.
