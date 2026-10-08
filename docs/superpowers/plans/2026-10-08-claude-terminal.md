# Claude Terminal intégré Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Ajouter un terminal Claude Code Dockerisé, isolé par utilisateur et intégré à Nuxt, sans exposer la clé globale et sans modifier la couche LLM provider-neutral.

**Architecture:** FastAPI conserve identité, autorisation, état et WebSocket public. Le nouveau service interne terminal-manager est le seul service qui possède le socket Docker et ANTHROPIC_API_KEY; il lance un runtime non privilégié par session. Nuxt utilise xterm.js et ne reçoit jamais de secret ni de référence Docker.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, httpx, Docker SDK Python, Docker Compose, Nuxt 4, Vue 3, TypeScript, xterm.js, Vitest, Playwright, pytest.

**Spec:** docs/superpowers/specs/2026-10-08-claude-terminal-design.md

## Global Constraints

- ClaudeCodeProvider et SafeCliRunner continuent de retirer ANTHROPIC_*.
- ANTHROPIC_API_KEY existe seulement dans terminal-manager, jamais dans API, Nuxt, PostgreSQL, volumes utilisateur, réponses ou journaux.
- L’API n’a ni socket Docker ni privilège Docker; seul terminal-manager le possède.
- Chaque session isole utilisateur, workspace, CLAUDE_CONFIG_DIR et runtime.
- Les runtimes sont non-root, read-only lorsque possible, sans privilège ni socket Docker, limités en CPU, mémoire et PID.
- Le navigateur ne fournit jamais une commande, un chemin, un nom de conteneur, un utilisateur ou une option Docker.
- CAPTCHA, MFA, login et formulaire ambigu restent bloquants; les tests ne font aucun envoi réel.
- Toutes les interactions sont accessibles, clavier, FR/EN, et aucune sortie terminal n’est injectée comme HTML.

## Review Focus

- Un utilisateur B ne peut ni lire, ni reconnecter, ni arrêter une session A connue.
- Aucune erreur ne divulgue clé, chemin hôte, commande ou contenu de terminal.
- La navigation et la reconnexion nettoient tous les listeners et sockets xterm.
- TTL ou crash nettoie exactement les runtime/volumes concernés, jamais une ressource Docker étrangère.
- Une configuration locale utilisateur ne peut pas assouplir la politique gérée.

---

### Task 1: Persister les sessions propriétaires

**Files:**
- Create: backend/app/claude_terminal/contracts.py
- Create: backend/app/claude_terminal/service.py
- Create: backend/app/claude_terminal/repository.py
- Modify: backend/app/domain/identifiers.py
- Modify: backend/app/infrastructure/database/models.py
- Modify: backend/app/infrastructure/database/mappers.py
- Modify: backend/app/repositories/sqlalchemy_repositories.py
- Create: backend/migrations/versions/rev_0028_claude_terminal_sessions.py
- Test: tests/test_claude_terminal_service.py
- Test: tests/test_v2_persistence_migrations.py

**Interfaces:**
- Produces ClaudeTerminalSessionId, ClaudeTerminalState, ClaudeTerminalSession and ClaudeTerminalSessionRepository.
- Produces ClaudeTerminalSessionService.create_for_user, get_for_user, stop_for_user and expire_due.
- Consumes UserId and the existing SQLAlchemy UUID/timestamp conventions.

- [ ] **Step 1: Write failing service tests**

    async def test_owner_can_stop_twice(service, user_id, now):
        terminal = await service.create_for_user(user_id, now=now)
        assert (await service.stop_for_user(user_id, terminal.id, now=now)).state.name == "TERMINATED"
        assert (await service.stop_for_user(user_id, terminal.id, now=now)).state.name == "TERMINATED"

    async def test_foreign_owner_sees_no_terminal(service, owner, other, now):
        terminal = await service.create_for_user(owner, now=now)
        assert await service.get_for_user(other, terminal.id) is None

- [ ] **Step 2: Run the tests**

