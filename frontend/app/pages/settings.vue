<!--
  Settings — auto-apply policy, copilot model, schedule, manual runs. Ported from
  webapp/src/routes/SettingsPage.tsx (React route "/settings").

  The form is a local copy of the server's settings, synced when a fetch lands and
  saved wholesale, exactly as V1 did: the PUT replaces the whole document, so
  binding the inputs straight to a refetching query object would let a background
  refresh overwrite a field mid-edit.

  Two details are backend truths rather than styling. The placeholder endpoints
  mirror server/chat.py's `_BACKEND_DEFAULTS`, and the "must be local" note
  describes a server-side check — the copy exists so the rejection is not a
  surprise.
-->
<script setup lang="ts">
const { t, localize: l } = useLocale()
import { computed, ref, watch } from 'vue'
import type { LlmBackend, Settings } from '~/types/domain'
import { useRunStatus, useSettingsQuery } from '~/composables/useQueries'
import { useSaveSettings, useTriggerDiscovery, useTriggerFull } from '~/composables/useMutations'
import { ApiError } from '~/utils/api-client'
import { useMorningRun } from '~/composables/useMorningRun'
import { sourceHealthMessage } from '~/utils/discovery-messages'
import type { MorningRunStatus, MorningScheduleRequest } from '~/types/v2'
import { V2_ENDPOINTS } from '~/utils/endpoints'

const CREATIVITY = ['conservative', 'balanced', 'bold'] as const
const CADENCE = ['daily', 'weekdays'] as const

const BACKENDS = computed<{ value: LlmBackend, label: string }[]>(() => [
  { value: 'claude_cli', label: l('Claude Code CLI (configured account or API)', 'Claude Code CLI (compte ou API configuré)') },
  { value: 'ollama', label: l('Ollama (local server)', 'Ollama (serveur local)') },
  { value: 'lmstudio', label: l('LM Studio (local server)', 'LM Studio (serveur local)') },
])

const BACKEND_HINTS: Record<string, { url: string, model: string }> = {
  ollama: { url: 'http://localhost:11434/v1', model: 'llama3.2' },
  lmstudio: { url: 'http://localhost:1234/v1', model: 'local-model' },
}

const { data, status: fetchStatus } = useSettingsQuery()
const save = useSaveSettings()
const { data: runStatus } = useRunStatus()
const discover = useTriggerDiscovery()
const full = useTriggerFull()
const morning = useMorningRun()
const { locale } = useLocale()
const morningForm = ref<MorningScheduleRequest>({ enabled: false, local_time: '08:00', cadence: 'daily' })
const morningSaved = ref(false)
watch(morning.schedule, value => {
  if (value) morningForm.value = { enabled: value.enabled, local_time: value.local_time, cadence: value.cadence }
}, { immediate: true })

