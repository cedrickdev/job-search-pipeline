// The `/api/v2` payload types — derived, not hand-written.
//
// This file is the counterpoint to types/domain.ts, and the difference is the
// backend's, not a change of taste. The V1 routes declare no `response_model`, so
// FastAPI documents each 200 as an empty schema and openapi-typescript can only
// type them `unknown` — which is why domain.ts spells V1's responses out by hand.
// Every V2 route declares one (backend/app/api/schemas.py), so the generated
// document already carries the exact field list and these are aliases into it.
//
// The practical consequence: a field renamed or dropped on the backend fails
// `nuxt typecheck` here after `npm run gen:api`, and `npm run check:api` fails in
// CI if the committed document has drifted from the application. Nothing below is
// a second source of truth, which is the whole reason to prefer aliases over a
// second set of interfaces.
//
// One thing is deliberately *not* here: a type for the session or CSRF token.
// Neither ever appears in a response body — they arrive as `Set-Cookie` headers,
// and the session cookie is `HttpOnly` and unreadable by this app on purpose
// (docs/AUTHENTICATION.md §The two cookies). There is nothing to model.
import type { components } from '~/types/api'

type Schemas = components['schemas']

/** The whole of what a client learns about its own account. No credentials. */
export type Account = Schemas['AccountResponse']

/** The current session's window: issued, expires, last seen. No id, no digest. */
export type SessionWindow = Schemas['SessionResponse']

/** The reply to register, login and `GET /auth/session`. */
export type SignedIn = Schemas['SignedInResponse']

/** A candidate profile as submitted: no id, no owner, no timestamps. */
export type CandidateProfileDraft = Schemas['CandidateProfileDraft']

/** A saved profile, echoed back with its id and `updated_at`. */
export type CandidateProfile = Schemas['CandidateProfileResponse']

/** A saved search as submitted. `areas` needs at least one entry. */
export type SearchProfileDraft = Schemas['SearchProfileDraft']

/** A saved search, echoed back with its id and timestamps. */
export type SearchProfile = Schemas['SearchProfileResponse']

/** The list wrapper — every V2 response is an object, never a bare array. */
export type SearchProfileList = Schemas['SearchProfileListResponse']

/** Which onboarding step to show, and whether finishing is allowed. */
export type OnboardingState = Schemas['OnboardingStateResponse']

/** One employer, as the directory describes it. Shared: no owner, no `user_id`. */
export type Company = Schemas['CompanyResponse']

/** One page of employers, with the window it came from. */
export type CompanyList = Schemas['CompanyListResponse']

/** One employer with its aliases, its careers endpoints and its provenance. */
export type CompanyDetail = Schemas['CompanyDetailResponse']

/** What a discovery pass did: what ran, what was written, what stayed ambiguous. */
export type CompanyDiscoveryRun = Schemas['CompanyDiscoveryRunResponse']

/** Which ATS an employer publishes on. A closed set — Phase 6 supports three. */
export type AtsPlatform = Schemas['AtsPlatform']

/** `SUPPORTED | NOT_SUPPORTED | UNKNOWN`. The third is not a missing answer. */
export type SpontaneousSupport = Schemas['SpontaneousApplicationSupport']

export type Location = Schemas['Location']
export type LanguageProficiency = Schemas['LanguageProficiency']
export type LanguageLevel = LanguageProficiency['level']

/** One geographic area of a saved search: a country, a radius, or remote-only. */
export type SearchArea = SearchProfileDraft['areas'][number]

// --- Phase 8: geo explorer (docs/GEO_SEARCH.md, docs/MAP_EXPLORER.md) ---------
//
// The Phase 7 API answers three geo reads. Every type below is an alias into the
// generated document, for the same reason the company types are: a field renamed on
// the backend fails `nuxt typecheck` here after `npm run gen:api`, so the map is
// never coding against a shape the API no longer returns.

/** One page of opportunities placed in space: `{ opportunities, limit, offset }`. No `total`. */
export type OpportunityGeoResponse = Schemas['OpportunityGeoResponse']

