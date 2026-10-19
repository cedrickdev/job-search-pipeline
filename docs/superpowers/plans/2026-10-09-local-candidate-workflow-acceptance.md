# Recette locale du parcours candidat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corriger et prouver un parcours candidat suisse local, du profil jusqu’à la préparation Playwright, avec une interface responsive, accessible et bilingue.

**Architecture:** FastAPI reste l’autorité pour le profil, la collecte, les documents et les états de candidature. Nuxt affiche uniquement les réponses typées de l’API et localise leurs états. MapLibre dessine exclusivement les coordonnées fournies par le serveur ; le navigateur Playwright reste isolé et les tests n’envoient rien vers un employeur.

**Tech Stack:** FastAPI, PostgreSQL/PostGIS, Playwright Python worker, Nuxt 4, Vue 3, TypeScript, Nuxt UI, Vitest, Playwright Chromium.

**Spec:** `docs/superpowers/specs/2026-10-09-local-candidate-workflow-acceptance-design.md`

## Global Constraints

- Le périmètre est CH uniquement ; aucun nouveau Country Pack ni source non approuvée.
- Aucun LinkedIn, jobs.ch, CAPTCHA, MFA, login externe ou formulaire ambigu n’est automatisé.
- `JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED=false` dans tous les tests et contrôles locaux.
- Aucun test ne charge clé API, mot de passe, cookie externe, email ou hôte d’employeur.
- Une offre sans coordonnées reste dans la liste et n’obtient jamais un marqueur inventé.
- Les documents restent fondés sur les preuves ; un refus du garde de preuves n’est jamais remplacé par un document inventé.
- Les parcours inclus fonctionnent en FR/EN, au clavier, sans débordement horizontal à 320 px, 375 px, 768 px et bureau.
- Les modifications hors périmètre déjà présentes dans le worktree sont préservées et jamais ajoutées à un commit de ce jalon.

## Review Focus

- Une collecte partielle conserve les résultats précédents et explique la couverture.
- Une panne de style/tuiles de carte garde la liste d’offres utilisable.
- Un échec du garde de preuves ne montre ni PDF ni toast de réussite.
- Un challenge Playwright produit `REQUIRES_HUMAN`, sans tentative de soumission.
- Changer de locale pendant un formulaire conserve les données et ne mélange pas les langues.

### Task 1: Créer la recette Playwright du parcours complet local

**Files:**
- Modify: `frontend/tests/e2e/support/api.ts`
- Modify: `frontend/tests/e2e/support/fixtures.ts`
- Create: `frontend/tests/e2e/local-candidate-workflow.spec.ts`

**Interfaces:**
- Consumes les routes V2 présentes dans `frontend/app/utils/endpoints.ts`.
- Produces un scénario fixture-only : connexion → onboarding → CV de référence → collecte CH → offre → document → préparation humaine.

- [ ] **Step 1: Écrire le test rouge du parcours**

```ts
await page.getByRole('button', { name: /refresh opportunities|actualiser les offres/i }).click()
await expect.poll(() => calls.post('/api/v2/me/search-profiles/search-1/discovery-runs')).toBe(1)
await expect(page.getByText(/requires human review|vérification humaine requise/i)).toBeVisible()
expect(calls.post('/api/v2/applications/app-1/submit')).toBe(0)
```

- [ ] **Step 2: Vérifier l’échec initial**

Run: `npx playwright test tests/e2e/local-candidate-workflow.spec.ts --project=chromium`

Expected: FAIL jusqu’à ce que les fixtures et le câblage complet soient présents.

- [ ] **Step 3: Ajouter les fixtures déterministes et routes HTTP**

Créer un seul propriétaire, une recherche CH, une offre localisée, une offre non localisée, une collecte réussie, un document rendu et une application préparée qui retourne `REQUIRES_HUMAN`. Toute route non déclarée doit faire échouer le test ; aucune requête ne peut atteindre un hôte externe.

- [ ] **Step 4: Vérifier le scénario**

Run: `npx playwright test tests/e2e/local-candidate-workflow.spec.ts --project=chromium`

Expected: PASS et zéro soumission réelle.

- [ ] **Step 5: Commit**

Run: `git add frontend/tests/e2e/support/api.ts frontend/tests/e2e/support/fixtures.ts frontend/tests/e2e/local-candidate-workflow.spec.ts && git commit -m "test: cover safe local candidate workflow"`

### Task 2: Rendre explicites le profil, l’onboarding et le CV de référence

**Files:**
- Modify: `frontend/app/pages/onboarding.vue`
- Modify: `frontend/app/pages/profile.vue`
- Modify: `frontend/app/components/ProfileForm.vue`
- Modify: `frontend/app/components/ReferenceCvImport.vue`
- Modify: `frontend/app/composables/useLocale.ts`
- Test: `frontend/tests/nuxt/pages/onboarding.spec.ts`
- Test: `frontend/tests/nuxt/pages/profile.spec.ts`
- Test: `frontend/tests/nuxt/components/ReferenceCvImport.spec.ts`

