"""Bounded loading and normalization of raw Live2D master-data tables."""

from __future__ import annotations

import json
import ntpath
import re
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.sanitize import sanitize_url

__all__ = [
    "LIVE2D_TABLE_NAMES",
    "DEFAULT_MASTER_DATA_BRANCH",
    "DEFAULT_MASTER_DATA_ARCHIVE_MAX_BYTES",
    "DEFAULT_MASTER_DATA_EXTRACTED_MAX_BYTES",
    "Live2DMasterDataError",
    "Live2DMasterDataArchiveError",
    "Live2DMasterDataDownloadError",
    "Live2DMasterDataFileError",
    "Live2DMasterDataJSONError",
    "Live2DMasterDataLocationError",
    "Live2DMasterDataShapeError",
    "Live2DMasterDataSnapshot",
    "LocalMasterDataProvider",
    "MasterDataProvider",
    "OnlineMasterDataProvider",
    "PreparedLive2DMasterData",
    "build_live2d_master_data_archive_url",
    "default_online_master_db_version",
    "locate_live2d_master_data_root",
    "prepare_online_master_data",
]


DEFAULT_MASTER_DATA_BRANCH = "main"
DEFAULT_MASTER_DATA_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MASTER_DATA_EXTRACTED_MAX_BYTES = 512 * 1024 * 1024
_ARCHIVE_CHUNK_SIZE = 1024 * 1024
_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".zip")
_VERSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")


class Live2DMasterDataError(ValueError):
    """Base error for invalid or unavailable raw Live2D master data."""


class Live2DMasterDataDownloadError(Live2DMasterDataError):
    """Raised when an online master-data archive cannot be downloaded."""


class Live2DMasterDataArchiveError(Live2DMasterDataError):
    """Raised when an online master-data archive is unsafe or malformed."""


class Live2DMasterDataLocationError(Live2DMasterDataError):
    """Raised when an archive does not contain one complete master-data root."""


class Live2DMasterDataFileError(Live2DMasterDataError):
    """Raised when a required Live2D master-data file cannot be read."""


class Live2DMasterDataJSONError(Live2DMasterDataError):
    """Raised when a required Live2D master-data file is not valid JSON."""


class Live2DMasterDataShapeError(Live2DMasterDataError):
    """Raised when a required Live2D table has an unsupported JSON shape."""


def _freeze_value(value: object) -> object:
    """Copy JSON containers into immutable equivalents without validating values."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(child) for child in value)
    return value


def _freeze_tables(
    tables: Mapping[str, object],
) -> Mapping[str, tuple[Mapping[str, object], ...]]:
    frozen: dict[str, tuple[Mapping[str, object], ...]] = {}
    for table_name in LIVE2D_TABLE_NAMES:
        rows = tables[table_name]
        if not isinstance(rows, (list, tuple)):
            raise Live2DMasterDataShapeError(
                f"normalized Live2D table '{table_name}' must be a sequence of row mappings"
            )

        frozen_rows: list[Mapping[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise Live2DMasterDataShapeError(
                    f"normalized Live2D table '{table_name}' contains a non-object row"
                )
            frozen_row = {key: _freeze_value(value) for key, value in row.items()}
            frozen_rows.append(MappingProxyType(frozen_row))
        frozen[table_name] = tuple(frozen_rows)
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class Live2DMasterDataSnapshot:
    """Immutable, normalized input for :func:`build_live2d_index`."""

    master_db_version: str
    tables: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.master_db_version, str) or not self.master_db_version.strip():
            raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")
        if not isinstance(self.tables, Mapping):
            raise Live2DMasterDataShapeError(
                "normalized Live2D tables must be a mapping of the six required tables"
            )

        missing = [table_name for table_name in LIVE2D_TABLE_NAMES if table_name not in self.tables]
        if missing:
            raise Live2DMasterDataShapeError(
                "normalized Live2D tables are missing: " + ", ".join(missing)
            )
        unknown = sorted(set(self.tables) - set(LIVE2D_TABLE_NAMES))
        if unknown:
            raise Live2DMasterDataShapeError(
                "unsupported normalized Live2D tables: " + ", ".join(unknown)
            )

        object.__setattr__(self, "tables", _freeze_tables(self.tables))


@runtime_checkable
class MasterDataProvider(Protocol):
    """Synchronous source of the normalized Live2D master-data snapshot."""

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Load all six required Live2D business tables."""

        ...


