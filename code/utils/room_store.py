import os
import time
from utils import jsonstore
from utils.settings import SETTINGS_DIR

ROOMS_FILE = os.path.join(SETTINGS_DIR, "rooms.json")


class RoomStore:
    def __init__(self):
        self._rooms = {}
        self.load()

    def load(self):
        loaded = jsonstore.load_json(ROOMS_FILE, default=[], expect_type=list)
        for r in loaded:
            if isinstance(r, dict):
                rid = r.get("room_id", "")
                if rid:
                    self._rooms[rid] = r

    def save(self) -> bool:
        return jsonstore.save_json(ROOMS_FILE, list(self._rooms.values()))

    def add(self, room: dict):
        rid = room.get("room_id", "")
        if rid:
            room = dict(room)
            room["saved_at"] = room.get("saved_at", time.time())
            self._rooms[rid] = room
            self.save()

    def remove(self, room_id: str):
        if room_id in self._rooms:
            del self._rooms[room_id]
            self.save()

    def get(self, room_id: str):
        return self._rooms.get(room_id)

    def get_all(self) -> list:
        return list(self._rooms.values())

    def exists(self, room_id: str) -> bool:
        return room_id in self._rooms


room_store = RoomStore()
