"""HTTP logging protection is explicit and covers signed paths as well as queries."""

import io
import logging
import subprocess
import sys

import httpx
import pytest

from foldr_client import DirectUpload, FoldrClient
from foldr_client.logging import HTTPLogFilter


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("httpx", False),
        ("httpx.client", False),
        ("httpcore", False),
        ("httpcore.http11", False),
        ("httpcore.connection.deep", False),
        ("httpx_app", True),
        ("httpcore_metrics", True),
        ("app.httpx", True),
        ("foldr_client", True),
        ("root", True),
    ],
)
def test_filter_matches_only_http_dependency_logger_hierarchies(name, allowed):
    record = logging.LogRecord(name, logging.CRITICAL, __file__, 1, "message", (), None)
    assert HTTPLogFilter().filter(record) is allowed


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
def test_signed_download_and_upload_urls_are_suppressed_on_output_handlers(
    monkeypatch,
    caplog,
    level,
):
    filtered_output, raw_output = io.StringIO(), io.StringIO()
    filtered_handler, raw_handler = (
        logging.StreamHandler(filtered_output),
        logging.StreamHandler(raw_output),
    )
    filtered_handler.addFilter(HTTPLogFilter())
    # Install directly on dependency loggers to exercise the common handler
    # configuration that a root-logger filter alone would fail to protect.
    for name in ("httpx", "httpcore", "foldr_test_app"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [filtered_handler, raw_handler])
        caplog.set_level(level, logger=name)
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "propagate", False)
    core_logger = logging.getLogger("httpcore.http11")
    monkeypatch.setattr(core_logger, "handlers", [])
    caplog.set_level(logging.NOTSET, logger="httpcore.http11")
    monkeypatch.setattr(core_logger, "disabled", False)
    monkeypatch.setattr(core_logger, "propagate", True)
    download_url = "https://storage.example/download-secret?signature=download-query-secret"
    upload_url = "https://storage.example/upload-secret?signature=upload-query-secret"
    requests = []

    def transport(request):
        requests.append(request)
        # MockTransport bypasses HTTPCore. Emit a representative dependency
        # debug record here to cover headers and exception details too.
        core_logger.debug("headers=%s exception=%s", "storage-header-secret", "detail-secret")
        if request.url.host == "foldr.example":
            return httpx.Response(302, headers={"Location": download_url})
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"hello")
        assert request.headers["x-storage-proof"] == "storage-header-secret"
        assert request.read() == b"hello"
        return httpx.Response(201)

    with FoldrClient(
        "https://foldr.example",
        access_token="foldr-token-secret",
        transport=httpx.MockTransport(transport),
    ) as client:
        with client.stream_content("share", "/file") as content:
            assert b"".join(content.iter_bytes()) == b"hello"
    with DirectUpload(
        {
            "url": upload_url,
            "method": "PUT",
            "headers": {"X-Storage-Proof": "storage-header-secret"},
            "size": 5,
        },
        transport=httpx.MockTransport(transport),
    ) as upload:
        upload.send(b"hello", content_length=5)
    logging.getLogger("foldr_test_app").info("application event remains visible")

    assert len(requests) == 3
    assert download_url in raw_output.getvalue()
    assert upload_url in raw_output.getvalue()
    if level == logging.DEBUG:
        assert "storage-header-secret" in raw_output.getvalue()
        assert "detail-secret" in raw_output.getvalue()
    assert filtered_output.getvalue() == "application event remains visible\n"


def test_import_and_construction_do_not_change_logging_configuration():
    # A fresh interpreter verifies the first import, not only a cached module.
    script = """
import logging

loggers = [logging.getLogger(name) for name in ('', 'httpx', 'httpcore')]
for logger in loggers:
    logger.addHandler(logging.NullHandler())
    logger.addFilter(logging.Filter())
    logger.setLevel(logging.WARNING)

def snapshot():
    return [(tuple(item.handlers), tuple(item.filters), item.level,
             item.propagate, item.disabled) for item in loggers]

before = snapshot()
from foldr_client.logging import HTTPLogFilter
HTTPLogFilter()
assert snapshot() == before
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
