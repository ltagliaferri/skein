"""Tests for SKEIN address parser."""

from skein.address import parse, ParsedAddress


class TestParse:
    """Test address parsing."""

    def test_bare_folio_id(self):
        result = parse("brief-20251226-n1br")
        assert result == ParsedAddress(folio_id="brief-20251226-n1br", project=None)
        assert not result.is_qualified

    def test_project_qualified(self):
        result = parse("speakbot:brief-20251226-n1br")
        assert result == ParsedAddress(
            folio_id="brief-20251226-n1br", project="speakbot"
        )
        assert result.is_qualified

    def test_project_with_hyphens(self):
        result = parse("my-project:issue-20260101-abcd")
        assert result.project == "my-project"
        assert result.folio_id == "issue-20260101-abcd"

    def test_at_prefix_treated_as_bare(self):
        """Future @peer addresses should be treated as bare for now."""
        result = parse("@patrick:brief-20251226-n1br")
        assert result.project is None
        assert result.folio_id == "@patrick:brief-20251226-n1br"
        assert not result.is_qualified

    def test_http_prefix_treated_as_bare(self):
        """Future URL addresses should be treated as bare for now."""
        result = parse("https://example.com:brief-20251226-n1br")
        assert result.project is None
        assert result.folio_id == "https://example.com:brief-20251226-n1br"

    def test_empty_project_treated_as_bare(self):
        result = parse(":brief-20251226-n1br")
        assert result.project is None
        assert result.folio_id == ":brief-20251226-n1br"

    def test_empty_folio_id_treated_as_bare(self):
        result = parse("speakbot:")
        assert result.project is None
        assert result.folio_id == "speakbot:"

    def test_no_colon(self):
        result = parse("finding-20260101-zzzz")
        assert result.project is None
        assert result.folio_id == "finding-20260101-zzzz"

    def test_multiple_colons_splits_on_first(self):
        """Only the first colon is the separator."""
        result = parse("project:folio:with:colons")
        assert result.project == "project"
        assert result.folio_id == "folio:with:colons"
