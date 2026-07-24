# Release bootstrap files

`interskein-primer.txt` is the locked collaboration primer. Do not rewrite it.

After building the approved release wheel twice with one recorded
`SOURCE_DATE_EPOCH`, generate the two wheel-only, fully hashed requirement sets
from the local wheel and the fixed PyPI index:

```bash
uv pip compile bootstrap/interskein.in \
  --find-links /absolute/path/to/dist \
  --default-index https://pypi.org/simple \
  --emit-index-url --generate-hashes --universal --only-binary=:all: \
  --no-annotate --no-header --custom-compile-command "release regeneration command" \
  --output-file bootstrap/interskein-pinned.txt

uv pip compile bootstrap/sigstore.in \
  --default-index https://pypi.org/simple \
  --emit-index-url --generate-hashes --universal --only-binary=:all: \
  --no-annotate --no-header --custom-compile-command "release regeneration command" \
  --output-file bootstrap/sigstore-pinned.txt
```

Confirm `interskein-pinned.txt` contains only the approved local wheel hash for
the root `interskein==0.3.0` line. `--only-binary=:all:` is also mandatory at
install time; hashes for index sdists may appear in generated transitive entries,
but pip is forbidden from selecting them.

Signing is an interactive Patrick gate. After PyPI serves the exact approved
wheel bytes, directly sign all three raw files:

```bash
python -m sigstore sign --bundle bootstrap/sigstore-pinned.txt.sigstore.json \
  bootstrap/sigstore-pinned.txt
python -m sigstore sign --bundle bootstrap/interskein-pinned.txt.sigstore.json \
  bootstrap/interskein-pinned.txt
python -m sigstore sign --bundle bootstrap/interskein-primer.txt.sigstore.json \
  bootstrap/interskein-primer.txt
```

Verify each bundle offline against subject `patricksmyth01@gmail.com` and issuer
`https://accounts.google.com` before deploying the six files to
`<station-data-dir>/bootstrap/`.
