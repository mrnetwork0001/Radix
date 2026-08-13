# Radix API Contract

Frozen interface between `backend/` and `frontend/`. Both sides code against
this document. Backend serves on **:8000**, frontend dev server on **:5173**
(Vite proxies `/api` → `http://localhost:8000`).

All responses are JSON. All latency fields are **float milliseconds**, measured
around the HydraDB call only — that number is shown in the UI as proof of
traversal speed, so it must not include FastAPI serialisation overhead.

## Core object shapes

### `GraphNode`

```jsonc
{
  "id": 1000004,                 // HydraDB integer vertex id
  "label": "Package",            // Package | Version | Maintainer | Service | Lockfile
  "name": "tanstack-query",      // display name, present on every node type
  "ecosystem": "npm",            // Package/Version only
  "is_compromised": false,       // Package only
  "risk_score": 0.0,             // Package only, 0.0–1.0
  "downloads_weekly": 4200000,   // Package only
  "semver": "4.28.0",            // Version only
  "published_at": "2026-08-11T04:12:00Z",
  "compromised_window": false,   // Version only
  "username": "dev_alex",        // Maintainer only
  "email": "alex@npm.org",       // Maintainer only
  "key_fingerprint": "0x89A...", // Maintainer only
  "repo_url": "github.com/org/auth",  // Service only
  "criticality": "tier-1",       // Service only
  "filename": "package-lock.json",    // Lockfile only
  "commit_hash": "a8f3b..."      // Lockfile only
}
```

Type-specific fields are omitted when not applicable — the frontend must treat
them as optional and branch on `label`.

### `GraphEdge`

```jsonc
{
  "source": 4000001,        // vertex id
  "target": 1000004,        // vertex id
  "type": "DEPENDS_ON",     // DEPENDS_ON | MAINTAINED_BY | RESOLVED_IN | TYPOSQUAT_OF
  "constraint": "^4.0.0",   // DEPENDS_ON only
  "is_dev": false,          // DEPENDS_ON only
  "transitive_depth": 1,    // DEPENDS_ON only
  "since": "2024-01-02T00:00:00Z",   // MAINTAINED_BY only
  "resolved_version": "4.28.0",       // RESOLVED_IN only
  "edit_distance": 1,                 // TYPOSQUAT_OF only
  "similarity_score": 0.94            // TYPOSQUAT_OF only
}
```

`DEPENDED_ON_BY` and `MAINTAINS` are internal inverse edges and are **never**
returned by the API — they exist only to satisfy HydraDB's forward-traversal
rule and would double every line in the visualiser.

### `BlastRadius`

```jsonc
{
  "exposed_services": 7,
  "total_services": 20,
  "percentage": 35.0,
  "exposed_lockfiles": 4,
  "total_lockfiles": 10,
  "tier1_exposed": 3
}
```

## Endpoints

### `GET /api/health`

```jsonc
{ "status": "ok", "hydra_ready": true, "latency_ms": 1.8, "seeded": true }
```

Returns `hydra_ready: false` rather than erroring when HydraDB is unreachable,
so the dashboard can render a degraded state instead of a blank page.

### `GET /api/graph/full`

Optional `?limit=<int>` caps returned nodes (default: all).

```jsonc
{
  "nodes": [ /* GraphNode */ ],
  "edges": [ /* GraphEdge */ ],
  "stats": {
    "total_nodes": 512, "total_edges": 1340,
    "packages": 380, "versions": 60, "maintainers": 22,
    "services": 20, "lockfiles": 10,
    "compromised_packages": 1, "tracked_lockfiles": 10
  },
  "latency_ms": 24.1
}
```

### `GET /api/closure/{package_id}`

Transitive **reverse** dependency closure. `?depth=<1-8>` (default 6).

```jsonc
{
  "root": { /* GraphNode */ },
  "depth": 6,
  "latency_ms": 6.4,
  "affected_package_ids": [1000012, 1000031],
  "affected_service_ids": [4000001],
  "affected_lockfile_ids": [5000003],
  "affected_nodes": [ /* GraphNode, hydrated */ ],
  "paths": [[1000004, 1000012, 4000001]],   // vertex-id chains, root first
  "blast_radius": { /* BlastRadius */ }
}
```

`paths` come from `algo.SSpaths` and drive the infection animation, so each is
ordered from the compromised root outward to the victim.

### `POST /api/simulate-breach`

```jsonc
// request
{ "package_id": 1000004, "version": "4.28.0", "window_hours": 48, "depth": 6 }
```

`package_name` (e.g. `"tanstack-query"`) is accepted in place of `package_id`.

```jsonc
// response
{
  "incident_id": "RDX-2026-0001",
  "root": { /* GraphNode */ },
  "compromised_version": "4.28.0",
  "closure": { /* the GET /api/closure body */ },
  "maintainer_risk": {
    "maintainer": { /* GraphNode */ },
    "sister_packages": [ /* GraphNode + "flagged": bool, "published_within_window": bool */ ],
    "risk_note": "3 sister packages published within 48h of the breach"
  },
  "typosquats": [ /* GraphNode + edit_distance, similarity_score */ ],
  "blast_radius": { /* BlastRadius */ },
  "timeline": [ { "t": "2026-08-11T04:12:00Z", "event": "…", "severity": "critical" } ],
  "latency_ms": 8.9
}
```

### `GET /api/maintainer-risk/{maintainer_id}`

Two-hop maintainer subgraph: `Package → MAINTAINED_BY → Maintainer → MAINTAINS →
Other_Package`.

```jsonc
{
  "maintainer": { /* GraphNode */ },
  "sister_packages": [ /* GraphNode + flagged, published_within_window */ ],
  "risk_note": "…", "latency_ms": 3.1
}
```

### `GET /api/typosquats/{package_id}`

```jsonc
{
  "target": { /* GraphNode */ },
  "candidates": [ /* GraphNode + edit_distance, similarity_score */ ],
  "latency_ms": 2.2
}
```

### `POST /api/generate-fix`

```jsonc
// request
{ "package_id": 1000004, "bad_version": "4.28.0", "service_ids": [4000001] }
```

`service_ids` is optional; when omitted every exposed service is patched.

```jsonc
// response
{
  "safe_version": "4.27.6",
  "reason": "last release before the compromise window opened",
  "patches": [
    {
      "service": "auth-microservice",
      "lockfile": "package-lock.json",
      "commit_hash": "a8f3b2c",
      "diff": "--- a/package-lock.json\n+++ b/package-lock.json\n@@ …",
      "overrides": { "tanstack-query": "4.27.6" }
    }
  ],
  "pr_title": "fix(security): pin tanstack-query to 4.27.6",
  "pr_body": "…markdown…"
}
```

`diff` is a ready-to-render unified diff string; the UI shows it verbatim with
`+`/`-` line colouring.
