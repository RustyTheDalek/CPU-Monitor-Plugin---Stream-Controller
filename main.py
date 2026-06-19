# Import StreamController modules
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder

# Import actions
from .actions.NvtopAction.NvtopAction import NvtopAction

class NvtopPlugin(PluginBase):
    def __init__(self):
        super().__init__()

        ## Register actions
        self.nvtop_action_holder = ActionHolder(
            plugin_base = self,
            action_base = NvtopAction,
            action_id = "com_jay_NvtopPlugin::NvtopAction",
            action_name = "GPU Monitor",
        )
        self.add_action_holder(self.nvtop_action_holder)

        # Register plugin
        self.register(
            plugin_name = "GPU Monitor (nvtop)",
            github_repo = "https://github.com/StreamController/PluginTemplate",
            plugin_version = "1.0.1",
            app_version = "1.1.1-alpha"
        )