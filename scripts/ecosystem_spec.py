#!/usr/bin/env python3.12
"""Deterministic definition of the Radix demo ecosystem.

This module is pure Python: it builds the whole graph in memory (nodes, edges,
metrics) without touching HydraDB. ``seed_ecosystem.py`` is the thing that
writes it. Keeping the two apart means the dataset can be reasoned about,
snapshotted and unit-checked offline, and it makes the blast-radius tuning loop
fast — the reverse closure is verified by a local BFS before a single row is
sent over the wire.

Determinism
-----------
Every random draw comes from one ``random.Random(SEED)`` instance and every
iteration order is over a list, never a set. Ids are allocated through
``IdSpace`` in a fixed construction order, so re-running produces byte-identical
output and ``MERGE``-by-id stays genuinely idempotent.

The TanStack worm
-----------------
The centrepiece is a supply-chain compromise of ``tanstack-query``. The
dependency graph is *constructed*, not sampled, so that a reverse closure from
it at depth <= 6 reaches exactly 7 of the 20 services (35%). See
``INFECTION_CHAINS`` for the ladder and ``_assert_blast_radius`` for the check.
"""

from __future__ import annotations

import hashlib
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The shared id space lives with the backend; the seeder must not reinvent it.
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.schema import (  # noqa: E402
    DEPENDED_ON_BY,
    DEPENDS_ON,
    LOCKFILE,
    MAINTAINED_BY,
    MAINTAINER,
    MAINTAINS,
    PACKAGE,
    RESOLVED_IN,
    SERVICE,
    TYPOSQUAT_OF,
    VERSION,
    IdSpace,
    lockfile_key,
    maintainer_key,
    package_key,
    service_key,
    version_key,
)

SEED = 20260813

# --- Incident timeline ------------------------------------------------------
# "now" is frozen so the seed is reproducible across days.
NOW = datetime(2026, 8, 13, 9, 0, 0, tzinfo=timezone.utc)
#: Moment dev_alex's npm credentials were used from an unrecognised host.
BREACH_AT = datetime(2026, 8, 11, 3, 14, 0, tzinfo=timezone.utc)
WINDOW_HOURS = 48
WINDOW_END = BREACH_AT + timedelta(hours=WINDOW_HOURS)

COMPROMISED_PACKAGE = "tanstack-query"
COMPROMISED_VERSION = "4.28.0"
SAFE_VERSION = "4.27.6"
COMPROMISED_MAINTAINER = "dev_alex"

TARGET_PACKAGES = 390
TARGET_VERSIONS = 60
TARGET_MAINTAINERS = 22
TARGET_SERVICES = 20
TARGET_LOCKFILES = 10

#: Reverse closure at this depth must land inside this fraction of services.
BLAST_DEPTH = 6
BLAST_RANGE = (0.30, 0.45)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance.

    HydraDB has no string functions, so every typosquat metric is computed here
    at seed time and stored as an edge property for the traversal to read back.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """Normalised 1 - (edit distance / longest name); 1.0 means identical."""
    longest = max(len(a), len(b)) or 1
    return round(1.0 - levenshtein(a, b) / longest, 4)


# --- Graph value objects ----------------------------------------------------


@dataclass(frozen=True)
class Node:
    id: int
    label: str
    props: dict


@dataclass(frozen=True)
class Edge:
    id: int
    type: str
    src: int
    src_label: str
    dst: int
    dst_label: str
    props: dict


@dataclass
class Ecosystem:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def nodes_of(self, label: str) -> list[Node]:
        return [n for n in self.nodes if n.label == label]

    def edges_of(self, edge_type: str) -> list[Edge]:
        return [e for e in self.edges if e.type == edge_type]


# --- Package catalogue ------------------------------------------------------
# Layers model where a package sits in a real dependency tree: 0 = application
# framework, 4 = single-purpose leaf. Edges only ever run downhill, which keeps
# the generated graph acyclic and makes variable-length traversal cheap.

#: Allocated first and in this order so that ``tanstack-query`` lands on vertex
#: id 1_000_004 — the id used throughout docs/API_CONTRACT.md examples.
TANSTACK_FAMILY = [
    "tanstack-table",
    "tanstack-router",
    "tanstack-virtual",
    "tanstack-form",
    COMPROMISED_PACKAGE,
    "tanstack-store",
    "tanstack-ranger",
]