def _validated_branch(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH must be a non-empty string"
        )
    branch = value.strip()
    if (
        "\x00" in branch
        or "\\" in branch
        or branch.startswith("/")
        or branch.endswith("/")
        or any(component in {"", ".", ".."} for component in branch.split("/"))
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in branch)
    ):
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH is not a safe Git branch name"
        )
    return branch


def _validated_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL must be a non-empty repository or archive URL"
        )
    url = value.strip()
    if "\x00" in url:
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL contains a NUL byte"
        )
    try:
        parts = urlsplit(url)
        _ = parts.hostname
    except ValueError as exc:
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL is not a valid URL"
        ) from exc
    if parts.scheme not in {"http", "https", "file"}:
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL must use http, https, or file"
        )
    if parts.scheme in {"http", "https"} and not parts.netloc:
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL must include a host"
        )
    return url


def default_online_master_db_version(branch: str = DEFAULT_MASTER_DATA_BRANCH) -> str:
    """Return a stable label for the latest snapshot of a branch.

    The label deliberately describes a moving branch rather than pretending to
    be a pinned revision.  Slashes and other branch-name punctuation are
    normalized because the Live2D index contract accepts identifier tokens.
    """

    validated = _validated_branch(branch)
    label = re.sub(r"[^A-Za-z0-9_.:+-]", "-", validated)
    label = re.sub(r"-+", "-", label).strip("-")
    if not label:
        raise Live2DMasterDataDownloadError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH cannot produce a version label"
        )
    version = f"latest:{label}"
    if not _VERSION_TOKEN_RE.fullmatch(version):  # pragma: no cover - guarded above
        raise Live2DMasterDataDownloadError("online master-data version label is unsafe")
    return version


def build_live2d_master_data_archive_url(
    repository_or_archive_url: str,
    *,
    branch: str = DEFAULT_MASTER_DATA_BRANCH,
) -> str:
    """Resolve a GitHub repository URL to one branch archive URL.

    Direct archive URLs are returned unchanged.  GitHub repository URLs use
    codeload directly, avoiding the GitHub API and per-table raw requests.
    """

    url = _validated_url(repository_or_archive_url)
    validated_branch = _validated_branch(branch)
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    path = unquote(parts.path)
    path_parts = [part for part in path.strip("/").split("/") if part]
    path_lower = path.casefold()

    if host in {"github.com", "www.github.com"}:
        if path_lower.endswith(_ARCHIVE_SUFFIXES):
            return url
        if len(path_parts) == 2:
            owner, repository = path_parts
            if repository.endswith(".git"):
                repository = repository[:-4]
            if not owner or not repository:
                raise Live2DMasterDataDownloadError(
                    "GitHub master-data repository URL must include owner and repository"
                )
            encoded_owner = quote(owner, safe="")
            encoded_repository = quote(repository, safe="")
            encoded_branch = quote(validated_branch, safe="/")
            return (
                f"https://codeload.github.com/{encoded_owner}/{encoded_repository}"
                f"/tar.gz/refs/heads/{encoded_branch}"
            )
        raise Live2DMasterDataDownloadError(
            "GitHub master-data URL must be a repository URL or a direct archive URL"
        )

    if host == "codeload.github.com" and "/tar.gz/" in path_lower:
        return url
    if path_lower.endswith(_ARCHIVE_SUFFIXES):
        return url
    # Non-GitHub hosts cannot be reliably classified as repositories from their
    # URL alone.  Treat them as direct archive URLs and let archive validation
    # produce the useful error if the payload is not an archive.
    return url


