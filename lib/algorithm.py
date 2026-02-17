from abc import ABC, abstractmethod
import re

from lib.item import Item


class Algorithm(ABC):
    @staticmethod
    def get_item_id_from_link(link: str) -> int:
        match = re.search(r"item:([0-9]+):", link)
        assert match
        return int(match.group(1))

    def process_item(self, link: str) -> int:
        item_id = self.get_item_id_from_link(link)
        base_item_level, has_midnight_scaling = self._get_item_info(item_id)
        item = Item(link, base_item_level, has_midnight_scaling)
        return self._process(item)

    def process_item_info(self, item_id: int, bonus_ids: list[int], player_level: int = 0, content_tuning_id: int = 0) -> int:
        base_item_level, has_midnight_scaling = self._get_item_info(item_id)
        item = Item.from_info(bonus_ids, base_item_level, has_midnight_scaling, player_level, content_tuning_id)
        return self._process(item)

    @abstractmethod
    def _get_item_info(self, item_id: int) -> tuple[int, bool]:
        pass

    @abstractmethod
    def _process(self, item: Item) -> int:
        pass