/** One opportunity as the geo read returns it: its `location` (and `point`) may be null. */
export type OpportunityGeoItem = Schemas['OpportunityGeoItemResponse']

/** One page of employers placed in space: `{ companies, limit, offset }`. No `total`. */
export type CompanyGeoResponse = Schemas['CompanyGeoResponse']

/** One employer with its located office. Here `location` is non-null, but `point` may be. */
export type CompanyGeoItem = Schemas['CompanyGeoItemResponse']

/** A resolved place: its `point`, its precision, its provenance. `point` may be null. */
export type GeoLocation = Schemas['GeoLocationResponse']

/** A coordinate. `{ latitude, longitude }` — the order the API uses, not GeoJSON's. */
export type GeoPoint = Schemas['GeoPointResponse']

/** `RESOLVED | COMPANY_FALLBACK | REMOTE | UNRESOLVED` — "I cannot place this" is a value. */
export type GeoStatus = Schemas['GeoStatus']

/** How far a remote role reaches. `HYBRID` is here on purpose — it is judged by distance. */
export type RemoteScope = Schemas['RemoteScope']

/** What the `remote` query param accepts: `exclude | include | only` (lower-case, on the wire). */
export type RemoteSelection = Schemas['RemoteSelection']

/** A closed set of employment types the `opportunity_type` filter accepts. */
export type OpportunityType = Schemas['OpportunityType']

/** `ON_SITE | HYBRID | REMOTE` — the `workplace_mode` filter, distinct from remote scope. */
export type WorkplaceMode = Schemas['WorkplaceMode']

/** The contract shape of an opportunity, when the source stated one. */
export type ContractType = Schemas['ContractType']

/** Which configured radius an item fell inside, so one marker can name the circle it matched. */
export type MatchedRadius = Schemas['MatchedRadiusResponse']

/** How precisely a place is known — drives whether a pin claims an address or a city. */
export type LocationPrecision = Schemas['LocationPrecision']

/** Where a place came from: the source, the geocoder, or a human. */
export type LocationProvenance = Schemas['LocationProvenance']

/** The geocoder's own confidence in a match, when it reported one. */
export type GeocodingConfidence = Schemas['GeocodingConfidence']

// --- Phase 10: candidate evidence and ATS documents --------------------------
//
// The write side of the truth guarantee (the evidence store) and the read side of
// what rests on it (the generated résumé and cover letter). Every type is an alias
// into the generated document, like the company and geo types above: a field the
// backend renames fails `nuxt typecheck` here after `npm run gen:api`, so the UI is
// never coding against a shape the API no longer returns
// (docs/CANDIDATE_EVIDENCE.md, docs/ATS_DOCUMENTS.md).

/** One attested fact on the candidate's profile: what it says and where it came from. */
export type CandidateEvidence = Schemas['CandidateEvidenceResponse']

/** One claim the platform may state, and the evidence ids it rests on (never empty). */
export type CandidateClaim = Schemas['CandidateClaimResponse']

/** The whole attested record: `{ evidence, claims }`, both echoed with their ids. */
export type CandidateEvidenceList = Schemas['CandidateEvidenceListResponse']

/** One evidence record as submitted: no id, no owner, no `recorded_at`. */
export type AddEvidenceRequest = Schemas['AddEvidenceRequest']

/** One claim as submitted: its type, label and the evidence ids it cites. */
export type AddClaimRequest = Schemas['AddClaimRequest']

/** What sort of record backs a fact — `CV_BULLET`, `DIPLOMA`, `PERMIT_DOCUMENT`, … */
export type EvidenceKind = Schemas['EvidenceKind']

/** Which pipeline filed an evidence record — never an `LLM_GENERATED`, by design. */
export type EvidenceProvenance = Schemas['EvidenceProvenance']

/** The kind of assertion a claim makes — `SKILL`, `EXPERIENCE`, `EDUCATION`, … */
export type ClaimType = Schemas['ClaimType']

