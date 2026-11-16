"""Project-aware local development MCP bridge."""

__version__ = "0.11.0"

from .application import GovernanceApplication, RequestContext  # noqa: F401
from .authorization import AuthorizationManager  # noqa: F401
from .claude import ClaudeExecutorAdapter  # noqa: F401
from .containment import (  # noqa: F401
    ContainedProcessRunner,
    ContainmentDoctorReport,
    ContainmentMode,
    ContainmentPolicy,
)
from .errors import ConradDomainError, ErrorFamily  # noqa: F401
from .evidence import EvidenceBundleManager  # noqa: F401
from .executor_contract import (  # noqa: F401
    DeterministicFakeExecutor,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCheckpoint,
    ExecutorHealthStatus,
    ExecutorLifecycleState,
    ExecutorPort,
    ExecutorRegistry,
    ExecutorResourceLimits,
    NetworkMode,
)
from .identity import (  # noqa: F401
    AuthProvenance,
    CallerPrincipal,
    PrincipalRole,
    resolve_caller_principal,
)
from .openhands_executor import (  # noqa: F401
    PINNED_OPENHANDS_IMAGE,
    OpenHandsAdapterConfig,
    OpenHandsExecutor,
)
from .pr import PullRequestManager  # noqa: F401
from .requirement import RequirementManager  # noqa: F401
from .review import (  # noqa: F401
    ApprovalSummaryDTO,
    PromotionAuthorityTrace,
    ReviewDecision,
)
from .schema_compat import (  # noqa: F401
    PUBLIC_TOOL_CATALOG,
    ToolCatalogEntry,
    ToolLifecycleStage,
)
from .transports import (  # noqa: F401
    LocalHttpTransportAdapter,
    NgrokMtlsTransportAdapter,
    PeerTransportMetadata,
    StdioTransportAdapter,
    TransportCapabilities,
    TransportConfig,
    TransportMode,
)

__all__ = [
    "__version__",
    "ApprovalSummaryDTO",
    "AuthProvenance",
    "AuthorizationManager",
    "CallerPrincipal",
    "ClaudeExecutorAdapter",
    "ConradDomainError",
    "ContainedProcessRunner",
    "ContainmentDoctorReport",
    "ContainmentMode",
    "ContainmentPolicy",
    "DeterministicFakeExecutor",
    "ErrorFamily",
    "EvidenceBundleManager",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorCapabilities",
    "ExecutorCheckpoint",
    "ExecutorHealthStatus",
    "ExecutorLifecycleState",
    "ExecutorPort",
    "ExecutorRegistry",
    "ExecutorResourceLimits",
    "GovernanceApplication",
    "LocalHttpTransportAdapter",
    "NetworkMode",
    "NgrokMtlsTransportAdapter",
    "OpenHandsAdapterConfig",
    "OpenHandsExecutor",
    "PINNED_OPENHANDS_IMAGE",
    "PUBLIC_TOOL_CATALOG",
    "PeerTransportMetadata",
    "PrincipalRole",
    "PromotionAuthorityTrace",
    "PullRequestManager",
    "RequestContext",
    "RequirementManager",
    "ReviewDecision",
    "StdioTransportAdapter",
    "ToolCatalogEntry",
    "ToolLifecycleStage",
    "TransportCapabilities",
    "TransportConfig",
    "TransportMode",
    "resolve_caller_principal",
]