Run: uv run pytest tests/test_claude_terminal_service.py -q

Expected: FAIL because no terminal-session contract exists.

- [ ] **Step 3: Implement the domain and database contract**

Add ClaudeTerminalSessionId as a UUID NewType. Add states CREATING, ACTIVE, STOPPING, TERMINATED, FAILED and EXPIRED. Add claude_terminal_sessions with owner_user_id, state, opaque runtime_reference, created_at, last_activity_at, expires_at, ended_at and finite safe_error_code. Index owner/state and expires_at. The revision descends from 0027, uses a state check constraint, and downgrades by dropping indexes then table.

- [ ] **Step 4: Implement owner-scoped repository and transitions**

Every repository query filters user_id. Stopping is idempotent for the owner and foreign/missing ids return None. Store only opaque manager references and finite error codes.

- [ ] **Step 5: Verify and commit**

Run: uv run pytest tests/test_claude_terminal_service.py tests/test_v2_persistence_migrations.py -q

Expected: PASS.

Commit:
    git add backend/app/claude_terminal backend/app/domain/identifiers.py backend/app/infrastructure/database/models.py backend/app/infrastructure/database/mappers.py backend/app/repositories/sqlalchemy_repositories.py backend/migrations/versions/rev_0028_claude_terminal_sessions.py tests/test_claude_terminal_service.py tests/test_v2_persistence_migrations.py
    git commit -m "feat: persist isolated Claude terminal sessions"

### Task 2: Créer le terminal-manager et le runtime sécurisé

**Files:**
- Create: terminal_manager/app.py
- Create: terminal_manager/config.py
- Create: terminal_manager/protocol.py
- Create: terminal_manager/runtime.py
- Create: docker/terminal-manager.Dockerfile
- Create: docker/claude-terminal.Dockerfile
- Create: docker/claude-managed-settings.json
- Modify: pyproject.toml
- Modify: uv.lock
- Test: tests/test_terminal_manager.py
- Test: tests/test_terminal_runtime_image.py

**Interfaces:**
- Produces POST /internal/sessions, DELETE /internal/sessions/{session_id} and WS /internal/sessions/{session_id}/stream, all protected by X-Terminal-Manager-Token.
- Produces TerminalRuntimeManager.create, stop, attach, resize and reap.
- Consumes only session UUID and expiry timestamp; never a browser command or user path.

- [ ] **Step 1: Write failing hardening tests with a fake Docker gateway**

    def test_runtime_is_non_privileged_and_isolated(fake_docker, manager):
        runtime = manager.create(TEST_SESSION_ID, expires_at=FUTURE)
        assert runtime.container_name == f"jobsearch-terminal-{TEST_SESSION_ID}"
        assert fake_docker.created["user"] == "claude"
        assert fake_docker.created["privileged"] is False
        assert fake_docker.created["network_mode"] == "none"
        assert fake_docker.created["security_opt"] == ["no-new-privileges:true"]

- [ ] **Step 2: Run the manager tests**

Run: uv run pytest tests/test_terminal_manager.py tests/test_terminal_runtime_image.py -q

Expected: FAIL because manager and images do not exist.

- [ ] **Step 3: Implement the private protocol**

Validate UUID, expiry, 1..500 terminal columns/rows and manager token before Docker access. Allow only four public-safe internal failures: TERMINAL_UNAVAILABLE, TERMINAL_CAPACITY_REACHED, TERMINAL_START_FAILED and TERMINAL_CONNECTION_CLOSED.

- [ ] **Step 4: Implement lifecycle via injectable Docker gateway**

Create workspace and config volumes from validated UUIDs. Start a non-root runtime with read-only root, dropped capabilities, no-new-privileges, resource limits, no ports and no Docker socket. Open Claude as a TTY exec; relay binary output, resize the exec, stop containers and delete both volumes. Reap only containers carrying the exact application/session labels.

- [ ] **Step 5: Build version-pinned images and policy**

