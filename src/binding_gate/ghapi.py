"""A minimal GitHub REST client for the workflows (PR 15): artifacts and workflow runs.

Used by the budget ledger (``binding_gate.ledger``) and the dev pipeline helpers
(``binding_gate.pipeline``) inside GitHub Actions, with the workflow's own
``GITHUB_TOKEN`` (permission ``actions: read``). Nothing here runs in the tests
against the network: every function takes a ``transport`` that the tests replace.

Endpoints used (REST API version ``2022-11-28``):

* ``GET /repos/{owner}/{repo}/actions/artifacts`` (paginated, ``per_page`` 100;
  optional ``name`` filter) - every artifact of the repository, with ``expired``,
  ``archive_download_url`` and ``workflow_run.id``;
* ``GET /repos/{owner}/{repo}/actions/runs/{run_id}`` - ``event``, ``path``,
  ``status`` and ``head_repository`` of one workflow run;
* ``GET /repos/{owner}/{repo}/actions/runs?status=in_progress`` - runs in progress;
* ``GET /repos/{owner}/{repo}/actions/artifacts/{id}/zip`` - answers with a
  redirect (HTTP 302) to a short-lived storage URL. The redirect is followed
  WITHOUT the ``Authorization`` header (the storage URL is pre-signed and some
  storage services reject an extra bearer token).

UNVERIFIED (could not be exercised against GitHub when this was written,
2026-11-22): the exact redirect status code of the zip endpoint (any 3xx with a
``Location`` header is followed), and whether expired artifacts stay listed (the
ledger treats a listed expired artifact as unknown spend unless a ledger
snapshot covers it; see ``binding_gate.ledger``).

No secret value is ever printed: errors name the URL path and the HTTP status only.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 200
USER_AGENT = "binding-gate-workflow"

Transport = Callable[[str, str, Mapping[str, str]], tuple[int, dict[str, str], bytes]]
"""``(method, url, headers) -> (status, response headers, body)``; must NOT follow redirects."""


class GitHubAPIError(RuntimeError):
    """A request to the GitHub API failed (the message never contains the token)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None  # urllib then raises HTTPError for the 3xx, which urllib_transport returns as is


def urllib_transport(method: str, url: str, headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
    """The standard-library transport (uses the proxy settings of the environment); never follows redirects."""
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, method=method, headers=dict(headers))
    try:
        with opener.open(request, timeout=120) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubAPIError(f"{method} {urllib.parse.urlsplit(url).path}: {type(exc).__name__}") from None


@dataclass
class GitHubAPI:
    """``repository`` is ``owner/name``; ``token`` the workflow's GITHUB_TOKEN."""

    repository: str
    token: str
    api_url: str = DEFAULT_API_URL
    transport: Transport = urllib_transport
    _runs: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_environment(cls, *, transport: Transport | None = None) -> GitHubAPI:
        """From the variables GitHub Actions sets in every job (GITHUB_REPOSITORY, GITHUB_TOKEN, GITHUB_API_URL).

        GITHUB_TOKEN is not an experiment setting and not one of the study's
        secrets; the workflow passes ``github.token`` to the step explicitly.
        """
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not repository or "/" not in repository:
            raise GitHubAPIError("GITHUB_REPOSITORY is not set (owner/name); this command runs inside GitHub Actions")
        if not token:
            raise GitHubAPIError("GITHUB_TOKEN is not set; pass `GITHUB_TOKEN: ${{ github.token }}` to the step")
        return cls(repository, token, os.environ.get("GITHUB_API_URL", DEFAULT_API_URL), transport or urllib_transport)

    # -- plumbing ------------------------------------------------------------ #

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return f"{self.api_url.rstrip('/')}/repos/{self.repository}{path}{query}"

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        status, _headers, body = self.transport("GET", self.url(path, params), self._headers())
        if status != 200:
            raise GitHubAPIError(f"GET /repos/{self.repository}{path}: HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAPIError(f"GET /repos/{self.repository}{path}: the answer is not JSON") from exc
        if not isinstance(data, dict):
            raise GitHubAPIError(f"GET /repos/{self.repository}{path}: the answer is not a JSON object")
        return data

    def paginate(self, path: str, key: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get_json(path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            batch = data.get(key)
            if not isinstance(batch, list):
                raise GitHubAPIError(f"GET /repos/{self.repository}{path}: no list {key!r} in the answer")
            out.extend(x for x in batch if isinstance(x, dict))
            total = data.get("total_count")
            if len(batch) < PER_PAGE or (isinstance(total, int) and len(out) >= total):
                return out
        raise GitHubAPIError(f"GET /repos/{self.repository}{path}: more than {MAX_PAGES} pages")

    # -- endpoints ----------------------------------------------------------- #

    def artifacts(self, name: str | None = None) -> list[dict[str, Any]]:
        """Every artifact of the repository (expired ones included, flagged ``expired``), newest first."""
        params = {"name": name} if name else None
        return self.paginate("/actions/artifacts", "artifacts", params)

    def run(self, run_id: int) -> dict[str, Any]:
        """One workflow run (cached)."""
        if run_id not in self._runs:
            self._runs[run_id] = self.get_json(f"/actions/runs/{int(run_id)}")
        return self._runs[run_id]

    def active_runs(self) -> list[dict[str, Any]]:
        """Workflow runs that are in progress (a queued or pending run has not spent anything yet; when it
        starts, its own ledger check sees this run in progress)."""
        return [run for run in self.paginate("/actions/runs", "workflow_runs", {"status": "in_progress"})
                if isinstance(run.get("id"), int)]

    def download_artifact(self, artifact: Mapping[str, Any]) -> bytes:
        """The zip of one artifact: the API redirect is followed without the Authorization header."""
        artifact_id = int(artifact["id"])
        url = self.url(f"/actions/artifacts/{artifact_id}/zip")
        status, headers, body = self.transport("GET", url, self._headers())
        for _hop in range(5):
            if status == 200:
                return body
            if 300 <= status < 400 and headers.get("location"):
                status, headers, body = self.transport("GET", headers["location"], {"User-Agent": USER_AGENT})
                continue
            break
        raise GitHubAPIError(f"download of artifact {artifact_id} ({artifact.get('name')!r}): HTTP {status}")


def is_trusted_run(run: Mapping[str, Any], repository: str) -> bool:
    """True for a run started by hand (``workflow_dispatch``) in this repository itself.

    Only users with write access can start ``workflow_dispatch`` runs, so artifacts
    of such runs cannot come from a pull request of a fork (whose workflow file the
    fork controls). The budget ledger reads artifacts of trusted runs only.
    """
    head = run.get("head_repository")
    head_name = head.get("full_name") if isinstance(head, Mapping) else None
    return run.get("event") == "workflow_dispatch" and (head_name is None or head_name == repository)


def extract_zip(data: bytes, destination: str | Path) -> list[str]:
    """Extract an artifact zip into ``destination``, refusing absolute paths and ``..`` (returns member names)."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            pure = PurePosixPath(member.filename)
            if pure.is_absolute() or ".." in pure.parts or member.filename.startswith(("/", "\\")):
                raise GitHubAPIError(f"refusing artifact member {member.filename!r} (path outside the destination)")
            if member.is_dir():
                continue
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                target.write_bytes(source.read())
            names.append(member.filename)
    return names
