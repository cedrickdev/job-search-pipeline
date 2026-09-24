// The interview simulator's client state — the one place a practice session's current
// question, its last coaching and its readiness live while the panel is open.
//
// It mirrors stores/chat.ts (options API: typed state, getters, async actions) and holds
// server data for the same reason: a session is a loop — ask, answer, grade, adapt — and
// each turn's result has to accumulate somewhere a component can render. Everything here
// is derived from the server; the store never decides an outcome, it records the one the
// server returned.
//
// The phase's rule is visible in what this store does NOT hold. An answer's evaluation is
// coaching only — dimensions, strengths, improvements, a suggested answer — with no
// probability and no verdict, because `InterviewAnswerEvaluation` has no such field. And
// readiness is never taken from a submit or a summary the model wrote: it is the
// platform's own signal, fetched from its own endpoint after every grade, so a completed
// session's readiness is computed, not authored. Practice, never prediction
// (docs/INTERVIEW_SIMULATOR.md).
import { defineStore } from 'pinia'
import type {
  CandidateProfile,
  InterviewAnswer,
  InterviewAnswerEvaluation,
  InterviewAnswerOutcome,
  InterviewMode,
  InterviewQuestion,
  InterviewSession,
  InterviewSessionDetail,
  InterviewSessionList,
  InterviewSessionSummary,
  InterviewSessionSummaryList,
  InterviewTurn,
  SessionReadiness,
} from '~/types/v2'
import { apiGet, apiPost, apiPostForm } from '~/utils/api-client'
import {
  interviewEvaluate,
  interviewSession as interviewSessionPath,
  V2_ENDPOINTS,
} from '~/utils/endpoints'
import { errorMessage } from '~/utils/v2-errors'

interface InterviewState {
  // The account's practice sessions, newest first — the left-hand list.
  sessions: InterviewSession[]
  // Completed sessions' readiness summaries — the trend the platform kept, not a forecast.
  history: InterviewSessionSummary[]
  // The candidate profile a new session grounds on; null until loaded (or none saved).
  profileId: string | null
  // The open session and everything derived from it while its panel is up.
  activeId: string | null
  session: InterviewSession | null
  // The question awaiting an answer, if any; null at CREATED and after each answer.
  question: InterviewQuestion | null
  // The most recently answered question — kept so its coaching, and its sequence for a
  // strict re-grade, survive the moment `question` is cleared.
  lastQuestion: InterviewQuestion | null
  lastAnswer: InterviewAnswer | null
  lastEvaluation: InterviewAnswerEvaluation | null
  // The platform's readiness signal for the open session — fetched from its own endpoint.
  readiness: SessionReadiness | null
  // The finalizing summary, once the session is completed.
  summary: InterviewSessionSummary | null
  // `loading` is a panel switch (fetching a session's detail); `busy` is an action in
  // flight (ask, answer, grade, complete, abandon) so a component can disable its controls.
  loading: boolean
  busy: boolean
  error: string | null
}

