"""Decorate a prepared object's execution path; keep its observer untouched."""

from .rules import validate_options
from .runtime import ProxyRuntime


class ProxyEnvironment:
    def __init__(self, base, options, target_factory):
        self.base, self.options, self.target_factory = base, options, target_factory
        self.proxy = None

    def prepare(self, context, events):
        self.base.prepare(context, events)
        target = self.target_factory(context)
        if getattr(self, "confirmation", None):
            target["confirmation"] = self.confirmation
        self.proxy = ProxyRuntime(context, events, target, self.options["rule"])
        self.proxy.start()

    def cleanup(self, context, events):
        try:
            if self.proxy is not None:
                self.proxy.close()
        finally:
            self.base.cleanup(context, events)


class ProxyBehavior:
    def __init__(self, base, environment):
        self.base, self.environment = base, environment

    def __getattr__(self, name):
        return getattr(self.base, name)

    def bindings(self, context):
        bindings = self.base.bindings(context)
        for binding in bindings:
            if binding["origin"] != self.environment.proxy.target["origin"]:
                raise RuntimeError("Execution binding differs from the verified proxy target")
            binding["origin"] = self.environment.proxy.origin
        return bindings

    def metadata(self, calls):
        return {**self.base.metadata(calls), "proxy_service": self.environment.proxy.identity if self.environment.proxy else None}


def attach_proxy(plugin, options, target_factory):
    config = validate_options(options)
    if config["rule"] and config["rule"]["schema_version"] == 2 and not plugin.run_metadata.get("commit_loss_enabled"):
        raise ValueError("Confirmed drop requires the D22 controller")
    environment = ProxyEnvironment(plugin.environment, config, target_factory)
    plugin.environment = environment
    plugin.execution.behavior = ProxyBehavior(plugin.execution.behavior, environment)
    plugin.run_metadata.update(proxy_enabled=True, proxy_version=1)
    plugin.run_metadata["mode"] = plugin.run_metadata["mode"].replace("无故障注入", "独立网络故障代理；观察器直连")
    return plugin
