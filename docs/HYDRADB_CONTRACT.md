# HydraDB Contract — Empirically Verified

> Every statement below was **verified by live probe** against
> `ghcr.io/hydra-db/hydradb:latest` (digest `db78309a233b`) on 2026-08-13.
> Do not guess beyond this document. If you need a behaviour that is not
> listed here, probe it first with `curl` before writing code against it.

HydraDB is an **object-store-native distributed graph database** written in Rust.
It speaks a **deliberate subset of OpenCypher** — not the whole language. Queries
outside the subset are rejected at *parse time* with a clear reason, which is why
the constraints below are hard requirements rather than style preferences.

---

## 1. Running the server

No S3/MinIO needed: `CLOUD_PROVIDER=local` uses a local-filesystem object store.

Two non-obvious requirements, both discovered the hard way:

1. **Run as root (`user: "0:0"`).** The image declares `USER 10001:10001`, but
   Docker named volumes are created root-owned. As UID 10001 the node boots,
   serves `/readyz`, and then silently fails every heartbeat write with
   `object store put failed at data/_graph_nodes/v1/node-0`.
2. **Mount volumes *at* the data paths**, not at a parent. `LOCAL_PATH` must
   already exist when the node starts, or it aborts with
   `UnableToCanonicalize { path: ..., NotFound }`. Docker auto-creates a
   named-volume mountpoint, so mounting directly at the store path satisfies this.

`RUST_MIN_STACK=33554432` is also mandatory — the async query futures exceed the
default thread stack, and without it the node accepts connections and then dies
with a stack overflow on the **first query**.

Ports: **7687** Bolt · **8443** HTTP JSON · **9090** admin (`/readyz`, `/metrics`).

The auth token is read from a file (`GRAPH_AUTH_TOKEN_FILE`); there is no
env-var form. It must be **≥32 bytes**.

## 2. HTTP query API

`POST /v1/graphs/{graph_id}/query`

Headers: `Authorization: Bearer <token>`, `X-Graph-Namespace: <namespace>`,
`Content-Type: application/json`.

Request body (from `src/client/http.rs::HttpQueryRequestBody`):

```jsonc
{
  "cell_id":    "cell-0",   // REQUIRED
  "query":      "MATCH ...",// REQUIRED — exactly ONE statement
  "parameters": {},          // $name params; may hold a list-of-maps for UNWIND
  "timeout_ms": 30000,
  "page_size":  1024,
  "query_id": null, "bookmark": null, "read_epoch": null,
  "cursor": null, "consistency": null
}
```

Response — **values are type-tagged and must be unwrapped**:

```jsonc
{
  "columns": ["id", "name"],
  "rows": [[{"type":"vertex_id","value":4}, {"type":"string","value":"deep"}]],
  "read_epoch": 12, "next_cursor": null, "bookmark": "sgk:1:..."
}
```

Tags: `null`, `vertex_id`, `integer`, `signed_integer`, `float`, `boolean`,
`string`, `list`, `path`. Errors return HTTP 4xx with
`{"error":{"code":"invalid_request","message":"..."}}`.

## 3. The constraint that shapes the entire architecture

```
MATCH (b)-[:DEPENDS_ON*1..3]->(a {id:4})
→ "variable-length MATCH requires a fixed source id"
```

**Variable-length traversal only runs forward from a fixed source id.** You
cannot anchor a variable-length pattern at its *target*.

Radix's core feature is the **reverse** transitive closure — *who transitively
depends on this compromised package* — which is exactly the rejected shape.

### Resolution: materialised inverse edges

Every `DEPENDS_ON` is mirrored at seed time by a `DEPENDED_ON_BY` edge pointing
the other way. Reverse closure then becomes a *forward* traversal:

```cypher
MATCH (victim {id: $pkg})-[:DEPENDED_ON_BY*1..6]->(dependent)
RETURN DISTINCT dependent.id AS id
```

Verified: returns the complete closure `{1,2,3}` over a 3-hop chain. This costs
2× edge storage and is the standard modelling answer for a direction-restricted
engine. **All reverse/blast-radius traversal must use `DEPENDED_ON_BY`.**

Note the maximum in `*1..N` is **required** — `*` and `*1..` are both rejected,
because unbounded traversal has no predictable cost on a large graph.

## 4. Writing data (the seeding contract)

Nodes are keyed by a **non-negative integer `id`**. Names are properties, so the
seeder owns a deterministic integer ID space (see `backend/app/schema.py`).

**Node upsert** — `MERGE` by id, then `SET`. Folding extra properties into the
`MERGE` pattern is rejected, because the pattern *is* the identity being matched:

```cypher
UNWIND $rows AS row
MERGE (n {id: row.vertex})
SET n:Package, n.name = row.name, n.risk_score = row.risk_score
```

**Edge create** — two rules, both found by probe:

- each endpoint needs **exactly one label**
  (`UNWIND MATCH CREATE endpoints require exactly one label`)
- a relationship carrying properties needs an explicit **`id: row.<field>`**
  (`UNWIND relationship CREATE properties require id: row.<field>`)

