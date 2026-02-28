from functools import cmp_to_key
from math import floor

from lib.algorithm import Algorithm
from lib.dbc_file import CurveType, DBC, ItemBonus, ItemBonusType
from lib.item import Item


class DirectDBCAlgorithm(Algorithm):
    def __init__(self, dbc: DBC):
        self._dbc = dbc

    def _get_item_info(self, item_id: int) -> tuple[int, bool]:
        return self._dbc.item_sparse.get_info(item_id)

    def _process(self, item: Item) -> int:
        bonuses: list[ItemBonus] = []
        bonus_ids = list(self._get_bonus_ids(item))

        def collect_bonus(bonus: ItemBonus):
            self._apply_eager_midnight_scaling(item, bonus)
            if bonus.bonus_type == ItemBonusType.INCREASE_ITEM_LEVEL:
                bonuses.append(bonus)
                return
            prev_index = next((i for i, b in enumerate(bonuses) if b.bonus_type.bonus_group == bonus.bonus_type.bonus_group), None)
            if prev_index is None:
                bonuses.append(bonus)
            else:
                prev_prio = bonuses[prev_index].dbc_priority
                new_prio = bonus.dbc_priority
                if prev_prio is not None and new_prio is not None:
                    if new_prio <= prev_prio:
                        bonuses[prev_index] = bonus
                else:
                    bonuses[prev_index] = bonus

        # Indirect bonuses first (APPLY_BONUS targets) — direct bonuses override these
        for bonus_id in bonus_ids:
            for bonus in self._dbc.item_bonus.get(bonus_id):
                if bonus.bonus_type == ItemBonusType.APPLY_BONUS:
                    for indirect in self._dbc.item_bonus.get(bonus.Value_0):
                        if indirect.bonus_type != ItemBonusType.APPLY_BONUS:
                            collect_bonus(indirect)
        # Direct bonuses (override indirect via last-wins dedup)
        for bonus_id in bonus_ids:
            for bonus in self._dbc.item_bonus.get(bonus_id):
                if bonus.bonus_type != ItemBonusType.APPLY_BONUS:
                    collect_bonus(bonus)
        if not bonuses:
            if not item.has_midnight_scaling:
                item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
            return item.item_level

        for bonus in bonuses:
            if bonus.bonus_type == ItemBonusType.INCREASE_ITEM_LEVEL:
                if not bonus.Value_0:
                    continue
                item.item_level = item.item_level + bonus.Value_0
            elif bonus.bonus_type == ItemBonusType.SCALING_CONFIG:
                if not self._dbc.item_scaling_config.has(bonus.Value_0):
                    continue
                scaling_entry = self._dbc.item_scaling_config.get(bonus.Value_0)
                if not self._dbc.item_offset_curve.has(scaling_entry.ItemOffsetCurveID):
                    continue
                offset_entry = self._dbc.item_offset_curve.get(scaling_entry.ItemOffsetCurveID)
                scaled_item_level = scaling_entry.ItemLevel if scaling_entry.ItemLevel else (item.modifier_player_level or 80)
                if not scaling_entry.ItemLevel and item.modifier_content_tuning_id and scaling_entry.ItemSquishEraID == 2:
                    scaled_item_level = self._apply_content_tuning(scaled_item_level, item.modifier_content_tuning_id, bonus_type=bonus.bonus_type)
                item.item_level = self._get_curve_point_value(offset_entry.CurveID, scaled_item_level) + offset_entry.Offset
                if scaling_entry.ItemSquishEraID != 1 or scaling_entry.Flags & 1:
                    item.has_midnight_scaling = True
                elif item.has_midnight_scaling:
                    item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
            elif bonus.bonus_type == ItemBonusType.SCALING_CONFIG_2:
                if not self._dbc.item_scaling_config.has(bonus.Value_0):
                    continue
                scaling_entry = self._dbc.item_scaling_config.get(bonus.Value_0)
                if not self._dbc.item_offset_curve.has(scaling_entry.ItemOffsetCurveID):
                    continue
                offset_entry = self._dbc.item_offset_curve.get(scaling_entry.ItemOffsetCurveID)
                drop_level = item.modifier_player_level or 80
                if item.modifier_content_tuning_id and not item.modifier_player_level:
                    drop_level = self._apply_content_tuning(drop_level, item.modifier_content_tuning_id, bonus.bonus_type)
                item.item_level = self._get_curve_point_value(offset_entry.CurveID, drop_level) + offset_entry.Offset
                if scaling_entry.ItemSquishEraID != 1:
                    item.has_midnight_scaling = True
                elif item.has_midnight_scaling:
                    item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
            elif bonus.bonus_type == ItemBonusType.OFFSET_CURVE:
                curve_id = bonus.Value_0
                item.item_level = self._get_curve_point_value(bonus.Value_0, bonus.Value_1)
                if bonus.Value_2 != 1:
                    item.has_midnight_scaling = True
                elif item.has_midnight_scaling:
                    item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
            elif bonus.bonus_type == ItemBonusType.MIDNIGHT_ITEM_LEVEL:
                item.item_level += bonus.Value_0
            elif bonus.bonus_type == ItemBonusType.BASE_ITEM_LEVEL:
                item.item_level = bonus.Value_0
            elif bonus.bonus_type == ItemBonusType.CRAFTING_QUALITY:
                if not item.has_midnight_scaling:
                    item.has_midnight_scaling = True
                    item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
                item.item_level += bonus.Value_2 if bonus.Value_1 == 1 else bonus.Value_0
            elif bonus.bonus_type == ItemBonusType.STAT_SCALING:
                curve_id = bonus.Value_3
                if not curve_id or not self._dbc.curve_point.get(curve_id):
                    continue
                content_tuning_id = bonus.Value_2
                drop_level = item.modifier_player_level or 80
                if item.modifier_content_tuning_id:
                    drop_level = self._apply_content_tuning(drop_level, item.modifier_content_tuning_id, bonus.bonus_type)
                elif content_tuning_id:
                    drop_level = self._apply_content_tuning(drop_level, content_tuning_id, bonus.bonus_type)
                item.item_level = self._get_curve_point_value(curve_id, drop_level)
            elif bonus.bonus_type == ItemBonusType.STAT_FIXED:
                curve_id = bonus.Value_3
                if not curve_id or not self._dbc.curve_point.get(curve_id):
                    if curve_id and not self._dbc.curve_point.get(curve_id):
                        # Curve exists but has no data points — game produces item level 1
                        item.item_level = 1
                    continue
                content_tuning_id = bonus.Value_2
                drop_level = item.modifier_player_level or 80
                if item.modifier_content_tuning_id:
                    drop_level = self._apply_content_tuning(drop_level, item.modifier_content_tuning_id, bonus.bonus_type)
                elif content_tuning_id:
                    drop_level = self._apply_content_tuning(drop_level, content_tuning_id, bonus.bonus_type)
                item.item_level = self._get_curve_point_value(curve_id, drop_level)
        item.item_level = max(item.item_level, 1)
        if not item.has_midnight_scaling:
            item.item_level = self._get_midnight_squish_curve_point_value(item.item_level)
        return item.item_level

    _TREE_BONUS_ID = 3524

    def _get_bonus_ids(self, item: Item) -> list[int]:
        # Initial pass to handle simple APPLY_BONUS types before sorting
        bonus_ids = []
        for id in item.bonus_ids:
            if id == self._TREE_BONUS_ID:
                tree_ids = self._dbc.get_tree_bonus_ids(item.item_id)
                if tree_ids:
                    bonus_ids.extend(tree_ids)
                    continue
            bonuses = self._dbc.item_bonus.get(id)
            if len(bonuses) == 1 and bonuses[0].bonus_type == ItemBonusType.APPLY_BONUS:
                # The game only follows at most one level of redirection with bonuses
                if not any(b.bonus_type == ItemBonusType.APPLY_BONUS for b in self._dbc.item_bonus.get(bonuses[0].Value_0)):
                    bonus_ids.append(bonuses[0].Value_0)
            else:
                bonus_ids.append(id)
        def compare_bonus_ids(a, b):
            a_priority = max((entry.Type.sort_priority for entry in self._dbc.item_bonus.get(a)), default=0)
            b_priority = max((entry.Type.sort_priority for entry in self._dbc.item_bonus.get(b)), default=0)
            if a_priority != b_priority:
                return a_priority - b_priority
            return a - b
        bonus_ids.sort(key=cmp_to_key(compare_bonus_ids))
        return bonus_ids

    def _apply_eager_midnight_scaling(self, item: Item, bonus: ItemBonus):
        if bonus.bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
            if not self._dbc.item_scaling_config.has(bonus.Value_0):
                return
            sc = self._dbc.item_scaling_config.get(bonus.Value_0)
            if bonus.bonus_type == ItemBonusType.SCALING_CONFIG:
                if sc.ItemSquishEraID != 1 or sc.Flags & 1:
                    item.has_midnight_scaling = True
            else:
                if sc.ItemSquishEraID != 1:
                    item.has_midnight_scaling = True
        elif bonus.bonus_type == ItemBonusType.OFFSET_CURVE:
            if bonus.Value_2 != 1:
                item.has_midnight_scaling = True

    def _get_midnight_squish_curve_point_value(self, value: float) -> int:
        squish_entry = self._dbc.item_squish_era.get_midnight()
        _, upper_bound = self._dbc.curve_point.find_points(squish_entry.CurveID, value)
        if upper_bound.Pos_0 < value:
            # If we're above the top of the curve, the game just defaults to 1
            return 1
        return self._get_curve_point_value(squish_entry.CurveID, value)

    def _get_curve_point_value(self, curve_id: int, value: float) -> int:
        curve = self._dbc.curve.get(curve_id)
        curve_type = CurveType(curve.Type)
        if curve_type in (CurveType.LINEAR, CurveType.REQ_LEVEL_AND_ITEM_LEVEL):
            pass
        else:
            raise Exception(f"Unknown curve type: {curve_type}")
        lower_bound, upper_bound = self._dbc.curve_point.find_points(curve_id, value)
        if lower_bound.Pos_0 >= value:
            result = lower_bound.Pos_1
        elif upper_bound.Pos_0 < value:
            result = upper_bound.Pos_1
        else:
            assert upper_bound.Pos_0 != value
            # Linear interpolation
            slope = (upper_bound.Pos_1 - lower_bound.Pos_1) / (upper_bound.Pos_0 - lower_bound.Pos_0)
            result = lower_bound.Pos_1 + slope * (value - lower_bound.Pos_0)
        return int(floor(result + 0.5))

    def _get_drop_level(self, item: Item, with_content_tuning: bool = False) -> int:
        player_level = item.modifier_player_level
        if not player_level:
            return 0
        if with_content_tuning and item.modifier_content_tuning_id:
            player_level = self._apply_content_tuning(player_level, item.modifier_content_tuning_id)
        return player_level

    def _resolve_content_tuning_id(self, content_tuning_id: int, bonus_type: ItemBonusType | None = None) -> int:
        for redirect in self._dbc.conditional_content_tuning.get(content_tuning_id):
            if redirect.RedirectEnum == 7:
                return redirect.RedirectContentTuningID
            if redirect.RedirectEnum == 14 and bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
                return redirect.RedirectContentTuningID
        return content_tuning_id

    def _apply_content_tuning(self, drop_level: int, content_tuning_id: int, bonus_type: ItemBonusType | None = None) -> int:
        content_tuning_id = self._resolve_content_tuning_id(content_tuning_id, bonus_type)
        if not self._dbc.content_tuning.has(content_tuning_id):
            return drop_level
        content_tuning = self._dbc.content_tuning.get(content_tuning_id)
        if content_tuning.MaxLevelScalingOffset == 3:
            prev_expansion_max = 70
            return min(drop_level, prev_expansion_max + content_tuning.MaxLevelSquish)
        elif content_tuning.MaxLevelScalingOffset == 2:
            base = min(drop_level, 80)
            if content_tuning.MaxLevelSquish == 0:
                if content_tuning.MinLevelSquish >= 80:
                    return max(base, content_tuning.MinLevelSquish)
                return base
            elif content_tuning.MaxLevelSquish < 0:
                result = base + content_tuning.MaxLevelSquish
                if content_tuning.MinLevelSquish > 80:
                    result = max(result, content_tuning.MinLevelSquish)
                return result
            else:
                # MaxLevelSquish > 0
                if content_tuning.MinLevelScalingOffset == 2:
                    return base + content_tuning.MaxLevelSquish
                return drop_level
        elif content_tuning.MaxLevelScalingOffset == 1:
            if bonus_type == ItemBonusType.SCALING_CONFIG:
                return min(max(drop_level, content_tuning.MinLevelSquish), content_tuning.MaxLevelSquish)
            return min(drop_level, content_tuning.MaxLevelSquish + 1)
        if bonus_type in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
            if content_tuning.MaxLevelSquish <= 0 and (content_tuning.MinLevelSquish <= 0 or content_tuning.Flags & 0x4):
                return drop_level
            elif content_tuning.MinLevelSquish > content_tuning.MaxLevelSquish > 0:
                return content_tuning.MinLevelSquish
        elif bonus_type == ItemBonusType.SCALING_CONFIG:
            if content_tuning.Flags & 0x4:
                return drop_level
            if content_tuning.MaxLevelSquish > 0:
                min_level = content_tuning.MinLevelSquish + content_tuning.AllowedMinOffset
                return min(max(drop_level, min_level), content_tuning.MaxLevelSquish)
            elif content_tuning.MinLevelSquish > 0:
                return min(drop_level, 80)
            else:
                return drop_level
        else:
            # For other bonus types (like SCALING_CONFIG_2)
            if content_tuning.HPScalingCurveID in {77585}:
                return drop_level
            elif content_tuning.MaxLevelSquish == 0:
                return min(drop_level, 80)
            elif content_tuning.MaxLevelSquish < 0:
                return drop_level
        min_level = content_tuning.MinLevelSquish + content_tuning.AllowedMinOffset
        return min(max(drop_level, min_level), content_tuning.MaxLevelSquish)