Install one pinned Claude Code version in the runtime image. Set CLAUDE_CONFIG_DIR to its session-private config volume. Put restrictive managed settings in docker/claude-managed-settings.json with no credentials; user/project configuration must not override it. Manager image contains Docker tooling but no Anthropic credential.

- [ ] **Step 6: Verify and commit**

Run: uv run pytest tests/test_terminal_manager.py tests/test_terminal_runtime_image.py -q

Expected: PASS, including no API key serialization and no runtime Docker mount.

Commit:
    git add terminal_manager docker/terminal-manager.Dockerfile docker/claude-terminal.Dockerfile docker/claude-managed-settings.json pyproject.toml uv.lock tests/test_terminal_manager.py tests/test_terminal_runtime_image.py
    git commit -m "feat: add isolated Claude terminal manager"

### Task 3: Ajouter la configuration et Docker Compose

**Files:**
- Modify: .env.example
- Modify: backend/app/core/settings.py
- Modify: docker-compose.yml
- Modify: docs/CREDENTIAL_SECURITY.md
- Modify: docs/PRODUCTION_DEPLOYMENT.md
- Test: tests/test_terminal_settings.py
- Test: tests/test_terminal_compose_security.py

**Interfaces:**
- Produces ClaudeTerminalSettings and a private terminal-manager Compose service.
- API receives manager URL/token only; manager receives the Anthropic key and Docker socket only.

- [ ] **Step 1: Write failing settings and Compose tests**

    def test_api_never_consumes_anthropic_key():
        settings = ClaudeTerminalSettings.from_env({
            "JOBSEARCH_TERMINAL_MANAGER_URL": "http://terminal-manager:8788",
            "JOBSEARCH_TERMINAL_MANAGER_TOKEN": "manager-token",
            "ANTHROPIC_API_KEY": "not-for-api",
        })
        assert settings.manager_url == "http://terminal-manager:8788"
        assert "ANTHROPIC_API_KEY" not in settings.model_dump()

    def test_only_manager_mounts_docker_socket(compose):
        assert "/var/run/docker.sock:/var/run/docker.sock" not in compose["services"]["api"].get("volumes", [])
        assert "/var/run/docker.sock:/var/run/docker.sock" in compose["services"]["terminal-manager"]["volumes"]

- [ ] **Step 2: Run the tests**

Run: uv run pytest tests/test_terminal_settings.py tests/test_terminal_compose_security.py -q

Expected: FAIL before the settings/service exist.

- [ ] **Step 3: Implement strict settings**

Require URL/token only when enabled; validate positive TTL, limits, resource caps and allowed origins. Document empty secret values only in .env.example. API configuration must not declare or read ANTHROPIC_API_KEY.

- [ ] **Step 4: Implement Compose boundary**

Create terminal-manager without host ports on an internal network. Mount socket/key only there. Configure API with manager URL/token, never the key/socket. Add healthcheck and restart policy. Add runtime image build but no long-lived static runtime service.

- [ ] **Step 5: Update operator documentation, verify, commit**

Run: uv run pytest tests/test_terminal_settings.py tests/test_terminal_compose_security.py -q
Run: docker compose config

Expected: PASS and only terminal-manager receives ANTHROPIC_API_KEY.

Commit:
    git add .env.example backend/app/core/settings.py docker-compose.yml docs/CREDENTIAL_SECURITY.md docs/PRODUCTION_DEPLOYMENT.md tests/test_terminal_settings.py tests/test_terminal_compose_security.py
    git commit -m "feat: isolate Claude terminal deployment secrets"

### Task 4: Exposer l’API propriétaire et le relais WebSocket

**Files:**
- Create: backend/app/services/claude_terminal.py
- Create: backend/app/api/routes/claude_terminal.py
- Modify: backend/app/api/dependencies.py
- Modify: backend/app/api/router.py
- Modify: backend/app/api/schemas.py
- Modify: backend/app/api/routes/llm.py
- Test: tests/test_v2_claude_terminal.py
- Test: tests/test_claude_terminal_websocket.py

