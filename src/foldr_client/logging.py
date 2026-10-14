"""Opt-in protection against credentials in HTTP dependency logs."""

import logging


class HTTPLogFilter(logging.Filter):
    """Suppress HTTPX and HTTPCore records, which can contain signed URLs.

    Attach an instance to every output handler that could receive these records,
    including handlers attached directly to dependency loggers. Filtering a
    parent logger does not filter records propagated from its descendants.

    Suppression covers all levels and the complete record: storage credentials
    may occur in URL paths, query strings, headers, or exception details. Records
    from unrelated loggers are unchanged. Importing or constructing this filter
    never installs it or changes the application's logging configuration.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(
            record.name == name or record.name.startswith(name + ".")
            for name in ("httpx", "httpcore")
        )
