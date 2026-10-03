"""Galaxy kernel services that connect independent core subsystems."""
from .projector import KernelProjector
from .watcher import KernelWatcher

__all__ = ["KernelProjector", "KernelWatcher"]
