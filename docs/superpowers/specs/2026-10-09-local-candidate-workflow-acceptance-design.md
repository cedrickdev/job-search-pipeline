# Recette locale du parcours candidat — conception

## But

Rendre le parcours local d’un candidat cohérent, actionnable et vérifiable de bout en bout en Suisse : configurer son profil, importer son CV de référence, rechercher des opportunités déjà autorisées, les voir honnêtement dans la liste et sur la carte, analyser l’adéquation, générer des documents ATS, puis préparer une candidature sans envoyer de demande réelle pendant les tests.

Le résultat n’est pas une promesse de candidature autonome sur n’importe quel site. Les CAPTCHA, MFA, connexions obligatoires, formulaires ambigus, LinkedIn et jobs.ch restent hors du périmètre d’exécution automatisée. Les soumissions réelles restent désactivées par défaut.

## Utilisateur et résultat attendu

Un candidat suisse peut, sur ordinateur ou mobile :

1. créer un compte et terminer l’onboarding, y compris l’acceptation des conditions ;
2. consulter et modifier volontairement son profil, avec coordonnées verrouillées hors du mode édition ;
3. importer un CV de référence, savoir immédiatement qu’il a été enregistré et préciser son usage ;
4. lancer une collecte manuelle pour une recherche CH dont il est propriétaire ;
5. distinguer un résultat vide d’une collecte partielle ou en erreur ;
6. explorer les opportunités dans une liste et sur une carte MapLibre, sans faux point géographique ;
7. ouvrir une offre, lire son score et ses critères d’éligibilité, préparer le dossier et télécharger un CV/une lettre adaptés ;
8. préparer une candidature dans un navigateur isolé seulement lorsqu’un adaptateur compatible peut inspecter le formulaire ;
9. comprendre sans ambiguïté si une intervention humaine est nécessaire et pourquoi.

Le parcours doit fonctionner en français et en anglais, au clavier, avec une mise en page mobile qui n’expose jamais la navigation latérale en permanence.

## Périmètre du jalon

### Inclus

- raccordement UI ↔ API des écrans nécessaires au parcours ci-dessus ;
- états chargement, vide, succès et erreur, avec notifications accessibles ;
- audit responsive et accessibilité ciblé sur authentification, onboarding, profil, carte, opportunité, documents et candidatures ;
- cohérence FR/EN de ces écrans et des erreurs affichées ;
- génération locale de documents ATS basée sur les preuves du candidat et l’offre sélectionnée ;
- tests backend, Nuxt et Playwright contre données synthétiques locales ;
- correction des défauts qui empêchent le parcours, avec régression automatisée.

### Exclus

- ajout d’un autre pays ou d’un Country Pack supplémentaire ;
- authentification/crawl LinkedIn, jobs.ch, CAPTCHA, MFA ou contournement anti-bot ;
- candidature réelle automatique dans les tests ;
- nouvelle intégration de paiement ou facturation réelle ;
- activation d’une clé LLM réelle ou d’un terminal Claude réel pendant la recette ;
- migration d’architecture hors nécessité directe d’un défaut avéré.

## Principes de conception

### Une information affichée vient d’une source réelle

Le front-end ne fabrique ni offre, ni entreprise, ni localisation, ni score, ni état de candidature. Les widgets de carte reçoivent des opportunités de l’API, avec leurs coordonnées certifiées ou l’absence explicite de coordonnées. Une collecte sans résultat, partielle ou en erreur explique son état et conserve les précédents résultats autorisés.

### Les actions sont explicites et réversibles autant que possible

Un bouton « Préparer » affiche une progression et retourne un résultat concret, une erreur exploitable ou un blocage humain. Une notification de succès ne se déclenche qu’après la réponse API correspondante. Les actions irréversibles restent séparées et n’acquièrent aucune autorité via un texte LLM.

### Le responsive est structurel

La navigation latérale devient un panneau modal accessible sur mobile. Les actions principales restent visibles et atteignables, tandis que les filtres, détails et actions secondaires passent dans des panneaux déclenchés par des boutons clairement libellés. Aucun écran ne doit déborder horizontalement à 320 px, 375 px ou 768 px.

