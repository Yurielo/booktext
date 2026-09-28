# BookText.app

This is the native SwiftUI Phase-E shell. It owns file selection, preflight,
JSONL progress, cancellation, and result actions; Python BookText Core remains
the only implementation of OCR, QA, decisions, CDM, and export.

Build a local `.app` with:

```bash
./scripts/build_booktext_app.sh
open BookTextApp/build/BookText.app
```

The project is a standard Swift Package that Xcode can open directly. The
current Mac can use `BOOKTEXT_CORE_PATH` before first launch, or edit the Core
script path in the app. The value and last output reference are persisted in
`UserDefaults`.
