"""Own and freeze JSON values at the planning snapshot boundary."""
from collections.abc import Mapping
from types import MappingProxyType


class _FrozenList(tuple):
    # Preserve comparisons with legacy JSON-list readers while forbidding writes.
    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return False

    __hash__ = tuple.__hash__

    def __ne__(self, other):
        return not self == other


def freeze_json(value):
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("snapshot object keys must be strings")
        return MappingProxyType({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return _FrozenList(freeze_json(v) for v in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("snapshot must contain JSON values")


def thaw_json(value):
    if isinstance(value, Mapping):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(v) for v in value]
    return value
