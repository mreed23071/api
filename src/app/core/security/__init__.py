"""Authentication and authorization.

Deliberately empty of re-exports. `app.core.config` imports `Scope` from
`app.core.security.principal`, and `app.core.security.providers` imports
`app.core.config` - re-exporting from this package would turn that into an
import cycle. Import from the submodule you need.
"""
