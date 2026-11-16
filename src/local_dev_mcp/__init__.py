"""Project-aware local development MCP bridge."""

__version__ = "0.11.0"

from .application import GovernanceApplication, RequestContext  # noqa: F401
from .authorization import AuthorizationManager  # noqa: F401
from .errors import ConradDomainError, ErrorFamily  # noqa: F401
from .evidence import EvidenceBundleManager  # noqa: F401
from .pr import PullRequestManager  # noqa: F401
from .requirement import RequirementManager  # noqa: F401
from .schema_compat import (  # noqa: F401
    PUBLIC_TOOL_CATALOG,
    ToolCatalogEntry,
    ToolLifecycleStage,
)

__all__ = [
    "__version__",
    "AuthorizationManager",
    "ConradDomainError",
    "ErrorFamily",
    "EvidenceBundleManager",
    "GovernanceApplication",
    "PUBLIC_TOOL_CATALOG",
    "PullRequestManager",
    "RequestContext",
    "RequirementManager",
    "ToolCatalogEntry",
    "ToolLifecycleStage",
]
