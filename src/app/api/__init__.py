"""The HTTP layer.

Structure:

    api/
    ├── errors.py     error envelope + exception handlers (the only place a
    │                 domain error becomes a status code)
    ├── deps.py       composition root - builds services from request scope
    ├── system.py     unversioned liveness/readiness probes
    ├── router.py     mounts every API version, plus the version index
    └── v1/
        ├── routes/   one module per bounded context
        └── schemas/  the wire contract THIS version publishes

The rule that makes versioning real: **a route may only reference schemas from
its own version.** Domain DTOs are internal and may change freely; v1 schemas
are frozen once published.
"""
