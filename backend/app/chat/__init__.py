"""The career chat as a control plane — parse, validate, execute (Phase 13).

The domain module `backend.app.domain.chat` holds the *grammar*: the closed set of
typed actions the chat may propose, and the rows a conversation persists. This package
is the *machinery* around that grammar, and it exists to enforce one rule the whole
phase rests on — **prose has zero authority**. A sentence the model streams to the user
changes nothing; the only thing that can reach an application service is a validated
`ChatAction`, parsed out of a fenced block independently of the prose, confirmed by a
human, and re-authorized (ownership, domain state, policy, eligibility, the execution
gate) before it runs. A proposal is a request to act, never a permission to.

The modules, in the order a turn flows through them:

- `prompts` — the versioned system prompt that tells the model the grammar and the
  contract (it is *told* the rules; the layer *enforces* them regardless);
- `context` — the bounded, user-isolated, secret-free snapshot the model is given, so
  it proposes actions against ids that exist rather than ones it invents;
- `parsing` — the fenced proposal block pulled out of the prose and validated against the
  domain union, independently of the sentence around it (the authority boundary);
- `validators` — a confirmed action re-authorized against ownership and coarse domain
  state, returning a verdict the executor records rather than raising;
- `executor` — the one place a proposal a human confirmed finally reaches a service,
  idempotent by the proposal's id and audited whatever the outcome.

Nothing here gives the model database access, a shell, a filesystem, the network or a
secret: it speaks prose and emits proposals, and the application does the rest.
"""
