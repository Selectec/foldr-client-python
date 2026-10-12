# Developing the Foldr client

From the repository root, create a virtual environment and install development
dependencies:

```console
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

Tests use HTTPX mock transports and lazy byte streams. They need no tokens, site,
network access, browser or underlying storage. Keep response fixtures synthetic
and assert observable protocol behavior, resource cleanup and credential
boundaries. Request-contract tests define the expected local routes and request
shapes explicitly. They do not establish compatibility with a live deployment.

Verify the wheel in a fresh environment, from a directory outside the source tree:

```console
python -m venv tmp/install-check
# Windows; use tmp/install-check/bin/python on Linux/macOS
tmp\install-check\Scripts\python -m pip install dist/foldr_client-0.1.0-py3-none-any.whl
tmp\install-check\Scripts\python -m pip check
tmp\install-check\Scripts\python -c "from foldr_client import FoldrClient, FoldrOAuth; from importlib.metadata import version; print(version('foldr-client'))"
```

CI runs tests and lint on Linux for Python 3.10 through 3.14 and on Windows for
Python 3.13. It builds both sdist and wheel, installs the wheel in a clean virtual
environment, checks dependencies, runs a fake-HTTP smoke check outside the source
tree, runs the full test suite against the installed wheel, and builds a wheel
from the sdist without using the checkout's sources.
CI has read-only repository permissions and contains no deployment or publication.

The project is licensed under the [MIT License](../LICENSE). CI builds and checks
distributions; publishing a package is a separate release action.

The package owns every HTTPX transport passed to its constructors; give each
instance its own transport. A custom transport can supply custom TLS settings, but
must not inject credentials or perform retries on the caller's behalf. TLS
verification is enabled and environment proxy/netrc settings are not inherited.
The client is synchronous. Use one explicit authentication context per instance,
and keep active stream lifetime within that instance's lifetime.

Before live verification, use a caller-provided authorized Foldr test site and
appropriate scopes. Verify account, listing, search incompleteness,
seekable/nonseekable downloads, native exports, direct
redirects and OAuth with that site's registration. Never commit tokens, callback
codes, signed URLs or captured production response bodies.

The full client additionally needs an authorized test share for deliberate write,
version, field, upload-session and sharing-request checks. Verify 202 operations,
approval outcomes, deletion recoverability and partial failures with synthetic
test files. Public discovery/document probes do not authorize modifying live
content. Keep any deployment-specific details and test credentials outside the
repository. Record the server version and tested features in your own test
environment; the offline suite makes no claim of live server verification.
