"""Read-only web surface for the content-hash station (Slice 4).

The legacy SKEIN web app (``skein/web``) is reused in shape but not in code: a
content-hash store adapter (:mod:`skein_next.web.adapter`) presents the new store
behind the same handful of read methods the templates expect, so the page
structure and four of the five templates carry over unchanged. The one thing
rebuilt is cross-references — under hash identity you can't regex folio ids out
of prose, so references come from the thread graph and the alias table instead.
"""