function morningError(detail: string | null): string | null {
  if (!detail) return null
  if (detail === 'discovery_source_unapproved') return l(
    'Discovery is paused: no reviewed Swiss ATS board is active. Contact the administrator.',
    'Recherche suspendue : aucun board ATS suisse vérifié n’est actif. Contactez l’administrateur.',
  )
  if (detail === 'queue_confirmation_missing') return l(
    'The queued search could not be confirmed. Check the morning-run status before retrying.',
    'La recherche en attente n’a pas pu être confirmée. Vérifiez son statut avant de réessayer.',
  )
  if (detail === 'network_error') return l('The server could not be reached. Check your connection and retry.', 'Serveur inaccessible. Vérifiez votre connexion et réessayez.')
  return detail
}
function morningStatus(run: MorningRunStatus): string {
  const labels: Record<MorningRunStatus['status'], [string, string]> = {
    NOT_STARTED: ['Not started', 'Non démarrée'], QUEUED: ['Queued', 'En attente'],
    RUNNING: ['Running', 'En cours'], SUCCEEDED: ['Discovery completed', 'Recherche terminée'],
    PARTIAL: ['Partial discovery', 'Recherche partielle'], FAILED: ['Discovery failed', 'Recherche échouée'],
  }
  return l(...labels[run.status])
}
function preparationStatus(stage: string): string {
  const labels: Record<string, [string, string]> = {
    DISCOVERED: ['Offer discovered; assessment queued', 'Offre trouvée ; évaluation en attente'],
    MATCHED: ['Assessment complete; documents pending', 'Évaluation terminée ; documents en attente'],
    CV_RENDERED: ['CV ready; letter pending', 'CV prêt ; lettre en attente'],
    DOCUMENTS_RENDERED: ['CV and letter ready; not submitted', 'CV et lettre prêts ; candidature non envoyée'],
    SKIPPED: ['Offer skipped by policy', 'Offre écartée par la politique'],
    REQUIRES_HUMAN: ['Human review required', 'Vérification humaine nécessaire'],
    FAILED: ['Preparation failed', 'Échec de la préparation'],
  }
  return l(...(labels[stage] ?? ['Unknown preparation state', 'État de préparation inconnu']))
}
function versionDownload(versionId: string): string {
  return V2_ENDPOINTS.documentVersionDownload.replace('{version_id}', encodeURIComponent(versionId))
}
function preparationReason(code: string): string {
  const labels: Record<string, [string, string]> = {
    DECISION_NOT_ACTIONABLE: ['Not eligible under the current policy', 'Non admissible selon la politique actuelle'],
    ELIGIBILITY_REVIEW_REQUIRED: ['Eligibility needs review', 'Admissibilité à vérifier'],
    DECISION_REQUIRES_HUMAN: ['Decision needs review', 'Décision à vérifier'],
    POLICY_CHANGED: ['Profile or policy changed; review required', 'Profil ou politique modifié ; vérification nécessaire'],
    DOCUMENT_GUARD_REJECTED: ['Document failed the evidence check', 'Le document n’a pas passé la vérification des preuves'],
  }
  return labels[code] ? l(...labels[code]) : code
}
function preparationTaskFailure(code: string | null | undefined): string {
  if (code === 'PROVIDER_UNAVAILABLE' || code === 'PROVIDER_TIMEOUT') return l(
    'Preparation stopped: the model provider is unavailable. Check your provider settings.',
    'Préparation arrêtée : le fournisseur de modèle est indisponible. Vérifiez ses réglages.',
  )
  return l('Preparation stopped. Check the worker status before retrying.',
    'Préparation arrêtée. Vérifiez l’état du service de traitement avant de réessayer.')
}
function applicationProgress(item: MorningRunStatus['preparations'][number]): string | null {
  if (!item.application_id) return null
  if (item.application_state === 'SUBMITTED')
    return l('Submission confirmed', 'Candidature confirmée')
  if (item.application_state === 'SUBMISSION_STATE_UNKNOWN')
    return l('Submission unconfirmed; do not retry automatically', 'Envoi non confirmé ; ne pas relancer automatiquement')
  if (item.application_state === 'REQUIRES_HUMAN')
    return l('Human intervention required; not sent', 'Intervention humaine nécessaire ; non envoyée')
  if (item.application_state === 'READY_FOR_REVIEW')
    return l('Form review required; not sent', 'Vérification du formulaire nécessaire ; non envoyée')
  if (item.submission_task_status === 'QUEUED')
    return l('Queued for browser submission; not sent', 'En attente d’envoi par navigateur ; non envoyée')
  if (item.submission_task_status === 'RUNNING' || item.application_state === 'SUBMITTING')
    return l('Browser submission in progress; not confirmed', 'Envoi par navigateur en cours ; non confirmé')
  if (item.submission_task_status === 'DEAD_LETTERED')
    return l('Browser submission stopped; inspect the history', 'Envoi par navigateur arrêté ; consulter l’historique')
  if (item.application_blocker_code === 'LIVE_SUBMISSIONS_DISABLED')
    return l('Approved but live sending is disabled', 'Approuvée mais l’envoi réel est désactivé')
  if (item.application_state === 'APPROVED')
    return l('Approved; not yet sent', 'Approuvée ; pas encore envoyée')
  if (item.application_state === 'PLANNED' || item.application_state === 'PREPARING')
    return l('Application preparation in progress', 'Préparation de la candidature en cours')
  return l('Application not sent; inspect the history', 'Candidature non envoyée ; consulter l’historique')
}
async function saveMorningSchedule() {
  morningSaved.value = false
  const result = await morning.saveSchedule(morningForm.value)
  morningSaved.value = result !== null
}

