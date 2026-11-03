"""Finding and loading playbooks: a directory with playbook.yaml, and optionally pack.py."""

import importlib.util
from pathlib import Path
from types import ModuleType

from onboarding.lib import github, hosts
from onboarding.playbook import Playbook, PlaybookError, check_refs, load_playbook
from onboarding.registry import Registry

BUNDLED = Path(__file__).parent / "playbooks"


def bundled() -> dict[str, Path]:
    return {p.name: p for p in sorted(BUNDLED.iterdir()) if (p / "playbook.yaml").exists()}


def directory_of(spec: str) -> Path:
    path = Path(spec)
    directory = path if (path / "playbook.yaml").exists() else bundled().get(spec)
    if directory is None:
        raise PlaybookError(f"no playbook {spec!r}. Bundled: {', '.join(bundled()) or 'none'}")
    return directory


def pack_module(directory: Path) -> ModuleType | None:
    """The playbook's pack.py, if it has one. It is code that runs with the operator's powers."""
    pack = directory / "pack.py"
    if not pack.exists():
        return None
    module_spec = importlib.util.spec_from_file_location(f"onboarding_pack_{directory.name}", pack)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def load(spec: str) -> tuple[Playbook, Registry]:
    """`spec` is a bundled playbook's name, or the path of a playbook directory.

    A pack.py is code: only load playbooks you trust.
    """
    directory = directory_of(spec)
    playbook = load_playbook(directory)
    registry = Registry()
    hosts.register(registry)
    github.register(registry)
    if (module := pack_module(directory)) is not None:
        module.register(registry)
    check_refs(playbook, registry.probes, registry.actions)
    return playbook, registry