def _archive_member_parts(name: object) -> tuple[str, ...]:
    if not isinstance(name, str):
        raise Live2DMasterDataArchiveError("archive member name is not text")
    normalized = name.rstrip("/")
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith("/")
        or ntpath.isabs(normalized)
        or ntpath.splitdrive(normalized)[0]
        or PurePosixPath(normalized).is_absolute()
    ):
        raise Live2DMasterDataArchiveError(f"archive contains an unsafe path: {name!r}")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise Live2DMasterDataArchiveError(f"archive contains an unsafe path: {name!r}")
    parts = tuple(normalized.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise Live2DMasterDataArchiveError(f"archive contains an unsafe path: {name!r}")
    return parts


def _archive_target(extract_root: Path, parts: tuple[str, ...]) -> Path:
    target = extract_root.joinpath(*parts)
    resolved_root = extract_root.resolve(strict=False)
    try:
        target.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:  # pragma: no cover - the component checks guard this
        raise Live2DMasterDataArchiveError(
            f"archive member escapes extraction root: {'/'.join(parts)!r}"
        ) from exc
    return target


def _ensure_archive_parent(target: Path, extract_root: Path) -> None:
    parent = target.parent
    if parent.exists() and parent.is_symlink():
        raise Live2DMasterDataArchiveError(f"archive member traverses a symbolic link: {target}")
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.resolve(strict=True).relative_to(extract_root.resolve(strict=True))
    except ValueError as exc:  # pragma: no cover - the component checks guard this
        raise Live2DMasterDataArchiveError(
            f"archive member escapes extraction root: {target}"
        ) from exc


def _copy_archive_file(
    source: IO[bytes],
    target: Path,
    expected_size: int,
    extracted_bytes: int,
    max_extracted_bytes: int,
) -> int:
    if expected_size < 0:
        raise Live2DMasterDataArchiveError(f"archive member has a negative size: {target}")
    if extracted_bytes + expected_size > max_extracted_bytes:
        raise Live2DMasterDataArchiveError(
            "archive extracted contents exceed the configured Live2D master-data size limit"
        )
    copied = 0
    try:
        with target.open("xb") as destination:
            while True:
                chunk = source.read(_ARCHIVE_CHUNK_SIZE)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise Live2DMasterDataArchiveError(
                        f"archive member size changed while extracting: {target}"
                    )
                destination.write(chunk)
    except Live2DMasterDataError:
        raise
    except OSError as exc:
        raise Live2DMasterDataArchiveError(
            f"cannot extract archive member {target}: {exc}"
        ) from exc
    if copied != expected_size:
        raise Live2DMasterDataArchiveError(
            f"archive member is truncated: {target} (expected {expected_size}, got {copied})"
        )
    return copied


def _extract_tar_archive(
    archive_path: Path,
    extract_root: Path,
    *,
    max_extracted_bytes: int,
) -> None:
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
            seen: set[tuple[str, ...]] = set()
            extracted_bytes = 0
            for member in members:
                parts = _archive_member_parts(member.name)
                if parts in seen:
                    raise Live2DMasterDataArchiveError(
                        f"archive contains a duplicate path: {member.name!r}"
                    )
                seen.add(parts)
                if member.issym() or member.islnk():
                    raise Live2DMasterDataArchiveError(
                        f"archive contains a symbolic or hard link: {member.name!r}"
                    )
                if not member.isdir() and not member.isfile():
                    raise Live2DMasterDataArchiveError(
                        f"archive contains an unsupported special file: {member.name!r}"
                    )

                target = _archive_target(extract_root, parts)
                if member.isdir():
                    if target.exists() and not target.is_dir():
                        raise Live2DMasterDataArchiveError(
                            f"archive path collides with a file: {member.name!r}"
                        )
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise Live2DMasterDataArchiveError(
                            f"cannot extract archive directory {member.name!r}: {exc}"
                        ) from exc
                    continue

                _ensure_archive_parent(target, extract_root)
                if target.exists() or target.is_symlink():
                    raise Live2DMasterDataArchiveError(
                        f"archive path collides with an existing path: {member.name!r}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise Live2DMasterDataArchiveError(
                        f"cannot read archive member: {member.name!r}"
                    )
                with source:
                    extracted_bytes += _copy_archive_file(
                        source,
                        target,
                        member.size,
                        extracted_bytes,
                        max_extracted_bytes,
                    )
    except Live2DMasterDataError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise Live2DMasterDataArchiveError(
            f"Live2D master-data archive is not a readable tar archive: {archive_path}"
        ) from exc


def _zip_member_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _extract_zip_archive(
    archive_path: Path,
    extract_root: Path,
    *,
    max_extracted_bytes: int,
) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            seen: set[tuple[str, ...]] = set()
            extracted_bytes = 0
            for info in archive.infolist():
                parts = _archive_member_parts(info.filename)
                if parts in seen:
                    raise Live2DMasterDataArchiveError(
                        f"archive contains a duplicate path: {info.filename!r}"
                    )
                seen.add(parts)
                mode = _zip_member_mode(info)
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise Live2DMasterDataArchiveError(
                        f"archive contains an unsupported link or special file: {info.filename!r}"
                    )

                target = _archive_target(extract_root, parts)
                if info.is_dir() or info.filename.endswith("/"):
                    if target.exists() and not target.is_dir():
                        raise Live2DMasterDataArchiveError(
                            f"archive path collides with a file: {info.filename!r}"
                        )
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise Live2DMasterDataArchiveError(
                            f"cannot extract archive directory {info.filename!r}: {exc}"
                        ) from exc
                    continue

                _ensure_archive_parent(target, extract_root)
                if target.exists() or target.is_symlink():
                    raise Live2DMasterDataArchiveError(
                        f"archive path collides with an existing path: {info.filename!r}"
                    )
                try:
                    with archive.open(info) as source:
                        extracted_bytes += _copy_archive_file(
                            source,
                            target,
                            info.file_size,
                            extracted_bytes,
                            max_extracted_bytes,
                        )
                except Live2DMasterDataError:
                    raise
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise Live2DMasterDataArchiveError(
                        f"cannot extract archive member {info.filename!r}"
                    ) from exc
    except Live2DMasterDataError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise Live2DMasterDataArchiveError(
            f"Live2D master-data archive is not a readable zip archive: {archive_path}"
        ) from exc


def locate_live2d_master_data_root(extracted_root: Path | str) -> Path:
    """Find the unique directory containing all six required table files."""

    try:
        root = Path(extracted_root)
    except TypeError as exc:
        raise Live2DMasterDataLocationError(
            "extracted Live2D master-data root must be a filesystem path"
        ) from exc
    if root.is_symlink() or not root.is_dir():
        raise Live2DMasterDataLocationError(
            f"extracted Live2D master-data root is not a directory: {root}"
        )

    candidates: list[Path] = []
    try:
        directories = [root]
        directories.extend(
            path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()
        )
        for directory in directories:
            if all(
                (table_path := directory / f"{table_name}.json").is_file()
                and not table_path.is_symlink()
                for table_name in LIVE2D_TABLE_NAMES
            ):
                candidates.append(directory)
    except (OSError, RuntimeError) as exc:
        raise Live2DMasterDataLocationError(
            f"cannot inspect extracted Live2D master-data archive: {root}"
        ) from exc

    if not candidates:
        names = ", ".join(f"{table_name}.json" for table_name in LIVE2D_TABLE_NAMES)
        raise Live2DMasterDataLocationError(
            f"online Live2D master-data archive does not contain one directory with all "
            f"required tables ({names})"
        )
    if len(candidates) > 1:
        rendered = ", ".join(str(path) for path in candidates)
        raise Live2DMasterDataLocationError(
            f"online Live2D master-data archive contains ambiguous table directories: {rendered}"
        )
    return candidates[0]


_TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "character2ds": ("id", "characterId", "assetName"),
    "costume2ds": ("id", "character2dId"),
    "systemLive2ds": ("id", "characterId", "motion", "expression"),
    "bondsLive2ds": ("id", "characterId", "motion", "expression"),
    "bondsRankUpLive2ds": ("id", "characterId", "motion", "expression"),
    "loginBonusLive2ds": ("id", "characterId", "motion", "expression"),
}


