"""Filter composability: tag/day filters preserve current page + other params."""
from web.app import tag_chip, period_buttons


def test_tag_chip_preserves_path():
    h = tag_chip("solana", current_path="/posts", current_qs={"days": 7, "sort": "likes"})
    assert "/posts?" in h
    assert "tag=solana" in h
    assert "days=7" in h
    assert "sort=likes" in h


def test_tag_chip_clears_when_active():
    h = tag_chip("solana", current_path="/posts", current_qs={"days": 7, "tag": "solana"}, active=True)
    # active chip should not re-encode itself
    assert "tag=solana" not in h
    assert "days=7" in h


def test_period_buttons_preserve_tag_and_path():
    h = period_buttons(7, current_path="/posts", current_qs={"tag": "solana", "sort": "likes"})
    assert "/posts?tag=solana" in h
    assert "days=7" in h
    # All-time button should not include days=
    assert "/posts?tag=solana" in h


def test_tag_chip_on_dashboard_stays_on_dashboard():
    h = tag_chip("nft", current_path="/", current_qs={"days": 14, "sort": "views"})
    assert h.startswith("<a href=\"/?")
    assert "tag=nft" in h
