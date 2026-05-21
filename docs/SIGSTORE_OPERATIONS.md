# Sigstore operations notes

Operational notes for the `skein/signing.py` sigstore boundary. The signing
module wraps `sigstore-python` at a thin boundary (see the module docstring).
Most of the time you don't need to think about it. The notes below cover the
two operational gotchas that come up in practice.

## TUF metadata cache — per-worker isolation

`sigstore-python` keeps a TUF metadata cache on disk. By default it lives in
the user data directory (typically `~/.local/share/sigstore-python/tuf/` on
Linux). When two processes share the same cache and both try to refresh TUF
at the same time, they can race on the cache files. The race window is small
but real.

skein wraps the race fail-closed: a corrupted-cache read surfaces as
`VerifyStatus.OFFLINE_NO_TRUSTED_ROOT` from `_map_sigstore_exception`
(`signing.py` — TUFError / MetadataError / RootError branch), and the verify
result is degraded-but-safe. No silent wrong answer. But operators see flaky
verify behavior with no obvious cause.

Recommended pattern: **give each concurrent process its own
`SIGSTORE_CACHE_DIR`.**

- pytest-xdist workers:
  ```bash
  # In a pytest fixture or pre-test hook:
  os.environ["SIGSTORE_CACHE_DIR"] = f"/tmp/sigstore-{os.getpid()}"
  ```
- Shell scripts that fan out:
  ```bash
  SIGSTORE_CACHE_DIR=/tmp/sigstore-$$ ./my-parallel-job.sh
  ```
- CI runners that share a host across jobs: set
  `SIGSTORE_CACHE_DIR` in the job's environment to a job-scoped path.
- `multiprocessing.Pool` initializer:
  ```python
  def _init_worker():
      os.environ["SIGSTORE_CACHE_DIR"] = f"/tmp/sigstore-{os.getpid()}"
  ```

The friction that motivated this note is `friction-20260521-yxdw` (filed by
bosch-0520, oracle round-7 pass).

## Recognising the symptom

If you see `VerifyStatus.OFFLINE_NO_TRUSTED_ROOT` from verify calls and you
expect upstream Sigstore to be healthy, suspect a cache race before suspecting
a real outage. Signals:

- The failure is transient — retry succeeds.
- The failure correlates with concurrent process count (more workers → more
  failures).
- `~/.local/share/sigstore-python/tuf/` (or `$SIGSTORE_CACHE_DIR`) shows
  partial-write artefacts (`.tmp`, zero-byte metadata).

The verify-side catch-all in `_map_sigstore_exception` already logs
unrecognised sigstore-python exception classes at WARNING level (per
`brief-20260514-7i3w`); the TUF branch maps cleanly without a warning because
TUFError / MetadataError / RootError are a known surface and the
fail-closed mapping is intentional.

## Live sigstore tests

The signing-test suite is offline by default. The few tests that hit live
Sigstore are gated by `SKEIN_TEST_SIGSTORE_LIVE=1` (search the test tree for
`pytest.mark.staging`). The first live run populates the TUF cache; subsequent
runs reuse it. Keep the cache directory writable, or expect TUFError →
`OFFLINE_NO_TRUSTED_ROOT`.

## Out of scope here

- Setting up Sigstore staging credentials.
- OIDC identity provider configuration (see `finding-20260513-w5hq`).
- The signing module's `aud=sigstore` policy (locked in
  `finding-20260513-tx8r`).
