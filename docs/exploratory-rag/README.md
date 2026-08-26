# docs/exploratory-rag/ — archived notes, not bridge documentation

The two files in this directory —
[`DEVELOPMENT_GUIDE.md`](DEVELOPMENT_GUIDE.md) and
[`MANUAL_INSTALLATION_GUIDE.md`](MANUAL_INSTALLATION_GUIDE.md) — are
**not documentation for the MCP Tool Bridge (BridgeLab)**.

They are notes from a separate, exploratory RAG-augmentation initiative
(LightRAG / RAG-Anything / tokengate / OpenRAG / MCP-Hacker News /
github-to-mcp / the RAG-MCP paper, arXiv:2505.03275). They were originally
written at the repository root, where their titles
("Manual Installation Guide for Claude/Local-Dev-MCP Enhancement") were
misleading: they read as if they described how to install the bridge, but
they describe installing those unrelated RAG tools and their multimodal
dependencies instead.

They have been moved here so the repository root is authoritative for the
bridge, and so these notes are not lost (their git history is preserved via
the rename). Treat them as historical context only.

- For the bridge itself: see the root [`README.md`](../../README.md),
  [`AGENTS.md`](../../AGENTS.md), and the reference docs in
  [`docs/`](../README.md) (ARCHITECTURE, TOOLS, CAPABILITIES, GETTING-STARTED,
  SECURITY).
- The vendored clones referenced here (`LightRAG/`, `RAG-Anything/`,
  `tokengate/` at the repo root) are **gitignored** and are not part of the
  bridge repository.

Note: these archived files contain some corrupted (non-ASCII) characters and
an un-evaluated `$(date +%Y-%m-%d)` literal carried over from their original
authoring; their content is left untouched.