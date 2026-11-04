"""Point OpenSSL at a CA bundle that actually exists inside the app bundle.

The macOS app ships its own Python.framework. That interpreter's OpenSSL was
built with a default CA path baked in (under the framework's own
``etc/openssl``), and that directory is NOT present in the shipped bundle --
so every HTTPS request from the packaged app failed with::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

Measured: the same `fulcra data-updates` call fails from the bundled CLI and
succeeds verbatim when SSL_CERT_FILE points at the bundled certifi CA file.

``certifi`` is already shipped in the bundle, so the fix is to point the
environment at it. Doing it through ``os.environ`` (rather than building an
SSLContext) is deliberate: the daemon shells out to the bundled ``fulcra``
CLI, and a child process inherits the environment but not our context.

This is a NO-OP wherever the platform CA store is usable -- a dev venv, a
system Python, Linux -- so it cannot change behaviour outside the bundle.
"""
from __future__ import annotations

import logging
import os
import ssl

_log = logging.getLogger("fulcra_collect.ssl_bootstrap")

_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def platform_ca_store_is_usable() -> bool:
    """True when OpenSSL's compiled-in default CA location exists."""
    paths = ssl.get_default_verify_paths()
    if paths.openssl_cafile and os.path.exists(paths.openssl_cafile):
        return True
    return bool(paths.openssl_capath and os.path.isdir(paths.openssl_capath))


def ensure_ca_bundle(*, env: dict | None = None) -> str | None:
    """Ensure HTTPS can verify certificates. Returns the path set, or None.

    Honours an operator-supplied ``SSL_CERT_FILE``: if someone has pointed us
    at a corporate CA bundle we must not override it.
    """
    environ = os.environ if env is None else env
    if environ.get("SSL_CERT_FILE"):
        return None
    if platform_ca_store_is_usable():
        return None
    try:
        import certifi
    except ImportError:  # pragma: no cover - certifi is a hard dependency
        _log.warning(
            "no usable CA store and certifi is not installed; HTTPS requests "
            "will fail certificate verification")
        return None
    bundle = certifi.where()
    if not os.path.exists(bundle):
        _log.warning("certifi reported %s but it does not exist", bundle)
        return None
    for var in _VARS:
        environ.setdefault(var, bundle)
    _log.info(
        "no usable platform CA store; pointed %s at the bundled certifi CA "
        "file (%s) so HTTPS and child processes can verify certificates",
        ", ".join(_VARS), bundle)
    return bundle
