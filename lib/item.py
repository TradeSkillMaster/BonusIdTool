class Item:
    _DROP_LEVEL_MODIFIER_TYPE = 9
    _CONTENT_TUNING_ID_MODIFIER_TYPE = 28

    def __init__(self, link: str, base_item_level: int, has_midnight_scaling: bool = False):
        self.item_level = base_item_level
        self.has_midnight_scaling = has_midnight_scaling

        parts = link.split(':')
        self.item_id = int(parts[2])
        item_string_parts = [p for p in parts[14:] if not p.startswith("|h")]
        num_bonus_ids = int(item_string_parts.pop(0)) if len(item_string_parts) > 0 and item_string_parts[0] else 0
        self.bonus_ids = [int(item_string_parts.pop(0)) for _ in range(num_bonus_ids)]

        num_modifiers = int(item_string_parts.pop(0)) if len(item_string_parts) > 0 and item_string_parts[0] else 0
        self.modifier_player_level = 0
        self.modifier_content_tuning_id = 0
        for _ in range(num_modifiers):
            modifier_type = int(item_string_parts.pop(0))
            modifier_value = int(item_string_parts.pop(0))
            if modifier_type == self._DROP_LEVEL_MODIFIER_TYPE:
                self.modifier_player_level = modifier_value
            elif modifier_type == self._CONTENT_TUNING_ID_MODIFIER_TYPE:
                self.modifier_content_tuning_id = modifier_value

    @classmethod
    def from_info(cls, item_id: int, bonus_ids: list[int], base_item_level: int, has_midnight_scaling: bool = False, player_level: int = 0, content_tuning_id: int = 0) -> 'Item':
        item = object.__new__(cls)
        item.item_id = item_id
        item.item_level = base_item_level
        item.has_midnight_scaling = has_midnight_scaling
        item.bonus_ids = bonus_ids
        item.modifier_player_level = player_level
        item.modifier_content_tuning_id = content_tuning_id
        return item
