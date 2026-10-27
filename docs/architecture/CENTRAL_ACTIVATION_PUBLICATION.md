# Central activation publication

BookText's current activated release lives in
`Yurielo/four-product-architecture/registry.json`. BookText does not keep a
second current-version registry. See that repository's `docs/architecture.md`
and `README.md` for the binding event schema, publisher, stale-event handling,
and rollback rules.

## Existing release boundary

BookText builds and verifies its `.app` in `scripts/build_booktext_app.sh`.
Deployment, Owner Acceptance and Activation remain in BookText's existing
native release lifecycle. The build script does not install an app. A clean
GUI source checkout records its exact commit as `BookTextSourceCommit` in the
built bundle; a development build from a dirty checkout records `unavailable`,
which remains an unknown source commit in any later receipt.

After the existing native lifecycle reports successful Activation, serialize
its already-approved evidence to JSON with these fields:

```json
{
  "status": "SUCCEEDED",
  "native_reference": "https://github.com/Yurielo/booktext/issues/123#issuecomment-1234567890",
  "activated_at": "2026-10-27T12:34:56Z",
  "native_evidence": {
    "existing_release_record": "the existing native lifecycle evidence"
  }
}
```

`activated_at` may be null or omitted if the native evidence does not establish
the time. Do not substitute build, comment, or observation time. The evidence
file is a machine-readable copy of existing native completion evidence; it
does not create QA, Owner Acceptance, or deployment steps.

## Complete and retry publication

Run one `complete` command after successful Activation. It writes the durable
receipt first, then calls the central publisher in the same invocation. Use a
Python environment that has the `jsonschema` dependency required by the central
publisher, and pass the existing architecture checkout:

```sh
python3 tools/activation_publication.py complete \
  --registry-root /path/to/four-product-architecture \
  --native-reference 'https://github.com/Yurielo/booktext/issues/123#issuecomment-1234567890' \
  --native-evidence /path/to/existing-native-activation.json \
  --target /Applications/BookText.app
```

The adapter verifies the installed BookText bundle signature and file-list
digest, reads the configured external Core path from BookText preferences,
requires that Core checkout to be clean, and records its exact commit and Git
tree. It keeps GUI and Core identities separate. The GUI source commit is
unknown if the built bundle has no valid `BookTextSourceCommit`. It records no
SeminarText or VideoText dependency. The current activation must already exist;
this command cannot repeat initial registration.

The generated event and native receipt are written atomically and retained at
`~/Library/Application Support/BookText/Activation/<native-reference-sha256>/`.
That path is an immutable retry receipt, not a competing version authority.
The separate `prepare` command only records a receipt; use it when publication
must run separately. After `complete` reports `PUBLICATION_PENDING`, retry with
the exact receipt path it printed:

```sh
python3 tools/activation_publication.py publish \
  --registry-root /path/to/four-product-architecture \
  --receipt "$HOME/Library/Application Support/BookText/Activation/<native-reference-sha256>"
```

Publication calls the existing central `registry.py publish` implementation.
The adapter uses a disposable central checkout, preserving the user's central
repository worktree. If Git publication fails after activation, it reports
`PUBLICATION_PENDING` and retains the exact event and receipt for retry. An
identical published event is idempotent. If a later BookText event has already
advanced the central predecessor, retry is rejected as `PUBLICATION_STALE`; do
not reseal the old event or repeat Deployment. Other products' events do not
change the BookText predecessor. The central publisher's compare-and-swap still
rejects a concurrent change between inspection and publication.

## Rollback

The adapter does not perform rollback. After an already-authorized BookText
rollback has completed and native verification has succeeded, prepare its new
evidence with `--action rollback --rollback-of sha256:<prior-event-id>`. The
adapter verifies that the installed GUI/Core identities and target exactly
match that earlier activation. The central publisher requires those exact
historical release, target, and dependency identities, plus a new receipt
bound to the restored bytes. An old activation receipt cannot attest a
rollback. No rollback is inferred from a source commit difference or central
publication failure.