const form = ref<Settings | null>(null)
// Copied, not referenced: the inputs mutate this object directly.
watch(data, (next) => {
  if (next) form.value = { ...next.settings }
}, { immediate: true })

const isLoading = computed(() => fetchStatus.value === 'pending')
const running = computed(() => runStatus.value?.state === 'running')
const runningLabel = computed(() =>
  runStatus.value?.started_at
    ? l(`Running… (started ${runStatus.value.started_at.slice(11, 16)})`, `En cours… (démarré à ${runStatus.value.started_at.slice(11, 16)})`)
    : l('Running…', 'En cours…'),
)
const isLocalModel = computed(() => form.value !== null && form.value.llm_backend !== 'claude_cli')
const hint = computed(() => (form.value ? BACKEND_HINTS[form.value.llm_backend] : undefined))
function choiceLabel(value: string): string {
  const choices: Record<string, [string, string]> = {
    conservative: ['conservative', 'prudente'], balanced: ['balanced', 'équilibrée'], bold: ['bold', 'audacieuse'],
    daily: ['daily', 'tous les jours'], weekdays: ['weekdays', 'jours ouvrés'],
  }
  const pair = choices[value]
  return pair ? l(...pair) : value
}
// Only an ApiError carries a server `detail`; anything else is a transport
// failure with nothing worth putting next to the button.
const errDetail = computed(() =>
  save.error instanceof ApiError ? String(save.error.detail) : null,
)
function runError(error: unknown): string {
  if (error instanceof ApiError && typeof error.detail === 'string') return error.detail
  return l('The run could not be started. Check the server connection and try again.', 'Impossible de démarrer. Vérifiez la connexion au serveur, puis réessayez.')
}
</script>