**Interfaces:**
- Produces POST/GET/DELETE /api/v2/settings/claude-terminal/sessions and WS /api/v2/settings/claude-terminal/sessions/{session_id}/stream.
- Consumes authenticated user, service/repository and InternalTerminalManagerClient.
- Deletes POST /api/v2/settings/llm/claude-code/terminal and the host Terminal.app/xterm launcher.

- [ ] **Step 1: Write failing API tests**

    async def test_create_hides_runtime_and_secrets(client, headers):
        response = await client.post("/api/v2/settings/claude-terminal/sessions", headers=headers)
        assert response.status_code == 201
        assert {"id", "state", "expires_at", "stream_url"} <= set(response.json())
        assert "runtime_reference" not in response.text
        assert "ANTHROPIC" not in response.text

    async def test_foreign_owner_cannot_open_socket(client, owner, other):
        session_id = await create_terminal(client, owner)
        assert await connect_socket(client, session_id, other) == 1008

- [ ] **Step 2: Run tests**

Run: uv run pytest tests/test_v2_claude_terminal.py tests/test_claude_terminal_websocket.py -q

Expected: FAIL because integrated routes do not exist.

- [ ] **Step 3: Implement typed manager client and orchestration**

Create CREATING, call manager under timeout, then mark ACTIVE only after success; failures become FAILED with finite safe code. Stop transitions STOPPING to TERMINATED idempotently. Audit event type, actor/session UUID, time and safe code only.

- [ ] **Step 4: Implement REST and WebSocket validation**

Responses contain only id, state, timestamps, safe_error_code and relative stream_url. Return 404 for foreign/missing session. Verify authenticated cookie and allowlisted Origin before WebSocket acceptance. Accept only JSON input, resize and ping frames; reject invalid/oversized frames. Forward output as binary, do not log contents, update last_activity, and close both sides in finally.

- [ ] **Step 5: Remove the host launcher, verify, commit**

Run: uv run pytest tests/test_v2_claude_terminal.py tests/test_claude_terminal_websocket.py tests/test_claude_terminal_service.py -q

Expected: PASS for owner isolation, origin validation, malformed frames and retry-safe deletion.

Commit:
    git add backend/app/services/claude_terminal.py backend/app/api/routes/claude_terminal.py backend/app/api/dependencies.py backend/app/api/router.py backend/app/api/schemas.py backend/app/api/routes/llm.py tests/test_v2_claude_terminal.py tests/test_claude_terminal_websocket.py
    git commit -m "feat: expose secure Claude terminal sessions"

### Task 5: Ajouter le contrat Nuxt et xterm

**Files:**
- Modify: frontend/package.json
- Modify: frontend/package-lock.json
- Modify: frontend/openapi.json
- Modify: frontend/app/types/api.d.ts
- Modify: frontend/app/types/v2.ts
- Modify: frontend/app/utils/endpoints.ts
- Create: frontend/app/composables/useClaudeTerminal.ts
- Modify: frontend/app/composables/useLlmConnections.ts
- Test: frontend/tests/nuxt/composables/useClaudeTerminal.spec.ts

**Interfaces:**
- Produces useClaudeTerminal with create, connect, sendInput, resize, stop and disconnect.
- Removes useOpenClaudeCodeTerminal and claudeCodeTerminal endpoint escape hatch.

- [ ] **Step 1: Write failing composable tests**

    it("does not connect without an active session", async () => {
      const terminal = useClaudeTerminal(makeApi())
      await expect(terminal.connect()).rejects.toThrow("active session required")
    })

    it("bounds resize frames", () => {
      expect(() => encodeResize(0, 42)).toThrow()
      expect(encodeResize(120, 42)).toEqual({ type: "resize", cols: 120, rows: 42 })
    })

- [ ] **Step 2: Run unit test**

Run: npm test -- --run frontend/tests/nuxt/composables/useClaudeTerminal.spec.ts

Expected: FAIL because the composable is absent.

