# Terminal Claude Code intégré — conception

## But et périmètre

Le produit doit proposer un terminal Claude Code dans l’interface web, utilisable pour préparer des candidatures et travailler sur les documents de l’utilisateur sans ouvrir un terminal du système hôte. L’instance utilise une unique clé `ANTHROPIC_API_KEY` fournie côté serveur par l’environnement ou un gestionnaire de secrets. Chaque utilisateur possède toutefois son propre espace de travail, sa propre configuration Claude et ses propres sessions.

Cette fonctionnalité ne remplace pas la couche de fournisseurs LLM. Le fournisseur `ClaudeCodeProvider` actuel continue de retirer tout `ANTHROPIC_*` de ses sous-processus et de ne jamais recevoir de clé. Le terminal est un sous-système distinct, documenté et isolé, avec une frontière d’identifiants dédiée.

Le premier déploiement cible Docker Compose en local et doit rester portable vers un VPS. Il ne couvre pas une authentification Anthropic individuelle, la conservation de transcriptions de terminal, le partage de session entre utilisateurs, ni le contournement de CAPTCHA, MFA, connexion obligatoire ou formulaire ambigu.

## Résultat attendu

Depuis Paramètres → Assistant IA, un utilisateur autorisé peut ouvrir un terminal Claude Code intégré. Il voit un terminal interactif xterm.js dans l’application. La création, l’attachement, le redimensionnement et la fermeture de la session ont des états visibles, accessibles et traduits en français et en anglais. Une session arrêtée, expirée ou refusée n’expose ni son historique ni un secret.

Le terminal peut préparer ou réviser des documents et conseiller l’utilisateur. Il ne modifie pas les protections existantes du workflow de candidature : les arrêts humains sur CAPTCHA, MFA, authentification et formulaire incertain demeurent obligatoires.

## Topologie et frontières de confiance

```text
Navigateur (xterm.js)
  │ WebSocket authentifié + contrôle d’origine
  ▼
API FastAPI publique ── requête de session ──► gestionnaire de terminal privé
  │                                               │ Docker API privée seulement
  │ métadonnées sûres                             ▼
  └──────────────► PostgreSQL                conteneur Claude éphémère
                                                    │
                                      volume privé utilisateur/session
                                      + politique partagée en lecture seule
                                      + ANTHROPIC_API_KEY injectée à l’exécution
```

L’API publique ne reçoit pas le socket Docker et ne peut pas créer un conteneur elle-même. Seul un composant `terminal-manager`, sur un réseau interne, accède au Docker Engine. Les conteneurs de terminal ne sont jamais privilégiés, ne montent jamais le système de fichiers hôte ni le socket Docker et n’acceptent pas de trafic entrant public.

## Identité, WebSocket et autorisation

1. L’utilisateur authentifié demande la création ou la reprise d’une session via une route HTTP. Le backend détermine l’utilisateur à partir de la session existante ; aucun `user_id`, chemin de travail ou nom de conteneur ne vient du navigateur.
2. Le service crée une session avec un UUID imprévisible, son propriétaire, son état et ses échéances. Il demande au gestionnaire privé de créer le runtime, puis retourne seulement l’identifiant de session et une URL WebSocket relative.
3. La connexion WebSocket réutilise l’authentification serveur, vérifie strictement l’en-tête `Origin` contre les origines configurées et confirme que la session appartient à l’utilisateur courant avant tout pont d’E/S.
4. Les messages du navigateur sont des octets de terminal et des événements limités de redimensionnement ou fermeture ; ils ne sont jamais interprétés comme des commandes API, des chemins ou des options Docker.
5. Une reconnexion n’est acceptée que pour le propriétaire, pendant la période de grâce et seulement tant que la session est active. Les tentatives non autorisées reçoivent un code d’échec générique et sont auditées sans données sensibles.

## Cycle de vie et modèle de données

Une table `claude_terminal_sessions` conserve exclusivement : `id`, `owner_user_id`, `state`, `created_at`, `last_activity_at`, `expires_at`, `ended_at`, `runtime_reference` (opaque), et un code d’erreur sûr facultatif. Elle ne conserve ni clé, ni jeton, ni entrée ou sortie du terminal, ni transcript, ni commande.

États autorisés : `CREATING → ACTIVE → STOPPING → TERMINATED`. Les sorties terminales peuvent aussi être `FAILED` ou `EXPIRED`; elles sont finales. La reprise est une nouvelle connexion à une session `ACTIVE`, pas la création d’un deuxième runtime. L’arrêt est idempotent. Toute transition est effectuée transactionnellement par le gestionnaire de session et contrôlée par propriétaire.

Chaque runtime reçoit :

- un volume privé par utilisateur et session pour le répertoire de travail ;
- un répertoire `CLAUDE_CONFIG_DIR` privé au même périmètre, isolant les sessions et états Claude ;
- un fichier de politique commun en lecture seule, sans identifiant et sans secret ;
- `ANTHROPIC_API_KEY` seulement dans l’environnement du processus au démarrage.

Le volume est détruit à la terminaison sauf si une future politique explicite de conservation des artefacts est ajoutée. Les identifiants runtime sont opaques et ne sont pas présentés à l’interface.

## Gestion de la clé et politique Claude

