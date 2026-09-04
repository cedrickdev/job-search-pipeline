// The two search tracks, ported verbatim from webapp/src/routes/JobsPage.tsx.
//
// Labels and hints are French because the pipeline they describe is: "Jobs
// étudiants" is part-time student work, "Travail (dev / remote)" is the developer
// track. The hints name a canton and a stack, which is candidate-specific data
// hardcoded in a UI constant — a real problem, but a pre-existing one. Turning it
// into configuration is Country Packs' job (Phase 5); changing it here would be a
// product change wearing a migration's clothes.
export const TRACKS = [
  { key: 'job', label: 'Jobs étudiants', hint: 'Temps partiel · Canton de Vaud' },
  { key: 'travail', label: 'Travail (dev / remote)', hint: 'Laravel · DevOps · à distance' },
] as const

export type TrackKey = (typeof TRACKS)[number]['key']
