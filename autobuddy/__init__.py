from pynicotine.pluginsystem import BasePlugin

TARGET_ROOM = "SickGaming.net"
TRIGGER_PHRASE = "add me plz"


class Plugin(BasePlugin):
	name = "Auto Buddy"
	description = "Automatically adds users who ask to be added in SickGaming.net"
	version = "1.0"

	def server_connect_notification(self):
		"""Join target room after connecting to Soulseek server."""
		self.log("Connected. Joining room: %s", TARGET_ROOM)
		self.core.chatrooms.show_room(TARGET_ROOM, switch_page=False)

	def incoming_public_chat_notification(self, room, user, line):
		"""Triggered whenever someone talks in a room."""
		if room != TARGET_ROOM:
			return

		if user == self.core.users.login_username:
			return

		if TRIGGER_PHRASE in line.lower():
			self.log("%s asked to be added. Adding to buddy list.", user)
			self.core.buddies.add_buddy(user)
			self.send_public(room, f"{user}: added.")
