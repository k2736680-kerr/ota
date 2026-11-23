"""三条链路。差异只放在各自文件和 yaml。"""

from server.links.glass_link import GlassLink
from server.links.ring_link import RingLink
from server.links.watch_link import WatchLink

_LINKS = {"glass": GlassLink, "ring": RingLink, "watch": WatchLink}


def build_link(link_id: str, link: dict, adb, ui, logger, sleeper=None):
    cls = _LINKS.get(link_id)
    if cls is None:
        raise KeyError(f"未知链路: {link_id}")
    return cls(link, adb, ui, logger, sleeper)
