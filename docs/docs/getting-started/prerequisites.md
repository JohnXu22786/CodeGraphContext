# System Prerequisites

CodeGraphContext (CGC) is designed as a client-server architecture. To ensure a successful installation, understand the primary roles and requirements of the environment.

---

## Architecture Components

1. **The Ingestion Engine**: The core Python package responsible for scanning source directories, running Tree-sitter and SCIP syntax parsers, and linking references.
2. **The Graph Storage Layer**: The database backend containing nodes and edges representing code entities and their interactions.
3. **The Interface Clients**:
    - **CLI (`cgc`)**: Terminal interface used for managing indices, running analytical searches, and system diagnostics.
    - **MCP Server**: Gateway enabling Model Context Protocol communication for IDEs and AI assistants.

---

## Hardware & OS Requirements

| Resource | Minimum Requirement | Notes |
| :--- | :--- | :--- |
| **Operating System** | Linux, macOS, or Windows | Native installs use the included LadybugDB backend by default. |
| **Python Version** | Python 3.10 or higher | KuzuDB is optional and needs a published wheel for the target Python/platform. |
| **Memory** | 4 GB RAM | Large repositories benefit from 8 GB+ memory during initial scans. |

---

## Database Backend Selection

CGC supports multiple database engines. You only need to set up the engine that fits your requirements.

| Database Backend | Setup Type | Target Platform | Use Case |
| :--- | :--- | :--- | :--- |
| **LadybugDB (Default)** | In-process (Embedded) | Cross-Platform | Included in the standard install; zero extra setup. |
| **FalkorDB Lite** | In-process (Embedded) | Unix (Linux/macOS), Python 3.12+ | Optional `falkordb-embedded` extra. |
| **KùzuDB** | In-process (Embedded) | Linux/macOS/Windows with compatible wheels | Optional `kuzu` extra. |
| **FalkorDB Remote** | Networked Server | Cross-Platform Client | Connects to a remote FalkorDB/Redis-compatible server. |
| **Neo4j** | Networked Server | Cross-Platform Client | Enterprise clustering, Neo4j Browser, AuraDB. |
| **Nornic DB** | Embedded / Bolt client | Cross-Platform | Neo4j-compatible driver without a full Neo4j deployment. |

---

## Development Environment Interfaces

To use CodeGraphContext inside your coding workflow, ensure you have an MCP-compliant workspace interface, such as:

- **Cursor IDE** (Native MCP Support)
- **VS Code** (with the Continue or similar MCP extension)
- **Claude Desktop** (Native local process or SSE support)
- **Windsurf IDE / OpenCode**
