# Security

## This project handles API keys. It never stores them.

The panel reads its OpenCode Zen key from a credentials document that lives
**outside this repository**:

```
$DSH_HOME/.credentials.yaml        # default: ~/.dsh/.credentials.yaml
```

That file is expected to be owner-only (`0600`); `dsh_key_store.py` enforces
that on read and write, and refuses a document anyone else can read.

## What the code promises

- The key is only ever sent in an `Authorization: Bearer <key>` header, to the
  endpoint that key belongs to (`opencode.ai` for Zen, `openrouter.ai` for the
  OpenRouter row).
- The key is never printed, logged, echoed back, or rendered. The UI shows a
  fingerprint — a short prefix plus a digest — which is what `--show-key` and
  the key rows display.
- Writes are validated against the live endpoint *before* they happen, so a
  mistyped key cannot overwrite a working one, then committed under a
  `<file>.lock` writer lock with a temp-file + `rename` and `0600` permissions.
- `test_key_management.py` asserts at the end of every run that your real
  `~/.dsh/.credentials.yaml` is unchanged byte for byte.

## Reporting a problem

If you find a way to make the panel leak a credential — into a log, the UI, an
error message, or a file that is not owner-only — please open an issue, or
contact the maintainer privately if you would rather not disclose it publicly
first.

## If you commit a key by accident

Rotate it. Deleting the commit is not enough: GitHub keeps unreachable objects
for a while, and forks, caches and CI logs keep their own copies. Revoke the key
at the provider, issue a new one, and store it with `dsh_key_store.py set`.
