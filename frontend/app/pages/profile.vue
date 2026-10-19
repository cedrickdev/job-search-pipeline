<!--
  Your account: the credentials-free facts about it, the candidate profile, and the
  saved searches.

  Three deliberate choices.

  **It runs inside the shell**, unlike login, register and onboarding: this is an
  ordinary screen of the application, and the sidebar's other pages work while it is
  open. It carries `middleware: 'auth'`, so an expired session lands on the login
  form with a `?redirect=` back here rather than on an empty page.

  **Pausing a search is the same wholesale `PUT` as editing one.** The search is sent
  back with `is_active` flipped and every other field spread from what was loaded —
  the write replaces the search, so a request carrying only `is_active` would reset
  the areas and the keyword lists to their defaults. It is the hazard
  SearchProfileForm.vue documents, in the one place that writes a search without
  showing the form.

  **Deleting goes through ConfirmBar**, the same gate the drawer's destructive
  actions use. A saved search is not recoverable — there is no undo endpoint — and a
  `DELETE` on a click alone is one mis-click from somebody's whole search history.

  Nothing here can change an email or a password: those are the account's identity
  and the endpoints for changing them do not exist in this phase. The screen says so
  rather than rendering inputs that would 404.
-->
<script setup lang="ts">
const { t, localize: l } = useLocale()
import { computed, ref } from 'vue'
import {
  useProfileQuery,
  useSaveProfile,
  useSearchProfileActions,
  useSearchProfilesQuery,
} from '~/composables/useAccount'
import { useSessionStore } from '~/stores/session'
import type { CandidateProfileDraft, SearchArea, SearchProfile, SearchProfileDraft } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'
import { countryOptionsFor, useCountryPacksQuery } from '~/composables/useCountryPacks'

definePageMeta({ middleware: 'auth' })

useHead({ title: () => `${l('Your account', 'Votre compte')} · Command Center` })

const session = useSessionStore()
const { data: profile, status: profileStatus } = useProfileQuery()
const { data: searches, status: searchStatus } = useSearchProfilesQuery()
const { data: countryPacks } = useCountryPacksQuery()
const countryOptions = computed(() => countryOptionsFor(countryPacks.value))
const saveProfile = useSaveProfile()
const { create, update, remove } = useSearchProfileActions()

/** The search being edited: an id, `'new'` for the create form, or null. */
const editing = ref<string | null>(null)
/** The search whose deletion is awaiting confirmation. */
const confirming = ref<string | null>(null)
/** Candidate details remain read-only until the user explicitly enters edit mode. */
const profileEditing = ref(false)

const list = computed(() => searches.value?.search_profiles ?? [])
const loadingProfile = computed(() => profileStatus.value === 'pending' && !profile.value)
const loadingSearches = computed(() => searchStatus.value === 'pending' && !searches.value)

const editingSearch = computed(() =>
  editing.value === null || editing.value === 'new'
    ? null
    : list.value.find(entry => entry.id === editing.value) ?? null)
// `create` and `update` are used separately rather than through one handle: the new
// search's pending state must not disable the row being edited, and their errors
// belong to different pieces of the screen.
const removeMessage = computed(() => (remove.error ? errorMessage(remove.error) : null))
/** A date as the reader's locale writes it; the API sends UTC ISO-8601. */
function when(iso: string | null): string {
  if (iso === null) return '—'
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '—' : at.toLocaleString()
}

/** One area in a few words, so a row says where it looks without unfolding. */
function areaLabel(area: SearchArea): string {
  if (area.kind === 'COUNTRY') return area.country
  if (area.kind === 'REMOTE_ONLY') {
    return area.country === null || area.country === undefined
      ? 'remote'
      : `remote (${area.country})`
  }
  return `${area.radius_km} km radius`
}

function areaSummary(search: SearchProfile): string {
  return search.search.areas.map(areaLabel).join(', ')
}