def _normalize_row(table_name: str, row: object, row_index: int) -> dict[str, object]:
    if not isinstance(row, Mapping):
        # Keep malformed rows visible to association.py, which owns row-level
        # diagnostics, while ensuring no raw value crosses this boundary.
        return {}

    normalized = {
        field_name: row[field_name] for field_name in _TABLE_FIELDS[table_name] if field_name in row
    }
    if table_name != "costume2ds":
        return normalized

    live2d_asset_name_present = "live2dAssetbundleName" in row
    if live2d_asset_name_present:
        live2d_asset_name = row["live2dAssetbundleName"]
        if "assetName" in row and row["assetName"] != live2d_asset_name:
            raise Live2DMasterDataError(
                f"{table_name}[{row_index}] has conflicting asset names: "
                "assetName does not agree with live2dAssetbundleName"
            )
        normalized["assetName"] = live2d_asset_name
    return normalized


def _read_table(root: Path, table_name: str) -> list[dict[str, object]]:
    table_path = root / f"{table_name}.json"
    if not table_path.exists():
        raise Live2DMasterDataFileError(
            f"Live2D master-data table '{table_name}' is missing: {table_path}"
        )
    if not table_path.is_file():
        raise Live2DMasterDataFileError(
            f"Live2D master-data table '{table_name}' is not a file: {table_path}"
        )

    try:
        raw_table = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Live2DMasterDataJSONError(
            f"Live2D master-data table '{table_name}' contains invalid JSON: {table_path}"
        ) from exc

    if not isinstance(raw_table, list):
        raise Live2DMasterDataShapeError(
            f"Live2D master-data table '{table_name}' JSON root must be an array: {table_path}"
        )
    return [_normalize_row(table_name, row, row_index) for row_index, row in enumerate(raw_table)]


