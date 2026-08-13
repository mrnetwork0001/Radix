"""Radix — graph-native software supply-chain security sentinel.

Radix answers one question fast: *when this package is compromised, what breaks?*
The answer is a reverse transitive closure over a dependency graph stored in
HydraDB, plus the maintainer, typosquat, and blast-radius context that turns a
list of ids into an incident report.

Layout:

* :mod:`app.schema`       — the shared integer id space and label/edge constants.
* :mod:`app.hydra_client` — the HydraDB transport, value decoder, and traversals.
* :mod:`app.models`       — pydantic models mirroring ``docs/API_CONTRACT.md``.
"""

__version__ = "1.0.0"