`ANTHROPIC_API_KEY` est définie uniquement dans l’environnement du gestionnaire privé (ou via un gestionnaire de secrets). Elle est validée au démarrage sans être journalisée. Elle ne figure jamais dans `.env.example` avec une valeur réelle, dans PostgreSQL, dans les volumes utilisateur, dans `settings.json`, dans les réponses HTTP/WebSocket, dans les logs, ni dans les diagnostics UI.

L’image runtime épingle une version validée de Claude Code. Son fichier de politique gérée, au format pris en charge par cette version, est fourni par l’image ou monté en lecture seule et garde la priorité sur les réglages utilisateur et projet. Il définit des garde-fous de travail et d’usage mais ne contient pas de credentials. Aucun réglage contrôlable par un utilisateur ne peut assouplir cette politique.

Les connexions LLM utilisateur existantes restent provider-neutral. Ni leur modèle de données ni `ClaudeCodeProvider` ne changent pour transporter cette clé globale.

## Isolation, réseau et ressources

Les runtimes exécutent un utilisateur non-root, un système de fichiers racine en lecture seule lorsque l’image le permet, un volume de travail écrit uniquement, des capacités Linux réduites, `no-new-privileges`, des limites CPU/mémoire/PID, et un délai maximal de démarrage. Le réseau sortant est restreint au minimum nécessaire au service Anthropic ; aucun réseau interne applicatif, PostgreSQL, Redis ou Docker Engine n’est accessible depuis un terminal.

Un reaper traite périodiquement les sessions inactives et expirées, arrête les runtimes orphelins et nettoie leurs volumes. Le démarrage échoue proprement lorsque la clé, l’image, le gestionnaire ou la capacité disponible ne sont pas valides. Les limites de sessions simultanées par utilisateur et globales sont configurables et refusent la demande avant la création du runtime.

## Contrat d’API et UX

L’ancien `POST /api/v2/settings/llm/claude-code/terminal`, qui lançait `xterm` ou Terminal.app sur l’hôte, sera supprimé après migration. Il sera remplacé par des routes documentées pour créer, lire, arrêter et attacher une session à son WebSocket, sous la zone Paramètres/Assistant IA. L’OpenAPI et les types générés deviennent la source de vérité côté Nuxt.

L’interface doit :

- présenter l’état de disponibilité sans révéler si la clé est présente ;
- afficher des messages accessibles et localisés pour création, connexion, expiration, indisponibilité et arrêt ;
- proposer ouvrir, reconnecter (si permis), arrêter et fermer avec des états de chargement ;
- nettoyer le WebSocket, le terminal xterm et les listeners au changement de route ;
- ne jamais insérer le contenu du terminal dans le DOM comme HTML.

La terminalisation est un outil d’assistance, non une autorisation de soumission autonome. Toute candidature conserve les limites quotidiennes, les critères utilisateur et les points d’arrêt humain déjà définis.

## Erreurs et observabilité

Les erreurs publiques se limitent à des codes sûrs : `TERMINAL_UNAVAILABLE`, `TERMINAL_CAPACITY_REACHED`, `TERMINAL_SESSION_NOT_FOUND`, `TERMINAL_NOT_OWNER`, `TERMINAL_EXPIRED`, `TERMINAL_START_FAILED` et `TERMINAL_CONNECTION_CLOSED`. Aucun message ne divulgue un chemin hôte, une adresse Docker, une clé ou la sortie interne de Claude.

Les événements d’audit sont : création demandée, création réussie/échouée, attachement, détachement, arrêt demandé, arrêt terminé, expiration et refus d’accès. Ils contiennent l’ID de session, l’utilisateur, l’horodatage, l’état et le code sûr ; jamais une commande, un prompt, un transcript ou une valeur d’environnement.

Les métriques exposent des compteurs de sessions et de refus, ainsi que les durées de vie et démarrage. Elles ne portent aucun identifiant personnel brut.

## Vérification

Les tests backend couvrent l’autorisation propriétaire, les transitions idempotentes, les limites, les codes d’erreur, l’expiration et la garantie qu’aucun secret n’est sérialisé ou journalisé. Les tests d’intégration utilisent un faux gestionnaire, jamais le Docker Engine de production ni Anthropic.

Les tests Nuxt couvrent les états accessibles et bilingues, l’ouverture, l’arrêt, la reconnexion, le nettoyage et l’absence de rendu HTML non sûr. Les tests Playwright simulent le WebSocket et valident le parcours de paramètres sans envoyer de candidature. Une vérification Compose confirme que l’API publique n’a pas de socket Docker, que seul le gestionnaire y accède et que les volumes des utilisateurs sont distincts.

## Migration et exploitation

La migration est additive : schéma de session, gestionnaire privé, image runtime, routes, interface et tests. Une fois le parcours intégré disponible, le bouton et l’endpoint de lancement macOS/xterm sont retirés, leurs textes i18n et leurs types OpenAPI mis à jour. Les documents de sécurité, de déploiement et d’architecture sont alignés dans la même phase.

En local, Docker Compose charge la clé depuis un fichier `.env` non versionné et la transmet uniquement au gestionnaire. Sur VPS, la même variable provient du mécanisme de secrets du déploiement. Dans les deux cas, l’application refuse l’ouverture d’un terminal si le gestionnaire n’est pas sain ou si la clé n’est pas configurée, sans la révéler.
