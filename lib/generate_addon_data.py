import json
import logging
import os
from collections import Counter
from math import floor
from typing import Any

from lib.dbc_file import CurveType, DBC, ItemBonus, ItemBonusType
from lib.lua_writer import write_lua, write_lua_cbor


class AddonDataGenerator:
    _OP_GROUP: dict[str, str] = {'scale': 'S', 'set': 'S', 'add': 'Q'}

    _CLEAN_SIDE_EFFECTS = {
        ItemBonusType.NONE, ItemBonusType.GROUP_ID, ItemBonusType.RECEIVE_TOAST,
        ItemBonusType.REQ_LEVEL_CURVE, ItemBonusType.SET_APPEARANCE_MODIFIER,
        ItemBonusType.MULTIPLY_REPAIR_COST, ItemBonusType.DISENCHANT_LOOT,
        ItemBonusType.CAN_DISENCHANT, ItemBonusType.SET_BIND_TYPE,
        ItemBonusType.PVP_ITEM_LEVEL, ItemBonusType.BONDING_WITH_PRIORITY,
        ItemBonusType.INCREASE_BONUS_STAT, ItemBonusType.ITEM_EFFECT_ID,
        ItemBonusType.ITEM_LIMIT_CATEGORY_ID, ItemBonusType.SCRAPPING_LOOT_ID,
    }
    _FALLBACK_SIDE_EFFECTS = _CLEAN_SIDE_EFFECTS | {ItemBonusType.SET_ITEM_QUALITY}

    _ILEVEL_TYPES = {
        ItemBonusType.INCREASE_ITEM_LEVEL, ItemBonusType.SCALING_CONFIG,
        ItemBonusType.SCALING_CONFIG_2, ItemBonusType.OFFSET_CURVE,
        ItemBonusType.MIDNIGHT_ITEM_LEVEL, ItemBonusType.BASE_ITEM_LEVEL,
        ItemBonusType.CRAFTING_QUALITY, ItemBonusType.STAT_SCALING,
        ItemBonusType.STAT_FIXED, ItemBonusType.APPLY_BONUS,
    }

    def __init__(self, dbc: DBC):
        self._dbc = dbc

    def generate(self) -> dict[str, Any]:
        """Generate addon data from DBC files.

        Returns the addon data dict ready for serialization or further processing.
        """
        bonuses = self._export_bonuses()
        referenced_curve_ids = self._collect_referenced_curves()
        curves = self._export_curves(referenced_curve_ids)
        self._simplify_constant_curves(bonuses, curves)
        self._prune_unreferenced_curves(bonuses, curves)
        curves_list, curve_index_map = self._dedup_curves(curves)
        self._remap_bonus_curve_ids(bonuses, curve_index_map)
        squish_curve_index = curve_index_map[str(self._squish_curve_id)]
        logging.info("Curves: %d unique curves (%d total points)",
                     len(curves_list), sum(len(v) for v in curves_list))

        content_tuning = self._build_content_tuning()
        ct_remap_int = self._dedup_content_tuning(bonuses, content_tuning)

        item_levels, midnight_items = self._build_item_data()
        logging.info("Item data: %d items, %d midnight items",
                     len(item_levels), len(midnight_items))

        squish_max = int(max(float(k) for k in curves_list[squish_curve_index]))
        level_to_bonus_string = self._build_bonus_string_table(
            bonuses, curves_list, squish_curve_index, squish_max)

        addon_data: dict[str, Any] = {
            "build": self._dbc._build,
            "squish_curve": squish_curve_index,
            "squish_max": squish_max,
            "bonuses": bonuses,
            "curves": curves_list,
            "content_tuning": content_tuning,
            "item_levels": item_levels,
            "midnight_items": midnight_items,
            "level_to_bonus_string": level_to_bonus_string,
        }
        if ct_remap_int:
            addon_data["content_tuning_remap"] = ct_remap_int

        logging.info("Bonuses: %d bonus list IDs", len(bonuses))
        logging.info("Curves: %d curves (%d total points)",
                     len(curves_list), sum(len(v) for v in curves_list))
        logging.info("Squish curve: index %d", squish_curve_index)
        logging.info("Content tuning: %d entries", len(content_tuning))
        logging.info("Item data: %d items, %d midnight items", len(item_levels), len(midnight_items))

        return addon_data

    # -- Bonus export --

    def _export_bonuses(self) -> dict[str, Any]:
        bonuses: dict[str, Any] = {}
        for parent_id, entries in self._dbc.item_bonus._entries.items():
            sp = max((self._sort_priority(e.bonus_type) for e in entries), default=0)
            exported_entries = [self._export_bonus(e) for e in entries]

            # Check for simple APPLY_BONUS redirect
            if len(entries) == 1 and entries[0].bonus_type == ItemBonusType.APPLY_BONUS:
                target_id = entries[0].Value_0
                target_entries = self._dbc.item_bonus.get(target_id)
                if not any(e.bonus_type == ItemBonusType.APPLY_BONUS for e in target_entries):
                    redirect: dict[str, Any] = {'redirect': target_id}
                    if sp:
                        redirect['sort_priority'] = sp
                    bonuses[str(parent_id)] = redirect
                    continue

            # Pre-resolve APPLY_BONUS entries into indirect list
            indirect = []
            direct = []
            for bonus in exported_entries:
                if bonus is None:
                    continue
                if bonus['op'] == 'apply':
                    target_id = bonus['target']
                    for target_entry in self._dbc.item_bonus.get(target_id):
                        if target_entry.bonus_type != ItemBonusType.APPLY_BONUS:
                            exported = self._export_bonus(target_entry)
                            if exported is not None:
                                indirect.append(exported)
                else:
                    direct.append(bonus)

            direct = self._dedup_entries(direct)
            indirect = self._dedup_entries(indirect)

            # Flatten to a single operation per bonus ID
            ops = indirect or direct
            if not ops:
                continue
            data = dict(ops[0])
            if sp:
                data['sort_priority'] = sp
            if indirect:
                data['indirect'] = True
            if len(ops) > 1:
                other = ops[1]
                assert other['op'] == 'add', f"Unexpected other op: {other['op']} for bonus {parent_id}"
                data['extra_amount'] = other['amount']
            bonuses[str(parent_id)] = data

        return bonuses

    def _export_bonus(self, entry: ItemBonus) -> dict[str, Any] | None:
        """Convert a DBC ItemBonus entry to an operation dict.
        Returns None for entries that have no effect on item level calculation."""
        dbc = self._dbc
        bt = entry.bonus_type

        if bt == ItemBonusType.INCREASE_ITEM_LEVEL:
            if not entry.Value_0:
                return None
            return {'op': 'legacy_add', 'amount': entry.Value_0}

        elif bt in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
            if not dbc.item_scaling_config.has(entry.Value_0):
                return None
            sc = dbc.item_scaling_config.get(entry.Value_0)
            if not dbc.item_offset_curve.has(sc.ItemOffsetCurveID):
                return None
            oc = dbc.item_offset_curve.get(sc.ItemOffsetCurveID)

            if bt == ItemBonusType.SCALING_CONFIG:
                sets_midnight = sc.ItemSquishEraID != 1 or bool(sc.Flags & 1)
            else:
                sets_midnight = sc.ItemSquishEraID != 1

            result: dict[str, Any] = {
                'op': 'scale',
                'curve_id': oc.CurveID,
                'offset': oc.Offset,
                'midnight': 'set' if sets_midnight else 'squish',
            }
            if entry.Value_1:
                result['priority'] = entry.Value_1
            if bt == ItemBonusType.SCALING_CONFIG:
                if sc.ItemLevel:
                    result['default_level'] = sc.ItemLevel
                if sc.ItemSquishEraID == 2:
                    result['content_tuning_key'] = 'scaling_config'
            else:
                result['content_tuning_key'] = 'scaling_config2'
                result['content_tuning_default_only'] = True
            return result

        elif bt == ItemBonusType.OFFSET_CURVE:
            curve_id = entry.Value_0
            input_value = entry.Value_1
            if curve_id and dbc.curve_point.get(curve_id):
                item_level = self._get_curve_point_value(curve_id, input_value)
            else:
                item_level = 0
            sets_midnight = entry.Value_2 != 1
            return {
                'op': 'set',
                'item_level': item_level,
                'midnight': 'set' if sets_midnight else 'squish',
            }

        elif bt == ItemBonusType.MIDNIGHT_ITEM_LEVEL:
            return {'op': 'add', 'amount': entry.Value_0}

        elif bt == ItemBonusType.BASE_ITEM_LEVEL:
            return {'op': 'set', 'item_level': entry.Value_0}

        elif bt == ItemBonusType.CRAFTING_QUALITY:
            amount = entry.Value_2 if entry.Value_1 == 1 else entry.Value_0
            return {'op': 'add', 'amount': amount, 'midnight': 'force'}

        elif bt in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
            curve_id = entry.Value_3
            if not curve_id:
                return None
            elif not dbc.curve_point.get(curve_id):
                if bt == ItemBonusType.STAT_FIXED:
                    return {'op': 'set', 'item_level': 1}
                return None
            result = {
                'op': 'scale',
                'curve_id': curve_id,
                'content_tuning_key': 'stat',
            }
            if entry.Value_2:
                result['content_tuning_id'] = entry.Value_2
            return result

        elif bt == ItemBonusType.APPLY_BONUS:
            return {'op': 'apply', 'target': entry.Value_0}

        else:
            return None

    # -- Curves --

    def _collect_referenced_curves(self) -> set[int]:
        """Collect all curve IDs referenced by bonus entries and the squish curve."""
        dbc = self._dbc
        referenced = set()

        for _, entries in dbc.item_bonus._entries.items():
            for entry in entries:
                if entry.bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
                    if dbc.item_scaling_config.has(entry.Value_0):
                        sc = dbc.item_scaling_config.get(entry.Value_0)
                        if dbc.item_offset_curve.has(sc.ItemOffsetCurveID):
                            referenced.add(dbc.item_offset_curve.get(sc.ItemOffsetCurveID).CurveID)
                elif entry.bonus_type in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
                    if entry.Value_3 and dbc.curve_point.get(entry.Value_3):
                        referenced.add(entry.Value_3)

        squish_entry = dbc.item_squish_era.get_midnight()
        self._squish_curve_id = squish_entry.CurveID
        referenced.add(self._squish_curve_id)

        # Validate curve types
        for curve_id in referenced:
            curve = dbc.curve.get(curve_id)
            curve_type = CurveType(curve.Type)
            assert curve_type in (CurveType.LINEAR, CurveType.REQ_LEVEL_AND_ITEM_LEVEL), \
                f"Unexpected curve type {curve_type} for curve {curve_id}"

        return referenced

    def _export_curves(self, curve_ids: set[int]) -> dict[str, dict[str, int | float]]:
        curves = {}
        for curve_id in sorted(curve_ids):
            points = self._export_curve_points(curve_id)
            if points:
                curves[str(curve_id)] = points
        return curves

    def _simplify_constant_curves(self, bonuses: dict, curves: dict) -> None:
        """Replace scale ops that reference constant curves with set ops."""
        constant_curves = {cid: next(iter(pts.values()))
                           for cid, pts in curves.items()
                           if len(set(pts.values())) == 1}

        for bid, bonus in list(bonuses.items()):
            if 'redirect' in bonus or bonus.get('op') != 'scale':
                continue
            curve_key = str(bonus['curve_id'])
            if curve_key not in constant_curves:
                continue
            simplified: dict[str, Any] = {
                'op': 'set',
                'item_level': constant_curves[curve_key] + bonus.get('offset', 0),
            }
            for key in ('midnight', 'priority', 'sort_priority', 'indirect', 'extra_amount'):
                if key in bonus:
                    simplified[key] = bonus[key]
            bonuses[bid] = simplified

    def _prune_unreferenced_curves(self, bonuses: dict, curves: dict) -> None:
        """Remove curves no longer referenced by any bonus."""
        still_referenced = {str(self._squish_curve_id)}
        for bonus in bonuses.values():
            if bonus.get('op') == 'scale':
                still_referenced.add(str(bonus['curve_id']))
        for key in list(curves):
            if key not in still_referenced:
                del curves[key]

    @staticmethod
    def _dedup_curves(curves: dict) -> tuple[list, dict[str, int]]:
        """Deduplicate curves by value and convert to an indexed array.

        Returns (curves_list, curve_index_map) where curve_index_map maps
        original curve_id strings to array indices.
        """
        curve_index_map: dict[str, int] = {}
        curves_by_value: dict[str, int] = {}
        curves_list = []
        for cid in sorted(curves.keys(), key=int):
            key = json.dumps(curves[cid], sort_keys=True)
            if key in curves_by_value:
                curve_index_map[cid] = curves_by_value[key]
            else:
                idx = len(curves_list)
                curves_list.append(curves[cid])
                curves_by_value[key] = idx
                curve_index_map[cid] = idx
        return curves_list, curve_index_map

    @staticmethod
    def _remap_bonus_curve_ids(bonuses: dict, curve_index_map: dict[str, int]) -> None:
        """Replace raw curve_id references in bonuses with array indices."""
        for bonus in bonuses.values():
            if 'redirect' in bonus:
                continue
            if bonus.get('op') == 'scale':
                bonus['curve_id'] = curve_index_map[str(bonus['curve_id'])]

    # -- Content tuning --

    def _build_content_tuning(self) -> dict[str, Any]:
        """Pre-compute content tuning operations per bonus-type group."""
        content_tuning: dict[str, Any] = {}
        for ct_id, entry in self._dbc.content_tuning._entries.items():
            ct_data = self._compute_content_tuning_ops(entry)
            if ct_data:
                content_tuning[str(ct_id)] = ct_data

        self._apply_conditional_redirects(content_tuning)
        self._compact_content_tuning(content_tuning)
        return content_tuning

    @staticmethod
    def _compute_content_tuning_ops(entry) -> dict[str, Any] | None:
        max_so = entry.MaxLevelScalingOffset
        min_so = entry.MinLevelScalingOffset
        max_lvl = entry.MaxLevelSquish
        min_lvl = entry.MinLevelSquish
        has_flag_4 = bool(entry.Flags & 0x4)
        is_df = entry.HPScalingCurveID in {77585}
        min_with_offset = min_lvl + entry.AllowedMinOffset

        if max_so == 3:
            op = ['cap', 70 + max_lvl]
            ops = {'scaling_config': op, 'scaling_config2': op, 'stat': op}
        elif max_so == 2:
            if max_lvl == 0:
                op = ['const', min_lvl] if min_lvl >= 80 else ['cap', 80]
            elif max_lvl < 0:
                op = ['cap_add_floor', 80, max_lvl, min_lvl] if min_lvl > 80 else ['cap_add', 80, max_lvl]
            else:
                op = ['cap_add', 80, max_lvl] if min_so == 2 else None
            ops = {'scaling_config': op, 'scaling_config2': op, 'stat': op}
        elif max_so == 1:
            ops = {
                'scaling_config': ['clamp', min_lvl, max_lvl],
                'scaling_config2': ['cap', max_lvl + 1],
                'stat': ['cap', max_lvl + 1],
            }
        else:
            # stat group
            if max_lvl <= 0 and (min_lvl <= 0 or has_flag_4):
                stat_op = None
            elif min_lvl > max_lvl > 0:
                stat_op = ['const', min_lvl]
            else:
                stat_op = ['clamp', min_with_offset, max_lvl]
            # scaling_config group
            if has_flag_4:
                sc_op = None
            elif max_lvl > 0:
                sc_op = ['clamp', min_with_offset, max_lvl]
            elif min_lvl > 0:
                sc_op = ['cap', 80]
            else:
                sc_op = None
            # scaling_config2 group
            if is_df:
                sc2_op = None
            elif max_lvl == 0:
                sc2_op = ['cap', 80]
            elif max_lvl < 0:
                sc2_op = None
            else:
                sc2_op = ['clamp', min_with_offset, max_lvl]
            ops = {'scaling_config': sc_op, 'scaling_config2': sc2_op, 'stat': stat_op}

        # Normalize clamp(x,x) -> const(x)
        for k, v in ops.items():
            if v and v[0] == 'clamp' and v[1] == v[2]:
                ops[k] = ['const', v[1]]

        ct_data = {k: v for k, v in ops.items() if v is not None}
        return ct_data or None

    def _apply_conditional_redirects(self, content_tuning: dict) -> None:
        for parent_id, redirects in self._dbc.conditional_content_tuning._entries.items():
            for redirect in redirects:
                target_ops = content_tuning.get(str(redirect.RedirectContentTuningID), {})
                if redirect.RedirectEnum == 7:
                    keys = ('scaling_config', 'scaling_config2', 'stat')
                elif redirect.RedirectEnum == 14:
                    keys = ('scaling_config', 'scaling_config2')
                else:
                    continue
                parent_key = str(parent_id)
                parent_ops = content_tuning.get(parent_key, {})
                merged = dict(parent_ops)
                for k in keys:
                    if k in target_ops:
                        merged[k] = target_ops[k]
                    else:
                        merged.pop(k, None)
                if merged:
                    content_tuning[parent_key] = merged
                elif parent_key in content_tuning:
                    del content_tuning[parent_key]

    @staticmethod
    def _compact_content_tuning(content_tuning: dict) -> None:
        """Use 'op' as default when all three keys share a common value."""
        for ct_id, ct_data in list(content_tuning.items()):
            if set(ct_data.keys()) != {'scaling_config', 'scaling_config2', 'stat'}:
                continue
            values = list(ct_data.values())
            value_counts = Counter(json.dumps(v) for v in values)
            default_json, count = value_counts.most_common(1)[0]
            if count >= 2:
                default_val = json.loads(default_json)
                compact: dict[str, Any] = {'op': default_val}
                for k, v in ct_data.items():
                    if v != default_val:
                        compact[k] = v
                content_tuning[ct_id] = compact

    @staticmethod
    def _dedup_content_tuning(bonuses: dict, content_tuning: dict) -> dict[int, int]:
        """Deduplicate content tuning entries and remap references in bonuses.

        Returns the remap table (non-canonical -> canonical) as int keys/values.
        """
        ct_remap: dict[str, str] = {}
        ct_by_value: dict[str, str] = {}
        for ct_id in sorted(content_tuning.keys(), key=int):
            key = json.dumps(content_tuning[ct_id], sort_keys=True)
            if key in ct_by_value:
                ct_remap[ct_id] = ct_by_value[key]
            else:
                ct_by_value[key] = ct_id

        if not ct_remap:
            return {}

        # Remap ct_id references in bonuses
        for bonus in bonuses.values():
            if 'redirect' in bonus:
                continue
            for b in [bonus] + ([bonus['other']] if isinstance(bonus.get('other'), dict) else []):
                if 'content_tuning_id' in b:
                    ct_str = str(b['content_tuning_id'])
                    if ct_str in ct_remap:
                        b['content_tuning_id'] = int(ct_remap[ct_str])

        # Remove non-canonical entries
        for ct_id in ct_remap:
            del content_tuning[ct_id]

        ct_remap_int = {int(k): int(v) for k, v in ct_remap.items()}
        logging.info("CT dedup: removed %d duplicate entries (%d remaining), remap table: %d entries",
                     len(ct_remap), len(content_tuning), len(ct_remap_int))
        return ct_remap_int

    # -- Item data --

    def _build_item_data(self) -> tuple[dict[str, int], list[int]]:
        """Build per-item base level and midnight scaling data from ItemSparse."""
        item_levels = {}
        midnight_items = []
        for e in self._dbc.item_sparse._entries.values():
            if e.Stackable > 1:
                continue
            item_levels[str(e.ID)] = e.ItemLevel
            if e.ItemSquishEraID == 2:
                midnight_items.append(e.ID)
        midnight_items.sort()
        return item_levels, midnight_items

    # -- Bonus string table --

    def _build_bonus_string_table(self, bonuses: dict, curves: list,
                                  squish_curve_index: int, squish_max: int) -> dict[int, str]:
        """Precompute bonus string lookup table for GetBonusStringForLevel."""
        squish_curve_points = curves[squish_curve_index]

        def get_squish_value(value):
            if value > squish_max:
                return 1
            return self._interpolate_curve(squish_curve_points, value)

        def simulate_combo(set_bonus, add_bonus, target_level):
            """Returns True if the combo produces target_level for both midnight and non-midnight items."""
            for has_midnight in (False, True):
                il = set_bonus['item_level']
                ms = has_midnight
                midnight_op = set_bonus.get('midnight')
                if midnight_op == 'set':
                    ms = True
                elif midnight_op == 'squish' and ms:
                    il = get_squish_value(il)

                if add_bonus:
                    add_midnight = add_bonus.get('midnight')
                    if add_midnight == 'force' and not ms:
                        ms = True
                        il = get_squish_value(il)
                    il += add_bonus['amount']
                    if add_midnight == 'set':
                        ms = True
                    elif add_midnight == 'squish' and ms:
                        il = get_squish_value(il)

                il = max(il, 1)
                if not ms:
                    il = get_squish_value(il)
                if il != target_level:
                    return False
            return True

        def is_clean(bid):
            return self._get_side_effects(bid) <= self._CLEAN_SIDE_EFFECTS

        def is_fallback(bid):
            return self._get_side_effects(bid) <= self._FALLBACK_SIDE_EFFECTS

        # Collect candidates
        direct_candidates: dict[int, list] = {}
        set_bonuses_by_level: dict[int, list] = {}
        add_ops: list[tuple] = []

        for bid in sorted((int(k) for k in bonuses), key=int):
            bonus = bonuses[str(bid)]
            if 'redirect' in bonus:
                continue
            op = bonus.get('op')
            clean = is_clean(bid)
            fallback = clean or is_fallback(bid)
            if not fallback:
                continue

            if op == 'set':
                item_level = bonus['item_level']
                effective = item_level if bonus.get('midnight') == 'set' else get_squish_value(item_level)
                if simulate_combo(bonus, None, effective):
                    direct_candidates.setdefault(effective, []).append((f"1:{bid}", clean))
                set_bonuses_by_level.setdefault(effective, []).append((bid, bonus, clean))
            elif op == 'add':
                add_ops.append((bid, bonus, clean))
            elif op == 'scale':
                default_level = bonus.get('default_level')
                if default_level is not None:
                    curve_idx = bonus['curve_id']
                    raw_level = self._interpolate_curve(curves[curve_idx], default_level) \
                        + bonus.get('offset', 0) + bonus.get('extra_amount', 0)
                    midnight = bonus.get('midnight')
                    effective = raw_level if midnight == 'set' else get_squish_value(raw_level)
                    synthetic = {'item_level': raw_level, 'midnight': midnight}
                    if simulate_combo(synthetic, None, effective):
                        direct_candidates.setdefault(effective, []).append((f"1:{bid}", clean))
                    set_bonuses_by_level.setdefault(effective, []).append((bid, synthetic, clean))

        # Build combo candidates
        combo_candidates: dict[int, list] = {}
        for add_bid, add_bonus, add_clean in add_ops:
            for base_level, set_entries in set_bonuses_by_level.items():
                for set_bid, set_bonus, set_clean in set_entries:
                    if set_bid >= add_bid:
                        continue
                    target = base_level + add_bonus['amount']
                    if not simulate_combo(set_bonus, add_bonus, target):
                        continue
                    combo_clean = add_clean and set_clean
                    combo_candidates.setdefault(target, []).append(
                        (f"2:{set_bid}:{add_bid}", combo_clean))

        # Build final lookup: prefer clean, fall back to SET_ITEM_QUALITY
        level_to_bonus_string = {}
        for level in range(1, 171):
            candidates = direct_candidates.get(level, []) + combo_candidates.get(level, [])
            clean = [s for s, c in candidates if c]
            if clean:
                level_to_bonus_string[level] = clean[0]
                continue
            fallback = [s for s, _ in candidates]
            if fallback:
                level_to_bonus_string[level] = fallback[0]

        logging.info("Bonus string data: %d/%d levels covered (%d clean, %d fallback)",
                     len(level_to_bonus_string), 170,
                     sum(1 for l in level_to_bonus_string
                         if any(c for s, c in direct_candidates.get(l, []) + combo_candidates.get(l, [])
                                if s == level_to_bonus_string[l])),
                     sum(1 for l in level_to_bonus_string
                         if not any(c for s, c in direct_candidates.get(l, []) + combo_candidates.get(l, [])
                                    if s == level_to_bonus_string[l])))

        return level_to_bonus_string

    def _get_side_effects(self, bid: int) -> set[ItemBonusType]:
        entries = self._dbc.item_bonus.get(bid)
        if not entries:
            return set()
        return {e.bonus_type for e in entries if e.bonus_type not in self._ILEVEL_TYPES}

    # -- Utility methods --

    def _get_curve_point_value(self, curve_id: int, value: int) -> int:
        """Evaluate a curve at an integer value."""
        lower_bound, upper_bound = self._dbc.curve_point.find_points(curve_id, value)
        if lower_bound.Pos_0 >= value:
            result = lower_bound.Pos_1
        elif upper_bound.Pos_0 < value:
            result = upper_bound.Pos_1
        else:
            slope = (upper_bound.Pos_1 - lower_bound.Pos_1) / (upper_bound.Pos_0 - lower_bound.Pos_0)
            result = lower_bound.Pos_1 + slope * (value - lower_bound.Pos_0)
        return int(floor(result + 0.5))

    def _export_curve_points(self, curve_id: int) -> dict[str, int | float] | None:
        """Export curve points as a {x: y, ...} dict sorted by x."""
        points = self._dbc.curve_point.get(curve_id)
        if not points:
            return None
        sorted_points = sorted(points, key=lambda p: p.Pos_0)
        return {str(_compact_number(p.Pos_0)): _compact_number(p.Pos_1) for p in sorted_points}

    @staticmethod
    def _interpolate_curve(points: dict[str, int | float], value: int | float) -> int:
        """Evaluate an exported curve dict at a value via linear interpolation."""
        numeric = {float(k): v for k, v in points.items()}
        lower_bound, upper_bound = -float('inf'), float('inf')
        for level, item_level in numeric.items():
            if level == value:
                return int(floor(item_level + 0.5))
            elif level < value:
                lower_bound = max(lower_bound, level)
            else:
                upper_bound = min(upper_bound, level)
        if lower_bound == -float('inf'):
            return int(floor(numeric[upper_bound] + 0.5))
        if upper_bound == float('inf'):
            return int(floor(numeric[lower_bound] + 0.5))
        result = numeric[lower_bound] + (value - lower_bound) / (upper_bound - lower_bound) * (numeric[upper_bound] - numeric[lower_bound])
        return int(floor(result + 0.5))

    @staticmethod
    def _sort_priority(bonus_type: ItemBonusType) -> int:
        if bonus_type in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
            return 1
        if bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
            return 2
        if bonus_type == ItemBonusType.OFFSET_CURVE:
            return 3
        if bonus_type in (ItemBonusType.MIDNIGHT_ITEM_LEVEL, ItemBonusType.CRAFTING_QUALITY):
            return 4
        return 0

    @staticmethod
    def _dedup_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the last entry per group. Ops without a group always kept."""
        op_group = AddonDataGenerator._OP_GROUP
        seen: dict[str, int] = {}
        for i, entry in enumerate(entries):
            group = op_group.get(entry['op'])
            if group is not None:
                seen[group] = i
        return [e for i, e in enumerate(entries)
                if op_group.get(e['op']) is None or seen[op_group[e['op']]] == i]

    @staticmethod
    def write(addon_data: dict[str, Any], build: str) -> list[str]:
        """Write addon data to JSON and Lua files.

        Returns the list of paths written.
        """
        json_path = os.path.join('.cache', build, 'addon_data.json')
        with open(json_path, 'w') as f:
            json.dump(addon_data, f, indent=2)

        lua_cache_path = os.path.join('.cache', build, 'addon_data.lua')
        write_lua(addon_data, lua_cache_path)

        paths = [json_path, lua_cache_path]

        lua_addon_path = os.path.join('..', 'LibBonusId', 'Data.lua')
        if os.path.isdir(os.path.dirname(lua_addon_path)):
            write_lua_cbor(addon_data, lua_addon_path, crlf=True)
            paths.append(lua_addon_path)
        else:
            logging.info("Skipping %s (directory not found)", lua_addon_path)
        logging.info("Wrote addon data to %s", ', '.join(paths))
        return paths


def _compact_number(v: float) -> int | float:
    """Convert to int when the value is a whole number."""
    return int(v) if v == int(v) else v
