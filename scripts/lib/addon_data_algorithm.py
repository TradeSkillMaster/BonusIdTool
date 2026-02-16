import json
import os
from math import floor, inf

from lib.algorithm import Algorithm
from lib.item import Item


class AddonDataAlgorithm(Algorithm):
    _OP_GROUP = {'scale': 'S', 'set': 'S', 'add': 'Q'}

    def __init__(self, build: str):
        addon_data_path = os.path.join('.cache', build, 'addon_data.json')
        with open(addon_data_path, 'r') as f:
            data = json.load(f)

        self._bonuses = data['bonuses']
        self._curves = data['curves']
        self._squish_curve_index = data['squish_curve']
        self._squish_max = data['squish_max']
        self._content_tuning = data['content_tuning']
        self._item_levels = data.get('item_levels', {})
        self._midnight_items = set(data.get('midnight_items', []))
        # Expand CT remap: add entries pointing non-canonical IDs to canonical data
        for src, dst in data.get('content_tuning_remap', {}).items():
            dst_str = str(dst)
            if dst_str in self._content_tuning:
                self._content_tuning[str(src)] = self._content_tuning[dst_str]

    def _get_item_info(self, item_id: int) -> tuple[int, bool]:
        base_level = self._item_levels.get(str(item_id), 0)
        has_midnight = item_id in self._midnight_items
        return base_level, has_midnight

    def process_item(self, link: str) -> int:
        base_item_level, has_midnight_scaling = self._get_item_info(self.get_item_id_from_link(link))
        item = Item(link, base_item_level, has_midnight_scaling)

        bonuses: list[dict] = []
        bonus_ids = self._get_bonus_ids(item)

        def collect_bonus(bonus: dict):
            if bonus.get('midnight') == 'set':
                item.has_midnight_scaling = True

            group = self._OP_GROUP.get(bonus['op'])
            if group is None:
                bonuses.append(bonus)
                return
            prev_index = next((i for i, b in enumerate(bonuses) if self._OP_GROUP.get(b['op']) == group), None)
            if prev_index is None:
                bonuses.append(bonus)
            else:
                prev_prio = bonuses[prev_index].get('priority')
                new_prio = bonus.get('priority')
                if prev_prio is not None and new_prio is not None:
                    if new_prio <= prev_prio:
                        bonuses[prev_index] = bonus
                else:
                    bonuses[prev_index] = bonus

        def collect_all(bonus_id):
            data = self._bonuses.get(str(bonus_id))
            if not data or 'redirect' in data:
                return
            collect_bonus(data)

        # Collect indirect entries first, then direct (direct overrides via dedup)
        for bonus_id in bonus_ids:
            data = self._bonuses.get(str(bonus_id))
            if data and data.get('indirect'):
                collect_all(bonus_id)
        for bonus_id in bonus_ids:
            data = self._bonuses.get(str(bonus_id))
            if data and not data.get('indirect'):
                collect_all(bonus_id)

        if not bonuses:
            if not item.has_midnight_scaling:
                item.item_level = self._get_squish_value(item.item_level)
            return item.item_level

        for bonus in bonuses:
            op = bonus['op']
            if op == 'legacy_add':
                item.item_level += bonus['amount']
            elif op == 'add':
                if bonus.get('midnight') == 'force' and not item.has_midnight_scaling:
                    item.has_midnight_scaling = True
                    item.item_level = self._get_squish_value(item.item_level)
                item.item_level += bonus['amount']
            elif op == 'set':
                item.item_level = bonus['item_level']
            elif op == 'scale':
                drop_level = bonus.get('default_level') or item.modifier_player_level or 80
                if 'content_tuning_key' in bonus:
                    ct = item.modifier_content_tuning_id or bonus.get('content_tuning_id')
                    if ct and (not bonus.get('content_tuning_default_only') or not item.modifier_player_level):
                        drop_level = self._apply_content_tuning(drop_level, ct, bonus['content_tuning_key'])
                item.item_level = self._get_curve_value(bonus['curve_id'], drop_level) + bonus.get('offset', 0)

            if 'extra_amount' in bonus:
                item.item_level += bonus['extra_amount']

            # Unified post-op midnight handling
            midnight = bonus.get('midnight')
            if midnight == 'set':
                item.has_midnight_scaling = True
            elif midnight == 'squish' and item.has_midnight_scaling:
                item.item_level = self._get_squish_value(item.item_level)

        item.item_level = max(item.item_level, 1)
        if not item.has_midnight_scaling:
            item.item_level = self._get_squish_value(item.item_level)
        return item.item_level

    def _get_bonus_ids(self, item: Item) -> list[int]:
        bonus_ids = []
        for id in item.bonus_ids:
            data = self._bonuses.get(str(id))
            if data and 'redirect' in data:
                bonus_ids.append(data['redirect'])
            else:
                bonus_ids.append(id)
        bonus_ids.sort(key=lambda id: (self._bonuses.get(str(id), {}).get('sort_priority', 0), id))
        return bonus_ids

    def _get_curve_value(self, curve_id: int, value: float) -> int:
        points = self._curves[curve_id]
        return self._interpolate(points, value)

    def _get_squish_value(self, value: float) -> int:
        if value > self._squish_max:
            return 1
        return self._get_curve_value(self._squish_curve_index, value)

    @staticmethod
    def _interpolate(curve: dict, value: float) -> int:
        lower_x, lower_y = -inf, 0
        upper_x, upper_y = inf, 0
        for k, v in curve.items():
            level = float(k)
            if level == value:
                return int(floor(v + 0.5))
            elif level < value:
                if level > lower_x:
                    lower_x, lower_y = level, v
            else:
                if level < upper_x:
                    upper_x, upper_y = level, v
        if lower_x == -inf:
            return int(floor(upper_y + 0.5))
        if upper_x == inf:
            return int(floor(lower_y + 0.5))
        result = lower_y + (value - lower_x) / (upper_x - lower_x) * (upper_y - lower_y)
        return int(floor(result + 0.5))

    def _get_midnight_squish_curve_point_value(self, value: float) -> int:
        """Alias for test runner compatibility."""
        return self._get_squish_value(value)

    def _apply_content_tuning(self, drop_level: int, content_tuning_id: int, ct_key: str) -> int:
        ct = self._content_tuning.get(str(content_tuning_id))
        if not ct:
            return drop_level
        op = ct.get(ct_key, ct.get('op'))
        if not op:
            return drop_level
        name = op[0]
        if name == 'cap':
            return min(drop_level, op[1])
        elif name == 'clamp':
            return min(max(drop_level, op[1]), op[2])
        elif name == 'const':
            return op[1]
        elif name == 'cap_add':
            return min(drop_level, op[1]) + op[2]
        elif name == 'cap_add_floor':
            return max(min(drop_level, op[1]) + op[2], op[3])
        return drop_level
