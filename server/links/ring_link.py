"""戒指链路。"""

from server.common.version_util import ring_version_from_page
from server.links.base_link import BaseLink


class RingLink(BaseLink):
    link_id = "ring"

    def read_firmware(self) -> dict:
        """升级页「当前版本:…」文案优先，不绑 resourceId。"""
        reported = ring_version_from_page(self.ui.page_text(), pkg=self.link.get("pkg"))
        if reported:
            return {
                "reported": reported,
                "announced": "",
                "githash": self.ui.read_text(self.link["locators"].get("githash_field")),
            }
        return super().read_firmware()
