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
# Local build, signing, and launch

Build the app only through the repository script:

```bash
./scripts/build_booktext_app.sh
```

It assembles `BookText.app`, applies an ad-hoc local signature *after* every
`Contents/` file has been copied, and fails unless strict macOS verification
passes. This local build does not need a Developer ID certificate or changes
to SIP/Gatekeeper. Launch the resulting bundle normally, for example:

```bash
open BookTextApp/build/BookText.app
```
