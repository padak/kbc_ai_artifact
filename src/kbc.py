"""Keboola Storage Files access layer.

The hub stores everything it needs in Storage Files: serving envelopes in the
host project and canonical copies in the author's project. Both use the same
small interface, defined here as :class:`FilesBackend`:

* :class:`KbcFilesBackend` talks to a real stack through ``kbcstorage`` (the
  official sapi-python-client), which handles the S3/GCS/Azure upload dance.
* :class:`InMemoryFilesBackend` is the drop-in used by tests.

Every failure coming out of ``kbcstorage``/``requests`` is re-raised as
:class:`BackendError` with a concise message that never contains the token.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kbcstorage.client import Client

from src.auth import AuthError, storage_headers

logger = logging.getLogger(__name__)

__all__ = [
    "PAGE_SIZE",
    "BackendError",
    "FileInfo",
    "FilesBackend",
    "KbcFilesBackend",
    "InMemoryFilesBackend",
]

#: Storage Files list pagination size; also the "is there a next page?" probe.
PAGE_SIZE = 100

_REDACTED = "<redacted>"


class BackendError(Exception):
    """Raised for any failure of the underlying files backend."""


@dataclass(frozen=True)
class FileInfo:
    """Metadata of a single Storage File, as the hub cares about it."""

    id: int
    name: str
    tags: list[str]
    created: str  # ISO timestamp exactly as returned by the API
    size_bytes: int


class FilesBackend(Protocol):
    """The subset of Storage Files operations the hub needs."""

    def upload(self, name: str, content: bytes, tags: list[str]) -> int:
        """Upload ``content`` as a permanent file and return its id."""
        ...

    def search_by_tag(self, tag: str) -> list[FileInfo]:
        """Return all files carrying ``tag``, newest first (id descending)."""
        ...

    def download(self, file_id: int) -> bytes:
        """Return the raw bytes of a file."""
        ...

    def delete(self, file_id: int) -> None:
        """Delete a file. Deleting a missing file is not an error."""
        ...


def _safe_message(exc: Exception, token: str = "") -> str:
    """Stringify an exception, making sure the token never leaks into logs."""
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if token:
        message = message.replace(token, _REDACTED)
    return message


def _looks_like_not_found(exc: Exception) -> bool:
    """True when the exception reads like a 404 from the Storage API."""
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _file_info_from_api(payload: dict[str, Any]) -> FileInfo | None:
    """Map one Storage API file dict to :class:`FileInfo`.

    Returns ``None`` (and warns) for entries without a usable integer id;
    a single malformed row must not break a whole listing.
    """
    try:
        file_id = int(payload["id"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Skipping file entry without a usable id: %r", payload)
        return None
    raw_tags = payload.get("tags") or []
    try:
        size_bytes = int(payload.get("sizeBytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    return FileInfo(
        id=file_id,
        name=str(payload.get("name") or ""),
        tags=[str(tag) for tag in raw_tags],
        created=str(payload.get("created") or ""),
        size_bytes=size_bytes,
    )


class KbcFilesBackend:
    """:class:`FilesBackend` backed by a real Keboola stack via ``kbcstorage``."""

    def __init__(
        self, stack_url: str, token: str, project_id: int | None = None
    ) -> None:
        self.stack_url = stack_url.rstrip("/")
        self._token = token
        self._project_id = project_id
        self._client: Client | None = None

    def _files(self) -> Any:
        """Return the ``files`` sub-client, building the client on first use."""
        if self._client is None:
            try:
                self._client = Client(self.stack_url, self._token)
            except Exception as exc:  # noqa: BLE001 - normalized to BackendError
                raise BackendError(
                    f"Could not create a Storage client for {self.stack_url}: "
                    f"{_safe_message(exc, self._token)}"
                ) from exc
            self._apply_auth(self._client.files)
        return self._client.files

    def _apply_auth(self, endpoint: Any) -> None:
        """Point one ``kbcstorage`` endpoint at the right authentication scheme.

        ``kbcstorage`` only knows ``X-StorageApi-Token``. A programmatic bearer
        authenticates as ``Authorization`` plus ``X-KBC-ProjectId`` instead, so
        the endpoint's header map is rewritten in place — ``requests`` drops a
        header whose value is ``None``, which is how the token header is
        removed without touching the library.
        """
        try:
            headers = storage_headers(self._token, self._project_id)
        except AuthError as exc:
            raise BackendError(str(exc)) from exc
        if "Authorization" not in headers:
            return
        endpoint._auth_header.update(headers)
        endpoint._auth_header["X-StorageApi-Token"] = None

    def upload(self, name: str, content: bytes, tags: list[str]) -> int:
        """Write ``content`` to a temp file named ``name`` and upload it.

        ``kbcstorage`` uploads from a real path and derives the Storage file
        name from the basename, so the temp file must be named exactly ``name``.
        """
        file_name = Path(name).name
        if not file_name:
            raise BackendError(f"Invalid file name: {name!r}")
        files = self._files()
        temp_dir = tempfile.mkdtemp(prefix="kbc-upload-")
        try:
            file_path = Path(temp_dir) / file_name
            file_path.write_bytes(content)
            file_id = files.upload_file(
                str(file_path), tags=list(tags), is_permanent=True
            )
            try:
                return int(file_id)
            except (TypeError, ValueError) as exc:
                raise BackendError(
                    f"Upload of {file_name!r} returned an unusable id: {file_id!r}"
                ) from exc
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized to BackendError
            raise BackendError(
                f"Upload of {file_name!r} failed: {_safe_message(exc, self._token)}"
            ) from exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def search_by_tag(self, tag: str) -> list[FileInfo]:
        """List every file tagged ``tag``, paginating until the last page."""
        files = self._files()
        found: list[FileInfo] = []
        offset = 0
        while True:
            try:
                page = files.list(tags=[tag], limit=PAGE_SIZE, offset=offset)
            except Exception as exc:  # noqa: BLE001 - normalized to BackendError
                raise BackendError(
                    f"Listing files tagged {tag!r} failed: "
                    f"{_safe_message(exc, self._token)}"
                ) from exc
            page = page or []
            for payload in page:
                info = _file_info_from_api(payload)
                if info is not None:
                    found.append(info)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        found.sort(key=lambda info: info.id, reverse=True)
        return found

    def download(self, file_id: int) -> bytes:
        """Download a file into a temp dir and return its bytes."""
        files = self._files()
        temp_dir = tempfile.mkdtemp(prefix="kbc-download-")
        try:
            local_path = files.download(file_id, temp_dir)
            if not local_path:
                # Older client versions return None; the temp dir holds exactly
                # one downloaded file in that case.
                candidates = [p for p in Path(temp_dir).iterdir() if p.is_file()]
                if len(candidates) != 1:
                    raise BackendError(
                        f"Download of file {file_id} produced "
                        f"{len(candidates)} files, expected exactly one"
                    )
                local_path = candidates[0]
            return Path(local_path).read_bytes()
        except BackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized to BackendError
            raise BackendError(
                f"Download of file {file_id} failed: "
                f"{_safe_message(exc, self._token)}"
            ) from exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def delete(self, file_id: int) -> None:
        """Delete a file; an already-missing file counts as success."""
        files = self._files()
        try:
            files.delete(file_id)
        except Exception as exc:  # noqa: BLE001 - normalized to BackendError
            if _looks_like_not_found(exc):
                logger.warning(
                    "File %s already gone, treating delete as done", file_id
                )
                return
            raise BackendError(
                f"Delete of file {file_id} failed: "
                f"{_safe_message(exc, self._token)}"
            ) from exc


class InMemoryFilesBackend:
    """:class:`FilesBackend` kept entirely in memory. Used by tests."""

    def __init__(self) -> None:
        #: Everything stored so far, ``{file_id: (FileInfo, content)}``.
        self.files: dict[int, tuple[FileInfo, bytes]] = {}
        self._next_id = 1

    def upload(self, name: str, content: bytes, tags: list[str]) -> int:
        file_id = self._next_id
        self._next_id += 1
        info = FileInfo(
            id=file_id,
            name=name,
            tags=list(tags),
            created=datetime.now(timezone.utc).isoformat(),
            size_bytes=len(content),
        )
        self.files[file_id] = (info, bytes(content))
        return file_id

    def search_by_tag(self, tag: str) -> list[FileInfo]:
        matches = [info for info, _ in self.files.values() if tag in info.tags]
        matches.sort(key=lambda info: info.id, reverse=True)
        return matches

    def download(self, file_id: int) -> bytes:
        entry = self.files.get(file_id)
        if entry is None:
            raise BackendError(f"File {file_id} not found")
        return entry[1]

    def delete(self, file_id: int) -> None:
        self.files.pop(file_id, None)


if TYPE_CHECKING:  # pragma: no cover - static conformance check only
    _kbc_backend: FilesBackend = KbcFilesBackend("https://example.keboola.com", "")
    _memory_backend: FilesBackend = InMemoryFilesBackend()
