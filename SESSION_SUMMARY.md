# Session Summary: M-EV-01 Requirement Traceability Implementation

## Date
2026-08-10

## Objective
Implement M-EV-01: Requirement traceability — the highest-value single unblock from the BridgeLab capability elevation map.

## Accomplishments

### 1. Created RequirementManager Class
- **File**: `src/local_dev_mcp/requirement.py`
- **Class**: `RequirementManager`
- **Constructor Parameters**: 
  - `settings`: Settings object
  - `store`: store module (for database access)
  - `reader`: RepositoryReader instance
- **Key Features**:
  - Extracts normative IDs from Markdown using regex `^[A-Z]+-\d+[A-Z]?-R\d+`
  - Processes fenced normative blocks (```normative ... ```)
  - Classifies content as normative vs example/commentary using contextual heuristics
  - Maintains requirement ↔ job/artifact links via SQLite tables
  - Generates coverage matrices showing requirement coverage by jobs
  - Uses caching for performance (requirements and links cached by project_id)

### 2. Updated Schema (Additive Changes)
- **File**: `src/local_dev_mcp/store.py`
- **Changes**:
  - Added `requirements` table:
    ```sql
    CREATE TABLE IF NOT EXISTS requirements (
        requirement_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        text TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_line INTEGER NOT NULL,
        normative BOOLEAN NOT NULL
    );
    ```
  - Added `requirement_links` table:
    ```sql
    CREATE TABLE IF NOT EXISTS requirement_links (
        link_id INTEGER PRIMARY KEY AUTOINCREMENT,
        requirement_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        link_type TEXT NOT NULL,
        link_value TEXT NOT NULL,
        FOREIGN KEY (requirement_id) REFERENCES requirements(requirement_id),
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
    );
    ```
  - Added corresponding UPSERT functions:
    - `_UPSERT_REQUIREMENT`
    - `_UPSERT_REQUIREMENT_LINK`
  - Maintained `SCHEMA_VERSION = 1` (additive schema change)

### 3. Updated Module Exports
- **File**: `src/local_dev_mcp/__init__.py`
- **Change**: Added export: `from .requirement import RequirementManager  # noqa: F401`

## Implementation Details

### Requirement Extraction Logic
1. **Normative ID Detection**: Lines matching `^[A-Z]+-\d+[A-Z]?-R\d+$` are treated as potential requirement IDs
2. **Fenced Block Detection**: Content between ```normative and ``` fences is considered normative
3. **Contextual Classification**: Uses heuristics to distinguish actual requirements from examples/commentary by looking for indicators like "example", "for example", "note:", etc.

### Core Functionality
- **Linking**: Methods to link requirements to jobs, changed paths, and test names
- **Retrieval**: Methods to get links for specific requirements or jobs
- **Coverage Analysis**: `get_coverage_matrix_for_job()` generates:
  - Total/normative requirement counts
  - Covered requirement counts
  - Coverage percentages
  - Detailed requirement information with coverage status
  - Links broken down by type (job, changed_path, test_name)

### Architecture Compliance
- **Additive Tables**: Only added new tables, no modifications to existing schema
- **Lazy Import**: RequirementManager instantiated only when needed
- **Zero Surface**: No new MCP tools or public interfaces exposed yet
- **Internal-First**: Functionality available internally; prepares for future coverage-report tool (NEW SURFACE)
- **Thread-Safe**: Uses proper database transaction handling

## Current Task Status
- �� **#1 M-EV-01: Requirement traceability** - COMPLETED
- ��� **#2-#12** - PENDING (see IMPLEMENTATION_PROGRESS.md for details)

## Next Recommended Task
Based on the elevation map and unblock value: **#2 M-CT · MCPB-010 tail: Resumable state done better — checkpointers, stores, time-travel**

## Files Modified
1. `src/local_dev_mcp/requirement.py` (NEW)
2. `src/local_dev_mcp/store.py` (UPDATED)
3. `src/local_dev_mcp/__init__.py` (UPDATED)

## Verification
- Implementation follows BridgeLab's documented patterns from jobs.py, verification.py, etc.
- Additive schema changes maintain compatibility with existing code
- No breaking changes to existing functionality