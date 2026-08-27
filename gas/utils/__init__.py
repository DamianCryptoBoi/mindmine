"""Lazy public exports for utility modules.

Importing a miner should not load validator-only image augmentation modules.
"""

from importlib import import_module


_EXPORTS = {
    "print_info": "gas.utils.utils",
    "fail_with_none": "gas.utils.utils",
    "on_block_interval": "gas.utils.utils",
    "ExitContext": "gas.utils.utils",
    "get_metadata": "gas.utils.utils",
    "get_file_modality": "gas.utils.utils",
    "run_in_thread": "gas.utils.utils",
    "get_miner_uids": "gas.utils.metagraph",
    "create_set_weights": "gas.utils.metagraph",
    "autoupdate": "gas.utils.autoupdater",
    "apply_random_augmentations": "gas.utils.transforms",
    "get_base_transforms": "gas.utils.transforms",
    "get_random_augmentations": "gas.utils.transforms",
    "get_random_augmentations_medium": "gas.utils.transforms",
    "get_random_augmentations_hard": "gas.utils.transforms",
    "StateManager": "gas.utils.state_manager",
    "save_validator_state": "gas.utils.state_manager",
    "load_validator_state": "gas.utils.state_manager",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
