"""Integrity checks for the custom bundled plugins added in the 12h session.

Guards against drift between a plugin's declared ``provides_hooks`` (manifest)
and what its ``register(ctx)`` actually wires, and against registering a hook
name the core doesn't know about.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import VALID_HOOKS

# (import path, plugin dir under plugins/)
_CUSTOM_PLUGINS = [
    ("plugins.reflection", "reflection"),
    ("plugins.redact_output", "redact_output"),
    ("plugins.auto_index", "auto_index"),
]

_PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "plugins"


class _RecordingCtx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))


@pytest.mark.parametrize("import_path,plugin_dir", _CUSTOM_PLUGINS)
def test_manifest_parses_and_declares_hooks(import_path, plugin_dir):
    manifest_file = _PLUGINS_ROOT / plugin_dir / "plugin.yaml"
    assert manifest_file.is_file(), f"missing manifest: {manifest_file}"
    data = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    assert data.get("name")
    assert data.get("description")
    assert data.get("kind") == "standalone"
    assert isinstance(data.get("provides_hooks"), list) and data["provides_hooks"]


@pytest.mark.parametrize("import_path,plugin_dir", _CUSTOM_PLUGINS)
def test_register_wires_declared_hooks(import_path, plugin_dir):
    mod = importlib.import_module(import_path)
    assert hasattr(mod, "register"), f"{import_path} has no register()"

    ctx = _RecordingCtx()
    mod.register(ctx)

    registered_names = {name for name, _cb in ctx.hooks}
    assert registered_names, "register() wired no hooks"

    # Every registered hook must be a hook the core understands.
    for name in registered_names:
        assert name in VALID_HOOKS, f"{import_path} registered unknown hook {name!r}"

    # Manifest's provides_hooks must match what register() actually wires.
    manifest_file = _PLUGINS_ROOT / plugin_dir / "plugin.yaml"
    declared = set(yaml.safe_load(manifest_file.read_text(encoding="utf-8"))["provides_hooks"])
    assert declared == registered_names, (
        f"{import_path}: manifest provides_hooks {declared} != registered {registered_names}"
    )