```cypher
UNWIND $rows AS row
MATCH (s:Package {id: row.src}), (d:Package {id: row.dst})
CREATE (s)-[:DEPENDS_ON {id: row.eid, constraint: row.constraint}]->(d)
```

Idempotent re-seed uses `MERGE ... SET` with the same edge id:

```cypher
UNWIND $rows AS row
MATCH (s:Package {id: row.src}), (d:Package {id: row.dst})
MERGE (s)-[r:DEPENDS_ON {id: row.eid}]->(d)
SET r.constraint = row.constraint
```

Cross-label edges (`Service`→`Package`) work fine — the rule is one label *each*.

## 5. Reading — what works and what does not

| Works | Rejected |
|---|---|
| `MATCH` with one rel type, directed | undirected patterns, multiple types |
| `*1..N` from a **fixed source id** | `*`, `*1..`, target-anchored var-length |
| `WHERE` with `= <> < > <= >=`, `STARTS WITH`, `AND/OR/NOT` | `IN`, `CONTAINS`, `ENDS WITH`, `IS NULL` |
| `count`, `sum`, `avg`, `collect` | `min`, `max`, `count(DISTINCT *)` |
| `DISTINCT`, `ORDER BY`, `SKIP`, `LIMIT` | `RETURN *` |
| label + property filters on a var-length **target** | `WITH` that aliases/filters/orders |
| `UNION` / `UNION ALL` (reads, matching columns) | nested/mixed unions, unions with writes |

Two consequences worth stating plainly:

- **No `IN`** means you cannot filter by a list of ids server-side. Fan out per
  id, or filter client-side.
- **`WITH` is pass-through only**, so there are no multi-stage aggregation
  pipelines. HydraDB does *traversal*; Python does *scoring*. This is a clean
  split, not a workaround.
- **No string functions**, so Levenshtein/homoglyph analysis cannot run in
  Cypher. Typosquat edges are computed at seed time and stored as
  `TYPOSQUAT_OF` with `edit_distance` / `similarity_score` properties — the
  traversal then just reads them back.

## 6. Path extraction for the visualiser

A plain `MATCH` projects endpoints, not routes. To get whole **paths** — needed
to animate infection routes through the graph — use the native procedures:

```cypher
CALL algo.SSpaths({sourceNode: $id, relTypes: ['DEPENDED_ON_BY'],
                   maxLen: 6, pathCount: 200})
YIELD path RETURN path
```

Verified to return every path from the source (1-hop, 2-hop, 3-hop …), each with
full node labels/properties and relationship `src`/`dst`/`edge_type`/properties.
`pathCount` defaults low — **set it explicitly** or you get a single path.

`relDirection` accepts `'incoming'` / `'both'`, which traverses `DEPENDS_ON`
backwards. Both routes work; Radix prefers the `DEPENDED_ON_BY` inverse edge so
that closure counts and path extraction share one consistent edge type.

Procedures: `algo.SPpaths` (source→target), `algo.SSpaths` (one source),
`algo.MSpaths` (many sources). `RETURN` may only name yielded columns
(`path`, `pathWeight`, `pathCost`).

### Path value encoding (verified)

Inside a `path` value, properties use a **different, inner encoding** from the
outer type-tagged row values — a single-key wrapper named after the Rust variant:

```jsonc
{"nodes": [
   {"id": 701, "labels": ["Probe"],
    "properties": {"s": {"String":"hello"}, "i": {"Integer":42},
                   "f": {"Float":0.75},     "b": {"Bool":true}}}],
 "relationships": [
   {"id": 10, "edge_type": "PROBE_EDGE", "src": 701, "dst": 702,
    "properties": {"id": {"Integer":9500}, "w": {"Float":2.25}}}]}
```

Two traps here:

- the boolean tag is **`Bool`**, not `Boolean` — unlike the outer row encoding,
  which spells the same type `boolean`. A flattener written against the outer
  tag names will silently drop booleans.
- a relationship's `"id"` field is HydraDB's **internal** edge id (`10`), which
  is *not* the `id` property the seeder supplied (`9500`). Read the property
  from `properties.id` when you need the seeder's edge id.

## 7. Verified probe log

| # | Probe | Result |
|---|---|---|
| 2 | forward `*1..3` | ✅ `{2,3,4}` |
| 3 | target-anchored `*1..3` | ❌ requires fixed source id |
| 4 | reverse 1-hop | ✅ works |
| E | `DEPENDED_ON_BY*1..5` | ✅ full reverse closure |
| F | label + `STARTS WITH` on var-length target | ✅ works |
| G | `UNWIND` node upsert over HTTP | ✅ works |
| H | `UNWIND` edge create, unlabelled endpoints | ❌ needs one label each |
| L | `SSpaths` `pathCount:25` | ✅ full multi-hop paths |
| Q | `UNWIND` edge create w/ `id:` + props | ✅ works |
| R | `UNWIND` edge `MERGE` w/ `id:` + `SET` | ✅ idempotent |
| S | cross-label edge w/ props | ✅ works |
| T | read back edge props | ✅ `^5.0.0`, depth 1 |
