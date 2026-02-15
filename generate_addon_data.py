#!/usr/bin/env python3

import json
import logging
import os
import sys
from math import floor

from lib.dbc_file import CurveType, DBC, ItemBonusType


def _sort_priority(bonus_type):
    if bonus_type in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
        return 1
    if bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
        return 2
    if bonus_type == ItemBonusType.OFFSET_CURVE:
        return 3
    if bonus_type in (ItemBonusType.MIDNIGHT_ITEM_LEVEL, ItemBonusType.CRAFTING_QUALITY):
        return 4
    return 0


_OP_GROUP = {'scale': 'S', 'set': 'S', 'add': 'Q'}


def _get_curve_point_value(dbc, curve_id, value):
    """Evaluate a curve at an integer value — same logic as DirectDBCAlgorithm."""
    lower_bound, upper_bound = dbc.curve_point.find_points(curve_id, value)
    if lower_bound.Pos_0 >= value:
        result = lower_bound.Pos_1
    elif upper_bound.Pos_0 < value:
        result = upper_bound.Pos_1
    else:
        slope = (upper_bound.Pos_1 - lower_bound.Pos_1) / (upper_bound.Pos_0 - lower_bound.Pos_0)
        result = lower_bound.Pos_1 + slope * (value - lower_bound.Pos_0)
    return int(floor(result + 0.5))


def _export_bonus(entry, dbc):
    """Convert a DBC ItemBonus entry to an operation dict.
    Returns None for entries that have no effect on item level calculation."""
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

        # Determine midnight handling
        if bt == ItemBonusType.SCALING_CONFIG:
            sets_midnight = sc.ItemSquishEraID != 1 or bool(sc.Flags & 1)
        else:
            sets_midnight = sc.ItemSquishEraID != 1

        result = {
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
                result['ct_key'] = 'sc'
        else:
            result['ct_key'] = 'sc2'
            result['ct_default_only'] = True
        return result

    elif bt == ItemBonusType.OFFSET_CURVE:
        curve_id = entry.Value_0
        input_value = entry.Value_1
        if curve_id and dbc.curve_point.get(curve_id):
            item_level = _get_curve_point_value(dbc, curve_id, input_value)
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
            'ct_key': 'stat',
        }
        if entry.Value_2:
            result['ct_id'] = entry.Value_2
        return result

    elif bt == ItemBonusType.APPLY_BONUS:
        return {'op': 'apply', 'target': entry.Value_0}

    else:
        return None


def _dedup_entries(entries):
    """Keep only the last entry per group. Ops without a group always kept."""
    seen = {}  # group -> index
    for i, entry in enumerate(entries):
        group = _OP_GROUP.get(entry['op'])
        if group is not None:
            seen[group] = i
    return [e for i, e in enumerate(entries)
            if _OP_GROUP.get(e['op']) is None or seen[_OP_GROUP[e['op']]] == i]


def _compact_number(v):
    """Convert to int when the value is a whole number."""
    return int(v) if v == int(v) else v