- [ ] **Step 3: Install and type xterm dependencies**

Use official current documentation to pin @xterm/xterm and @xterm/addon-fit. Regenerate OpenAPI/types after Task 4. Do not add an untyped endpoint cast.

- [ ] **Step 4: Implement lifecycle**

Create session with typed API, build socket URL from same origin and relative stream_url, retain exactly one socket, serialize only input/resize/ping, and expose safe errors. disconnect removes all listeners without stopping; stop calls REST then disconnects.

- [ ] **Step 5: Verify and commit**

Run: npm run typecheck
Run: npm test -- --run frontend/tests/nuxt/composables/useClaudeTerminal.spec.ts

Expected: PASS.

Commit:
    git add frontend/package.json frontend/package-lock.json frontend/openapi.json frontend/app/types/api.d.ts frontend/app/types/v2.ts frontend/app/utils/endpoints.ts frontend/app/composables/useClaudeTerminal.ts frontend/app/composables/useLlmConnections.ts frontend/tests/nuxt/composables/useClaudeTerminal.spec.ts
    git commit -m "feat: add typed Claude terminal client"

### Task 6: Intégrer une interface terminal accessible et responsive

**Files:**
- Create: frontend/app/components/ClaudeTerminalPanel.vue
- Modify: frontend/app/components/LlmConnectionsSettings.vue
- Modify: frontend/app/composables/useLocale.ts
- Modify: frontend/app/assets/css/components.css
- Modify: frontend/app/assets/css/pages.css
- Test: frontend/tests/nuxt/components/ClaudeTerminalPanel.spec.ts
- Test: frontend/tests/nuxt/components/LlmConnectionsSettings.spec.ts
- Test: frontend/tests/e2e/settings-terminal.spec.ts

**Interfaces:**
- Produces the integrated xterm terminal in Settings → Assistant IA.
- Consumes useClaudeTerminal and never exposes manager settings/secrets.

- [ ] **Step 1: Write failing accessibility tests**

    it("has a labelled terminal region and stop button", async () => {
      const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: fakeTerminal } })
      expect(wrapper.get('[role="region"][aria-label="Claude Code terminal"]')).toBeTruthy()
      await wrapper.get('button[name="Stop terminal"]').trigger("click")
      expect(fakeTerminal.stop).toHaveBeenCalledOnce()
    })

    it("does not treat terminal data as HTML", () => {
      const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: failedTerminal("<img src=x>") } })
      expect(wrapper.html()).not.toContain("<img src=x>")
    })

- [ ] **Step 2: Run tests**

Run: npm test -- --run frontend/tests/nuxt/components/ClaudeTerminalPanel.spec.ts frontend/tests/nuxt/components/LlmConnectionsSettings.spec.ts

Expected: FAIL because UI still invokes host launcher.

- [ ] **Step 3: Implement xterm lifecycle and responsive controls**

Instantiate xterm client-side, fit through ResizeObserver, forward onData, render binary output with xterm only, and dispose terminal/observer/socket/listeners on unmount. Replace host-launch button with create/reconnect/stop controls, loading/disabled state, aria-live status and role alert failures. Preserve touch-size controls and horizontal terminal scroll at 390px without page overflow.

- [ ] **Step 4: Add FR/EN copy and E2E scenarios**

Translate availability, start, connect, reconnect, expiry, stop, capacity and unavailable states. Cover desktop/mobile viewport, keyboard controls, French locale and guarantee no former host-launch request.

- [ ] **Step 5: Verify and commit**

Run: npm test -- --run frontend/tests/nuxt/components/ClaudeTerminalPanel.spec.ts frontend/tests/nuxt/components/LlmConnectionsSettings.spec.ts
Run: npx playwright test frontend/tests/e2e/settings-terminal.spec.ts --project=chromium

Expected: PASS.

