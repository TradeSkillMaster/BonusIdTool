from abc import ABC, abstractmethod
import re

class Algorithm(ABC):
    @staticmethod
    def get_item_id_from_link(link: str) -> int:
        match = re.search(r"item:([0-9]+):", link)
        assert match
        return int(match.group(1))

    @abstractmethod
    def process_item(self, link: str) -> int:
        pass
