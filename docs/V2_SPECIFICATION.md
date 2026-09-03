# Job Search Pipeline — V2 Product Specification

## 1. Vision

Job Search Pipeline is an autonomous career opportunity platform that helps a user discover and pursue relevant professional opportunities across countries and opportunity types.

It must support:

- employment;
- student jobs;
- internships;
- apprenticeships;
- alternance/work-study;
- graduate programs;
- temporary work;
- configurable additional opportunity types.

The platform must not be modeled only around a `Job`. The central domain object is an `Opportunity`.

## 2. Core product outcomes

A user should be able to:

1. create an account;
2. complete an intelligent onboarding;
3. select country and geographic search area;
4. define opportunity types and constraints;
5. upload or build a canonical candidate profile;
6. start one or more autonomous search agents;
7. receive discovered opportunities from supported sources;
8. see companies and opportunities on an interactive map;
9. receive multidimensional fit and eligibility scoring;
10. receive a tailored ATS-compatible CV for each selected opportunity;
11. receive a tailored cover letter when appropriate;
12. automatically apply through supported channels;
13. track application lifecycle and recruiter responses;
14. prepare for interviews with an adaptive simulator;
15. control the platform through an AI chat;
16. learn from historical outcomes and improve future strategy.

## 3. Smart onboarding

Onboarding must feel conversational rather than like a long administrative form.

### Required information

- country of residence;
- countries in which the user wants to search;
- search center(s) and radius or whole-country search;
- remote/hybrid/on-site preferences;
- target opportunity types;
- target roles/categories;
- industries;
- availability;
- maximum weekly work hours where relevant;
- student status where relevant;
- work authorization / permit constraints;
- languages and levels;
- start date;
- salary expectations where relevant;
- commuting preferences;
- willingness to relocate;
- profile/CV upload;
- links to portfolio/GitHub/GitLab/LinkedIn where the user chooses to provide them.

### Output

The onboarding creates a `CandidateProfile`, one or more `SearchProfile` records, and a `SearchPolicy`.

## 4. Candidate Career Twin

The platform must maintain a structured candidate representation containing:

- experiences;
- education;
- projects;
- skills;
- languages;
- certifications;
- availability;
- work authorization;
- geographic preferences;
- compensation preferences;
- evidence for claims.

Each claim used by an LLM in application material must be traceable to candidate evidence.

### Evidence rule

The system must never fabricate:

- technologies;
- years of experience;
- employers;
- responsibilities;
- certifications;
- academic achievements;
- languages;
- numerical results.

The current V1 deterministic CV truth checks should be preserved and generalized into an `EvidenceGuard`.

## 5. Country Intelligence

Country-specific behavior must be represented by Country Packs, not hard-coded conditionals in business logic.

A Country Pack may define:

- terminology;
- opportunity types;
- supported public job sources;
- ATS conventions;
- languages;
- geographic hierarchy;
- salary/currency conventions;
- employment metadata relevant to eligibility;
- source-specific policies/capabilities.

Example:

`country_packs/ch/`
- metadata.yaml
- terminology.yaml
- opportunity_types.yaml
- sources.yaml
- eligibility.yaml

Country Packs contain configuration and adapters. They must not contain user data.

## 6. Company Discovery Engine

The platform must discover companies as first-class entities, not only job-board listings.

For a search profile, the system may:

1. discover relevant companies in the geographic area;
2. identify official websites;
3. identify careers pages;
4. detect ATS providers when possible;
5. fetch supported opportunities;
6. record whether a spontaneous application channel exists.

Core entities:

- `Company`
- `CompanyLocation`
- `CareerSite`
- `DetectedATS`

## 7. Opportunity Discovery Engine

Discovery sources must use a plugin contract.

Sources can include:

- official/public APIs;
- ATS feeds;
- company career pages;
- public job boards where technically and contractually supported;
- partner feeds;
- permitted HTML extraction.

Do not design the product around bypassing anti-bot controls or provider restrictions.

Each source advertises capabilities such as:

- countries;
- discovery support;
- detail fetching;
- application support;
- authentication needs;
- rate-limit strategy;
- policy status;
- health status.

### Normalized Opportunity

At minimum:

- id;
- source;
- external id;
- company;
- title;
- description;
- opportunity type;
- location;
- geographic coordinates if known;
- workplace mode;
- contract type;
- workload/percentage;
- salary if available;
- languages;
- posted date;
- discovered date;
- source URL;
- application URL;
- deduplication fingerprint.

## 8. Geo Opportunity Explorer

A map is a core product surface, not decoration.

### Search modes

- entire country;
- radius around one or more locations;
- remote-only;
- later: custom polygon.

### Map objects

- companies;
- open opportunities;
- saved opportunities;
- submitted applications;
- interviews;
- spontaneous-application targets.

### User interactions

- pan/zoom;
- clustering;
- search radius;
- filters;
- fit threshold;
- eligibility threshold;
- opportunity type;
- workplace mode;
- application status;
- company card;
- opportunity card.

### Geospatial backend

Target stack: PostgreSQL + PostGIS.

