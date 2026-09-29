# BookText post-audit cleanup report

Issue: #104  
Date: 2026-09-30  
Release baseline retained: `baa9bfae51796abc38d9805975fd0e7d5e33a6b9`

## Result

Cleanup completed conservatively.  Only two directories were deleted, each by
the production `reap_explicit` operation after it verified the exact marker,
the expected workspace ID, containment, and a dead owner PID.  No broad
temporary-directory deletion, name-based process termination, or deletion in
user folders was used.

## Removed

| Classification | Path class | Count | Reclaimed bytes |
| --- | --- | ---: | ---: |
| DELETE | exact stale `booktext.owned_workspace` with marker and dead owner | 2 | 10,761,085 |

The removed workspaces were managed transient areas below the system temporary
directory.  Their owners were PIDs `97233` and `97076`, both no longer alive.
One contained abandoned Surya batch images from the pre-fix controlled audit
interruption; the other contained only its ownership marker.

## Retained

- `/Applications/BookText.app` — current local application baseline.
- `docs/audits/FINAL_AUDIT.md` and `docs/audits/final_audit_report.json` —
  compact final-audit evidence.
- The isolated audit outputs and their canonical raw/CDM artifacts — retained
  for traceability rather than treated as cache.
- User source documents and outputs in Downloads/Documents/iCloud — outside
  BookText-managed ownership and untouched.
- The primary repository's uncommitted table-reconstruction experiments —
  deliberately untouched.

## Unknown / deliberately not deleted

Many historical `/private/tmp/booktext-*` paths are registered Git worktrees,
legacy evidence locations, or lack a current BookText ownership marker.  They
were classified as `UNKNOWN`, not deleted.  The cleanup scope does not infer
ownership from a filename and does not remove Git worktrees or unmarked data.

## Verification

- Full production regression suite: `150 PASS`.
- Native build and protocol tests: PASS.
- Strict bundle signing verification: PASS.
- LaunchServices launch of the freshly built app: PASS; the audit-owned
  app process was then stopped by its exact PID.
- A representative existing seven-page job remained machine-inspectable with
  complete status, valid CDM, and validated Markdown/HTML/DOCX/EPUB/PDF
  exports.
- No BookText-owned Surya/llama-server descendant remained after cleanup.

The release baseline behavior did not change.  This report records a one-time,
ownership-safe cleanup rather than a repository refactor.