async function onProfile(draft: CandidateProfileDraft) {
  try {
    await saveProfile.mutateAsync(draft)
    profileEditing.value = false
  }
  catch {
    // Keep the values editable and render the mutation error above the form.
  }
}

async function onSearchSubmit(draft: SearchProfileDraft) {
  const current = editingSearch.value
  try {
    if (current === null) await create.mutateAsync(draft)
    else await update.mutateAsync({ id: current.id, draft })
    editing.value = null
  }
  catch {
    // Left open, with the mutation's error rendered in the form: closing it would
    // throw away what the user typed.
  }
}

/**
 * Pause or resume a search.
 *
 * The whole draft goes back with one field changed. `PUT` replaces the search, so
 * sending `{is_active}` alone would empty its areas and keyword lists.
 */
function togglePaused(search: SearchProfile) {
  update.mutate({
    id: search.id,
    draft: { ...search.search, is_active: !search.search.is_active },
  })
}

async function onRemove(id: string) {
  confirming.value = null
  if (editing.value === id) editing.value = null
  await remove.mutateAsync(id).catch(() => {})
}

async function onSignOut() {
  await session.signOut()
  await navigateTo('/login')
}
</script>

<template>
  <div class="acct-page">
    <header class="acct-head">
      <h1>{{ t('page.account') }}</h1>
      <button class="btn-ghost" type="button" @click="onSignOut">
        {{ l('Sign out', 'Se déconnecter') }}
      </button>
    </header>

    <section class="settings-card">
      <h2>{{ l('Account', 'Compte') }}</h2>
      <dl class="acct-facts">
        <div>
          <dt>{{ l('Email', 'E-mail') }}</dt>
          <dd>{{ session.account?.email ?? '—' }}</dd>
        </div>
        <div>
          <dt>{{ l('Name', 'Nom') }}</dt>
          <dd>{{ session.account?.display_name ?? '—' }}</dd>
        </div>
        <div>
          <dt>{{ l('Member since', 'Membre depuis') }}</dt>
          <dd>{{ when(session.account?.created_at ?? null) }}</dd>
        </div>
        <div>
          <dt>{{ l('Setup finished', 'Configuration terminée') }}</dt>
          <dd>{{ when(session.account?.onboarding_completed_at ?? null) }}</dd>
        </div>
        <div>
          <dt>{{ l('This session expires', 'Expiration de cette session') }}</dt>
          <dd>{{ when(session.session?.expires_at ?? null) }}</dd>
        </div>
      </dl>
      <p class="auth-hint">
        {{ l('Changing your email address or password is not available yet. Signing out revokes this session on the server — other browsers you signed in from keep theirs until they expire.', 'La modification de votre adresse e-mail ou de votre mot de passe n’est pas encore disponible. La déconnexion révoque cette session sur le serveur ; les autres navigateurs resteront connectés jusqu’à expiration.') }}
      </p>
    </section>

    <section class="settings-card">
      <div class="profile-card-head">
        <h2>{{ l('Candidate profile', 'Profil candidat') }}</h2>
        <button
          v-if="!profileEditing"
          class="btn-primary"
          type="button"
          data-testid="edit-profile"
          @click="profileEditing = true"
        >
          {{ l('Edit profile', 'Modifier le profil') }}
        </button>
        <button v-else class="btn-ghost" type="button" @click="profileEditing = false">
          {{ l('Cancel', 'Annuler') }}
        </button>
      </div>
      <p class="auth-hint">
        {{ l('What postings are matched against. Everything here is yours alone; nothing is sent anywhere outside this installation.', 'Ces informations servent à évaluer les offres. Elles vous appartiennent et ne quittent pas cette installation.') }}
      </p>
      <p v-if="loadingProfile" class="ov-state">
        Loading…
      </p>
      <ProfileForm
        v-else
        :profile="profile ?? null"
        :editable="profileEditing || profile === null"
        :account-email="session.account?.email ?? null"
        :country-options="countryOptions"
        :pending="saveProfile.isPending"
        :error="saveProfile.error"
        @submit="onProfile"
      />
      <p v-if="saveProfile.isSuccess && !saveProfile.isPending" class="settings-ok" role="status">
        {{ l('Saved.', 'Enregistré.') }}
      </p>
      <ReferenceCvImport />
    </section>

    <section class="settings-card">
      <h2>{{ l('Saved searches', 'Recherches enregistrées') }}</h2>
      <p class="auth-hint">
        {{ l('Each one is a standing brief for the discovery runs. Paused searches are kept and skipped.', 'Chaque recherche définit les critères utilisés lors des explorations. Les recherches en pause sont conservées mais ignorées.') }}
      </p>

      <p v-if="removeMessage" class="auth-error" role="alert">
        {{ removeMessage }}
      </p>
      <p v-if="loadingSearches" class="ov-state">
        Loading…
      </p>
      <p v-else-if="list.length === 0" class="ov-empty">
        {{ l('No saved searches yet.', 'Aucune recherche enregistrée.') }}
      </p>
      <ul v-else class="acct-searches">
        <li v-for="search in list" :key="search.id" class="acct-search">
          <div class="acct-search-row">
            <div class="acct-search-main">
              <span class="acct-search-name">{{ search.search.name }}</span>
              <span class="acct-search-meta">
                {{ areaSummary(search) }} · {{ l('updated', 'mise à jour') }} {{ when(search.updated_at) }}
              </span>
            </div>
            <span :class="search.search.is_active ? 'acct-live' : 'acct-paused'">
              {{ search.search.is_active ? l('active', 'active') : l('paused', 'en pause') }}
            </span>
            <button
              class="btn-ghost"
              type="button"
              :disabled="update.isPending"
              :aria-label="`${search.search.is_active ? l('Pause', 'Mettre en pause') : l('Resume', 'Reprendre')} ${search.search.name}`"
              @click="togglePaused(search)"
            >
              {{ search.search.is_active ? l('Pause', 'Mettre en pause') : l('Resume', 'Reprendre') }}
            </button>
            <button
              class="btn-ghost"
              type="button"
              :aria-label="`${l('Edit', 'Modifier')} ${search.search.name}`"
              :aria-expanded="editing === search.id"
              @click="editing = editing === search.id ? null : search.id"
            >
              {{ l('Edit', 'Modifier') }}
            </button>
            <button
              class="btn-ghost"
              type="button"
              :disabled="remove.isPending"
              :aria-label="`${l('Delete', 'Supprimer')} ${search.search.name}`"
              @click="confirming = search.id"
            >
              {{ l('Delete', 'Supprimer') }}
            </button>
          </div>
          <ConfirmBar
            v-if="confirming === search.id"
            :label="`${l('Delete', 'Supprimer')} « ${search.search.name} »`"
            :pending="remove.isPending"
            @confirm="onRemove(search.id)"
            @cancel="confirming = null"
          />
          <SearchProfileForm
            v-if="editing === search.id"
            :search="search"
            :pending="update.isPending"
            :error="update.error"
            :submit-label="l('Save changes', 'Enregistrer les modifications')"
            :country-options="countryOptions"
            @submit="onSearchSubmit"
          />
        </li>
      </ul>

      <div v-if="editing === 'new'" class="acct-new">
        <h3>{{ l('New search', 'Nouvelle recherche') }}</h3>
        <SearchProfileForm
          :search="null"
          :pending="create.isPending"
          :error="create.error"
          :submit-label="l('Create search', 'Créer la recherche')"
          :country-options="countryOptions"
          @submit="onSearchSubmit"
        />
        <button class="btn-ghost" type="button" @click="editing = null">
          {{ l('Cancel', 'Annuler') }}
        </button>
      </div>
      <div v-else class="acct-actions">
        <button class="btn-primary" type="button" @click="editing = 'new'">
          {{ l('+ Add a search', '+ Ajouter une recherche') }}
        </button>
      </div>
    </section>
  </div>
</template>
