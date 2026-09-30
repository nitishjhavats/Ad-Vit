"""The creatives bucket, from the runtime's side.

Supabase Storage is its own service with its own API, and the runtime talks to
it over HTTP with the service-role key. That key is the one credential in this
codebase that is deliberately NOT a Postgres role: it authenticates to the
Storage service, which enforces its own bucket policies, and it never touches
the database connection pools - so it has nothing to do with the BYPASSRLS
question 20260911000007 answered.

Two operations, and the path is the tenancy.

Every object lives at ``<workspace_id>/<creative_id>.<ext>``. The storage
policies read the first path segment through ``is_workspace_member``, and this
module never builds a path from anything a caller supplied: the workspace comes
from an AuthorizedWorkspace and the creative id from a row this process wrote.
A caller cannot name a path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BUCKET = "creatives"

EXTENSIONS = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class StorageUnavailable(RuntimeError):
    """The Storage service refused or could not be reached. 503, never a silent
    empty result: a creative that cannot be fetched has not been rated."""


@dataclass(frozen=True, slots=True)
class SignedUpload:
    path: str
    url: str
    token: str


def object_path(workspace_id: str, creative_id: str, content_type: str) -> str:
    ext = EXTENSIONS.get(content_type)
    if ext is None:
        raise ValueError(f"{content_type!r} is not an accepted creative type")
    return f"{workspace_id}/{creative_id}.{ext}"


def _client() -> httpx.Client:
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise StorageUnavailable(
            "SUPABASE_SERVICE_ROLE_KEY is not configured, so the runtime cannot "
            "reach the creatives bucket"
        )
    return httpx.Client(
        base_url=f"{settings.supabase_url.rstrip('/')}/storage/v1",
        headers={
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "apikey": settings.supabase_service_role_key,
        },
        timeout=30.0,
    )


def sign_upload(path: str) -> SignedUpload:
    """A one-shot upload URL for exactly this path.

    The browser PUTs the bytes here directly, so a 300 MB video never passes
    through the runtime. The token is bound to the path and expires; a client
    cannot use it to write anywhere else.
    """
    try:
        with _client() as http:
            response = http.post(f"/object/upload/sign/{BUCKET}/{path}")
    except httpx.HTTPError as exc:
        raise StorageUnavailable(f"could not reach storage: {exc}") from exc

    if response.status_code != 200:
        raise StorageUnavailable(
            f"storage refused to sign an upload for {path}: "
            f"HTTP {response.status_code} {response.text[:200]}"
        )
    body = response.json()
    # The Storage API returns a relative URL under /storage/v1 plus the token
    # as a query parameter; the client uploads to the absolute form.
    settings = get_settings()
    return SignedUpload(
        path=path,
        url=f"{settings.supabase_url.rstrip('/')}/storage/v1{body['url']}",
        token=str(body.get("token", "")),
    )


def download_to(path: str, destination: Path) -> Path:
    """Fetch the object to a local file for ffmpeg. Streamed, so a large video
    is never held in memory."""
    try:
        with _client() as http, http.stream("GET", f"/object/{BUCKET}/{path}") as response:
            if response.status_code != 200:
                raise StorageUnavailable(
                    f"storage returned HTTP {response.status_code} for {path}"
                )
            with destination.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise StorageUnavailable(f"could not download {path}: {exc}") from exc
    return destination


def exists(path: str) -> bool:
    """Whether the client actually uploaded. Registering a creative before the
    bytes are there would produce a row that can never be analysed."""
    try:
        with _client() as http:
            response = http.head(f"/object/{BUCKET}/{path}")
    except httpx.HTTPError as exc:
        raise StorageUnavailable(f"could not check {path}: {exc}") from exc
    return response.status_code == 200


Downloader = Callable[[str, Path], Path]