/** A candidate document across its versions — one per posting, of one type. */
export type CandidateDocument = Schemas['CandidateDocumentResponse']

/** This account's documents, most recently updated first. */
export type CandidateDocumentList = Schemas['CandidateDocumentListResponse']

/** One attempt at a document: its content, the guard's verdict, and — if rendered — its artifact. */
export type DocumentVersion = Schemas['DocumentVersionResponse']

/** Where a rendered PDF lives, as a download needs it: media type, size, pages. No bytes, no key. */
export type DocumentArtifact = Schemas['DocumentArtifactResponse']

/** The one document generators take: an optional `language` override; the type is in the path. */
export type GenerateDocumentRequest = Schemas['GenerateDocumentRequest']

/** `RESUME | COVER_LETTER` — the two documents Phase 10 produces. */
export type CandidateDocumentType = Schemas['CandidateDocumentType']

/** Where a version sits in its lifecycle — `DRAFT | VALIDATING | VALIDATED | REJECTED | RENDERED | ARCHIVED`. */
export type DocumentStatus = Schemas['DocumentStatus']

/** Why the guard refused a version — `UNKNOWN_EVIDENCE`, `INVENTED_NUMBER`, `ALTERED_IDENTITY`, … */
export type DocumentViolationCode = Schemas['DocumentViolationCode']

/** One stored LLM connection, credential reduced to `has_api_key` — never the value. */
export type LLMConnection = Schemas['LLMConnectionResponse']

/** This account's connections, in the router's priority-then-id order. */
export type LLMConnectionList = Schemas['LLMConnectionListResponse']

/** A new connection as the settings form submits it. `api_key` is write-only. */
export type CreateLLMConnectionRequest = Schemas['CreateLLMConnectionRequest']

/** A partial edit: unset fields untouched, `api_key` XOR `remove_api_key` for the credential. */
export type UpdateLLMConnectionRequest = Schemas['UpdateLLMConnectionRequest']

/** The result of probing a connection's live provider — data, never an exception. */
export type LLMConnectionHealth = Schemas['LLMConnectionHealthResponse']

/** `CLAUDE_CODE | CODEX | OPENAI_COMPATIBLE | LOCAL_OPENAI_COMPATIBLE`. */
export type LLMProviderType = Schemas['LLMProviderType']

/** A provider's runtime reachability — `UNKNOWN | HEALTHY | DEGRADED | UNAVAILABLE | AUTH_REQUIRED | MISCONFIGURED`. */
export type ProviderHealthStatus = Schemas['ProviderHealthStatus']

// Phase 12: the application engine. One application's current state and target, the
// list, its append-only event trail, and the one-field create request.

/** One application: its lifecycle state, target and pinned materials. */
export type Application = Schemas['ApplicationResponse']

/** This account's applications, newest first. */
export type ApplicationList = Schemas['ApplicationListResponse']

/** One immutable entry in an application's audit trail. */
export type ApplicationEvent = Schemas['ApplicationEventResponse']

/** One application's events, oldest first. */
export type ApplicationEventList = Schemas['ApplicationEventListResponse']

/** The one-field body that opens an application for a posting. */
export type CreateApplicationRequest = Schemas['CreateApplicationRequest']

/** Where an application sits in its lifecycle — the value a UI renders and acts on. */
export type ApplicationState = Application['state']

/** The route an application takes to an employer. */
export type ApplicationChannel = Application['channel']

// Phase 13: the career-chat control plane. A thread and its list, the turns and the
// proposals a turn produces, one confirmed proposal's audited execution, and the SSE
// event a streaming turn emits. Every type is an alias into the generated document, so
// a field the backend renames fails `nuxt typecheck` here after `npm run gen:api`
// (docs/CAREER_CHAT.md).

/** One chat thread's caption and activity — never its messages inline. */
export type Conversation = Schemas['ConversationResponse']

/** `GLOBAL | OPPORTUNITY | APPLICATION | COMPANY | SEARCH_PROFILE` — a thread's domain anchor. */
export type ConversationScope = Schemas['ConversationScope']