**Interfaces:**
- Consumes `POST /api/v2/onboarding/complete`, `PUT /api/v2/me/profile` et `POST /api/v2/me/reference-cv`.
- Produces coordonnées verrouillées hors édition, sélecteur de fichier accessible, choix de destination et toasts confirmés par l’API.

- [ ] **Step 1: Écrire les régressions rouges**

```ts
await wrapper.get('[data-testid="profile-edit"]').trigger('click')
expect(wrapper.get<HTMLInputElement>('#profile-email').element.disabled).toBe(false)
await wrapper.get<HTMLInputElement>('#reference-cv-file').trigger('change', { target: { files: [pdf] } })
expect(toast.add).toHaveBeenCalledWith(expect.objectContaining({ color: 'success' }))
```

- [ ] **Step 2: Exécuter les tests ciblés**

Run: `npm test -- --run frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/components/ReferenceCvImport.spec.ts`

Expected: FAIL si le verrou, le toast ou un libellé accessible/traduit manque.

- [ ] **Step 3: Implémenter les corrections minimales**

Réemployer `UButton`, `USelect`, `UInput` et `useToast`. Le champ fichier natif demeure atteint via son `<label for>`, le nom du fichier passe par `aria-live="polite"`, et un toast ne se déclenche qu’après une mutation réussie. Ajouter toute copie à `useLocale` dans les deux langues.

- [ ] **Step 4: Vérifier les régressions**

Run: `npm test -- --run frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/components/ReferenceCvImport.spec.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add frontend/app/pages/onboarding.vue frontend/app/pages/profile.vue frontend/app/components/ProfileForm.vue frontend/app/components/ReferenceCvImport.vue frontend/app/composables/useLocale.ts frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/components/ReferenceCvImport.spec.ts && git commit -m "fix: clarify candidate profile and reference CV feedback"`

### Task 3: Réparer le câblage carte/liste et les états de collecte

**Files:**
- Modify: `frontend/app/pages/map.vue`
- Modify: `frontend/app/components/map/MapResultCard.vue`
- Modify: `frontend/app/components/map/OpportunityMap.vue`
- Modify: `frontend/app/composables/useDiscovery.ts`
- Modify: `frontend/app/utils/discovery-messages.ts`
- Test: `frontend/tests/nuxt/pages/map.spec.ts`
- Test: `frontend/tests/nuxt/components/MapResultCard.spec.ts`
- Test: `frontend/tests/nuxt/components/OpportunityMap.spec.ts`
- Test: `frontend/tests/e2e/map.spec.ts`

**Interfaces:**
- Consumes les endpoints de collecte, statut et résultats géographiques.
- Produces une actualisation unique, une santé de source localisée, un fallback liste-first et une action d’évaluation/préparation issue du serveur.

- [ ] **Step 1: Écrire les régressions rouges**

```ts
await wrapper.get('[data-testid="map-refresh"]').trigger('click')
expect(http.callsTo('/api/v2/me/search-profiles/search-1/discovery-runs')).toBe(1)
expect(wrapper.text()).toContain('Recherche incomplète')
await expect(page.getByTestId('map-results-list')).toContainText('Unlocated internship')
```

- [ ] **Step 2: Exécuter les tests de carte ciblés**

Run: `npm test -- --run frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/components/MapResultCard.spec.ts frontend/tests/nuxt/components/OpportunityMap.spec.ts && npx playwright test tests/e2e/map.spec.ts --project=chromium`

Expected: FAIL si un contrôle est décoratif, une erreur masque la liste ou un état est mal traduit.

- [ ] **Step 3: Connecter les contrôles aux composables typés**

Le bouton attend `useDiscovery().refresh`, est désactivé uniquement pendant la requête et invalide le résultat propriétaire seulement après succès. L’action évaluer/préparer attend la réponse typée. Les offres remote/non localisées restent sans marqueur. Une erreur MapLibre conserve la liste et affiche un message localisé.

- [ ] **Step 4: Vérifier la carte**

