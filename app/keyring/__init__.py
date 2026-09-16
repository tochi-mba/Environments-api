"""Everything that talks to keyring, the identity provider.

This package is the only one that imports :mod:`keyring_client`, which an import contract
enforces. :class:`JwksClient` is re-exported because the API layer has to name the type it
injects, and naming it through here keeps that one package the single door to keyring.
"""

from keyring_client import JwksClient

__all__ = ["JwksClient"]
