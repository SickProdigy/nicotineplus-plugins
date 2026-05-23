from pynicotine.pluginsystem import BasePlugin


class Plugin(BasePlugin):
	name = "Example Plugin"
	description = "A minimal Nicotine+ plugin example. Logs when connected."
	version = "1.0"

	def loaded_notification(self):
		self.log("Example Plugin loaded.")
