"""
SKEIN Web UI — read-only, server-rendered.

A plain document surface: an instance hosts one project; a project contains
sites; sites contain folios. You browse sites, read folios with their markdown
rendered, and see provenance (content hash now; Sigstore identity once signing
is wired into the publish path).

Deliberately unstyled beyond legibility. Looks come later, once we know the
shape. No HTMX, no static assets, no design system.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ..storage import JSONStore, get_data_dir_for_project
from ..utils import get_current_status

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# html=False escapes any raw HTML embedded in folio markdown, so rendered
# content cannot inject markup. That is the v0 sanitization posture; the
# untrusted-content wrapping protocol is a separate, later concern.
_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})

# Folio-ID shaped tokens, used to linkify cross-references inside content.
_FOLIO_ID_RE = re.compile(
    r"\b(?:issue|friction|brief|summary|finding|notion|tender|playbook|mantle|writ|plan|hypothesis)"
    r"-\d{8}-[a-z0-9]{4}\b"
)


def clean_title(title: str, fallback: str = "") -> str:
    """First line of a folio title, stripped of leading markdown decoration."""
    if not title:
        return fallback
    # Titles sometimes carry the whole opening block; keep the first real line.
    first = next((ln.strip() for ln in title.splitlines() if ln.strip()), "")
    first = re.sub(r"^#+\s*", "", first)
    first = re.sub(r"^\*\*|^__", "", first)
    if len(first) > 120:
        first = first[:117] + "..."
    return first or fallback


def render_markdown(content: str) -> str:
    """Render folio markdown to HTML (raw HTML escaped)."""
    if not content:
        return ""
    return _md.render(content)


def get_project_id() -> str:
    """Resolve the project this instance serves (env, then nearest .skein/)."""
    project_id = os.environ.get("SKEIN_PROJECT")
    if not project_id:
        import json

        cwd = Path.cwd()
        while cwd != cwd.parent:
            config_file = cwd / ".skein" / "config.json"
            if config_file.exists():
                try:
                    with open(config_file) as f:
                        project_id = json.load(f).get("project_id")
                    if project_id:
                        break
                except Exception:
                    pass
            cwd = cwd.parent
    return project_id or "default"


def get_store() -> JSONStore:
    return JSONStore(get_data_dir_for_project(get_project_id()))


def _status_of(folio, store) -> str:
    return get_current_status(folio.folio_id, store) or folio.status or "open"


def _provenance(folio) -> dict:
    """Provenance block for a folio. Grows a Sigstore identity once signing lands."""
    return {
        "content_hash": folio.content_hash,
        "signed": False,  # no signature_bundle on the model yet
        "signature_note": "UNSIGNED — Sigstore signing not yet wired into publish",
    }


def create_app() -> FastAPI:
    app = FastAPI(title="SKEIN", description="Read-only SKEIN surface", version="0.1.0")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["clean_title"] = clean_title

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, store: JSONStore = Depends(get_store)):
        sites = store.get_sites()
        folios = store.get_folios()

        counts = {}
        for f in folios:
            counts[f.site_id] = counts.get(f.site_id, 0) + 1

        recent = sorted(folios, key=lambda f: f.created_at, reverse=True)[:30]
        for f in recent:
            f.status = _status_of(f, store)

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
    async def site_detail(
        request: Request,
        site_id: str,
        type: Optional[str] = None,
        store: JSONStore = Depends(get_store),
    ):
        site = store.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found")

        folios = store.get_folios(site_id=site_id)
        for f in folios:
            f.status = _status_of(f, store)

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

    @app.get("/folio/{folio_id}", response_class=HTMLResponse)
    async def folio_detail(
        request: Request, folio_id: str, store: JSONStore = Depends(get_store)
    ):
        folio = store.get_folio(folio_id)
        if not folio:
            raise HTTPException(status_code=404, detail=f"Folio '{folio_id}' not found")
        folio.status = _status_of(folio, store)

        site = store.get_site(folio.site_id)

        # Cross-references: other folio IDs mentioned in the body.
        refs = []
        seen = set()
        for ref_id in _FOLIO_ID_RE.findall(folio.content or ""):
            if ref_id == folio_id or ref_id in seen:
                continue
            seen.add(ref_id)
            ref = store.get_folio(ref_id)
            if ref:
                refs.append(ref)

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
    async def search(
        request: Request,
        q: str = Query("", description="Search query"),
        store: JSONStore = Depends(get_store),
    ):
        results = store.search_folios(q, limit=100) if q.strip() else []
        for f in results:
            f.status = _status_of(f, store)
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


def run_server(host: str = "127.0.0.1", port: int = 8003, reload: bool = False):
    app = create_app()
    logger.info(f"Starting SKEIN web UI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
