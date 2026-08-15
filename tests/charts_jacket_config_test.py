from types import SimpleNamespace

from specialized import get_chart_jacket_url


def test_chart_jacket_url_uses_legacy_region_url_when_unconfigured() -> None:
    url = get_chart_jacket_url(SimpleNamespace(), "jp", 42)

    assert url == (
        "https://storage.sekai.best/sekai-jp-assets/music/jacket/jacket_s_042/jacket_s_042.png"
    )


def test_chart_jacket_url_uses_configured_base_url() -> None:
    config = SimpleNamespace(CHART_JACKET_BASE_URL="https://jackets.example/assets/")

    url = get_chart_jacket_url(config, "jp", 42)

    assert url == "https://jackets.example/assets/jacket_s_042/jacket_s_042.png"
