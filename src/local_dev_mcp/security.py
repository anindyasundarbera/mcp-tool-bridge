"""Security-hardening helpers for the local development bridge (M-CC-11).

Internal-only: this module adds no MCP tools. It provides the four opt-in,
per-project hardening mechanisms the milestone ships:

1. ``filesystem_read_allowed`` / ``filesystem_write_allowed`` — enforce a
   project's ``filesystem_policy`` read/write allowlists against a repository-
   relative path (trailing ``/**`` entries select a whole subtree).
2. ``projected_credential_env`` / ``credential_names`` — least-privilege,
   short-lived credential projection: only the env names a project declares in
   ``credential_policy.allow`` are copied from the bridge's own process
   environment into a worktree subprocess environment. Nothing else leaks.
3. ``sign_evidence_bundle`` / ``verify_evidence_signature`` — deterministic
   HMAC-SHA256 evidence-bundle signing over ``"<project_id>\\n<bundle_sha256>"``
   with a project key, plus key resolution from private, non-repo sources.
4. ``project_sensitivity_prefixes`` — the directory prefixes a project's
   ``sensitivity_policy`` marks sensitive (used by the evidence bundle's
   exclude filter to redact/count before export).

The signing key never lives in the repository: it is resolved from a
per-project environment variable, a shared environment variable, or a
gitignored file beneath the bridge's private state root.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CredentialPolicy, ProjectPolicy, SensitivityPolicy

_SIGNING_ALGORITHM = "hmac-sha256"


# ---------------------------------------------------------------------------
# filesystem_policy (read/write allowlists)
# ---------------------------------------------------------------------------


def _normalize_allow_entry(entry: str) -> str:
    """Reduce a policy allowlist entry to a plain directory prefix.

    ``docs/**`` and ``docs/`` both reduce to ``docs``; a bare ``docs`` is
    unchanged. Matching then treats the prefix as a directory boundary so
    ``docs`` does not accidentally allow ``docs2/…``.
    """
    normalized = entry.rstrip("/")
    if normalized.endswith("/**"):
        return normalized[:-3]
    return normalized


def _allowlist_allows(allow: tuple[str, ...], relative: str | PurePosixPath) -> bool:
    """True iff ``relative`` is at or beneath one of the allowlist prefixes."""
    if not allow:
        return True
    path = relative if isinstance(relative, PurePosixPath) else PurePosixPath(relative)
    for entry in allow:
        parent = PurePosixPath(_normalize_allow_entry(entry))
        if path == parent or parent in path.parents:
            return True
    return False


def filesystem_read_allowed(
    project: ProjectPolicy, relative: str | PurePosixPath
) -> bool:
    """True when the project's filesystem read allowlist permits ``relative``.

    A project without a ``filesystem_policy`` (the default) is always allowed;
    the existing protected-path and hidden-path rules still apply separately.
    """
    if project.filesystem_policy is None:
        return True
    return _allowlist_allows(project.filesystem_policy.read_allow, relative)


def filesystem_write_allowed(
    project: ProjectPolicy, relative: str | PurePosixPath
) -> bool:
    """True when the project's filesystem write allowlist permits ``relative``.

    A project without a ``filesystem_policy`` (the default) is always allowed;
    the existing approved write-root rule still applies separately.
    """
    if project.filesystem_policy is None:
        return True
    return _allowlist_allows(project.filesystem_policy.write_allow, relative)


# ---------------------------------------------------------------------------
# credential_policy (least-privilege, short-lived credential projection)
# ---------------------------------------------------------------------------


def credential_names(policy: CredentialPolicy | None) -> tuple[str, ...]:
    """The credential env names a project declares (empty when unset)."""
    return policy.allow if policy is not None else ()


def projected_credential_env(
    policy: CredentialPolicy | None,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a worktree subprocess environment for a project.

    Returns ``base`` (or an empty mapping) extended only with the credential
    env names the project's ``credential_policy`` declares, projected from the
    bridge's own process environment when present. No other credential from the
    bridge's environment leaks into a worktree subprocess. When the policy is
    unset, the returned mapping is exactly ``base`` (callers keep their
    existing behavior).
    """
    environment = dict(base or {})
    if policy is not None:
        for name in policy.allow:
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment


# ---------------------------------------------------------------------------
# signed_manifests (evidence-bundle signing)
# ---------------------------------------------------------------------------


def _key_env_names(project_id: str) -> tuple[str, str]:
    per_project = "BRIDGELAB_EVIDENCE_SIGNING_KEY_" + project_id.upper().replace(
        "-", "_"
    )
    return per_project, "BRIDGELAB_EVIDENCE_SIGNING_KEY"


def resolve_signing_key(
    project_id: str, *, state_root: Path | None = None
) -> bytes | None:
    """Resolve a project's evidence-signing key, or ``None`` when unconfigured.

    Key sources, in priority order:

    1. ``BRIDGELAB_EVIDENCE_SIGNING_KEY_<PROJECT_ID>`` (per-project env var)
    2. ``BRIDGELAB_EVIDENCE_SIGNING_KEY`` (shared env var)
    3. ``<state_root>/signing_keys/<project_id>.key`` (gitignored file,
       read-only, first line trimmed)

    The key is never read from the repository or any archive.
    """
    per_project, shared = _key_env_names(project_id)
    for name in (per_project, shared):
        value = os.environ.get(name)
        if value:
            return value.encode("utf-8")
    if state_root is not None:
        key_path = state_root / "signing_keys" / f"{project_id}.key"
        try:
            data = key_path.read_bytes().strip()
        except OSError:
            return None
        return data if data else None
    return None


def sign_evidence_bundle(
    bundle: dict[str, Any], project_id: str, key: bytes
) -> dict[str, Any]:
    """Sign a bundle's content address and attach its manifest record.

    The signature is a deterministic HMAC-SHA256 over
    ``"<project_id>\\n<bundle_sha256>"`` using the project key. The manifest is
    attached under ``"manifest"`` and carries the algorithm, project id, digest,
    and signature so a recipient can verify the bundle without the signing key.
    The ``bundle_sha256`` field is left untouched: it was computed over the
    bundle WITHOUT the manifest, so a recipient recomputes the digest by
    stripping both ``bundle_sha256`` and ``manifest`` and re-serializing with
    ``sort_keys=True`` and ``ensure_ascii=False``.
    """
    digest = bundle.get("bundle_sha256")
    if not digest or not isinstance(digest, str):
        raise ValueError("cannot sign a bundle without a bundle_sha256")
    signature = hmac.new(
        key, f"{project_id}\n{digest}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    manifest = {
        "algorithm": _SIGNING_ALGORITHM,
        "project_id": project_id,
        "bundle_sha256": digest,
        "signature": signature,
    }
    return {**bundle, "manifest": manifest}


def verify_evidence_signature(manifest: dict[str, Any], key: bytes) -> bool:
    """Return True iff ``manifest``'s signature is valid for ``key``.

    Constant-time comparison (``hmac.compare_digest``) so a timing attacker
    cannot recover the signature by byte-by-byte probing.
    """
    algorithm = manifest.get("algorithm")
    project_id = manifest.get("project_id")
    digest = manifest.get("bundle_sha256")
    signature = manifest.get("signature")
    if (
        algorithm != _SIGNING_ALGORITHM
        or not isinstance(project_id, str)
        or not isinstance(digest, str)
        or not isinstance(signature, str)
    ):
        return False
    expected = hmac.new(
        key, f"{project_id}\n{digest}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# sensitivity_classification (per-project sensitive prefixes)
# ---------------------------------------------------------------------------


def project_sensitivity_prefixes(
    policy: SensitivityPolicy | None,
) -> tuple[str, ...]:
    """Reduce a project's sensitivity policy to plain directory prefixes.

    Each declared sensitive path is normalized exactly like an allowlist entry
    (``config/secrets/**`` -> ``config/secrets``) so the evidence-bundle filter
    can match "at or beneath this directory" deterministically.
    """
    if policy is None:
        return ()
    return tuple(_normalize_allow_entry(entry) for entry in policy.sensitive_paths)
