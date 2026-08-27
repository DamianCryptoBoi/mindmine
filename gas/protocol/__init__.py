"""Lazy public exports for protocol modules.

The external-API generator only needs Epistula request handling at startup. Media
encoding and validator request helpers pull in dependencies that belong to other
runtime paths, so load them only when their public functions are requested.
"""

from importlib import import_module


_EXPORTS = {
    "generate_header": "gas.protocol.epistula",
    "verify_signature": "gas.protocol.epistula",
    "create_header_hook": "gas.protocol.epistula",
    "get_verifier": "gas.protocol.epistula",
    "determine_epistula_version_and_verify": "gas.protocol.epistula",
    "get_miner_type": "gas.protocol.validator_requests",
    "query_generative_miner": "gas.protocol.validator_requests",
    "image_to_bytes": "gas.protocol.encoding",
    "video_to_bytes": "gas.protocol.encoding",
    "media_to_bytes": "gas.protocol.encoding",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
