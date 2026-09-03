"""V2 application package (docs/ARCHITECTURE.md §2).

Phase 1 populates two subpackages only:

- `domain` — pure models, enums, value objects and validation rules;
- `compat` — the V1-facing mapping layer, deletable once persistence moves.

The api/, core/, matching/, geo/, llm/ and infrastructure/ packages the
architecture targets arrive with the phases that need them; creating them empty
now would only advertise structure that nothing enforces.
"""
