"""Read surface for the content-hash station, served on port 9001.

Two surfaces over one store, chosen by content negotiation:

- **The machine wire** (the product) — the native, verifiable envelope
  (``skein_next.envelope``) served as JSON or agent markdown, plus raw ``.md``
  source. Built straight from the store, not the legacy adapter.
- **HTML** — the human view, still rendered through :class:`ContentHashAdapter`
  and the Jinja templates. Until the HTML-from-wire migration (slice 3) the JSON
  wire (native) and the HTML view (adapter) may disagree on derived fields like
  ``status``/``site``; that is a known, time-boxed inconsistency, not a bug.

Negotiation (§7): an explicit ``.json``/``.md`` suffix wins; else ``Accept``
decides (``application/json`` → JSON, ``text/markdown`` → markdown, ``text/html``
→ HTML); else the User-Agent picks — browsers get HTML, ``skein``/CLI tools get
markdown, anything unrecognized defaults to HTML (the human surface).

Routes are synchronous so FastAPI runs each in the threadpool with its own
per-request adapter (and SQLite connection); the native wire reuses that same
connection via ``adapter.store``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from .. import envelope as envelope_mod
from .. import render as render_mod
from ..resolve import ResolveError, resolve_to_hash
from .adapter import ContentHashAdapter

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_PORT = 9001  # legacy stays on 8001/8003; new station is 9001
ENV_DATA_DIR = "SKEIN_NEXT_DATA_DIR"
ENV_PROJECT = "SKEIN_NEXT_PROJECT"

# The instance's own web:: authority, when published behind a domain. Unset in
# Phase 1 (bare-hash addresses); slice 3's stationfile supplies it.
ENV_AUTHORITY = "SKEIN_NEXT_AUTHORITY"

_MARKDOWN_MEDIA = "text/markdown; charset=utf-8"

# HTTP status for each resolve error code. The envelope is the product; the
# status is a sane secondary signal.
_ERROR_STATUS = {
    "not_found": 404,
    "invalid_address": 400,
    "short_hash_unsupported_remote": 400,
    "hash_mismatch": 422,
    "ambiguous_short_hash": 422,
}

# html=False escapes raw HTML embedded in folio markdown, so rendered content
# cannot inject markup — the v0 sanitization posture, same as the legacy app.
_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})


def clean_title(title: str, fallback: str = "") -> str:
    """First real line of a folio title, stripped of leading markdown decoration."""
    if not title:
        return fallback
    first = next((ln.strip() for ln in title.splitlines() if ln.strip()), "")
    first = re.sub(r"^#+\s*", "", first)
    first = re.sub(r"^\*\*|^__", "", first)
    if len(first) > 120:
        first = first[:117] + "..."
    return first or fallback


def render_markdown(content: str) -> str:
    if not content:
        return ""
    return _md.render(content)


def get_data_dir() -> Optional[str]:
    return os.environ.get(ENV_DATA_DIR)


def get_authority() -> Optional[str]:
    return os.environ.get(ENV_AUTHORITY)


DEFAULT_PROJECT = "interskein"


def get_project_id() -> str:
    """A display label for the station (env, else the data dir's parent name).

    Falls back to ``DEFAULT_PROJECT`` rather than an empty string. The corpus can
    be mounted straight at the data dir (e.g. ``/data`` in the container), which
    leaves ``.parent.name`` empty and otherwise rendered a bare ``— SKEIN`` title
    with no instance name. ``SKEIN_NEXT_PROJECT`` (set in compose) is the real
    configuration knob; this fallback only guarantees the label is never blank.
    """
    project = os.environ.get(ENV_PROJECT)
    if project:
        return project
    data_dir = get_data_dir()
    if data_dir:
        name = Path(data_dir).resolve().parent.name
        if name:
            return name
    return DEFAULT_PROJECT


def get_adapter() -> Iterator[ContentHashAdapter]:
    """Per-request adapter with its own connection; closed when the request ends."""
    adapter = ContentHashAdapter(get_data_dir())
    try:
        yield adapter
    finally:
        adapter.close()


# --- content negotiation ----------------------------------------------------

_AGENT_UA_TOKENS = ("skein", "curl", "wget", "python", "httpie", "go-http")


def split_representation(ref: str) -> tuple[str, Optional[str]]:
    """Split a trailing ``.json``/``.md`` suffix off an address path."""
    for suffix, name in ((".json", "json"), (".md", "md")):
        if ref.endswith(suffix):
            return ref[: -len(suffix)], name
    return ref, None


def negotiate(suffix: Optional[str], accept: Optional[str], user_agent: Optional[str]) -> str:
    """Pick a representation: ``json`` | ``md_raw`` | ``markdown`` | ``html``.

    A ``.json``/``.md`` suffix wins. Then an explicit ``Accept``. Absent both, the
    User-Agent decides — a browser (``Mozilla``) gets HTML, a CLI/agent tool gets
    markdown, and anything unrecognized defaults to HTML so the human surface (and
    the existing route tests) keep working.
    """
    if suffix == "json":
        return "json"
    if suffix == "md":
        return "md_raw"

    accept = accept or ""
    if "application/json" in accept:
        return "json"
    if "text/markdown" in accept:
        return "markdown"
    if "text/html" in accept:
        return "html"

    ua = (user_agent or "").lower()
    if "mozilla" in ua:
        return "html"
    if any(token in ua for token in _AGENT_UA_TOKENS):
        return "markdown"
    return "html"


def _wants_machine(request: Request, suffix: Optional[str]) -> Optional[str]:
    """The non-HTML representation for a collection route, or ``None`` for HTML."""
    repr_ = negotiate(suffix, request.headers.get("accept"), request.headers.get("user-agent"))
    return None if repr_ == "html" else repr_


def _immutable_headers(content_hash: str) -> dict:
    # Keyed by the full content hash: the object bytes can never change, so an
    # agent holding them need not refetch (§8).
    return {"ETag": f'"{content_hash}"', "Cache-Control": "public, immutable"}


def _folio_response(request: Request, store, content_hash: str, repr_: str, row) -> Response:
    env = envelope_mod.build_folio_envelope(store, content_hash, row=row)

    if repr_ == "json":
        headers = _immutable_headers(content_hash)
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(env, headers=headers)

    if repr_ == "md_raw":
        headers = _immutable_headers(content_hash)
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return PlainTextResponse(
            render_mod.render_raw_md(env), media_type=_MARKDOWN_MEDIA, headers=headers
        )

    # markdown: a per-fetch nonce makes each response unique, so it is never
    # immutably cached; the nonce also rides a header for parse-free splitting.
    text, nonce = render_mod.render_folio_markdown(env)
    return PlainTextResponse(
        text,
        media_type=_MARKDOWN_MEDIA,
        headers={"X-Skein-Nonce": nonce, "Cache-Control": "no-store"},
    )


def _error_response(
    code: str, address: str, repr_: str, *, origin: Optional[str] = None
) -> Response:
    env = envelope_mod.build_error_envelope(code, address, origin=origin)
    status = _ERROR_STATUS.get(code, 400)
    if repr_ == "json":
        return JSONResponse(env, status_code=status, headers={"Cache-Control": "no-store"})
    return PlainTextResponse(
        render_mod.render_error_markdown(env),
        status_code=status,
        media_type=_MARKDOWN_MEDIA,
        headers={"Cache-Control": "no-store"},
    )


def _collection_response(env: dict, repr_: str, *, title: str) -> Response:
    if repr_ == "json":
        return JSONResponse(env, headers={"Cache-Control": "no-cache"})
    text, nonce = render_mod.render_collection_markdown(env, title=title)
    return PlainTextResponse(
        text,
        media_type=_MARKDOWN_MEDIA,
        headers={"X-Skein-Nonce": nonce, "Cache-Control": "no-cache"},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="SKEIN (next)",
        description="Content-addressed SKEIN read surface (machine wire + HTML)",
        version="0.1.0",
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["clean_title"] = clean_title

    # --- index / catalog ----------------------------------------------------

    def _catalog_envelope(store) -> dict:
        rows = store.list_folios(limit=1_000_000)
        recent = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:30]
        entries = [envelope_mod.folio_entry(r) for r in recent]
        counts: dict = {}
        for r in store.folio_site_slugs().values():
            counts[r] = counts.get(r, 0) + 1
        sites = [
            {"slug": slug, "address": h, "href": f"/site/{slug}", "count": counts.get(slug, 0)}
            for slug, h in store.list_slugs()
        ]
        return envelope_mod.build_collection_envelope(
            "catalog",
            "/",
            entries,
            asserted={
                "name": get_project_id(),
                "wire": envelope_mod.SCHEMA,
                "profile": envelope_mod.CANON_PROFILE,
                "sites": sites,
                "total_folios": len(rows),
                "example": "skein fetch sha256::<digest>",
            },
            links={"catalog": "/"},
        )

    def _render_index_html(request, adapter):
        sites = adapter.get_sites()
        folios = adapter.get_folios()
        counts: dict = {}
        for f in folios:
            counts[f.site_id] = counts.get(f.site_id, 0) + 1
        recent = sorted(folios, key=lambda f: f.created_at, reverse=True)[:30]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "project_id": get_project_id(),
                "sites": sorted(sites, key=lambda s: counts.get(s.site_id, 0), reverse=True),
                "counts": counts,
                "recent": recent,
                "total_folios": len(folios),
            },
        )

    @app.get("/")
    def index(request: Request, adapter: ContentHashAdapter = Depends(get_adapter)):
        repr_ = _wants_machine(request, None)
        if repr_ is None:
            return _render_index_html(request, adapter)
        return _collection_response(
            _catalog_envelope(adapter.store), repr_, title=f"Catalog — {get_project_id()}"
        )

    @app.get("/.json")
    def index_json(request: Request, adapter: ContentHashAdapter = Depends(get_adapter)):
        return _collection_response(_catalog_envelope(adapter.store), "json", title="Catalog")

    @app.get("/.md")
    def index_md(request: Request, adapter: ContentHashAdapter = Depends(get_adapter)):
        return _collection_response(
            _catalog_envelope(adapter.store), "markdown", title=f"Catalog — {get_project_id()}"
        )

    # --- site ---------------------------------------------------------------

    def _site_envelope(store, slug: str) -> Optional[dict]:
        site_hash = store.resolve_slug(slug)
        if not site_hash:
            return None
        rows = store.folios_in_site(site_hash)
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        entries = [envelope_mod.folio_entry(r) for r in rows]
        return envelope_mod.build_collection_envelope(
            "site",
            f"/site/{slug}",
            entries,
            asserted={"slug": slug, "address": site_hash, "count": len(rows)},
            links={"catalog": "/", "self": f"/site/{slug}"},
        )

    @app.get("/site/{site_id}", response_class=HTMLResponse)
    def site_detail(
        request: Request,
        site_id: str,
        type: Optional[str] = None,
        adapter: ContentHashAdapter = Depends(get_adapter),
    ):
        slug, suffix = split_representation(site_id)
        repr_ = _wants_machine(request, suffix)
        if repr_ is not None:
            env = _site_envelope(adapter.store, slug)
            if env is None:
                return _error_response("not_found", f"/site/{slug}", repr_)
            return _collection_response(env, repr_, title=f"Site — {slug}")

        site = adapter.get_site(slug)
        if not site:
            raise HTTPException(status_code=404, detail=f"Site '{slug}' not found")
        folios = adapter.get_folios(site_id=slug)
        available_types = sorted({f.type for f in folios})
        if type:
            folios = [f for f in folios if f.type == type]
        folios.sort(key=lambda f: f.created_at, reverse=True)
        return templates.TemplateResponse(
            request,
            "site.html",
            {
                "request": request,
                "project_id": get_project_id(),
                "site": site,
                "folios": folios,
                "available_types": available_types,
                "current_type": type,
            },
        )

    # --- folio --------------------------------------------------------------

    def _render_folio_html(request, adapter, address: str):
        folio = adapter.get_folio(address)
        if not folio:
            raise HTTPException(status_code=404, detail=f"Folio '{address}' not found")
        site = adapter.get_site(folio.site_id) if folio.site_id else None
        refs = adapter.cross_refs(folio.content_hash)
        return templates.TemplateResponse(
            request,
            "folio.html",
            {
                "request": request,
                "project_id": get_project_id(),
                "folio": folio,
                "site": site,
                "body_html": render_markdown(folio.content),
                "provenance": adapter.provenance(folio.content_hash),
                "cross_refs": refs,
            },
        )

    def _serve_bundle(store, address: str) -> Response:
        try:
            content_hash = resolve_to_hash(address, store, local_authority=get_authority())
        except ResolveError as e:
            return _error_response(e.code, e.address, "json", origin=e.origin)
        bundle_json = store.get_signature(content_hash)
        if not bundle_json:
            return _error_response("not_found", f"{address}/bundle", "json")
        return Response(
            bundle_json, media_type="application/json", headers=_immutable_headers(content_hash)
        )

    @app.get("/folio/{ref:path}", response_class=HTMLResponse)
    def folio_detail(
        request: Request,
        ref: str,
        adapter: ContentHashAdapter = Depends(get_adapter),
    ):
        store = adapter.store

        # The signature bundle sub-resource (the dead-end-free provenance link).
        if ref.endswith("/bundle"):
            return _serve_bundle(store, ref[: -len("/bundle")])

        address, suffix = split_representation(ref)
        repr_ = negotiate(suffix, request.headers.get("accept"), request.headers.get("user-agent"))
        if repr_ == "html":
            return _render_folio_html(request, adapter, address)

        try:
            content_hash = resolve_to_hash(address, store, local_authority=get_authority())
        except ResolveError as e:
            return _error_response(e.code, e.address, repr_, origin=e.origin)
        row = store.get_folio(content_hash)
        if row is None:
            return _error_response("not_found", address, repr_)
        return _folio_response(request, store, content_hash, repr_, row)

    # --- search -------------------------------------------------------------

    @app.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        q: str = Query("", description="Search query"),
        adapter: ContentHashAdapter = Depends(get_adapter),
    ):
        results = adapter.search_folios(q, limit=100) if q.strip() else []
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "request": request,
                "project_id": get_project_id(),
                "q": q,
                "results": results,
            },
        )

    return app


def run_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    app = create_app()
    logger.info("Starting new-skein web UI on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