/** This account's threads, most recent activity first. */
export type ConversationList = Schemas['ConversationListResponse']

/** One stored turn: its prose only — the fenced proposal block was parsed out. */
export type ChatMessage = Schemas['ChatMessageResponse']

/** One thread's turns, oldest first — the transcript as it grew. */
export type ChatMessageList = Schemas['ChatMessageListResponse']

/** One typed action the model proposed, awaiting a human's confirm or dismiss. */
export type ChatActionProposal = Schemas['ChatActionProposalResponse']

/** One thread's proposals, oldest first. */
export type ChatActionProposalList = Schemas['ChatActionProposalListResponse']

/** The audited record of one confirmed proposal's execution. */
export type ChatActionExecution = Schemas['ChatActionExecutionResponse']

/** The SSE wire shape of one event a streaming turn emits. */
export type ChatStreamEvent = Schemas['ChatStreamEventResponse']

/** The discriminated `ChatAction` union verbatim — secret-free by construction. */
export type ChatAction = ChatActionProposal['action']

/** `PROPOSED | EXECUTED | REJECTED | FAILED | DISMISSED` — a card's actionability. */
export type ChatActionProposalStatus = ChatActionProposal['status']

/** `SUCCEEDED | REJECTED | FAILED` — how one confirm ended. */
export type ChatActionExecutionOutcome = ChatActionExecution['outcome']

/** A screen a confirmed `NAVIGATE` may point the client at. Closed on purpose. */
export type NavigationTarget = Schemas['NavigationTarget']

/** The optional-title body that opens a new thread. */
export type StartConversationRequest = Schemas['StartConversationRequest']

/** The one-field body of a user turn. */
export type SendMessageRequest = Schemas['SendMessageRequest']

/**
 * Every `error` slug `/api/v2` can answer with, as one union.
 *
 * Written out rather than derived: FastAPI documents the *shape* of an error body
 * but not the set of slugs, so this is the one V2 contract the generator cannot
 * check. It is a closed set on the backend — `backend/app/api/errors.py` maps each
 * exception to exactly one of these — and having it typed here is what makes a
 * `switch` on `ApiError.code` exhaustive in the compiler's eyes.
 */
export type V2ErrorCode =
  | 'account_disabled'
  | 'account_locked'
  | 'application_adapter_error'
  | 'application_channel_unsupported'
  | 'application_decision_missing'
  | 'application_document_not_ready'
  | 'application_duplicate'
  | 'application_form_changed'
  | 'application_missing_answer'
  | 'application_not_actionable'
  | 'application_not_found'
  | 'application_rate_limited'
  | 'application_submission_unknown'
  | 'artifact_unavailable'
  | 'candidate_profile_not_found'
  | 'capability_not_supported'
  | 'chat_proposal_not_actionable'
  | 'chat_proposal_not_found'
  | 'claim_cites_unknown_evidence'
  | 'company_not_found'
  | 'conflict'
  | 'context_length_exceeded'
  | 'conversation_not_found'
  | 'conversation_scope_not_found'
  | 'csrf_failed'
  | 'database_unavailable'
  | 'document_not_found'
  | 'document_not_rendered'
  | 'email_already_registered'
  | 'empty_chat_message'
  | 'insufficient_evidence'
  | 'invalid_credentials'
  | 'llm_connection_invalid'
  | 'llm_connection_not_found'
  | 'llm_secret_key_unavailable'
  | 'not_authenticated'
  | 'onboarding_incomplete'
  | 'output_limit_exceeded'
  | 'provider_auth_required'
  | 'provider_cancelled'
  | 'provider_content_filtered'
  | 'provider_internal_error'
  | 'provider_misconfigured'
  | 'provider_protocol_error'
  | 'provider_rate_limited'
  | 'provider_timeout'
  | 'provider_unavailable'
  | 'search_profile_not_found'
  | 'session_not_found'
  | 'structured_output_invalid'
  | 'validation_failed'
