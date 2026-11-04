"""File access policy: path confinement, deny-list, text allow-list, size caps.

Threat model (see docs/POLICY.md): a model or a document it read may try to make the server
read outside a registered project, read secrets, or read huge/binary files. Paths arrive as
untrusted strings. Checks run on the path as given **and** on the symlink-resolved path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from aios.errors import ConfigError, PolicyViolation

# Baseline only, not exhaustive. Extend through FileAccessPolicy fields.
DEFAULT_DENIED_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.pkcs12",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".htpasswd",
    ".dockercfg",
    "kubeconfig",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "*.secret",
    "*.secrets",
    ".credentials*",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "credentials.xml",
    "credentials.ini",
    "credentials.toml",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
    "secrets.ini",
    "service-account*.json",
    "service_account*.json",
    "serviceaccount*.json",
)

# Names that look like env files but are conventionally committed templates.
DEFAULT_DENIED_NAME_EXCEPTIONS: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)

DEFAULT_DENIED_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"}
)

DEFAULT_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
        ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
        ".md", ".mdx", ".rst", ".txt", ".adoc",
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".xml", ".svg",
        ".sql", ".graphql", ".gql", ".proto",
        ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
        ".java", ".kt", ".kts", ".gradle", ".scala", ".go", ".rs", ".rb", ".php",
        ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".swift", ".dart", ".lua", ".pl", ".r", ".tf",
    }
)  # fmt: skip

DEFAULT_ALLOWED_FILENAMES: frozenset[str] = frozenset(
    {
        "readme", "license", "notice", "changelog", "codeowners", "makefile", "dockerfile",
        "procfile", "gemfile", "rakefile", "justfile",
        ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
        ".prettierrc", ".eslintrc", ".nvmrc", ".python-version",
        ".env.example", ".env.sample", ".env.template",
    }
)  # fmt: skip

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# Windows 8.3 short-name aliases (e.g. CREDEN~1.JSO) could bypass name patterns. Heuristic:
# also rejects legitimate names such as "backup~1" (documented false positive).
_SHORT_NAME_ALIAS = re.compile(r"^[^.~]{1,8}~\d+(\.[^.]{0,3})?\Z")
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_MAX_PATH_CHARS = 4096
_BINARY_SAMPLE_BYTES = 8192


class FileAccessPolicy(BaseModel):
    """Tunable limits and lists. Defaults are deliberately conservative."""

    model_config = ConfigDict(frozen=True)

    max_file_bytes: int = 1_048_576
    allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS
    allowed_filenames: frozenset[str] = DEFAULT_ALLOWED_FILENAMES
    denied_name_patterns: tuple[str, ...] = DEFAULT_DENIED_NAME_PATTERNS
    denied_name_exceptions: frozenset[str] = DEFAULT_DENIED_NAME_EXCEPTIONS
    denied_dir_names: frozenset[str] = DEFAULT_DENIED_DIR_NAMES


def looks_binary(sample: bytes) -> bool:
    """True if the leading bytes contain a NUL, a common marker of binary content."""
    return b"\x00" in sample[:_BINARY_SAMPLE_BYTES]


class PathGuard:
    """Confines and vets paths for one registered project root.

    Callers pass **project-relative** paths. The guard never returns a path outside the root,
    and never lets a denied name or directory through, whether reached directly or via symlink.
    """

    def __init__(self, root: Path, policy: FileAccessPolicy | None = None) -> None:
        try:
            resolved = root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"project root cannot be resolved: {root}") from exc
        if not resolved.is_dir():
            raise ConfigError(f"project root is not a directory: {root}")
        self._root = resolved
        self._policy = policy if policy is not None else FileAccessPolicy()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def policy(self) -> FileAccessPolicy:
        return self._policy

    def resolve(self, relative_path: str) -> Path:
        """Return the real absolute path for ``relative_path`` or raise ``PolicyViolation``.

        Does not check existence, extension or size; use it for directories and listings.
        """
        parts = self._split(relative_path)
        for part in parts:
            self._check_syntax(part, relative_path)
        self._check_denied(parts, relative_path)

        resolved = self._root.joinpath(*parts).resolve(strict=False)
        if resolved != self._root and self._root not in resolved.parents:
            raise PolicyViolation("path_escape", f"path leaves the project root: {relative_path!r}")
        # Symlinks can point at denied names/dirs *inside* the root; vet the real location too.
        self._check_denied(resolved.relative_to(self._root).parts, relative_path)
        return resolved

    def check_readable_file(self, relative_path: str) -> Path:
        """Like :meth:`resolve`, and the target must be an allowed, size-capped regular file."""
        path = self.resolve(relative_path)
        if not path.exists():
            raise PolicyViolation("not_found", f"no such file: {relative_path!r}")
        if not path.is_file():
            raise PolicyViolation("not_a_file", f"not a regular file: {relative_path!r}")
        if not self._extension_allowed(path.name):
            raise PolicyViolation(
                "extension_not_allowed", f"file type is not allowed: {relative_path!r}"
            )
        if path.stat().st_size > self._policy.max_file_bytes:
            raise PolicyViolation(
                "too_large",
                f"file exceeds {self._policy.max_file_bytes} bytes: {relative_path!r}",
            )
        return path

    def is_visible(self, relative_path: str) -> bool:
        """True if the entry may appear in listings/search results. Never raises."""
        try:
            path = self.resolve(relative_path)
            if path.is_dir():
                return True
            self.check_readable_file(relative_path)
        except (PolicyViolation, OSError):
            return False
        return True

    def vet_repo_path(self, relative_path: str, *, is_dir: bool = False) -> str:
        """Lexically vet a path *inside the repository* (history, diffs, status).

        Touches no files, so it works for deleted paths. Returns the normalised POSIX path or
        raises ``PolicyViolation``. Files must also have an allowed extension or file name.
        """
        parts = self._split(relative_path)
        if not parts:
            raise PolicyViolation("invalid_path", "empty path")
        for part in parts:
            self._check_syntax(part, relative_path)
        self._check_denied(parts, relative_path)
        if not is_dir and not self._extension_allowed(parts[-1]):
            raise PolicyViolation(
                "extension_not_allowed", f"file type is not allowed: {relative_path!r}"
            )
        return "/".join(parts)

    def is_repo_path_visible(self, relative_path: str, *, is_dir: bool = False) -> bool:
        """Non-raising form of :meth:`vet_repo_path`, for filtering git output."""
        try:
            self.vet_repo_path(relative_path, is_dir=is_dir)
        except PolicyViolation:
            return False
        return True

    def to_relative(self, path: Path) -> str:
        """POSIX-style project-relative form of an absolute path under the root."""
        return path.resolve(strict=False).relative_to(self._root).as_posix()

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _split(relative_path: str) -> list[str]:
        if len(relative_path) > _MAX_PATH_CHARS or _CONTROL_CHARS.search(relative_path):
            raise PolicyViolation("invalid_path", f"unusable path: {relative_path[:80]!r}")
        if relative_path.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(relative_path):
            raise PolicyViolation(
                "absolute_path", f"path must be project-relative: {relative_path!r}"
            )
        if ":" in relative_path:  # also blocks NTFS alternate data streams (file.txt:stream)
            raise PolicyViolation("invalid_path", f"':' is not allowed: {relative_path!r}")
        parts = [p for p in relative_path.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            raise PolicyViolation("path_escape", f"'..' is not allowed: {relative_path!r}")
        return parts

    @staticmethod
    def _check_syntax(part: str, original: str) -> None:
        if part.endswith((".", " ")):  # Windows silently strips these, aliasing other names
            raise PolicyViolation("invalid_path", f"trailing dot/space: {original!r}")
        if part.split(".", 1)[0].lower() in _RESERVED_DEVICE_NAMES:
            raise PolicyViolation("invalid_path", f"reserved device name: {original!r}")
        if _SHORT_NAME_ALIAS.match(part):
            raise PolicyViolation("invalid_path", f"possible 8.3 short-name alias: {original!r}")

    def _check_denied(self, parts: Sequence[str], original: str) -> None:
        policy = self._policy
        lowered = [p.lower() for p in parts]
        for name in lowered:
            if name in policy.denied_dir_names:
                raise PolicyViolation("denied_dir", f"directory is not accessible: {original!r}")
        if lowered:
            last = lowered[-1]
            if last not in policy.denied_name_exceptions and any(
                fnmatchcase(last, pattern.lower()) for pattern in policy.denied_name_patterns
            ):
                raise PolicyViolation("denied_name", f"file is not accessible: {original!r}")

    def _extension_allowed(self, name: str) -> bool:
        lowered = name.lower()
        return (
            Path(lowered).suffix in self._policy.allowed_extensions
            or lowered in self._policy.allowed_filenames
        )