Commit:
    git add frontend/app/components/ClaudeTerminalPanel.vue frontend/app/components/LlmConnectionsSettings.vue frontend/app/composables/useLocale.ts frontend/app/assets/css/components.css frontend/app/assets/css/pages.css frontend/tests/nuxt/components/ClaudeTerminalPanel.spec.ts frontend/tests/nuxt/components/LlmConnectionsSettings.spec.ts frontend/tests/e2e/settings-terminal.spec.ts
    git commit -m "feat: embed accessible Claude terminal UI"

### Task 7: Nettoyage automatique, audit et validation de livraison

**Files:**
- Create: backend/app/cli/reap_claude_terminals.py
- Modify: backend/app/tasks/worker.py
- Modify: backend/app/retention/service.py
- Modify: backend/app/observability/metrics.py
- Modify: docs/ARCHITECTURE.md
- Modify: docs/LLM_PROVIDER_ARCHITECTURE.md
- Modify: docs/OPERATIONS.md
- Modify: docs/IMPLEMENTATION_PLAN.md
- Modify: START.md
- Test: tests/test_claude_terminal_reaper.py
- Test: tests/test_v2_api_surface.py

**Interfaces:**
- Produces repeatable reaper command and scheduled bounded cleanup.
- Documents separate provider-neutral CLI and manager-injected terminal paths.

- [ ] **Step 1: Write failing cleanup and API-regression tests**

    async def test_reaper_expires_and_stops_runtime(service, manager, expired, now):
        await service.reap_due(now=now)
        manager.stop.assert_awaited_once_with(expired.id)
        assert (await service.get_for_user(expired.owner_user_id, expired.id)).state.name == "EXPIRED"

    def test_openapi_removes_host_launcher(openapi):
        assert "/api/v2/settings/claude-terminal/sessions" in openapi["paths"]
        assert "/api/v2/settings/llm/claude-code/terminal" not in openapi["paths"]

- [ ] **Step 2: Run tests**

Run: uv run pytest tests/test_claude_terminal_reaper.py tests/test_v2_api_surface.py -q

Expected: FAIL before reaper and removal are complete.

- [ ] **Step 3: Implement reaper and observability**

Process expired sessions in bounded batches; stop via manager only; remove only labelled orphan runtime/volumes. Emit state-count, failure-count and duration metrics. Audit creation, attachment, stop, expiry and refusal without prompts, outputs or environment values.

- [ ] **Step 4: Document and run the release matrix**

Document local .env setup, VPS secret injection, rotation, emergency stop, limits, health, cleanup and phase 18 acceptance gate. Then run:

    docker compose build api frontend terminal-manager
    docker compose run --rm migrate
    docker compose up -d --wait
    uv run pytest tests/test_claude_terminal_service.py tests/test_terminal_manager.py tests/test_terminal_settings.py tests/test_v2_claude_terminal.py tests/test_claude_terminal_websocket.py tests/test_claude_terminal_reaper.py tests/test_v2_api_surface.py -q
    cd frontend && npm run typecheck && npm test && npm run build
    npx playwright test frontend/tests/e2e/settings-terminal.spec.ts --project=chromium

Expected: all checks PASS, manager healthy, API has no key/socket and no actual application submission occurs.

- [ ] **Step 5: Manually accept local workflow**

1. User A opens terminal in Settings and receives interactive prompt inside app.
2. User B cannot attach or stop A session.
3. Resize and FR/EN controls remain functional.
4. Stop and expiry remove runtime plus both volumes.
5. Existing application workflow still blocks CAPTCHA, MFA, login and ambiguous forms.

- [ ] **Step 6: Commit final hardening**

    git add backend/app/cli/reap_claude_terminals.py backend/app/tasks/worker.py backend/app/retention/service.py backend/app/observability/metrics.py docs/ARCHITECTURE.md docs/LLM_PROVIDER_ARCHITECTURE.md docs/OPERATIONS.md docs/IMPLEMENTATION_PLAN.md START.md tests/test_claude_terminal_reaper.py tests/test_v2_api_surface.py
    git commit -m "feat: harden integrated Claude terminal"