@dataclass(frozen=True, slots=True)
class LocalMasterDataProvider:
    """Load the six raw Live2D tables from a local master-data directory."""

    root: Path | str
    master_db_version: str

    def __post_init__(self) -> None:
        try:
            root = Path(self.root)
        except TypeError as exc:
            raise Live2DMasterDataFileError("master-data root must be a filesystem path") from exc
        object.__setattr__(self, "root", root)

        if not isinstance(self.master_db_version, str) or not self.master_db_version.strip():
            raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Read and normalize exactly the six required Live2D table files."""

        root = Path(self.root)
        tables = {table_name: _read_table(root, table_name) for table_name in LIVE2D_TABLE_NAMES}
        return Live2DMasterDataSnapshot(
            master_db_version=self.master_db_version,
            tables=tables,
        )


@dataclass(slots=True)
class PreparedLive2DMasterData:
    """A local provider backed by one temporary online archive snapshot.

    The temporary directory owns both the downloaded archive and its extracted
    contents.  Callers should use this object as a context manager, or call
    :meth:`cleanup` after the provider has loaded its snapshot.
    """

    provider: LocalMasterDataProvider
    archive_url: str
    _temporary_directory: tempfile.TemporaryDirectory[str] = field(repr=False)

    @property
    def root(self) -> Path:
        """Return the located directory containing the six table files."""

        return Path(self.provider.root)

    @property
    def master_db_version(self) -> str:
        return self.provider.master_db_version

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Delegate table loading to the composed local provider."""

        return self.provider.load_live2d_snapshot()

    def cleanup(self) -> None:
        """Remove the run-scoped archive and extracted data."""

        self._temporary_directory.cleanup()

    def __enter__(self) -> PreparedLive2DMasterData:
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        self.cleanup()


def _request_timeout(value: object) -> float | None:
    if value in (None, 0, 0.0):
        return None
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise Live2DMasterDataDownloadError(
            f"master-data request timeout must be a positive number or disabled (got {value!r})"
        ) from exc
    if timeout <= 0:
        raise Live2DMasterDataDownloadError(
            f"master-data request timeout must be a positive number or disabled (got {value!r})"
        )
    return timeout


