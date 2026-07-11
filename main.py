# Import StreamController modules
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder

# Import actions
from .actions.CpuMonitorAction.CpuMonitorAction import CpuMonitorAction

class CpuMonitorPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        ## Register actions
        self.cpu_action_holder = ActionHolder(
            plugin_base = self,
            action_base = CpuMonitorAction,
            action_id = "com_rusty_CpuMonitorPlugin::CpuMonitorAction",
            action_name = "CPU Monitor",
        )
        self.add_action_holder(self.cpu_action_holder)

        # Register plugin
        self.register(
            plugin_name = "CPU Monitor",
            github_repo = "https://github.com/RustyTheDalek/CPU-Monitor-Plugin---Stream-Controller",
            plugin_version = "1.0.0",
            app_version = "1.1.1-alpha"
        )