The system should support efficient radius and bounding-area queries and spatial indexes.

## 9. Matching Engine

A single unexplained score is insufficient.

The scoring model should expose dimensions such as:

- overall fit;
- skills fit;
- experience fit;
- education fit;
- language fit;
- geographic fit;
- schedule fit;
- work-authorization eligibility;
- opportunity-type eligibility;
- evidence confidence.

Each score must include human-readable reasons.

Hard eligibility failures must be separated from soft matching signals.

## 10. Schedule Compatibility

Especially for student jobs and part-time work, the system must compare:

- user availability;
- maximum allowed weekly hours;
- required shifts;
- workload percentage;
- commute constraints.

A role can have high technical fit but low schedule compatibility.

## 11. ATS Resume Engine

For each selected opportunity, the system may generate an application-specific CV from a canonical candidate profile.

Requirements:

- ATS-friendly structure;
- opportunity-specific prioritization;
- keyword alignment without keyword stuffing;
- deterministic evidence validation;
- version history;
- reproducible generation metadata;
- language-aware output;
- PDF rendering;
- no fabricated facts.

Recommended generation flow:

Canonical Profile
→ Opportunity Analysis
→ Content Selection
→ LLM Rewriting
→ Evidence Guard
→ ATS Structural Checks
→ Render
→ Version Registration

## 12. Cover Letter Engine

The cover letter must be:

- tailored to the role;
- tailored to the company where enough verified context exists;
- grounded in candidate evidence;
- optional when not useful;
- versioned;
- stored as an application artifact.

## 13. Application Strategy Engine

The platform chooses an action instead of blindly applying to every listing.

Possible decisions:

- `SKIP`
- `SAVE`
- `PREPARE`
- `REQUIRE_REVIEW`
- `AUTO_APPLY`
- `APPLY_AND_OUTREACH`
- `SPONTANEOUS_APPLICATION`

The decision depends on:

- fit;
- eligibility;
- user policy;
- source capabilities;
- application risk;
- daily/weekly limits;
- missing required information.

## 14. Application Engine

The engine must route an application through the safest supported channel.

Potential channels:

- official API;
- ATS adapter;
- direct public form;
- browser automation via Playwright;
- email application;
- manual-required;
- unsupported.

Playwright is an execution adapter, not the domain brain.

Every submission must be auditable:

- timestamp;
- artifacts used;
- answers supplied;
- adapter;
- result;
- screenshots where useful;
- failure reason.

## 15. Autopilot and Copilot

### Autopilot

The system can automatically perform actions permitted by the user's policy.

### Copilot

The system prepares actions but requires user approval before irreversible submission.

Policy is user-configurable per search profile.

## 16. Application Workspace

Each opportunity/application gets a workspace with:

- overview;
- job/company details;
- scoring;
- CV versions;
- cover letters;
- answers;
- application timeline;
- interview preparation;
- chat;
- source metadata;
- audit events.

## 17. Interview Intelligence

V1 interview-prep generation should evolve into an adaptive simulator.

Modes:

- recruiter/HR;
- behavioral;
- technical;
- hiring manager;
- case study;
- final interview.

The simulator must:

1. generate role-specific questions;
2. accept text or voice answers;
3. evaluate clarity, relevance and completeness;
4. ask adaptive follow-ups;
5. give evidence-based coaching;
6. track readiness over time.

## 18. AI Career Chat

The chat is the natural-language control plane.

It must be able to explain and, subject to authorization, trigger typed actions.

Examples:

- explain why an opportunity was skipped;
- change search radius;
- disable a category;
- regenerate a CV;
- prepare interview questions;
- summarize the current funnel;
- change autopilot policy.

LLM output must never directly mutate state. It proposes typed commands that application services validate.

## 19. Outcome Tracking

Application state should support a lifecycle such as:

- discovered;
- analyzed;
- matched;
- prepared;
- approved;
- submitted;
- acknowledged;
- recruiter response;
- phone screen;
- interview;
- assessment;
- final interview;
- rejected;
- withdrawn;
- offer;
- accepted.

Every transition produces an immutable audit event.

## 20. Career Intelligence Loop

The system should learn from outcomes without silently changing user policy.

Potential analytics:

- response rate by role family;
- interview rate by CV strategy;
- response rate by source;
- response rate by application timing;
- outcome by geographic region;
- fit-score calibration;
- skills repeatedly causing rejection or low fit.

The system can recommend strategy changes, but important policy changes require user approval.

## 21. SaaS requirements

V2 is multi-user.

Required future capabilities:

- authentication;
- user-scoped data isolation;
- subscriptions/plans;
- usage quotas;
- encrypted credentials;
- audit logs;
- billing integration;
- GDPR-aware data lifecycle;
- export/delete account data.

## 22. Non-goals

V2 must not:

- fabricate candidate claims;
- bypass CAPTCHA or MFA;
- intentionally evade platform protections;
- apply outside user-defined policy;
- hide failed source health;
- couple business logic to a single LLM;
- assume all countries share the same opportunity terminology.
