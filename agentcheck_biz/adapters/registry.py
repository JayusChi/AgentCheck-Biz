"""Only factories explicitly registered by application code can run."""

import math
import json
import re
from typing import Callable

from .contracts import BusinessPlugin, PluginConfigurationError


class PluginRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[dict, dict], BusinessPlugin]] = {}

    def register(self, name: str, factory: Callable[[dict, dict], BusinessPlugin]):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not callable(factory):
            raise PluginConfigurationError("Invalid plugin name or factory")
        if name in self._factories:
            raise PluginConfigurationError(f"Plugin already registered: {name}")
        self._factories[name] = factory

    def resolve(self, name: str, case: dict, options: dict) -> BusinessPlugin:
        if not isinstance(name, str) or name not in self._factories:
            raise PluginConfigurationError(f"Unknown registered plugin: {name}")
        if not isinstance(options, dict):
            raise PluginConfigurationError("Plugin options must be an object")
        plugin = self._factories[name](case, options)
        if not isinstance(plugin, BusinessPlugin) or plugin.name != name or type(plugin.version) is not int or plugin.version < 1:
            raise PluginConfigurationError("Invalid plugin identity")
        for field, methods in (("environment", ("prepare", "cleanup")), ("execution", ("execute", "metadata")),
                               ("observer", ("observe",)), ("verifier", ("verify",))):
            if not all(callable(getattr(getattr(plugin, field), method, None)) for method in methods):
                raise PluginConfigurationError(f"Plugin {name} is missing {field} capability")
        if (not isinstance(plugin.operation_id, str) or not plugin.operation_id.strip()
                or type(plugin.timeout_seconds) not in (int, float)
                or not math.isfinite(plugin.timeout_seconds) or not 0 < plugin.timeout_seconds <= 300
                or not isinstance(plugin.run_prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plugin.run_prefix)):
            raise PluginConfigurationError("Invalid plugin run identity or timeout")
        if (not isinstance(plugin.case, dict) or not isinstance(plugin.run_metadata, dict)
                or not isinstance(plugin.run_metadata.get("mode"), str) or not plugin.run_metadata["mode"].strip()):
            raise PluginConfigurationError("Plugin must supply a JSON case and a declared execution mode")
        try:
            json.dumps([plugin.case, plugin.run_metadata], allow_nan=False)
        except (ValueError, TypeError) as error:
            raise PluginConfigurationError("Plugin configuration is not serializable") from error
        return plugin


def builtin_registry():
    from .gitea_mcp import gitea_mcp_plugin
    from .ticket_mcp import ticket_mcp_plugin
    from .gitea import gitea_plugin
    from .ticket_http import ticket_http_plugin
    from .ticket_local import ticket_local_plugin
    registry = PluginRegistry()
    registry.register("ticket-local", ticket_local_plugin)
    registry.register("ticket-http", ticket_http_plugin)
    registry.register("gitea", gitea_plugin)
    registry.register("ticket-mcp", ticket_mcp_plugin)
    registry.register("gitea-mcp", gitea_mcp_plugin)
    return registry