export const useInterviewStore = defineStore('interview', {
  state: (): InterviewState => ({
    sessions: [],
    history: [],
    profileId: null,
    activeId: null,
    session: null,
    question: null,
    lastQuestion: null,
    lastAnswer: null,
    lastEvaluation: null,
    readiness: null,
    summary: null,
    loading: false,
    busy: false,
    error: null,
  }),
  getters: {
    // Open for practice: the session exists and has not been completed or abandoned.
    isActive: (state): boolean => state.session?.is_active === true,
    // The loop is waiting on the candidate: a question is on the table.
    awaitingAnswer: (state): boolean => state.question !== null,
    // Finalized — the summary and its computed readiness are the last word.
    isComplete: (state): boolean => state.session?.status === 'COMPLETED',
    // A next question can be asked when the session is open and nothing is pending.
    canAsk (state): boolean {
      return state.session?.is_active === true && state.question === null
    },
  },
  actions: {
    /**
     * Resolve the account's candidate profile id — what a new session grounds on.
     *
     * A missing profile is an expected empty state, not an error to shout about: the id
     * stays null and the page steers the user to save one. Any other fault is swallowed
     * too, because a create that needs the id will surface the real refusal itself.
     */
    async loadProfile (): Promise<void> {
      try {
        const profile = await apiGet<CandidateProfile>(V2_ENDPOINTS.profile)
        this.profileId = profile.id
      }
      catch {
        this.profileId = null
      }
    },

    /** Load this account's sessions, newest first (the server orders them). */
    async loadSessions (): Promise<void> {
      try {
        const list = await apiGet<InterviewSessionList>(V2_ENDPOINTS.interviewSessions)
        this.sessions = list.sessions
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
    },

    /** Load the readiness history — completed sessions' computed signals, the trend line. */
    async loadHistory (): Promise<void> {
      try {
        const list = await apiGet<InterviewSessionSummaryList>(V2_ENDPOINTS.interviewHistory)
        this.history = list.summaries
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
    },

    // LOAD-MARKER

    /**
     * Open a new practice session over one opportunity, then select it.
     *
     * The profile id is required in the body — without it there is nothing to ground on,
     * so the store refuses before any request and points the user at their profile. The
     * server does the real grounding (profile owner-first, posting by id) and answers 404
     * if either is missing; that refusal flows back through `error`.
     */
    async create (opportunityId: string, mode: InterviewMode): Promise<string | null> {
      if (this.profileId === null) {
        this.error = 'Save a profile before starting a practice session.'
        return null
      }
      if (this.busy) return null
      this.busy = true
      this.error = null
      try {
        const session = await apiPost<InterviewSession>(V2_ENDPOINTS.interviewSessions, {
          candidate_profile_id: this.profileId,
          opportunity_id: opportunityId,
          mode,
        })
        this.sessions = [session, ...this.sessions]
        this.activeId = session.id
        this.session = session
        this.question = null
        this.lastQuestion = null
        this.lastAnswer = null
        this.lastEvaluation = null
        this.readiness = null
        this.summary = null
        return session.id
      }
      catch (caught) {
        this.error = errorMessage(caught)
        return null
      }
      finally {
        this.busy = false
      }
    },

    // SELECT-MARKER

    /**
     * Open an existing session: load its detail, derive where the loop stands, and fetch
     * the platform's live readiness.
     *
     * "Where the loop stands" is read, not remembered: the current question is the one
     * question with no answer, and the last coaching is the evaluation tied to the last
     * answer. Readiness is fetched from its own endpoint rather than taken from the
     * detail, because it is the platform's computed signal — the same rule a completed
     * session obeys.
     */
    async select (id: string): Promise<void> {
      this.activeId = id
      this.resetActive()
      this.loading = true
      try {
        const detail = await apiGet<InterviewSessionDetail>(
          interviewSessionPath(V2_ENDPOINTS.interviewSessionDetail, id))
        this.session = detail.session
        this.summary = detail.summary
        const answered = new Set(detail.answers.map(answer => answer.question_id))
        this.question = detail.questions.find(question => !answered.has(question.id)) ?? null
        const lastAnswer = detail.answers.at(-1) ?? null
        this.lastAnswer = lastAnswer
        this.lastQuestion = lastAnswer === null
          ? null
          : detail.questions.find(question => question.id === lastAnswer.question_id) ?? null
        this.lastEvaluation = lastAnswer === null
          ? null
          : detail.evaluations.find(evaluation => evaluation.answer_id === lastAnswer.id) ?? null
        this.readiness = await apiGet<SessionReadiness>(
          interviewSessionPath(V2_ENDPOINTS.interviewReadiness, id))
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.loading = false
      }
    },

    // TURN-MARKER

    /**
     * Ask the next question — discharge the plan's next topic, or adapt to the last answer.
     *
     * The server owns the choice; the store only records the turn it returns and the
     * session it moved (CREATED → IN_PROGRESS on the first ask). No answer was graded, so
     * readiness is left as it was.
     */
    async nextQuestion (): Promise<void> {
      if (this.activeId === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        const turn = await apiPost<InterviewTurn>(
          interviewSessionPath(V2_ENDPOINTS.interviewNextQuestion, this.activeId))
        this.session = turn.session
        this.question = turn.question
        this.lastQuestion = null
        this.lastAnswer = null
        this.lastEvaluation = null
        this.patchSession()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    /** Submit a typed answer to the current question, then refresh the platform readiness. */
    async submitText (content: string): Promise<void> {
      const text = content.trim()
      if (text === '' || this.activeId === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        const outcome = await apiPost<InterviewAnswerOutcome>(
          interviewSessionPath(V2_ENDPOINTS.interviewAnswers, this.activeId), { content: text })
        this.applyOutcome(outcome)
        await this.refreshReadiness()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    // VOICE-MARKER

    /**
     * Submit a spoken answer: the clip goes up as multipart under the field name the route
     * expects (`audio`), the server transcribes it, records it as `VOICE` and grades it.
     *
     * The transcript is the server's, never the client's — the store sends bytes and reads
     * back the answer it was turned into, with its transcript confidence.
     */
    async submitVoice (clip: Blob, filename = 'answer.webm'): Promise<void> {
      if (this.activeId === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        const form = new FormData()
        form.append('audio', clip, filename)
        const outcome = await apiPostForm<InterviewAnswerOutcome>(
          interviewSessionPath(V2_ENDPOINTS.interviewVoiceAnswers, this.activeId), form)
        this.applyOutcome(outcome)
        await this.refreshReadiness()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    /**
     * Strictly re-grade the last answered question.
     *
     * A submit degrades quietly when the provider cannot coach — the answer is kept, the
     * evaluation is null. This asks again and, unlike the submit, surfaces the provider's
     * failure (503 → `evaluation_unavailable`) rather than swallowing it. The sequence is
     * the answered question's, held in `lastQuestion` for exactly this.
     */
    async evaluate (): Promise<void> {
      if (this.activeId === null || this.lastQuestion === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        this.lastEvaluation = await apiPost<InterviewAnswerEvaluation>(
          interviewEvaluate(this.activeId, this.lastQuestion.sequence))
        await this.refreshReadiness()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    // CLOSE-MARKER

    /**
     * Complete the session: finalize it and take the summary.
     *
     * The summary's readiness is the platform's computed signal, so it becomes the panel's
     * readiness verbatim. The completed session drops out of the active set and its
     * summary joins the history.
     */
    async complete (): Promise<void> {
      if (this.activeId === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        const summary = await apiPost<InterviewSessionSummary>(
          interviewSessionPath(V2_ENDPOINTS.interviewComplete, this.activeId))
        this.summary = summary
        this.readiness = summary.readiness
        this.question = null
        if (this.session !== null) {
          this.session = { ...this.session, status: 'COMPLETED', is_active: false }
        }
        this.patchSession()
        await this.loadHistory()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    /** Abandon the session — a terminal state; the practice is walked away from, not lost. */
    async abandon (): Promise<void> {
      if (this.activeId === null || this.busy) return
      this.busy = true
      this.error = null
      try {
        this.session = await apiPost<InterviewSession>(
          interviewSessionPath(V2_ENDPOINTS.interviewAbandon, this.activeId))
        this.question = null
        this.patchSession()
      }
      catch (caught) {
        this.error = errorMessage(caught)
      }
      finally {
        this.busy = false
      }
    },

    // HELPERS-MARKER

    /**
     * Record a submit's outcome: the moved session, the answer, its coaching (or null).
     *
     * The answered question becomes `lastQuestion` (its sequence is what a re-grade needs)
     * and the current question is cleared — the loop now waits on the next ask.
     */
    applyOutcome (outcome: InterviewAnswerOutcome): void {
      this.session = outcome.session
      this.lastQuestion = this.question
      this.question = null
      this.lastAnswer = outcome.answer
      this.lastEvaluation = outcome.evaluation
      this.patchSession()
    },

    /**
     * Re-fetch the open session's readiness from its own endpoint.
     *
     * Secondary to the action that triggered it: a failure here keeps the prior signal and
     * stays silent, rather than masking the grade that just succeeded with a readiness error.
     */
    async refreshReadiness (): Promise<void> {
      if (this.activeId === null) return
      try {
        this.readiness = await apiGet<SessionReadiness>(
          interviewSessionPath(V2_ENDPOINTS.interviewReadiness, this.activeId))
      }
      catch {
        // keep the last known readiness
      }
    },

    /** Reflect the open session back into the list, so its row tracks its status. */
    patchSession (): void {
      const current = this.session
      if (current === null) return
      this.sessions = this.sessions.map(session =>
        session.id === current.id ? current : session)
    },

    /** Clear everything derived from a session — used when switching the open one. */
    resetActive (): void {
      this.session = null
      this.question = null
      this.lastQuestion = null
      this.lastAnswer = null
      this.lastEvaluation = null
      this.readiness = null
      this.summary = null
      this.error = null
    },
  },
})








