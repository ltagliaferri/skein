"""Tests for hypothesis tracking feature."""

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skein.models import Folio, Site, Thread
from skein.storage import JSONStore
from skein.utils import get_current_status


@pytest.fixture
def tmp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d)


@pytest.fixture
def store(tmp_dir):
    return JSONStore(tmp_dir)


def make_site(store, site_id="test-site"):
    """Create a test site."""
    site = Site(
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        purpose="Test site",
    )
    store.save_site(site)
    return site


def make_hypothesis(
    store,
    folio_id="hypothesis-20260305-test",
    site_id="test-site",
    title="IDOR on /api/orders",
    priority="medium",
    source=None,
    status="open",
):
    """Create a test hypothesis folio."""
    metadata = {"priority": priority}
    if source:
        metadata["source"] = source

    folio = Folio(
        folio_id=folio_id,
        type="hypothesis",
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        title=title,
        content=title,
        status=status,
        metadata=metadata,
    )
    store.save_folio(folio)
    return folio


def make_finding(store, folio_id="finding-20260305-evid", site_id="test-site"):
    """Create a test finding folio for evidence."""
    folio = Folio(
        folio_id=folio_id,
        type="finding",
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
        created_by="test-agent",
        title="Evidence finding",
        content="Proof of vulnerability",
        status="open",
        metadata={},
    )
    store.save_folio(folio)
    return folio


class TestHypothesisFolioType:
    """Test that hypothesis is a valid folio type."""

    def test_hypothesis_folio_creation(self, store):
        make_site(store)
        make_hypothesis(store)
        retrieved = store.get_folio("hypothesis-20260305-test")
        assert retrieved is not None
        assert retrieved.type == "hypothesis"
        assert retrieved.title == "IDOR on /api/orders"

    def test_hypothesis_metadata_fields(self, store):
        make_site(store)
        make_hypothesis(store, priority="high", source="RIFT-0042")
        retrieved = store.get_folio("hypothesis-20260305-test")
        assert retrieved.metadata["priority"] == "high"
        assert retrieved.metadata["source"] == "RIFT-0042"

    def test_hypothesis_default_status_is_open(self, store):
        make_site(store)
        make_hypothesis(store)
        retrieved = store.get_folio("hypothesis-20260305-test")
        assert retrieved.status == "open"

    def test_hypothesis_listed_by_type_filter(self, store):
        make_site(store)
        make_hypothesis(store, folio_id="hypothesis-20260305-aaa1")
        make_hypothesis(store, folio_id="hypothesis-20260305-aaa2")
        # Also add a non-hypothesis folio
        make_finding(store)

        all_folios = store.get_folios(site_id="test-site")
        hypotheses = [f for f in all_folios if f.type == "hypothesis"]
        assert len(hypotheses) == 2