def _export_curve_points(dbc, curve_id):
    """Export curve points as a {x: y, ...} dict sorted by x."""
    points = dbc.curve_point.get(curve_id)
    if not points:
        return None
    sorted_points = sorted(points, key=lambda p: p.Pos_0)
    return {str(_compact_number(p.Pos_0)): _compact_number(p.Pos_1) for p in sorted_points}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <build>", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d %(levelname)s [%(module)s:%(lineno)d] %(message)s',
        datefmt='%H:%M:%S'
    )

    build = sys.argv[1]
    dbc = DBC(build)

    # Export all bonuses with pre-resolved APPLY_BONUS
    bonuses = {}
    for parent_id, entries in dbc.item_bonus._entries.items():
        sp = max((_sort_priority(e.bonus_type) for e in entries), default=0)
        exported_entries = [_export_bonus(e, dbc) for e in entries]

        # Check for simple APPLY_BONUS redirect
        if len(entries) == 1 and entries[0].bonus_type == ItemBonusType.APPLY_BONUS:
            target_id = entries[0].Value_0
            target_entries = dbc.item_bonus.get(target_id)
            if not any(e.bonus_type == ItemBonusType.APPLY_BONUS for e in target_entries):
                redirect = {'redirect': target_id}
                if sp:
                    redirect['sp'] = sp
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
                for target_entry in dbc.item_bonus.get(target_id):
                    if target_entry.bonus_type != ItemBonusType.APPLY_BONUS:
                        exported = _export_bonus(target_entry, dbc)
                        if exported is not None:
                            indirect.append(exported)
            else:
                direct.append(bonus)

        direct = _dedup_entries(direct)
        indirect = _dedup_entries(indirect)

        # Flatten to a single operation per bonus ID
        ops = indirect or direct
        if not ops:
            continue
        data = dict(ops[0])
        if sp:
            data['sp'] = sp
        if indirect:
            data['indirect'] = True
        if len(ops) > 1:
            data['then'] = ops[1]
        bonuses[str(parent_id)] = data

    # Collect all referenced curve IDs (for curves still needed at runtime)
    referenced_curve_ids = set()

    # From SCALING_CONFIG/SCALING_CONFIG_2 bonus entries (inlined curve_id)
    for parent_id, entries in dbc.item_bonus._entries.items():
        for entry in entries:
            if entry.bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
                if dbc.item_scaling_config.has(entry.Value_0):
                    sc = dbc.item_scaling_config.get(entry.Value_0)
                    if dbc.item_offset_curve.has(sc.ItemOffsetCurveID):
                        referenced_curve_ids.add(dbc.item_offset_curve.get(sc.ItemOffsetCurveID).CurveID)
            # OFFSET_CURVE results are pre-computed, no runtime curve needed
            elif entry.bonus_type in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
                if entry.Value_3 and dbc.curve_point.get(entry.Value_3):
                    referenced_curve_ids.add(entry.Value_3)

    # Squish curve
    squish_entry = dbc.item_squish_era.get_midnight()
    squish_curve_id = squish_entry.CurveID
    referenced_curve_ids.add(squish_curve_id)

    # Validate curve types
    for curve_id in referenced_curve_ids:
        curve = dbc.curve.get(curve_id)
        curve_type = CurveType(curve.Type)
        assert curve_type in (CurveType.LINEAR, CurveType.REQ_LEVEL_AND_ITEM_LEVEL), \
            f"Unexpected curve type {curve_type} for curve {curve_id}"

    # Export raw curve points
    curves = {}
    for curve_id in sorted(referenced_curve_ids):
        if curve_id == squish_curve_id:
            continue  # squish curve stored separately
        points = _export_curve_points(dbc, curve_id)
        if points:
            curves[str(curve_id)] = points

    # Squish curve (separate, with special out-of-range handling)
    squish_curve = _export_curve_points(dbc, squish_curve_id)

    # Simplify scale ops with constant curves into set ops
    constant_curves = {cid: next(iter(pts.values()))
                       for cid, pts in curves.items()
                       if len(set(pts.values())) == 1}

    def _simplify_scale(bonus):
        if bonus.get('op') != 'scale':
            return bonus
        curve_key = str(bonus['curve_id'])
        if curve_key not in constant_curves:
            return bonus
        result = {'op': 'set', 'item_level': constant_curves[curve_key] + bonus.get('offset', 0)}
        for key in ('midnight', 'priority'):
            if key in bonus:
                result[key] = bonus[key]
        return result

    for bid, bonus in list(bonuses.items()):
        if 'redirect' in bonus:
            continue
        simplified = _simplify_scale(bonus)
        if simplified is not bonus:
            for key in ('sp', 'indirect', 'then'):
                if key in bonus:
                    simplified[key] = bonus[key]
            bonuses[bid] = simplified
        if 'then' in bonuses[bid]:
            bonuses[bid]['then'] = _simplify_scale(bonuses[bid]['then'])

    # Remove curves no longer referenced by any bonus
    still_referenced = set()
    for bonus in bonuses.values():
        if bonus.get('op') == 'scale':
            still_referenced.add(str(bonus['curve_id']))
        if isinstance(bonus.get('then'), dict) and bonus['then'].get('op') == 'scale':
            still_referenced.add(str(bonus['then']['curve_id']))
    curves = {k: v for k, v in curves.items() if k in still_referenced}

    # Dedup curves and convert to array with index-based references
    # 1. Find unique curves (dedup by value)
    curve_index_map = {}  # old curve_id str -> array index
    curves_by_value = {}  # json(points) -> array index
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

    # 2. Remap curve_id references in bonuses to array indices
    def _remap_curve(bonus):
        if bonus.get('op') == 'scale':
            cid_str = str(bonus['curve_id'])
            bonus['curve_id'] = curve_index_map[cid_str]
    for bonus in bonuses.values():
        if 'redirect' in bonus:
            continue
        _remap_curve(bonus)
        if isinstance(bonus.get('then'), dict):
            _remap_curve(bonus['then'])

    curves = curves_list
    logging.info("Curves: %d unique curves (%d total points)",
                 len(curves), sum(len(v) for v in curves))

    # Pre-compute content tuning operations per bonus-type group.
    # Groups: 'sc' (SCALING_CONFIG), 'sc2' (SCALING_CONFIG_2), 'stat' (STAT_SCALING/STAT_FIXED)
    # Operations: ["cap", max], ["clamp", min, max], ["const", val],
    #             ["cap_add", cap, offset], ["cap_add_floor", cap, offset, floor]
    # None = passthrough (omitted from output)
    content_tuning = {}
    for ct_id, entry in dbc.content_tuning._entries.items():
        max_so = entry.MaxLevelScalingOffset
        min_so = entry.MinLevelScalingOffset
        max_lvl = entry.MaxLevelSquish
        min_lvl = entry.MinLevelSquish
        has_flag_4 = bool(entry.Flags & 0x4)
        is_df = entry.HPScalingCurveID in {77585}
        min_with_offset = min_lvl + entry.AllowedMinOffset

        if max_so == 3:
            op = ['cap', 70 + max_lvl]
            ops = {'sc': op, 'sc2': op, 'stat': op}
        elif max_so == 2:
            if max_lvl == 0:
                op = ['const', min_lvl] if min_lvl >= 80 else ['cap', 80]
            elif max_lvl < 0:
                op = ['cap_add_floor', 80, max_lvl, min_lvl] if min_lvl > 80 else ['cap_add', 80, max_lvl]
            else:
                op = ['cap_add', 80, max_lvl] if min_so == 2 else None
            ops = {'sc': op, 'sc2': op, 'stat': op}
        elif max_so == 1:
            ops = {
                'sc': ['clamp', min_lvl, max_lvl],
                'sc2': ['cap', max_lvl + 1],
                'stat': ['cap', max_lvl + 1],
            }
        else:
            # max_scaling_offset == 0 (default) — differs by bonus type group
            # stat group
            if max_lvl <= 0 and (min_lvl <= 0 or has_flag_4):
                stat_op = None
            elif min_lvl > max_lvl > 0:
                stat_op = ['const', min_lvl]
            else:
                stat_op = ['clamp', min_with_offset, max_lvl]
            # sc group
            if has_flag_4:
                sc_op = None
            elif max_lvl > 0:
                sc_op = ['clamp', min_with_offset, max_lvl]
            elif min_lvl > 0:
                sc_op = ['cap', 80]
            else:
                sc_op = None
            # sc2 group
            if is_df:
                sc2_op = None
            elif max_lvl == 0:
                sc2_op = ['cap', 80]
            elif max_lvl < 0:
                sc2_op = None
            else:
                sc2_op = ['clamp', min_with_offset, max_lvl]
            ops = {'sc': sc_op, 'sc2': sc2_op, 'stat': stat_op}

        # Normalize clamp(x,x) → const(x)
        for k, v in ops.items():
            if v and v[0] == 'clamp' and v[1] == v[2]:
                ops[k] = ['const', v[1]]

        # Only store entries with at least one non-passthrough operation
        ct_data = {k: v for k, v in ops.items() if v is not None}
        if ct_data:
            content_tuning[str(ct_id)] = ct_data

    # Apply ConditionalContentTuning redirects: merge redirect target ops into parent
    for parent_id, redirects in dbc.conditional_content_tuning._entries.items():
        for redirect in redirects:
            target_ops = content_tuning.get(str(redirect.RedirectContentTuningID), {})
            if redirect.RedirectEnum == 7:
                keys = ('sc', 'sc2', 'stat')
            elif redirect.RedirectEnum == 14:
                keys = ('sc', 'sc2')
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

    # Compact content tuning: use 'op' as default when all three keys are present
    for ct_id, ct_data in list(content_tuning.items()):
        if set(ct_data.keys()) != {'sc', 'sc2', 'stat'}:
            continue
        values = list(ct_data.values())
        # Find the most common value to use as default
        from collections import Counter
        value_counts = Counter(json.dumps(v) for v in values)
        default_json, count = value_counts.most_common(1)[0]
        if count >= 2:
            default_val = json.loads(default_json)
            compact = {'op': default_val}
            for k, v in ct_data.items():
                if v != default_val:
                    compact[k] = v
            content_tuning[ct_id] = compact

    # Dedup content tuning: find entries with identical ops and build remap table
    ct_remap = {}  # non-canonical ct_id -> canonical ct_id
    ct_by_value = {}  # json(ops) -> canonical ct_id
    for ct_id in sorted(content_tuning.keys(), key=int):
        key = json.dumps(content_tuning[ct_id], sort_keys=True)
        if key in ct_by_value:
            ct_remap[ct_id] = ct_by_value[key]
        else:
            ct_by_value[key] = ct_id

    if ct_remap:
        # Remap ct_id references in bonuses
        for bonus in bonuses.values():
            if 'redirect' in bonus:
                continue
            for b in [bonus] + ([bonus['then']] if isinstance(bonus.get('then'), dict) else []):
                if 'ct_id' in b:
                    ct_str = str(b['ct_id'])
                    if ct_str in ct_remap:
                        b['ct_id'] = int(ct_remap[ct_str])

        # Remove non-canonical entries
        for ct_id in ct_remap:
            del content_tuning[ct_id]

        # Convert remap to int keys/values for compact storage
        ct_remap_int = {int(k): int(v) for k, v in ct_remap.items()}
        logging.info("CT dedup: removed %d duplicate entries (%d remaining), remap table: %d entries",
                     len(ct_remap), len(content_tuning), len(ct_remap_int))
    else:
        ct_remap_int = {}

    # Assemble and write
    addon_data = {
        "build": build,
        "squish_curve": squish_curve,
        "bonuses": bonuses,
        "curves": curves,
        "content_tuning": content_tuning,
    }
    if ct_remap_int:
        addon_data["ct_remap"] = ct_remap_int

    output_path = os.path.join('.cache', build, 'addon_data.json')
    with open(output_path, 'w') as f:
        json.dump(addon_data, f, indent=2)

    logging.info("Wrote addon data to %s", output_path)
    logging.info("Bonuses: %d bonus list IDs", len(bonuses))
    logging.info("Curves: %d curves (%d total points)",
                 len(curves), sum(len(v) for v in curves))
    logging.info("Squish curve: %d points", len(squish_curve))
    logging.info("Content tuning: %d entries", len(content_tuning))