NPM_BY_LAYER: dict[int, list[str]] = {
    0: [
        "react", "react-dom", "next", "vue", "svelte", "angular-core", "express",
        "koa", "fastify", "nestjs-core", "webpack", "vite", "rollup", "parcel",
        "turbo", "nx", "remix-run", "astro", "nuxt", "gatsby", "electron",
        "react-native", "storybook", "docusaurus",
    ],
    1: [
        "axios", "lodash", "moment", "dayjs", "date-fns", "zod", "yup", "joi",
        "ramda", "rxjs", "redux", "zustand", "jotai", "recoil", "immer",
        "formik", "react-hook-form", "react-router", "swr", "apollo-client",
        "urql", "graphql", "socket-io", "ws", "node-fetch", "undici", "got",
        "superagent", "mongoose", "sequelize", "prisma-client", "typeorm",
        "knex", "pg", "mysql2", "redis", "ioredis", "bullmq", "jsonwebtoken",
        "bcrypt", "passport", "helmet", "cors", "morgan", "winston", "pino",
        "jest", "vitest", "mocha", "chai", "sinon", "cypress", "playwright",
        "eslint", "prettier", "typescript", "babel-core", "esbuild", "swc-core",
        "postcss", "tailwindcss", "sass", "less", "styled-components",
        "emotion-react", "framer-motion", "three", "d3", "chart-js", "recharts",
        "victory", "plotly-js", "leaflet", "mapbox-gl", "stripe-node",
    ],
    2: [
        "chalk", "commander", "yargs", "inquirer", "ora", "cli-progress",
        "boxen", "figlet", "dotenv", "cross-env", "nodemon", "ts-node", "tsup",
        "concurrently", "husky", "lint-staged", "semantic-release", "fs-extra",
        "chokidar", "glob", "rimraf", "del", "archiver", "tar-stream", "sharp",
        "jimp", "multer", "busboy", "form-data", "qs", "query-string", "uuid",
        "nanoid", "cuid", "slugify", "marked", "markdown-it", "highlight-js",
        "prismjs", "dompurify", "cheerio", "jsdom", "puppeteer-core",
    ],
    3: [
        "debug", "ms", "semver", "picocolors", "kleur", "ansi-styles",
        "supports-color", "strip-ansi", "string-width", "wrap-ansi",
        "cli-cursor", "restore-cursor", "signal-exit", "onetime", "mimic-fn",
        "p-limit", "p-queue", "p-retry", "delay", "is-stream", "get-stream",
        "merge-stream", "readable-stream", "safe-buffer", "buffer-from",
        "minimist", "minimatch", "picomatch", "braces", "fill-range",
        "to-regex-range", "is-number", "is-glob", "is-extglob",
        "normalize-path", "anymatch", "binary-extensions",
    ],
    4: [
        "object-assign", "has-flag", "escape-string-regexp", "ansi-regex",
        "color-name", "color-convert", "emoji-regex", "eastasianwidth",
        "path-key", "shebang-regex", "shebang-command", "isexe", "kind-of",
        "extend-shallow", "assign-symbols", "isobject", "lru-cache", "yallist",
        "tslib", "graceful-fs", "universalify", "jsonfile",
    ],
}

PYPI_BY_LAYER: dict[int, list[str]] = {
    0: ["django", "flask", "fastapi", "pyramid", "tornado", "scrapy", "airflow", "dbt-core"],
    1: [
        "requests", "httpx", "starlette", "uvicorn", "gunicorn", "pydantic",
        "sqlalchemy", "alembic", "psycopg2-binary", "celery", "boto3", "numpy",
        "pandas", "scipy", "scikit-learn", "matplotlib", "pillow",
        "beautifulsoup4", "lxml", "jinja2", "click", "typer", "rich",
        "structlog", "loguru", "pytest", "black", "ruff", "mypy",
    ],
    2: [
        "urllib3", "anyio", "httpcore", "werkzeug", "itsdangerous", "packaging",
        "attrs", "python-dateutil", "pyyaml", "cryptography", "cffi",
        "charset-normalizer",
    ],
    3: ["certifi", "idna", "sniffio", "h11", "markupsafe", "six", "pytz", "toml", "tomli", "typing-extensions"],
    4: ["pycparser", "zipp", "iniconfig", "pluggy", "exceptiongroup"],
}

#: Internal first-party packages — a monorepo's own libraries. Gives the
#: services something plausible to depend on besides open source.
INTERNAL_PREFIXES = ["@acme/"]
INTERNAL_LAYER0 = [
    "ui-shell", "design-system", "app-scaffold", "admin-core", "web-runtime",
    "mobile-bridge", "checkout-flow", "onboarding-kit",
]
INTERNAL_LAYER1 = [
    "http-client", "auth-sdk", "telemetry", "feature-gate", "money", "i18n",
    "logger", "config", "retry", "cache-client", "event-bus", "schema-registry",
    "rate-limiter", "circuit-breaker", "tracing", "metrics-sdk", "queue-client",
    "storage-sdk", "search-client", "notification-sdk", "billing-sdk",
    "identity-sdk", "audit-sdk", "flags-client",
]
INTERNAL_LAYER2 = [
    "result", "assert", "uuid7", "clock", "env", "hash", "codec", "bytes",
    "paths", "deep-merge", "shallow-eq", "typed-emitter",
]

#: Filler OSS utilities, generated combinatorially to reach TARGET_PACKAGES.
FILLER_ADJ = ["fast", "tiny", "micro", "safe", "lazy", "async", "deep", "flat", "smart", "quick", "lite", "pure"]
FILLER_NOUN = ["json", "path", "query", "stream", "buffer", "event", "cache", "diff", "hash", "queue", "token", "regex"]
FILLER_TAIL = ["utils", "parse", "walk", "merge", "clone", "sort", "guard", "pool", "sync", "map"]


# --- The TanStack worm ------------------------------------------------------