class TestHypothesisVerdictViaStatus:
    """Test that verdicts work through the status thread system."""

    def test_verdict_changes_computed_status(self, store):
        make_site(store)
        hypo = make_hypothesis(store)

        # Simulate verdict by creating a status thread
        thread = Thread(
            thread_id="thread-20260305-verd",
            from_id=hypo.folio_id,
            to_id=hypo.folio_id,
            type="status",
            content="confirmed",
            weaver="test-agent",
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(thread)

        computed = get_current_status(hypo.folio_id, store)
        assert computed == "confirmed"

    def test_latest_verdict_wins(self, store):
        make_site(store)
        hypo = make_hypothesis(store)

        # First verdict
        t1 = Thread(
            thread_id="thread-20260305-vrd1",
            from_id=hypo.folio_id,
            to_id=hypo.folio_id,
            type="status",
            content="inconclusive",
            weaver="agent-1",
            created_at=datetime(2026, 3, 5, 10, 0, 0, tzinfo=timezone.utc),
        )
        store.save_thread(t1)

        # Second verdict supersedes
        t2 = Thread(
            thread_id="thread-20260305-vrd2",
            from_id=hypo.folio_id,
            to_id=hypo.folio_id,
            type="status",
            content="confirmed",
            weaver="agent-2",
            created_at=datetime(2026, 3, 5, 11, 0, 0, tzinfo=timezone.utc),
        )
        store.save_thread(t2)

        computed = get_current_status(hypo.folio_id, store)
        assert computed == "confirmed"

    def test_verdict_with_note_in_thread_content(self, store):
        make_site(store)
        hypo = make_hypothesis(store)

        thread = Thread(
            thread_id="thread-20260305-note",
            from_id=hypo.folio_id,
            to_id=hypo.folio_id,
            type="status",
            content="disconfirmed\nTested two accounts, 403 on cross-access",
            weaver="test-agent",
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(thread)

        # Status computation uses first line? No — it uses full content.
        # The get_current_status function returns the full content.
        # The verdict API endpoint puts verdict as first line.
        computed = get_current_status(hypo.folio_id, store)
        assert computed.startswith("disconfirmed")


class TestHypothesisEvidenceThreads:
    """Test evidence linking through reference threads."""

    def test_evidence_reference_thread(self, store):
        make_site(store)
        hypo = make_hypothesis(store)
        finding = make_finding(store)

        # Create reference thread from hypothesis to finding
        ref = Thread(
            thread_id="thread-20260305-evid",
            from_id=hypo.folio_id,
            to_id=finding.folio_id,
            type="reference",
            content="Evidence for confirmed verdict",
            weaver="test-agent",
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(ref)

        # Verify thread is discoverable
        threads = store.get_threads(from_id=hypo.folio_id, type="reference")
        assert len(threads) == 1
        assert threads[0].to_id == finding.folio_id


class TestHypothesisPriorityOrdering:
    """Test that hypothesis next respects priority ordering."""

    def test_priority_ordering(self, store):
        make_site(store)
        make_hypothesis(store, folio_id="hypothesis-20260305-low1", priority="low", title="Low priority")
        make_hypothesis(store, folio_id="hypothesis-20260305-hi01", priority="high", title="High priority")
        make_hypothesis(store, folio_id="hypothesis-20260305-med1", priority="medium", title="Medium priority")

        folios = store.get_folios(site_id="test-site")
        hypotheses = [f for f in folios if f.type == "hypothesis"]
        open_hypos = [h for h in hypotheses if h.status == "open"]

        # Sort like the endpoint does
        priority_order = {"high": 0, "medium": 1, "low": 2}
        open_hypos.sort(
            key=lambda h: (
                priority_order.get(h.metadata.get("priority", "medium"), 1),
                h.created_at,
            )
        )

        assert open_hypos[0].title == "High priority"
        assert open_hypos[-1].title == "Low priority"

    def test_same_priority_oldest_first(self, store):
        make_site(store)

        h1 = Folio(
            folio_id="hypothesis-20260305-old1",
            type="hypothesis",
            site_id="test-site",
            created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            created_by="test-agent",
            title="Older hypothesis",
            content="Older",
            status="open",
            metadata={"priority": "high"},
        )
        store.save_folio(h1)

        h2 = Folio(
            folio_id="hypothesis-20260305-new1",
            type="hypothesis",
            site_id="test-site",
            created_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
            created_by="test-agent",
            title="Newer hypothesis",
            content="Newer",
            status="open",
            metadata={"priority": "high"},
        )
        store.save_folio(h2)

        folios = store.get_folios(site_id="test-site")
        hypotheses = [f for f in folios if f.type == "hypothesis"]

        priority_order = {"high": 0, "medium": 1, "low": 2}
        hypotheses.sort(
            key=lambda h: (
                priority_order.get(h.metadata.get("priority", "medium"), 1),
                h.created_at,
            )
        )

        assert hypotheses[0].folio_id == "hypothesis-20260305-old1"


class TestHypothesisBurndown:
    """Test burndown/status counting."""

    def test_burndown_counts(self, store):
        make_site(store)
        make_hypothesis(store, folio_id="hypothesis-20260305-aaa1")
        make_hypothesis(store, folio_id="hypothesis-20260305-aaa2")
        make_hypothesis(store, folio_id="hypothesis-20260305-aaa3")

        # Verdict on one
        thread = Thread(
            thread_id="thread-20260305-vrd1",
            from_id="hypothesis-20260305-aaa1",
            to_id="hypothesis-20260305-aaa1",
            type="status",
            content="confirmed",
            weaver="test-agent",
            created_at=datetime.now(timezone.utc),
        )
        store.save_thread(thread)

        # Count manually (like the endpoint does)
        folios = store.get_folios(site_id="test-site")
        hypotheses = [f for f in folios if f.type == "hypothesis"]

        counts = {"pending": 0, "total": len(hypotheses)}
        for h in hypotheses:
            computed = get_current_status(h.folio_id, store)
            status = computed or h.status or "open"
            if status == "open":
                counts["pending"] += 1
            else:
                counts[status] = counts.get(status, 0) + 1

        assert counts["total"] == 3
        assert counts["pending"] == 2
        assert counts["confirmed"] == 1


class TestHypothesisPromotion:
    """Test promoting notions to hypotheses."""

    def test_notion_becomes_hypothesis(self, store):
        make_site(store)

        # Create a notion
        notion = Folio(
            folio_id="notion-20260305-idea",
            type="notion",
            site_id="test-site",
            created_at=datetime.now(timezone.utc),
            created_by="test-agent",
            title="Maybe there's an IDOR here",
            content="Noticed sequential IDs in the API responses",
            status="open",
            metadata={},
        )
        store.save_folio(notion)

        # Create hypothesis from notion
        hypo = Folio(
            folio_id="hypothesis-20260305-prom",
            type="hypothesis",
            site_id="test-site",
            created_at=datetime.now(timezone.utc),
            created_by="test-agent",
            title="Maybe there's an IDOR here",
            content="Noticed sequential IDs in the API responses",
            status="open",
            metadata={
                "priority": "medium",
                "source": "promoted from notion-20260305-idea",
            },
        )
        store.save_folio(hypo)

        retrieved = store.get_folio("hypothesis-20260305-prom")
        assert retrieved.type == "hypothesis"
        assert "promoted from" in retrieved.metadata["source"]
