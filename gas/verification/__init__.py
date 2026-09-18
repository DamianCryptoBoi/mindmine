"""Lazy public exports for verification modules.

The generator only needs C2PA media-format detection at startup. Load the
validator's OpenCV, CLIP, and verification pipeline dependencies on demand.
"""

from importlib import import_module


_EXPORTS = {
    "run_verification": "gas.verification.verification_pipeline",
    "verify_media": "gas.verification.verification_pipeline",
    "get_verification_summary": "gas.verification.verification_pipeline",
    "preload_clip_models": "gas.verification.clip_utils",
    "clear_clip_models": "gas.verification.clip_utils",
    "calculate_clip_alignment": "gas.verification.clip_utils",
    "calculate_clip_alignment_consensus": "gas.verification.clip_utils",
    "serialize_features": "gas.verification.clip_utils",
    "deserialize_features": "gas.verification.clip_utils",
    "find_near_duplicate_by_embedding": "gas.verification.clip_utils",
    "DEFAULT_EMBEDDING_SIMILARITY_THRESHOLD": "gas.verification.clip_utils",
    "compute_image_hash": "gas.verification.duplicate_detection",
    "compute_video_hash": "gas.verification.duplicate_detection",
    "compute_media_hash": "gas.verification.duplicate_detection",
    "compute_crop_resistant_hash": "gas.verification.duplicate_detection",
    "hamming_distance": "gas.verification.duplicate_detection",
    "count_crop_segment_matches": "gas.verification.duplicate_detection",
    "find_duplicates": "gas.verification.duplicate_detection",
    "check_duplicate_in_db": "gas.verification.duplicate_detection",
    "DEFAULT_HAMMING_THRESHOLD": "gas.verification.duplicate_detection",
    "DEFAULT_CROP_RESISTANT_MATCH_THRESHOLD": "gas.verification.duplicate_detection",
    "verify_c2pa": "gas.verification.c2pa_verification",
    "C2PAVerificationResult": "gas.verification.c2pa_verification",
    "TRUSTED_CERT_ISSUERS": "gas.verification.c2pa_verification",
    "TRUSTED_CA_ISSUERS": "gas.verification.c2pa_verification",
    "detect_media_format": "gas.verification.c2pa_verification",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
