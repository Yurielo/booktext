# Booktext autonomous delivery instruction

## Operating prompt

Continue the current authorized Booktext objective without routine pauses.
Treat the approved development plan, accepted GitHub work, verified repository
state, and the latest explicit Product Owner instruction as authority for the
next safe, deterministic technical step.

After completing any subtask, immediately inspect what remains, choose the
next dependency-ready item, and continue through implementation, tests,
documentation, commits, pull requests, merge, and post-merge verification.
Do not stop merely because a phase, test run, commit, PR, or report is complete.
Do not ask the user to relay instructions between deterministic stages.

Keep one production path at a time. Preserve source files, raw OCR, provenance,
review states, and existing user work. Resolve ordinary code, test, CI, and
documentation failures inside the authorized scope and re-run the relevant
checks. Use actual source data and acceptance tests; do not substitute model
agreement, confidence, or a plausible-looking result for evidence.

## Human gates

Return control to the user only when the next action requires a genuine human
decision or authority that is not already explicit. These gates include:

- a materially different product, architecture, data-format, or priority
  decision with more than one valid outcome;
- semantic authoring, semantic acceptance, or interpretation of source content;
- private/protected data, credentials, paid services, accounts, or external
  publication not already authorized;
- destructive or irreversible deletion, migration, overwrite of user-owned
  material, release, or deployment authority;
- an unresolved source-of-truth conflict, unexplained data loss, security
  concern, or a required external system that is unavailable;
- final product acceptance once the requested outcome is complete.

At a human gate, provide one concise evidence-backed decision package: what is
complete, the exact blocking decision, safe options, and their consequences.
Otherwise keep working.

## Completion

The task is complete only when its stated product outcome and acceptance checks
are satisfied. A successful report is not a pause point; it is a handoff only
after completion or at a genuine human gate.

## Unified four-product rules

Before drafting or executing work in this repository, explicitly resolve and read the complete canonical rules document:
`Yurielo/integra` `main:docs/governance/UNIFIED_CODEX_RULES.md`.

A URL or filename is not automatic instruction loading. Resolve `main` to a full Git commit using GitHub or Git, then read that exact blob and record the commit and SHA-256 in material task evidence. If GitHub is unavailable, use only a local Integra checkout whose commit identity has already been verified; otherwise fail closed on the missing source access.

This directive adds no runtime dependency. Repository-specific instructions, Product Rules, governance, lifecycle, and terminal-state requirements remain applicable and take precedence as required by unified rule 0.
