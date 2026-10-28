#!/usr/bin/env python3
"""Retired archival OCR-diagnostic entry point.

Its historical output remains ordinary diagnostic evidence. This command no
longer starts an LLM or contacts a local model service.
"""

from __future__ import annotations


def main() -> int:
    print("RETIRED_DIAGNOSTIC: hybrid_targeted_audit_v16 is archival evidence, not a supported BookText workflow")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
