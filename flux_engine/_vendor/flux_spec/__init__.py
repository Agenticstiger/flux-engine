"""Vendored fallback copy of the ``flux_spec`` distribution (Apache-2.0).

See ``NOTICE`` for attribution and ``VENDOR-NOTES.md`` for the upstream
commit this copy was taken from and every line changed to make it resolve
its data paths from inside this package instead of a checked-out FLUX
repository. ``flux_engine.spec`` is the only supported entry point into
this package from the rest of flux_engine — nothing else should import
``flux_engine._vendor.flux_spec`` directly except to resolve a data path.
"""
