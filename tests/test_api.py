import httpx
import pytest

from edinet_util_mcp_server import api


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)


def client(handler) -> api.EdinetClient:
    return api.EdinetClient("k", transport=httpx.MockTransport(handler), spacing=0)


def test_document_returns_the_zip_and_sends_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Subscription-Key"] == "k"
        assert request.url.params["type"] == "5"
        return httpx.Response(200, content=b"PK\x03\x04",
                              headers={"Content-Type": "application/octet-stream"})

    assert client(handler).document("S100AAAA", api.DOC_TYPE_CSV) == b"PK\x03\x04"


def test_json_error_with_http_200_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"metadata": {"status": "404",
                                                      "message": "Not Found"}})

    with pytest.raises(api.DocumentUnavailable, match="404 Not Found"):
        client(handler).document("S100AAAA", api.DOC_TYPE_CSV)


def test_daily_list_checks_metadata_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"metadata": {"status": "400",
                                                      "message": "Bad Request"}})

    with pytest.raises(api.EdinetError, match="400 Bad Request"):
        client(handler).daily_list("2026-06-26")


def test_429_widens_spacing_and_retries():
    answers = iter([httpx.Response(429), httpx.Response(
        200, json={"metadata": {"status": "200"}, "results": []})])
    edinet = client(lambda request: next(answers))
    assert edinet.daily_list("2026-06-26")["results"] == []
    assert edinet.requests == 2
    assert edinet._pacer.spacing == api.WIDE_SPACING


def test_load_api_key_reads_env_file_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nEDINET_API_KEY='abc'\n", encoding="utf-8")
    assert api.load_api_key(env) == "abc"
    with pytest.raises(api.EdinetError):
        api.load_api_key(tmp_path / "missing")