#: service -> the chain of packages between it and ``tanstack-query``.
#: Reverse-closure depth of the service is ``len(chain) + 1``, so this table
#: *is* the blast radius. The first seven land at depth <= 6; the last two sit
#: deliberately beyond the horizon so ``?depth=8`` visibly finds more.
INFECTION_CHAINS: list[tuple[str, list[str]]] = [
    ("checkout-web", []),
    ("admin-dashboard", ["admin-table-kit"]),
    ("api-gateway", ["gateway-cache-mw", "edge-query-client"]),
    ("user-profile-svc", ["profile-ui-shell", "edge-query-client"]),
    ("reporting-svc", ["report-builder", "chart-data-hooks", "query-adapter-core"]),
    ("analytics-pipeline", ["etl-dashboard", "metrics-widgets", "chart-data-hooks", "query-adapter-core"]),
    ("feature-flags", ["flag-console-ui", "config-panel-ui", "settings-forms", "form-state-bridge", "query-adapter-core"]),
    ("media-transcoder", ["legacy-admin-bridge", "flag-console-ui", "config-panel-ui", "settings-forms", "form-state-bridge", "query-adapter-core"]),
    ("session-store", ["deprecated-shim", "legacy-admin-bridge", "flag-console-ui", "config-panel-ui", "settings-forms", "form-state-bridge", "query-adapter-core"]),
]

#: Infected packages that lead nowhere — ecosystem satellites of the victim.
#: They fatten ``affected_package_ids`` without touching the service count.
INFECTION_SATELLITES: list[tuple[str, str]] = [
    ("tanstack-query-devtools", COMPROMISED_PACKAGE),
    ("query-persist-client", COMPROMISED_PACKAGE),
    ("react-query-firebase", COMPROMISED_PACKAGE),
    ("trpc-query-bridge", COMPROMISED_PACKAGE),
    ("vue-query-shim", COMPROMISED_PACKAGE),
    ("svelte-query-shim", COMPROMISED_PACKAGE),
    ("query-broadcast-sync", "query-adapter-core"),
    ("next-query-hydrate", "edge-query-client"),
    ("query-async-storage", "query-persist-client"),
    ("query-devtools-panel", "tanstack-query-devtools"),
]

#: Extra edges inside the infected region so SSpaths finds several routes to
#: the same victim. Each is chosen so it cannot shorten an existing chain
#: (``depth(src) <= depth(dst) + 1``); ``_assert_blast_radius`` re-checks.
INFECTION_CROSSLINKS: list[tuple[str, str]] = [
    ("admin-table-kit", "query-adapter-core"),
    ("profile-ui-shell", "query-adapter-core"),
    ("gateway-cache-mw", "admin-table-kit"),
    ("settings-forms", "chart-data-hooks"),
    ("config-panel-ui", "metrics-widgets"),
    ("etl-dashboard", "report-builder"),
    ("flag-console-ui", "etl-dashboard"),
]

TYPOSQUATS: list[tuple[str, str]] = [
    # (name, attack technique) — all target tanstack-query.
    ("tanstackquery", "separator-omission"),
    ("tanstack-querry", "character-doubling"),
    ("tanstack-qeury", "transposition"),
    ("tanstock-query", "substitution"),
    ("tаnstack-query", "homoglyph"),  # Cyrillic U+0430 in place of ASCII 'a'
    ("tanstack-queryjs", "suffix-append"),
]

#: Squats on other well-known packages, so typosquat detection reads as a
#: general capability rather than one hard-coded demo case.
OTHER_TYPOSQUATS: list[tuple[str, str, str]] = [
    ("loadash", "lodash", "transposition"),
    ("axois", "axios", "transposition"),
    ("expres", "express", "character-omission"),
]

# --- Services and lockfiles -------------------------------------------------

SERVICES: list[tuple[str, str, str, str]] = [
    # (name, criticality, team, language)
    ("auth-microservice", "tier-1", "identity", "typescript"),
    ("payments-api", "tier-1", "payments", "typescript"),
    ("checkout-web", "tier-1", "storefront", "typescript"),
    ("api-gateway", "tier-1", "platform", "go"),
    ("admin-dashboard", "tier-1", "internal-tools", "typescript"),
    ("billing-engine", "tier-1", "payments", "python"),
    ("user-profile-svc", "tier-2", "identity", "typescript"),
    ("notification-worker", "tier-2", "messaging", "typescript"),
    ("search-indexer", "tier-2", "discovery", "python"),
    ("analytics-pipeline", "tier-2", "data", "python"),
    ("inventory-svc", "tier-2", "supply", "go"),
    ("reporting-svc", "tier-2", "data", "typescript"),
    ("fraud-detector", "tier-2", "risk", "python"),
    ("recommendation-svc", "tier-3", "discovery", "python"),
    ("email-relay", "tier-3", "messaging", "typescript"),
    ("media-transcoder", "tier-3", "media", "go"),
    ("feature-flags", "tier-3", "platform", "typescript"),
    ("audit-log-svc", "tier-3", "compliance", "go"),
    ("session-store", "tier-3", "platform", "go"),
    ("shipping-orchestrator", "tier-3", "supply", "typescript"),
]

#: Services that carry a committed lockfile. The first four are all inside the
#: depth-6 blast radius and pin the compromised build, which is what
#: /api/generate-fix rewrites.
LOCKFILE_SERVICES = [
    ("checkout-web", "package-lock.json", "npm", True),
    ("admin-dashboard", "package-lock.json", "npm", True),
    ("api-gateway", "package-lock.json", "npm", True),
    ("reporting-svc", "pnpm-lock.yaml", "npm", True),
    ("payments-api", "package-lock.json", "npm", False),
    ("notification-worker", "yarn.lock", "npm", False),
    ("auth-microservice", "package-lock.json", "npm", False),
    ("search-indexer", "poetry.lock", "pypi", False),
    ("fraud-detector", "requirements.lock", "pypi", False),
    ("email-relay", "package-lock.json", "npm", False),
]