### La langue est globale et cohérente

Le cookie de locale de l’application pilote les libellés, messages d’erreur, états vides, boutons et attributs d’accessibilité des écrans inclus. Aucun mélange de copie française/anglaise ne doit subsister sur ces parcours. La langue d’un document reste une décision séparée, fondée sur l’offre et le profil, jamais sur le seul langage de l’interface.

### Les limites de sécurité restent visibles

Une préparation Playwright peut inspecter un formulaire compatible mais ne peut ni envoyer une candidature sans autorisation active, ni franchir une connexion, MFA, CAPTCHA ou question incertaine. L’interface doit présenter ces situations comme une étape à traiter, jamais comme un échec silencieux.

## Composants et flux

```text
Profil / Onboarding
  └─ POST/PUT API propriétaire ──► CandidateProfile + SearchProfile + CV de référence

Carte / Liste
  └─ collecte CH déclenchée ──► état de run + opportunités normalisées
       └─ API géographique ──► liste complète + GeoJSON des seules coordonnées connues

Opportunité
  └─ score + éligibilité ──► générateur de documents fondé sur les preuves
       └─ documents versionnés ──► consultation/téléchargement

Candidature
  └─ décision/politique ──► préparation Playwright isolée
       └─ état explicite : prête, intervention requise ou bloquée en sécurité
```

Le backend FastAPI demeure l’autorité sur les données, les décisions, les quotas, les documents et les transitions. Nuxt reste responsable du rendu, de l’accessibilité, de la locale et de l’orchestration d’actions déjà documentées ; aucune règle métier ou règle de sécurité n’est réimplémentée côté navigateur.

## Gestion des erreurs et états

| Situation | Rendu requis |
| --- | --- |
| Aucune offre collectée avec sources saines | état vide avec prochaine action de recherche |
| Source incomplète ou collecte partielle | résultats précédents conservés, avertissement localisé et source concernée |
| Carte/tuiles indisponibles | message localisé, liste d’offres utilisable sans carte |
| Offre sans coordonnées | offre dans la liste, aucun marqueur inventé |
| Génération de document refusée par les preuves | détails sûrs, invitation à compléter les preuves ; pas de document inventé |
| Formulaire non compatible, login, MFA, CAPTCHA ou ambiguïté | état « intervention requise », aucune soumission |
| API indisponible | erreur localisée, action de réessai et absence de faux succès |

## Vérification et critères d’acceptation

1. Le scénario synthétique complet en local atteint le statut de préparation sans appel vers un employeur ni soumission réelle.
2. Chaque clic important de la chaîne affiche un état de progression et un résultat accessible au lecteur d’écran.
3. Les vues des parcours inclus passent les tests de débordement/interaction aux largeurs 320, 375, 768 et bureau, en français et en anglais.
4. La carte fonctionne contre son style configuré ; une panne de style ne rend jamais les offres invisibles dans la liste.
5. Les documents produits ont une version, une langue déterminée sans supposition, des références de preuves et des octets PDF téléchargeables.
6. Une candidature préparée est toujours soumise aux politiques et aux garde-fous de l’Application Engine ; le commutateur de soumission réelle reste à `false` dans les tests.
7. Les tests ne chargent ni clé API, ni mot de passe, ni cookie externe, et ne contactent aucun ATS réel.

## Risques connus acceptés pour ce jalon

- La couverture des offres dépend des sources CH explicitement permises et saines ; elle ne peut pas démontrer « toutes les offres suisses ».
- La qualité typographique finale d’un CV doit encore être approuvée visuellement avec de vraies données candidate, même si les invariants ATS et PDF sont testés.
- Une configuration de tuiles publique est adaptée au développement local ; un fournisseur de tuiles ou style contractuel sera nécessaire sur VPS.
- La facturation réelle et les actions LLM avec une clé réelle font l’objet de configurations/opérations distinctes.