def _download_archive(
    archive_url: str,
    destination: Path,
    *,
    timeout: float | None,
    max_archive_bytes: int,
) -> None:
    if type(max_archive_bytes) is not int or max_archive_bytes <= 0:
        raise Live2DMasterDataDownloadError("master-data archive size limit must be positive")

    try:
        response = (
            urlopen(archive_url) if timeout is None else urlopen(archive_url, timeout=timeout)
        )
        with response:
            status = getattr(response, "status", getattr(response, "code", 200))
            if status not in (None, 200):
                raise Live2DMasterDataDownloadError(
                    f"online Live2D master-data archive returned HTTP status {status}"
                )
            headers = getattr(response, "headers", None)
            content_length = headers.get("Content-Length") if headers is not None else None
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise Live2DMasterDataDownloadError(
                        "online Live2D master-data archive has an invalid Content-Length"
                    ) from exc
                if declared_size < 0 or declared_size > max_archive_bytes:
                    raise Live2DMasterDataDownloadError(
                        "online Live2D master-data archive exceeds the configured download size limit"
                    )

            downloaded_bytes = 0
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(_ARCHIVE_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > max_archive_bytes:
                        raise Live2DMasterDataDownloadError(
                            "online Live2D master-data archive exceeds the configured download size limit"
                        )
                    output.write(chunk)
    except Live2DMasterDataError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise Live2DMasterDataDownloadError(
            f"cannot download online Live2D master-data archive: {sanitize_url(archive_url)}"
        ) from exc


def _extract_archive(
    archive_path: Path,
    extract_root: Path,
    *,
    max_extracted_bytes: int,
) -> None:
    if type(max_extracted_bytes) is not int or max_extracted_bytes <= 0:
        raise Live2DMasterDataArchiveError("master-data extracted size limit must be positive")
    try:
        extract_root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise Live2DMasterDataArchiveError(
            f"cannot create Live2D master-data extraction directory: {extract_root}"
        ) from exc

    try:
        if zipfile.is_zipfile(archive_path):
            _extract_zip_archive(
                archive_path,
                extract_root,
                max_extracted_bytes=max_extracted_bytes,
            )
        else:
            _extract_tar_archive(
                archive_path,
                extract_root,
                max_extracted_bytes=max_extracted_bytes,
            )
    except Live2DMasterDataError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise Live2DMasterDataArchiveError(
            f"cannot extract online Live2D master-data archive: {archive_path}"
        ) from exc


def prepare_online_master_data(
    repository_or_archive_url: str,
    *,
    branch: str = DEFAULT_MASTER_DATA_BRANCH,
    master_db_version: str | None = None,
    timeout: float | int | None = 180,
    max_archive_bytes: int = DEFAULT_MASTER_DATA_ARCHIVE_MAX_BYTES,
    max_extracted_bytes: int = DEFAULT_MASTER_DATA_EXTRACTED_MAX_BYTES,
) -> PreparedLive2DMasterData:
    """Download and prepare one latest branch archive for one association run.

    The returned provider reads all six tables from one extracted directory.
    The archive and extraction directory are removed when the returned object
    is cleaned up.  No GitHub API or per-table request is made.
    """

    validated_branch = _validated_branch(branch)
    archive_url = build_live2d_master_data_archive_url(
        repository_or_archive_url,
        branch=validated_branch,
    )
    version = (
        default_online_master_db_version(validated_branch)
        if master_db_version is None
        else master_db_version
    )
    if not isinstance(version, str) or not version.strip():
        raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")

    request_timeout = _request_timeout(timeout)
    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="sekai-live2d-master-")
    except OSError as exc:
        raise Live2DMasterDataFileError(
            "cannot create temporary directory for online Live2D master data"
        ) from exc

    temporary_root = Path(temporary_directory.name)
    archive_path = temporary_root / "master-data.archive"
    extracted_root = temporary_root / "extracted"
    try:
        _download_archive(
            archive_url,
            archive_path,
            timeout=request_timeout,
            max_archive_bytes=max_archive_bytes,
        )
        _extract_archive(
            archive_path,
            extracted_root,
            max_extracted_bytes=max_extracted_bytes,
        )
        table_root = locate_live2d_master_data_root(extracted_root)
        provider = LocalMasterDataProvider(table_root, version)
        return PreparedLive2DMasterData(
            provider=provider,
            archive_url=archive_url,
            _temporary_directory=temporary_directory,
        )
    except Live2DMasterDataError:
        temporary_directory.cleanup()
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        temporary_directory.cleanup()
        raise Live2DMasterDataArchiveError(
            "cannot prepare online Live2D master-data archive"
        ) from exc


@dataclass(slots=True)
class OnlineMasterDataProvider:
    """Load one latest online master-data archive as a normalized snapshot."""

    url: str
    branch: str = DEFAULT_MASTER_DATA_BRANCH
    master_db_version: str | None = None
    timeout: float | int | None = 180
    max_archive_bytes: int = DEFAULT_MASTER_DATA_ARCHIVE_MAX_BYTES
    max_extracted_bytes: int = DEFAULT_MASTER_DATA_EXTRACTED_MAX_BYTES
    _snapshot: Live2DMasterDataSnapshot | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.url = _validated_url(self.url)
        self.branch = _validated_branch(self.branch)
        if self.master_db_version is None:
            self.master_db_version = default_online_master_db_version(self.branch)
        elif not isinstance(self.master_db_version, str) or not self.master_db_version.strip():
            raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Download at most once, then return the immutable six-table snapshot."""

        if self._snapshot is None:
            prepared = prepare_online_master_data(
                self.url,
                branch=self.branch,
                master_db_version=self.master_db_version,
                timeout=self.timeout,
                max_archive_bytes=self.max_archive_bytes,
                max_extracted_bytes=self.max_extracted_bytes,
            )
            try:
                snapshot = prepared.provider.load_live2d_snapshot()
            finally:
                prepared.cleanup()
            self._snapshot = snapshot
        return self._snapshot