MAINTAINER_HANDLES = [
    COMPROMISED_MAINTAINER,
    "sindre_h", "tj_holowaychuk", "feross", "isaacs", "ljharb", "jaredpalmer",
    "kentcdodds", "rich_harris", "evanyou", "yyx990803_alt", "gaearon",
    "acdlite", "wesbos", "mjackson", "ryanflorence", "colinhacks",
    "developit", "lukeed", "antfu", "posva", "patak",
]


def _versions_for(name: str, rng: random.Random) -> list[str]:
    major = rng.randint(1, 9)
    minor = rng.randint(0, 20)
    patch = rng.randint(0, 15)
    return [f"{major}.{minor}.{patch}", f"{major}.{minor + 1}.0"]


class _Builder:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.ids = IdSpace()
        self.eco = Ecosystem()
        self.pkg: dict[str, Node] = {}          # name -> Node
        self.pkg_layer: dict[str, int] = {}
        self.pkg_eco: dict[str, str] = {}
        self.tainted: set[str] = set()          # names reachable *to* the victim
        self.service: dict[str, Node] = {}
        self.maintainer: dict[str, Node] = {}
        self.version: dict[tuple[str, str], Node] = {}
        self.lockfile: dict[str, Node] = {}
        self._dep_pairs: set[tuple[int, int]] = set()

    # -- primitives ---------------------------------------------------------

    def _node(self, label: str, key: str, props: dict) -> Node:
        node = Node(id=self.ids.node(label, key), label=label, props=props)
        self.eco.nodes.append(node)
        return node

    def _edge(self, edge_type: str, src: Node, dst: Node, props: dict) -> Edge:
        edge = Edge(
            id=self.ids.edge(),
            type=edge_type,
            src=src.id,
            src_label=src.label,
            dst=dst.id,
            dst_label=dst.label,
            props=props,
        )
        self.eco.edges.append(edge)
        return edge

    def _depends(self, src: Node, dst: Node, constraint: str, is_dev: bool = False, depth: int = 1) -> None:
        """Write a dependency *and* its materialised inverse.

        HydraDB rejects target-anchored variable-length traversal, so the
        reverse question ("who depends on the compromised package?") is only
        answerable if the inverse edge physically exists. Every DEPENDS_ON in
        this dataset is mirrored here — nowhere else creates one.
        """
        if (src.id, dst.id) in self._dep_pairs or src.id == dst.id:
            return
        self._dep_pairs.add((src.id, dst.id))
        self._edge(DEPENDS_ON, src, dst, {"constraint": constraint, "is_dev": is_dev, "transitive_depth": depth})
        self._edge(DEPENDED_ON_BY, dst, src, {"transitive_depth": depth})

    def _maintained(self, pkg: Node, maint: Node, since: datetime) -> None:
        self._edge(MAINTAINED_BY, pkg, maint, {"since": iso(since)})
        self._edge(MAINTAINS, maint, pkg, {"since": iso(since)})

    # -- packages -----------------------------------------------------------

    def _add_package(self, name: str, ecosystem: str, layer: int, **overrides) -> Node:
        rng = self.rng
        base_downloads = {0: (900_000, 12_000_000), 1: (400_000, 6_000_000),
                          2: (120_000, 2_000_000), 3: (2_000_000, 40_000_000),
                          4: (5_000_000, 90_000_000)}[layer]
        published = NOW - timedelta(days=rng.randint(3, 540), hours=rng.randint(0, 23))
        props = {
            "name": name,
            "ecosystem": ecosystem,
            "is_compromised": False,
            "is_typosquat": False,
            "risk_score": round(rng.uniform(0.02, 0.18), 4),
            "downloads_weekly": rng.randint(*base_downloads),
            "latest_version": f"{rng.randint(1, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 12)}",
            "last_published_at": iso(published),
            "license": rng.choice(["MIT", "MIT", "MIT", "Apache-2.0", "BSD-3-Clause", "ISC"]),
        }
        props.update(overrides)
        node = self._node(PACKAGE, package_key(ecosystem, name), props)
        self.pkg[name] = node
        self.pkg_layer[name] = layer
        self.pkg_eco[name] = ecosystem
        return node

    def _build_packages(self) -> None:
        rng = self.rng

        # TanStack family first: this ordering puts tanstack-query on 1_000_004,
        # the id used in docs/API_CONTRACT.md.
        for name in TANSTACK_FAMILY:
            if name == COMPROMISED_PACKAGE:
                self._add_package(
                    name, "npm", 1,
                    is_compromised=True,
                    risk_score=0.98,
                    downloads_weekly=4_200_000,
                    latest_version="4.28.1",
                    last_published_at=iso(BREACH_AT + timedelta(hours=6, minutes=26)),
                )
            else:
                self._add_package(name, "npm", 1, downloads_weekly=rng.randint(300_000, 2_400_000))

        # Infected region: chain links, then satellites. Registered before the
        # generic catalogue so the "clean" pool below can simply exclude them.
        chain_names: list[str] = []
        for _service, chain in INFECTION_CHAINS:
            for name in chain:
                if name not in chain_names:
                    chain_names.append(name)
        for name in chain_names:
            self._add_package(name, "npm", 1, downloads_weekly=rng.randint(40_000, 900_000))
            self.tainted.add(name)
        for name, _parent in INFECTION_SATELLITES:
            self._add_package(name, "npm", 2, downloads_weekly=rng.randint(15_000, 400_000))
            self.tainted.add(name)

        # Typosquats — low downloads, freshly published, high risk.
        for name, attack in TYPOSQUATS:
            distance = levenshtein(name, COMPROMISED_PACKAGE)
            self._add_package(
                name, "npm", 2,
                is_typosquat=True,
                typosquat_target=COMPROMISED_PACKAGE,
                attack_type=attack,
                risk_score=round(min(0.95, 0.55 + 0.12 * (4 - distance)), 4),
                downloads_weekly=rng.randint(120, 9_400),
                last_published_at=iso(BREACH_AT + timedelta(hours=rng.randint(1, 40))),
            )
        for name, target, attack in OTHER_TYPOSQUATS:
            self._add_package(
                name, "npm", 3,
                is_typosquat=True,
                typosquat_target=target,
                attack_type=attack,
                risk_score=round(rng.uniform(0.45, 0.7), 4),
                downloads_weekly=rng.randint(90, 4_000),
            )

        # Real-world catalogue.
        for layer in sorted(NPM_BY_LAYER):
            for name in NPM_BY_LAYER[layer]:
                if name not in self.pkg:
                    self._add_package(name, "npm", layer)
        for layer in sorted(PYPI_BY_LAYER):
            for name in PYPI_BY_LAYER[layer]:
                if name not in self.pkg:
                    self._add_package(name, "pypi", layer)

        # First-party monorepo packages.
        for names, layer in ((INTERNAL_LAYER0, 0), (INTERNAL_LAYER1, 1), (INTERNAL_LAYER2, 2)):
            for stem in names:
                self._add_package(INTERNAL_PREFIXES[0] + stem, "npm", layer)

        # Combinatorial filler to reach the target size. Iterating the product
        # in a fixed nested order keeps ids stable as long as the word lists are.
        filler: list[str] = []
        for adj in FILLER_ADJ:
            for noun in FILLER_NOUN:
                for tail in FILLER_TAIL:
                    filler.append(f"{adj}-{noun}-{tail}")
        idx = 0
        while len(self.pkg) < TARGET_PACKAGES and idx < len(filler):
            name = filler[idx]
            idx += 1
            if name in self.pkg:
                continue
            self._add_package(name, "npm", rng.choice([2, 3, 3, 4]))

    # -- maintainers --------------------------------------------------------

    def _build_maintainers(self) -> None:
        rng = self.rng
        for handle in MAINTAINER_HANDLES[:TARGET_MAINTAINERS]:
            compromised = handle == COMPROMISED_MAINTAINER
            created = NOW - timedelta(days=rng.randint(400, 4200))
            props = {
                "name": handle,
                "username": handle,
                "email": f"{handle}@npmjs.com",
                "key_fingerprint": "0x" + _digest("key", handle)[:16].upper(),
                "account_created_at": iso(created),
                "two_factor_enabled": False if compromised else rng.random() > 0.25,
                "is_compromised": compromised,
                "credential_status": "stolen" if compromised else "ok",
            }
            self.maintainer[handle] = self._node(MAINTAINER, maintainer_key(handle), props)

    def _wire_maintainers(self) -> None:
        rng = self.rng
        alex = self.maintainer[COMPROMISED_MAINTAINER]
        others = [self.maintainer[h] for h in MAINTAINER_HANDLES[1:TARGET_MAINTAINERS]]

        # dev_alex owns the victim plus five sister packages — the set the
        # maintainer sentinel walks via Maintainer -[:MAINTAINS]-> Package.
        alex_owned = [COMPROMISED_PACKAGE] + [n for n in TANSTACK_FAMILY if n != COMPROMISED_PACKAGE][:5]
        for name in alex_owned:
            self._maintained(self.pkg[name], alex, NOW - timedelta(days=rng.randint(300, 1600)))

        for name, node in self.pkg.items():
            if name in alex_owned:
                continue
            primary = others[hash_index(name, len(others))]
            self._maintained(node, primary, NOW - timedelta(days=rng.randint(60, 2000)))
            if rng.random() < 0.28:
                secondary = others[(hash_index(name, len(others)) + rng.randint(1, len(others) - 1)) % len(others)]
                if secondary.id != primary.id:
                    self._maintained(node, secondary, NOW - timedelta(days=rng.randint(30, 900)))

        self._alex_owned = alex_owned

    # -- versions -----------------------------------------------------------

    def _add_version(self, pkg_name: str, semver: str, published: datetime, **overrides) -> Node:
        rng = self.rng
        ecosystem = self.pkg_eco[pkg_name]
        pkg = self.pkg[pkg_name]
        props = {
            "name": f"{pkg_name}@{semver}",
            "semver": semver,
            "ecosystem": ecosystem,
            "package_id": pkg.id,
            "package_name": pkg_name,
            "published_at": iso(published),
            "compromised_window": WINDOW_END > published >= BREACH_AT,
            "integrity": "sha512-" + _digest("integrity", pkg_name, semver)[:32],
            "size_bytes": rng.randint(12_000, 2_400_000),
        }
        props.update(overrides)
        node = self._node(VERSION, version_key(ecosystem, pkg_name, semver), props)
        self.version[(pkg_name, semver)] = node
        # Package -[:RESOLVED_IN]-> Version keeps the release history walkable
        # with a single relationship type (HydraDB matches one type at a time).
        self._edge(RESOLVED_IN, pkg, node, {"resolved_version": semver})
        return node

    def _build_versions(self) -> None:
        rng = self.rng

        # The victim's release history straddles the compromise window.
        victim_releases = [
            ("4.26.1", datetime(2026, 6, 2, 11, 5, tzinfo=timezone.utc)),
            ("4.27.0", datetime(2026, 7, 1, 15, 40, tzinfo=timezone.utc)),
            ("4.27.5", datetime(2026, 7, 28, 8, 22, tzinfo=timezone.utc)),
            (SAFE_VERSION, datetime(2026, 8, 2, 13, 47, tzinfo=timezone.utc)),
            (COMPROMISED_VERSION, datetime(2026, 8, 11, 4, 12, tzinfo=timezone.utc)),
            ("4.28.1", datetime(2026, 8, 11, 9, 40, tzinfo=timezone.utc)),
        ]
        for semver, published in victim_releases:
            self._add_version(COMPROMISED_PACKAGE, semver, published)

        # dev_alex's sister packages: three of the five shipped inside the
        # 48h window, which is what /api/maintainer-risk flags.
        sisters = [n for n in self._alex_owned if n != COMPROMISED_PACKAGE]
        in_window = sisters[:3]
        for offset, name in enumerate(sisters):
            old_semver = f"{2 + offset}.4.{offset}"
            self._add_version(name, old_semver, NOW - timedelta(days=rng.randint(60, 220)))
            if name in in_window:
                published = BREACH_AT + timedelta(hours=6 + offset * 9, minutes=17 * (offset + 1))
                self.pkg[name].props["last_published_at"] = iso(published)
                self.pkg[name].props["risk_score"] = round(rng.uniform(0.55, 0.72), 4)
            else:
                published = BREACH_AT - timedelta(days=rng.randint(25, 90))
            self._add_version(name, f"{2 + offset}.5.0", published)

        # Fill the remainder with release pairs for well-known packages.
        popular = [n for n in NPM_BY_LAYER[0] + NPM_BY_LAYER[1] + PYPI_BY_LAYER[1] if n in self.pkg]
        idx = 0
        while len(self.version) < TARGET_VERSIONS and idx < len(popular):
            name = popular[idx]
            idx += 1
            for semver in _versions_for(name, rng):
                if len(self.version) >= TARGET_VERSIONS:
                    break
                if (name, semver) in self.version:
                    continue
                self._add_version(name, semver, NOW - timedelta(days=rng.randint(10, 400)))

    # -- services -----------------------------------------------------------

    def _build_services(self) -> None:
        rng = self.rng
        for name, criticality, team, language in SERVICES:
            props = {
                "name": name,
                "repo_url": f"github.com/acme-corp/{name}",
                "criticality": criticality,
                "team": team,
                "language": language,
                "deploy_env": rng.choice(["production", "production", "production", "staging"]),
                "monthly_requests": rng.randint(1_200_000, 940_000_000),
            }
            self.service[name] = self._node(SERVICE, service_key(name), props)

    # -- dependency wiring --------------------------------------------------

    def _wire_infection(self) -> None:
        """Construct the reverse-closure ladder, then the satellites.

        Nothing here is random: the depth of every exposed service is exactly
        ``len(chain) + 1`` by construction.
        """
        victim = self.pkg[COMPROMISED_PACKAGE]
        for service_name, chain in INFECTION_CHAINS:
            svc = self.service[service_name]
            hops = [self.pkg[n] for n in chain] + [victim]
            self._depends(svc, hops[0], "^4.0.0" if not chain else "^1.0.0")
            for a, b in zip(hops, hops[1:]):
                self._depends(a, b, "^4.27.0" if b.id == victim.id else "^2.0.0")

        for name, parent in INFECTION_SATELLITES:
            self._depends(self.pkg[name], self.pkg[parent], "^4.28.0")

        for src, dst in INFECTION_CROSSLINKS:
            self._depends(self.pkg[src], self.pkg[dst], "~1.4.0")

        # The victim's own (clean) dependencies.
        for dep in ["tslib", "lru-cache", "debug"]:
            if dep in self.pkg:
                self._depends(victim, self.pkg[dep], "^2.6.0")

    def _clean_candidates(self, min_layer: int) -> list[Node]:
        return [
            self.pkg[name]
            for name in self.pkg
            if name not in self.tainted
            and name != COMPROMISED_PACKAGE
            and not self.pkg[name].props["is_typosquat"]
            and self.pkg_layer[name] >= min_layer
        ]

    def _wire_dependencies(self) -> None:
        """Random-but-seeded dependency edges for the rest of the ecosystem.

        Invariant: no *clean* package or service ever gains an edge into the
        tainted set. That is what freezes the blast radius at the constructed
        value instead of letting random wiring drift it.
        """
        rng = self.rng
        fanout = {0: (4, 8), 1: (2, 5), 2: (1, 4), 3: (1, 3), 4: (0, 1)}
        by_layer_clean = {L: [] for L in range(5)}
        for name in self.pkg:
            if name in self.tainted or name == COMPROMISED_PACKAGE:
                continue
            by_layer_clean[self.pkg_layer[name]].append(self.pkg[name])

        for layer in range(5):
            pool = []
            for deeper in range(layer + 1, 5):
                pool.extend(by_layer_clean[deeper])
            if not pool:
                continue
            for src in by_layer_clean[layer]:
                low, high = fanout[layer]
                for dst in rng.sample(pool, min(rng.randint(low, high), len(pool))):
                    self._depends(src, dst, _constraint(rng), is_dev=rng.random() < 0.18)

        # Tainted packages also pull in ordinary open source — realistic, and
        # harmless because edges point *away* from the infected region.
        clean_pool = self._clean_candidates(min_layer=2)
        for name in sorted(self.tainted):
            for dst in rng.sample(clean_pool, rng.randint(1, 4)):
                self._depends(self.pkg[name], dst, _constraint(rng))

        # Services depend on application-layer packages.
        app_pool = [n for n in by_layer_clean[0] + by_layer_clean[1]]
        for name, _crit, _team, _lang in SERVICES:
            svc = self.service[name]
            for dst in rng.sample(app_pool, rng.randint(4, 9)):
                self._depends(svc, dst, _constraint(rng), is_dev=rng.random() < 0.1)

    # -- lockfiles ----------------------------------------------------------

    def _build_lockfiles(self) -> None:
        rng = self.rng
        victim = self.pkg[COMPROMISED_PACKAGE]
        bad_version = self.version[(COMPROMISED_PACKAGE, COMPROMISED_VERSION)]
        depths = self._reverse_depths()

        for service_name, filename, ecosystem, infected in LOCKFILE_SERVICES[:TARGET_LOCKFILES]:
            svc = self.service[service_name]
            commit = _digest("commit", service_name, filename)[:40]
            props = {
                "name": f"{service_name}/{filename}",
                "filename": filename,
                "commit_hash": commit,
                "short_commit": commit[:7],
                "service_id": svc.id,
                "service_name": service_name,
                "ecosystem": ecosystem,
                "generated_at": iso(NOW - timedelta(days=rng.randint(1, 30))),
                "entry_count": rng.randint(380, 1650),
                "pins_compromised": infected,
            }
            lock = self._node(LOCKFILE, lockfile_key(service_name, filename), props)
            self.lockfile[service_name] = lock
            svc.props["lockfile_id"] = lock.id
            svc.props["lockfile_name"] = filename

            # Service -> Lockfile keeps the lockfile attached in the visualiser
            # and lets the closure surface the service through its pinned tree.
            self._depends(svc, lock, "lockfile", depth=0)

            pool = [p for p in self._clean_candidates(min_layer=1) if self.pkg_eco[p.props["name"]] == ecosystem]
            for dst in rng.sample(pool, min(rng.randint(7, 13), len(pool))):
                self._depends(lock, dst, "=" + dst.props["latest_version"], depth=rng.randint(1, 4))

            if infected:
                resolved_depth = depths.get(COMPROMISED_PACKAGE, {}).get(service_name, 1)
                self._depends(lock, victim, "=" + COMPROMISED_VERSION, depth=resolved_depth)
                self._edge(RESOLVED_IN, lock, bad_version, {"resolved_version": COMPROMISED_VERSION})
                # Also pin the intermediate hops that pulled the victim in.
                chain = dict(INFECTION_CHAINS).get(service_name, [])
                for hop in chain:
                    self._depends(lock, self.pkg[hop], "=" + self.pkg[hop].props["latest_version"], depth=1)
            else:
                for (pkg_name, semver), vnode in list(self.version.items())[:4]:
                    if self.pkg_eco[pkg_name] == ecosystem and pkg_name not in self.tainted \
                            and pkg_name != COMPROMISED_PACKAGE:
                        self._edge(RESOLVED_IN, lock, vnode, {"resolved_version": semver})

    # -- typosquats ---------------------------------------------------------

    def _wire_typosquats(self) -> None:
        victim = self.pkg[COMPROMISED_PACKAGE]
        for name, attack in TYPOSQUATS:
            self._edge(TYPOSQUAT_OF, self.pkg[name], victim, {
                "edit_distance": levenshtein(name, COMPROMISED_PACKAGE),
                "similarity_score": similarity(name, COMPROMISED_PACKAGE),
                "attack_type": attack,
            })
        for name, target, attack in OTHER_TYPOSQUATS:
            if target not in self.pkg:
                continue
            self._edge(TYPOSQUAT_OF, self.pkg[name], self.pkg[target], {
                "edit_distance": levenshtein(name, target),
                "similarity_score": similarity(name, target),
                "attack_type": attack,
            })

    # -- verification -------------------------------------------------------

    def _reverse_depths(self, roots: list[str] | None = None) -> dict[str, dict[str, int]]:
        """BFS over the materialised inverse edges, mirroring the engine.

        Same traversal HydraDB will run, done locally so the blast radius can be
        tuned without a round trip.
        """
        adjacency: dict[int, list[int]] = {}
        for edge in self.eco.edges:
            if edge.type == DEPENDED_ON_BY:
                adjacency.setdefault(edge.src, []).append(edge.dst)

        name_by_id = {n.id: n.props["name"] for n in self.eco.nodes}
        out: dict[str, dict[str, int]] = {}
        for root in roots or [COMPROMISED_PACKAGE]:
            start = self.pkg[root].id
            seen = {start: 0}
            queue = deque([start])
            while queue:
                cur = queue.popleft()
                for nxt in adjacency.get(cur, ()):
                    if nxt not in seen:
                        seen[nxt] = seen[cur] + 1
                        queue.append(nxt)
            out[root] = {name_by_id[i]: d for i, d in seen.items() if i != start}
        return out

    def _assert_blast_radius(self) -> dict:
        depths = self._reverse_depths()[COMPROMISED_PACKAGE]
        service_names = [s[0] for s in SERVICES]
        exposed = sorted(n for n, d in depths.items() if n in self.service and d <= BLAST_DEPTH)
        deep = sorted(n for n, d in depths.items() if n in self.service and d <= 8)
        pct = len(exposed) / len(service_names)
        if not (BLAST_RANGE[0] <= pct <= BLAST_RANGE[1]):
            raise AssertionError(
                f"blast radius {pct:.1%} ({len(exposed)}/{len(service_names)}) outside "
                f"{BLAST_RANGE[0]:.0%}-{BLAST_RANGE[1]:.0%}; retune INFECTION_CHAINS"
            )
        crit = {s[0]: s[1] for s in SERVICES}
        return {
            "exposed_services": exposed,
            "exposed_service_count": len(exposed),
            "total_services": len(service_names),
            "exposed_pct": round(pct * 100, 1),
            "tier1_exposed": sum(1 for n in exposed if crit[n] == "tier-1"),
            "exposed_at_depth_8": len(deep),
            "exposed_lockfiles": sorted(n for n, d in depths.items()
                                        if any(lf.props["name"] == n for lf in self.eco.nodes_of(LOCKFILE))
                                        and d <= BLAST_DEPTH),
            "affected_packages": sum(1 for n, d in depths.items()
                                     if n in self.pkg and d <= BLAST_DEPTH),
            "service_depths": {n: depths[n] for n in sorted(depths) if n in self.service},
        }

    # -- entry point --------------------------------------------------------

    def build(self) -> Ecosystem:
        self._build_packages()
        self._build_maintainers()
        self._wire_maintainers()
        self._build_versions()
        self._build_services()
        self._wire_infection()
        self._wire_dependencies()
        self._build_lockfiles()
        self._wire_typosquats()

        blast = self._assert_blast_radius()
        victim = self.pkg[COMPROMISED_PACKAGE]
        counts = {label: len(self.eco.nodes_of(label)) for label in
                  (PACKAGE, VERSION, MAINTAINER, SERVICE, LOCKFILE)}
        edge_counts: dict[str, int] = {}
        for edge in self.eco.edges:
            edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1

        self.eco.meta = {
            "seed": SEED,
            "generated_for": iso(NOW),
            "node_counts": counts,
            "total_nodes": len(self.eco.nodes),
            "edge_counts": edge_counts,
            "total_edges": len(self.eco.edges),
            "api_visible_edges": sum(v for k, v in edge_counts.items()
                                     if k not in (DEPENDED_ON_BY, MAINTAINS)),
            "incident": {
                "package": COMPROMISED_PACKAGE,
                "package_id": victim.id,
                "compromised_version": COMPROMISED_VERSION,
                "compromised_version_id": self.version[(COMPROMISED_PACKAGE, COMPROMISED_VERSION)].id,
                "safe_version": SAFE_VERSION,
                "safe_version_id": self.version[(COMPROMISED_PACKAGE, SAFE_VERSION)].id,
                "maintainer": COMPROMISED_MAINTAINER,
                "maintainer_id": self.maintainer[COMPROMISED_MAINTAINER].id,
                "breach_at": iso(BREACH_AT),
                "window_hours": WINDOW_HOURS,
                "sister_packages": [
                    {"name": n, "id": self.pkg[n].id,
                     "published_within_window": self.pkg[n].props["last_published_at"] >= iso(BREACH_AT)
                     and self.pkg[n].props["last_published_at"] < iso(WINDOW_END)}
                    for n in self._alex_owned if n != COMPROMISED_PACKAGE
                ],
                "typosquats": [
                    {"name": n, "id": self.pkg[n].id,
                     "edit_distance": levenshtein(n, COMPROMISED_PACKAGE),
                     "similarity_score": similarity(n, COMPROMISED_PACKAGE),
                     "attack_type": a}
                    for n, a in TYPOSQUATS
                ],
            },
            "blast_radius": blast,
            "service_ids": {name: node.id for name, node in self.service.items()},
            "lockfile_ids": {name: node.id for name, node in self.lockfile.items()},
        }
        return self.eco


def _constraint(rng: random.Random) -> str:
    style = rng.random()
    major, minor, patch = rng.randint(1, 9), rng.randint(0, 20), rng.randint(0, 12)
    if style < 0.62:
        return f"^{major}.{minor}.{patch}"
    if style < 0.84:
        return f"~{major}.{minor}.{patch}"
    if style < 0.94:
        return f">={major}.{minor}.0"
    return f"{major}.{minor}.{patch}"


def hash_index(key: str, modulus: int) -> int:
    """Stable index from a name — Python's ``hash`` is salted per process."""
    return int(_digest("idx", key)[:8], 16) % modulus


def build_ecosystem() -> Ecosystem:
    """Build the full deterministic ecosystem graph."""
    return _Builder().build()


if __name__ == "__main__":
    import json

    eco = build_ecosystem()
    print(json.dumps(eco.meta, indent=2))