<template>
  <div class="settings-page">
    <h1>{{ t('page.settings') }}</h1>

    <LlmConnectionsSettings />

    <section class="settings-card" data-testid="v2-morning-run" aria-labelledby="v2-morning-heading">
      <h2 id="v2-morning-heading">{{ l('Swiss V2 discovery', 'Recherche suisse V2') }}</h2>
      <p class="settings-hint">{{ l('Discover Swiss opportunities and prepare evidence-checked CVs and letters. Live submission is disabled by default; check each application’s status below.', 'Recherche des offres suisses et prépare des CV et lettres fondés sur vos preuves. L’envoi réel est désactivé par défaut ; vérifiez ci-dessous le statut de chaque candidature.') }}</p>
      <label class="settings-row settings-row--check">
        <input v-model="morningForm.enabled" type="checkbox">
        <span>{{ l('Enable daily V2 discovery', 'Activer la recherche V2 quotidienne') }}</span>
      </label>
      <label class="settings-row">
        <span>{{ l('Swiss time', 'Heure suisse') }}</span>
        <input v-model="morningForm.local_time" type="time" :aria-label="l('V2 Swiss time', 'Heure suisse V2')">
      </label>
      <label class="settings-row">
        <span>{{ l('Cadence', 'Fréquence') }}</span>
        <select v-model="morningForm.cadence" :aria-label="l('V2 cadence', 'Fréquence V2')">
          <option v-for="c in CADENCE" :key="c" :value="c">{{ choiceLabel(c) }}</option>
        </select>
      </label>
      <p class="settings-hint">{{ l('A saved schedule runs only when the server scheduler is enabled by the operator and authorized sources are configured.', 'Un horaire enregistré ne s’exécute que si le planificateur serveur est activé et que les sources autorisées sont configurées.') }}</p>
      <p class="settings-hint">{{ l('Live Swiss source coverage is unverified. Do not enable live collection before source authorization.', 'La couverture réelle des sources suisses n’est pas vérifiée. N’activez pas la collecte réelle avant l’autorisation des sources.') }}</p>
      <div class="settings-actions">
        <button class="btn-ghost" type="button" :disabled="morning.saving.value" @click="saveMorningSchedule">
          {{ morning.saving.value ? l('Saving…', 'Enregistrement…') : l('Save V2 schedule', 'Enregistrer l’horaire V2') }}
        </button>
        <button class="btn-primary" type="button" :disabled="morning.busy.value" @click="morning.runNow()">
          {{ morning.busy.value ? l('Discovery pending…', 'Recherche en attente…') : l('Run V2 discovery', 'Lancer la recherche V2') }}
        </button>
      </div>
      <p v-if="morningSaved" role="status" class="settings-ok">{{ l('V2 schedule saved.', 'Horaire V2 enregistré.') }}</p>
      <p v-if="morningError(morning.scheduleError.value)" role="alert" class="settings-err">{{ morningError(morning.scheduleError.value) }}</p>
      <p v-if="morningError(morning.error.value)" role="alert" class="settings-err">{{ morningError(morning.error.value) }}</p>
      <p v-if="morning.runs.value.length === 0" class="settings-hint" role="status">{{ l('No active Swiss search or run yet.', 'Aucune recherche suisse active ni exécution pour le moment.') }}</p>
      <ul v-else class="morning-run-list">
        <li v-for="run in morning.runs.value" :key="run.search_profile_id" class="morning-run-item">
          <div class="morning-run-head">
            <strong :role="run.status === 'FAILED' ? 'alert' : 'status'">{{ morningStatus(run) }}</strong>
            <NuxtLink :to="`/map?profile=${encodeURIComponent(run.search_profile_id)}`">{{ l('View opportunities on map', 'Voir les offres sur la carte') }}</NuxtLink>
          </div>
          <p v-if="run.status === 'SUCCEEDED' || run.status === 'PARTIAL'" class="settings-hint">{{ l(`${run.matched_count} matched offers`, `${run.matched_count} offres correspondantes`) }}</p>
          <ul v-if="run.preparations?.length" class="morning-source-list" aria-live="polite">
            <li v-for="item in run.preparations" :key="item.opportunity_id">
              {{ l('Offer', 'Offre') }} {{ item.opportunity_id }} —
              <span v-if="item.task_status === 'DEAD_LETTERED'" role="alert">{{ preparationTaskFailure(item.task_failure_code) }}</span>
              <span v-else>{{ preparationStatus(item.stage) }}</span>
              <span v-if="item.task_status === 'QUEUED' && item.task_failure_code"> · {{ l('Retry scheduled', 'Nouvelle tentative programmée') }}</span>
              <span v-if="item.reason_code"> · {{ preparationReason(item.reason_code) }}</span>
              <span v-if="applicationProgress(item)" :role="item.application_state === 'SUBMISSION_STATE_UNKNOWN' || item.submission_task_status === 'DEAD_LETTERED' ? 'alert' : 'status'"> · {{ applicationProgress(item) }}</span>
              <NuxtLink v-if="item.application_id" :to="`/applications?application=${encodeURIComponent(item.application_id)}`">{{ l('Application history', 'Historique de la candidature') }}</NuxtLink>
              <span v-if="item.resume_version_id"> · <a :href="versionDownload(item.resume_version_id)">{{ l('Download exact CV version', 'Télécharger cette version du CV') }}</a></span>
              <span v-if="item.cover_letter_version_id"> · <a :href="versionDownload(item.cover_letter_version_id)">{{ l('Download exact letter version', 'Télécharger cette version de la lettre') }}</a></span>
            </li>
          </ul>
          <p v-if="run.preparations_truncated" class="settings-hint" role="status">{{ l('Showing the first 500 prepared offers. Narrow your search to inspect the rest.', 'Les 500 premières offres préparées sont affichées. Affinez la recherche pour voir les autres.') }}</p>
          <ul v-if="run.source_health.length" class="morning-source-list">
            <li v-for="source in run.source_health" :key="source.source_key">
              {{ source.source_key }} — {{ sourceHealthMessage(locale, source.status, source.reason ?? null) }}
            </li>
          </ul>
        </li>
      </ul>
    </section>

    <p v-if="isLoading || !form" class="settings-hint" role="status">
      {{ l('Legacy settings unavailable or loading.', 'Anciens réglages indisponibles ou en chargement.') }}
    </p>
    <template v-else>
    <p v-if="data?.status === 'invalid'" class="settings-warn" role="alert">
      {{ l('Your legacy settings file couldn’t be read; defaults are shown. Saving will rewrite it.', 'Impossible de lire le fichier des anciens réglages ; les valeurs par défaut sont affichées. L’enregistrement réécrira le fichier.') }}
    </p>

    <section class="settings-card">
      <h2>{{ l('Legacy — Auto-apply', 'Ancien système — Candidature automatique') }}</h2>
      <label class="settings-row settings-row--check">
        <input v-model="form.auto_apply" type="checkbox">
        <span>{{ l('Enable auto-apply for high-scoring roles', 'Activer les candidatures automatiques pour les offres bien notées') }}</span>
      </label>
      <label class="settings-row">
        <span>{{ l('Minimum score', 'Score minimum') }}</span>
        <input
          v-model.number="form.auto_apply_min_score"
          type="number"
          :min="0"
          :max="100"
          :aria-label="l('Minimum score', 'Score minimum')"
        >
      </label>
      <label class="settings-row">
        <span>{{ l('Daily cap', 'Limite quotidienne') }}</span>
        <input
          v-model.number="form.auto_apply_daily_cap"
          type="number"
          :min="1"
          :aria-label="l('Daily cap', 'Limite quotidienne')"
        >
      </label>
      <label class="settings-row">
        <span>{{ l('Tailoring creativity', 'Niveau de personnalisation') }}</span>
        <select v-model="form.tailor_creativity" :aria-label="l('Tailoring creativity', 'Niveau de personnalisation')">
          <option v-for="c in CREATIVITY" :key="c" :value="c">
            {{ choiceLabel(c) }}
          </option>
        </select>
      </label>
    </section>
    <section class="settings-card">
      <h2>{{ l('Copilot model', 'Modèle du copilote') }}</h2>
      <p class="settings-hint">
        {{ l('These settings control the legacy copilot. Claude Code runs locally but sends prompts to its configured model provider. Ollama and LM Studio use the configured local endpoint. For V2 documents and assistants, configure a connection in the model providers section above.', 'Ces réglages contrôlent l’ancien copilote. Claude Code s’exécute localement, mais transmet les requêtes au fournisseur de modèle configuré. Ollama et LM Studio utilisent le serveur local configuré. Pour les documents et assistants V2, configurez une connexion dans la section des fournisseurs de modèles ci-dessus.') }}
      </p>
      <label class="settings-row">
        <span>{{ l('Backend', 'Fournisseur') }}</span>
        <select v-model="form.llm_backend" :aria-label="l('Copilot backend', 'Fournisseur du copilote')">
          <option v-for="b in BACKENDS" :key="b.value" :value="b.value">
            {{ b.label }}
          </option>
        </select>
      </label>

      <template v-if="isLocalModel">
        <label class="settings-row">
          <span>{{ l('Endpoint', 'Point de terminaison') }}</span>
          <input
            v-model="form.llm_base_url"
            type="text"
            :placeholder="hint?.url"
            :aria-label="l('Local endpoint', 'Adresse du serveur local')"
          >
        </label>
        <label class="settings-row">
          <span>{{ l('Model', 'Modèle') }}</span>
          <input
            v-model="form.llm_model"
            type="text"
            :placeholder="hint?.model"
            :aria-label="l('Model name', 'Nom du modèle')"
          >
        </label>
        <p class="settings-hint">
        {{ l(`Leave blank to use the default (${hint?.url} · ${hint?.model}). The endpoint must be local (localhost or a loopback address).`, `Laissez vide pour utiliser la valeur par défaut (${hint?.url} · ${hint?.model}). L’adresse doit être locale (localhost ou adresse de boucle locale).`) }}
        </p>
      </template>
    </section>

    <section class="settings-card">
      <h2>{{ l('Legacy — Daily run schedule', 'Ancien système — Planification quotidienne') }}</h2>
      <p class="settings-hint">
        {{ l('These settings control the old V1 runner only. They do not schedule V2 discovery or guarantee an application.', 'Ces réglages pilotent uniquement l’ancien moteur V1. Ils ne planifient pas la recherche V2 et ne garantissent aucune candidature.') }}
      </p>
      <label class="settings-row settings-row--check">
        <input v-model="form.schedule_enabled" type="checkbox">
        <span>{{ l('Run the full pipeline automatically on a schedule', 'Exécuter automatiquement le processus complet selon un horaire') }}</span>
      </label>
      <label class="settings-row">
        <span>{{ l('Time', 'Heure') }}</span>
        <input v-model="form.schedule_time" type="time" :aria-label="l('Schedule time', 'Heure de planification')">
      </label>
      <label class="settings-row">
        <span>{{ l('Cadence', 'Fréquence') }}</span>
        <select v-model="form.schedule_cadence" :aria-label="l('Schedule cadence', 'Fréquence de planification')">
          <option v-for="c in CADENCE" :key="c" :value="c">
            {{ choiceLabel(c) }}
          </option>
        </select>
      </label>
    </section>
    <section class="settings-card">
      <h2>{{ l('Legacy — Run now', 'Ancien système — Lancer maintenant') }}</h2>
      <p class="settings-hint">
        {{ l('These buttons start the old V1 runner, not the V2 candidate workflow.', 'Ces boutons lancent l’ancien moteur V1, pas le parcours de candidature V2.') }}
      </p>
      <div class="settings-actions">
        <button class="btn-primary" type="button" :disabled="running" @click="discover.mutate()">
          {{ running ? runningLabel : l('Run now (discovery)', 'Rechercher les offres') }}
        </button>
        <button class="btn-ghost" type="button" :disabled="running" @click="full.mutate()">
          {{ l('Run full pipeline now', 'Lancer le processus complet') }}
        </button>
      </div>
      <p v-if="discover.error" class="settings-err" role="alert">{{ runError(discover.error) }}</p>
      <p v-if="full.error" class="settings-err" role="alert">{{ runError(full.error) }}</p>
      <p
        v-if="runStatus?.last_run && runStatus.state === 'idle'"
        :class="runStatus.last_run.ok ? 'settings-ok' : 'settings-err'"
        role="status"
      >
        {{ l('Last', 'Dernière exécution') }} {{ runStatus.last_run.kind }} :
        {{ runStatus.last_run.ok ? l('successful', 'réussie') : l('failed', 'échouée') }} — {{ runStatus.last_run.summary }}
      </p>
    </section>

    <section class="settings-card settings-card--save" data-testid="settings-save-footer">
      <div>
        <h2>{{ l('Save changes', 'Enregistrer les modifications') }}</h2>
        <p class="settings-hint">
          {{ l('Review your policy and schedule, then save them together.', 'Vérifiez votre politique et votre planification, puis enregistrez-les ensemble.') }}
        </p>
      </div>
      <div class="settings-actions settings-actions--save">
        <button class="btn-primary" type="button" :disabled="save.isPending" @click="save.mutate(form)">
          {{ save.isPending ? l('Saving…', 'Enregistrement…') : l('Save settings', 'Enregistrer les réglages') }}
        </button>
        <span v-if="save.isSuccess && !save.isPending" class="settings-ok" role="status">{{ l('Saved.', 'Enregistré.') }}</span>
        <span v-if="errDetail" class="settings-err" role="alert">{{ errDetail }}</span>
      </div>
    </section>
    </template>
  </div>
</template>