Run: `npm test -- --run frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/components/MapResultCard.spec.ts frontend/tests/nuxt/components/OpportunityMap.spec.ts && npx playwright test tests/e2e/map.spec.ts --project=chromium`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add frontend/app/pages/map.vue frontend/app/components/map/MapResultCard.vue frontend/app/components/map/OpportunityMap.vue frontend/app/composables/useDiscovery.ts frontend/app/utils/discovery-messages.ts frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/components/MapResultCard.spec.ts frontend/tests/nuxt/components/OpportunityMap.spec.ts frontend/tests/e2e/map.spec.ts && git commit -m "fix: wire resilient Swiss map discovery workflow"`

### Task 4: Prouver que documents et préparation reflètent le serveur

**Files:**
- Modify: `frontend/app/components/DocumentGenerateActions.vue`
- Modify: `frontend/app/composables/useDocuments.ts`
- Modify: `frontend/app/pages/applications.vue`
- Modify: `frontend/app/composables/useApplications.ts`
- Test: `frontend/tests/nuxt/components/DocumentGenerateActions.spec.ts`
- Test: `frontend/tests/nuxt/pages/documents.spec.ts`
- Test: `frontend/tests/nuxt/pages/applications.spec.ts`
- Test: `frontend/tests/e2e/documents.spec.ts`
- Test: `frontend/tests/e2e/applications.spec.ts`
- Test: `tests/test_v2_documents.py`
- Test: `tests/test_v2_browser_automation.py`

**Interfaces:**
- Consumes les routes de génération/liste/téléchargement de documents et la transition `POST /prepare`.
- Produces états busy/error/success visibles, téléchargement sûr et état `REQUIRES_HUMAN` sans bouton soumettre.

- [ ] **Step 1: Écrire les régressions rouges**

```ts
await wrapper.getByRole('button', { name: /generate resume|générer le CV/i }).trigger('click')
expect(wrapper.getByRole('status').text()).toMatch(/generated|généré/i)
expect(wrapper.find('[data-testid="submit-application"]').exists()).toBe(false)
```

```python
assert prepared.state is ApplicationState.REQUIRES_HUMAN
assert executor.submit_calls == 0
```

- [ ] **Step 2: Exécuter les suites ciblées**

Run: `uv run pytest tests/test_v2_documents.py tests/test_v2_browser_automation.py -q && npm test -- --run frontend/tests/nuxt/components/DocumentGenerateActions.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts && npx playwright test tests/e2e/documents.spec.ts tests/e2e/applications.spec.ts --project=chromium`

Expected: FAIL si une transition est invisible, les données restent périmées ou une soumission est proposée sans droit.

- [ ] **Step 3: Présenter uniquement le résultat de mutation serveur**

Attendre le résultat, invalider les requêtes seulement après succès, traduire les erreurs typées avec `v2-errors.ts`, conserver les actions autorisées par l’état retourné et expliquer les challenges. Ne créer ni PDF client, ni statut synthétique, ni raccourci de soumission.

- [ ] **Step 4: Vérifier les suites**

Run: `uv run pytest tests/test_v2_documents.py tests/test_v2_browser_automation.py -q && npm test -- --run frontend/tests/nuxt/components/DocumentGenerateActions.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts && npx playwright test tests/e2e/documents.spec.ts tests/e2e/applications.spec.ts --project=chromium`

Expected: PASS sans soumission réelle.

- [ ] **Step 5: Commit**

Run: `git add frontend/app/components/DocumentGenerateActions.vue frontend/app/composables/useDocuments.ts frontend/app/pages/applications.vue frontend/app/composables/useApplications.ts frontend/tests/nuxt/components/DocumentGenerateActions.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts frontend/tests/e2e/documents.spec.ts frontend/tests/e2e/applications.spec.ts tests/test_v2_documents.py tests/test_v2_browser_automation.py && git commit -m "fix: expose document and preparation outcomes safely"`

### Task 5: Compléter l’acceptance responsive, a11y et locale

**Files:**
- Modify: `frontend/app/layouts/default.vue`
- Modify: `frontend/app/assets/css/base.css`
- Modify: `frontend/app/assets/css/components.css`
- Modify: `frontend/app/assets/css/pages.css`
- Modify: `frontend/app/composables/useLocale.ts`
- Modify: `frontend/tests/e2e/sections.spec.ts`
- Modify: `frontend/tests/e2e/shell.spec.ts`
- Test: `frontend/tests/nuxt/layouts/default.spec.ts`
- Test: `frontend/tests/nuxt/pages/login.spec.ts`
- Test: `frontend/tests/nuxt/pages/onboarding.spec.ts`
- Test: `frontend/tests/nuxt/pages/profile.spec.ts`
- Test: `frontend/tests/nuxt/pages/map.spec.ts`
- Test: `frontend/tests/nuxt/pages/documents.spec.ts`
- Test: `frontend/tests/nuxt/pages/applications.spec.ts`

**Interfaces:**
- Consumes le cookie unique `app_locale` via `useLocale`.
- Produces un drawer mobile nommé, des contrôles clavier, des textes FR/EN complets et aucune largeur de page dépassante sur les écrans inclus.

- [ ] **Step 1: Écrire les assertions rouges de viewport et locale**

```ts
for (const width of [320, 375, 768, 1440]) {
  await page.setViewportSize({ width, height: 900 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
}
await expect(page.getByRole('button', { name: /menu|menu de navigation/i })).toBeVisible()
```

- [ ] **Step 2: Exécuter l’acceptance ciblée**

Run: `npm test -- --run frontend/tests/nuxt/layouts/default.spec.ts frontend/tests/nuxt/pages/login.spec.ts frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts && npx playwright test tests/e2e/shell.spec.ts tests/e2e/sections.spec.ts --project=chromium`

Expected: FAIL si la sidebar desktop est visible sur mobile, un nom accessible manque, la page déborde ou une action mélange les langues.

- [ ] **Step 3: Appliquer les corrections ciblées**

Réemployer le drawer/backdrop et les contrôles Nuxt UI existants. Préférer grid/flex/wrap et changements d’ordre CSS aux doublons de markup. Ajouter les clés FR/EN manquantes, conserver la locale entre pages et réserver `aria-live` aux statuts dynamiques.

- [ ] **Step 4: Vérifier le responsive**

Run: `npm test -- --run frontend/tests/nuxt/layouts/default.spec.ts frontend/tests/nuxt/pages/login.spec.ts frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts && npx playwright test tests/e2e/shell.spec.ts tests/e2e/sections.spec.ts --project=chromium`

Expected: PASS dans les deux langues et toutes les largeurs.

- [ ] **Step 5: Commit**

Run: `git add frontend/app/layouts/default.vue frontend/app/assets/css/base.css frontend/app/assets/css/components.css frontend/app/assets/css/pages.css frontend/app/composables/useLocale.ts frontend/tests/e2e/sections.spec.ts frontend/tests/e2e/shell.spec.ts frontend/tests/nuxt/layouts/default.spec.ts frontend/tests/nuxt/pages/login.spec.ts frontend/tests/nuxt/pages/onboarding.spec.ts frontend/tests/nuxt/pages/profile.spec.ts frontend/tests/nuxt/pages/map.spec.ts frontend/tests/nuxt/pages/documents.spec.ts frontend/tests/nuxt/pages/applications.spec.ts && git commit -m "fix: harden candidate workflow accessibility and responsiveness"`

### Task 6: Exécuter le gate local et documenter les limites vérifiées

**Files:**
- Modify: `START.md`
- Modify: `docs/MAP_EXPLORER.md`
- Modify: `docs/OPERATIONS.md`
- Test: `tests/test_v2_discovery_catalog_integration.py`
- Test: `frontend/tests/e2e/local-candidate-workflow.spec.ts`

**Interfaces:**
- Ne produit aucune nouvelle API.
- Produit une procédure locale qui explicite les flags, limites sources et blocages humains.

- [ ] **Step 1: Écrire l’assertion de sûreté finale**

```ts
await expect(page.getByText(/requires human review|vérification humaine requise/i)).toBeVisible()
expect(calls.externalHosts()).toEqual([])
```

- [ ] **Step 2: Vérifier l’intégration backend isolée**

Run: `docker compose --profile tools run --rm browser-tests python -m pytest -p no:cacheprovider tests/test_v2_discovery_catalog_integration.py tests/test_v2_documents.py tests/test_v2_browser_automation.py -q`

Expected: PASS seulement si les actions externes restent simulées et le flux backend durable est valide.

- [ ] **Step 3: Documenter procédure et limitations**

Documenter démarrage, migration et flag de préparation locale ; documenter que toute activation ATS réelle exige une revue distincte et que LinkedIn/jobs.ch, CAPTCHA et MFA sont non pris en charge. Documenter que la couverture carte dépend des sources CH et des coordonnées disponibles.

- [ ] **Step 4: Exécuter le gate de livraison**

Run: `docker compose build api worker frontend terminal-manager && docker compose run --rm migrate && docker compose --profile terminal --profile workers up -d --wait && docker compose --profile tools run --rm browser-tests python -m pytest -p no:cacheprovider tests/test_v2_discovery_catalog_integration.py tests/test_v2_documents.py tests/test_v2_browser_automation.py tests/test_v2_claude_terminal.py tests/test_v2_api_surface.py -q && npm --prefix frontend run typecheck && npm --prefix frontend test && npm --prefix frontend run build && npx --prefix frontend playwright test tests/e2e/local-candidate-workflow.spec.ts tests/e2e/shell.spec.ts tests/e2e/sections.spec.ts tests/e2e/map.spec.ts tests/e2e/documents.spec.ts tests/e2e/applications.spec.ts --project=chromium && git diff --check`

Expected: every command exits 0 and no test submits a real application.

- [ ] **Step 5: Commit**

Run: `git add START.md docs/MAP_EXPLORER.md docs/OPERATIONS.md docs/superpowers/specs/2026-10-09-local-candidate-workflow-acceptance-design.md docs/superpowers/plans/2026-10-09-local-candidate-workflow-acceptance.md && git commit -m "docs: record local candidate workflow acceptance"`
