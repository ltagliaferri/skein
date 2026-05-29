"""Read-only web surface for the content-hash station, served on port 9001.

The structure mirrors the legacy ``skein/web/app.py`` (same routes, same
templates) but reads through :class:`ContentHashAdapter` instead of the legacy
``JSONStore``. Routes are synchronous so FastAPI runs each in the threadpool with
its own per-request adapter (and SQLite connection), which is the connection
isolation the new store needs for a server. Cross-references are rebuilt from the
thread graph; everything else the templates show comes straight off the adapter.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterator, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from .adapter import ContentHashAdapter

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_PORT = 9001  # legacy stays on 8001/8003; new station is 9001
ENV_DATA_DIR = "SKEIN_NEXT_DATA_DIR"
ENV_PROJECT = "SKEIN_NEXT_PROJECT"

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


def get_project_id() -> str:
    """A display label for the station (env, else the data dir's name)."""
    project = os.environ.get(ENV_PROJECT)
    if project:
        return project
    data_dir = get_data_dir()
    return Path(data_dir).resolve().parent.name if data_dir else "skein-next"


def get_adapter() -> Iterator[ContentHashAdapter]:
    """Per-request adapter with its own connection; closed when the request ends."""
    adapter = ContentHashAdapter(get_data_dir())
    try:
        yield adapter
    finally:
        adapter.close()


def _provenance(folio) -> dict:
    """Provenance block. Grows a Sigstore identity once signing is wired in."""
    return {
        "content_hash": folio.content_hash,
        "signed": False,
        "signature_note": "UNSIGNED — Sigstore signing not yet wired into publish",
    }


def create_app() -> FastAPI:
    app = FastAPI(
        title="SKEIN (next)",
        description="Read-only content-hash SKEIN surface",
        version="0.1.0",
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["clean_title"] = clean_title

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, store: ContentHashAdapter = Depends(get_adapter)):
        sites = store.get_sites()
        folios = store.get_folios()

        counts: dict = {}
        for f in folios:
            counts[f.site_id] = counts.get(f.site_id, 0) + 1

        recent = sorted(folios, key=lambda f: f.created_at, reverse=True)[:30]

        return templates.TemplateResponse(
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

    @app.get("/site/{site_id}", response_class=HTMLResponse)
    def site_detail(
        request: Request,
        site_id: str,
        type: Optional[str] = None,
        store: ContentHashAdapter = Depends(get_adapter),
    ):
        site = store.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")

        folios = store.get_folios(site_id=site_id)
        available_types = sorted({f.type for f in folios})
        if type:
            folios = [f for f in folios if f.type == type]
        folios.sort(key=lambda f: f.created_at, reverse=True)

        return templates.TemplateResponse(
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

    @app.get("/folio/{folio_id:path}", response_class=HTMLResponse)
    def folio_detail(
        request: Request,
        folio_id: str,
        store: ContentHashAdapter = Depends(get_adapter),
    ):
        folio = store.get_folio(folio_id)
        if not folio:
            raise HTTPException(status_code=404, detail=f"Folio '{folio_id}' not found")

        site = store.get_site(folio.site_id) if folio.site_id else None
        refs = store.cross_refs(folio.content_hash)

        return templates.TemplateResponse(
            "folio.html",
            {
                "request": request,
                "project_id": get_project_id(),
                "folio": folio,
                "site": site,
                "body_html": render_markdown(folio.content),
                "provenance": _provenance(folio),
                "cross_refs": refs,
            },
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        q: str = Query("", description="Search query"),
        store: ContentHashAdapter = Depends(get_adapter),
    ):
        results = store.search_folios(q, limit=100) if q.strip() else []
        return templates.TemplateResponse(
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
