#!/usr/bin/env python3

import base64
import json
import logging
import os
import sys
import zlib
from math import floor

import cbor2

from lib.dbc_file import CurveType, DBC, ItemBonusType

ADDON_DATA_VERSION = 1

_LUA_KEYWORDS = frozenset({
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for',
    'function', 'goto', 'if', 'in', 'local', 'nil', 'not', 'or',
    'repeat', 'return', 'then', 'true', 'until', 'while',
})


def _snake_to_camel(s):
    """Convert snake_case to camelCase."""
    parts = s.split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _lua_key(k):
    """Format a Python dict key as a Lua table key."""
    if isinstance(k, int):
        return f'[{k}]'
    if isinstance(k, str):
        # Numeric string keys → number keys
        try:
            return f'[{int(k)}]'
        except ValueError:
            try:
                return f'[{float(k)}]'
            except ValueError:
                pass
        camel = _snake_to_camel(k)
        if camel.isidentifier() and camel not in _LUA_KEYWORDS:
            return camel
    return f'["{k}"]'


def _lua_value(val):
    """Serialize a Python value to compact inline Lua."""
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(int(val)) if val == int(val) else str(val)
    if isinstance(val, str):
        return f'"{_snake_to_camel(val)}"'
    if isinstance(val, list):
        return '{' + ', '.join(_lua_value(v) for v in val) + '}'
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            lk = _lua_key(k)
            lv = _lua_value(v)
            if lk.startswith('['):
                parts.append(f'{lk}={lv}')
            else:
                parts.append(f'{lk}={lv}')
        return '{' + ', '.join(parts) + '}'
    raise TypeError(f"Cannot serialize {type(val)} to Lua")


def _find_diffs(a, b, path=()):
    """Find leaf-level differences between two nested structures."""
    if type(a) != type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return [(path, a, b)]
        diffs = []
        for k in sorted(a.keys()):
            diffs.extend(_find_diffs(a[k], b[k], path + (k,)))
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path, a, b)]
        diffs = []
        for i in range(len(a)):
            diffs.extend(_find_diffs(a[i], b[i], path + (i,)))
        return diffs
    if a != b:
        return [(path, a, b)]
    return []


def _lua_expr(slope, intercept):
    """Format slope*i + intercept as a Lua expression."""
    if slope == 1:
        if intercept == 0:
            return 'i'
        return f'i+{intercept}' if intercept > 0 else f'i-{-intercept}'
    if slope == -1:
        return f'{intercept}-i' if intercept != 0 else '-i'
    base = f'{slope}*i'
    if intercept == 0:
        return base
    return f'{base}+{intercept}' if intercept > 0 else f'{base}-{-intercept}'


def _lua_value_with_subs(val, subs, path=()):
    """Serialize a value to Lua, substituting linear expressions at given paths."""
    if path in subs:
        return _lua_expr(*subs[path])
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(int(val)) if val == int(val) else str(val)
    if isinstance(val, str):
        return f'"{_snake_to_camel(val)}"'
    if isinstance(val, list):
        return '{' + ', '.join(_lua_value_with_subs(v, subs, path + (i,)) for i, v in enumerate(val)) + '}'
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            lk = _lua_key(k)
            lv = _lua_value_with_subs(v, subs, path + (k,))
            parts.append(f'{lk}={lv}')
        return '{' + ', '.join(parts) + '}'
    raise TypeError(f"Cannot serialize {type(val)} to Lua")


def _is_numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_runs(entries, min_run=3):
    """Find compressible runs in sorted (int_id, value) pairs.

    Returns list of segments:
    - ('single', id, value)
    - ('run', start_id, end_id, base_value, {path: (slope, intercept)})
    """
    segments = []
    i = 0
    n = len(entries)

    while i < n:
        bid_start, val_start = entries[i]
        j = i + 1
        varying = None

        while j < n:
            bid_cur, val_cur = entries[j]
            if bid_cur != bid_start + (j - i):
                break

            diffs = _find_diffs(val_start, val_cur)

            if j == i + 1:
                # First pair — establish pattern
                if not all(_is_numeric(d[1]) and _is_numeric(d[2]) for d in diffs):
                    break
                varying = {}
                ok = True
                for p, v1, v2 in diffs:
                    slope = v2 - v1
                    if slope != int(slope):
                        ok = False
                        break
                    slope = int(slope)
                    intercept = v1 - slope * bid_start
                    if intercept != int(intercept):
                        ok = False
                        break
                    varying[p] = (int(slope), int(intercept))
                if not ok:
                    varying = None
                    break
            else:
                # Verify pattern holds
                if len(diffs) != len(varying):
                    break
                ok = True
                for p, _, v_cur in diffs:
                    if p not in varying:
                        ok = False
                        break
                    slope, intercept = varying[p]
                    if v_cur != slope * bid_cur + intercept:
                        ok = False
                        break
                if not ok:
                    break
            j += 1

        run_len = j - i
        if run_len >= min_run:
            segments.append(('run', bid_start, bid_start + run_len - 1,
                             val_start, varying or {}))
            i = j
        else:
            segments.append(('single', entries[i][0], entries[i][1]))
            i += 1

    return segments


def _build_item_data(dbc):
    """Build per-item base level and midnight scaling data from ItemSparse.

    Returns:
        item_levels: dict {str(item_id): base_level} for all items
        midnight_items: sorted list of item IDs with ItemSquishEraID == 2
    """
    item_levels = {}
    midnight_items = []
    for e in dbc.item_sparse._entries.values():
        if e.Stackable > 1:
            continue
        item_levels[str(e.ID)] = e.ItemLevel
        if e.ItemSquishEraID == 2:
            midnight_items.append(e.ID)
    midnight_items.sort()
    return item_levels, midnight_items


def _write_lua(addon_data, path, crlf=False):
    """Write addon data as a Lua file with loop compression."""
    lines = []

    # Early-out if we shouldn't load this data
    build = addon_data["build"]
    lines.append(f'local DATA_VERSION = {ADDON_DATA_VERSION}')
    lines.append(f'local BUILD = "{build}"')
    lines.append('local Lib = LibStub("LibBonusId") ---@type LibBonusId')
    lines.append('if not Lib.ShouldLoadData(DATA_VERSION, BUILD) then return end')

    # Bonuses (compressed with loops)
    sorted_bonus_ids = sorted(addon_data['bonuses'].keys(), key=int)
    bonus_entries = [(int(k), addon_data['bonuses'][k]) for k in sorted_bonus_ids]
    bonus_segments = _find_runs(bonus_entries)

    lines.append('local bonuses = {}')
    for seg in bonus_segments:
        if seg[0] == 'single':
            _, bid, val = seg
            lines.append(f'bonuses[{bid}] = {_lua_value(val)}')
        else:
            _, start, end, base_val, varying = seg
            template = _lua_value_with_subs(base_val, varying)
            lines.append(f'for i = {start}, {end} do bonuses[i] = {template} end')

    # Content tuning
    lines.append('local contentTuning = {')
    for k in sorted(addon_data['content_tuning'].keys(), key=int):
        lines.append(f'\t[{k}] = {_lua_value(addon_data["content_tuning"][k])},')
    lines.append('}')

    # CT remap (inline assignments into contentTuning)
    ct_remap = addon_data.get('content_tuning_remap', {})
    if ct_remap:
        sorted_ct_keys = sorted(ct_remap.keys())
        ct_entries = [(int(k), ct_remap[k]) for k in sorted_ct_keys]
        ct_segments = _find_runs(ct_entries)

        for seg in ct_segments:
            if seg[0] == 'single':
                _, src, dst = seg
                lines.append(f'contentTuning[{src}] = contentTuning[{dst}]')
            else:
                _, start, end, base_val, varying = seg
                template = _lua_value_with_subs(base_val, varying)
                lines.append(f'for i = {start}, {end} do contentTuning[i] = contentTuning[{template}] end')

    # Curves
    lines.append('local curves = {')
    for curve in addon_data['curves']:
        lines.append('\t' + _lua_value(curve) + ',')
    lines.append('}')

    squish_index = addon_data["squish_curve"]
    lines.append(f'local squishCurve = curves[{squish_index + 1}]')
    squish_max = max(float(k) for k in addon_data["curves"][squish_index])
    squish_max_lua = int(squish_max) if squish_max == int(squish_max) else squish_max

    # Item levels (compressed with loops via helper function to avoid Lua 5.1's
    # SHRT_MAX limit on local variable registrations per chunk)
    item_levels = addon_data.get('item_levels', {})
    sorted_item_ids = sorted(item_levels.keys(), key=int)
    item_entries = [(int(k), item_levels[k]) for k in sorted_item_ids]
    item_segments = _find_runs(item_entries)

    lines.append('local items = {}')
    lines.append('local function SetItemRange(lo, hi, level) for i = lo, hi do items[i] = level end end')
    for seg in item_segments:
        if seg[0] == 'single':
            _, iid, val = seg
            lines.append(f'items[{iid}] = {_lua_value(val)}')
        else:
            _, start, end, base_val, varying = seg
            template = _lua_value_with_subs(base_val, varying)
            if varying:
                # Linear pattern — must use inline for loop (i is the loop variable)
                lines.append(f'for i = {start}, {end} do items[i] = {template} end')
            else:
                # Constant value — use helper to avoid local variable limit
                lines.append(f'SetItemRange({start}, {end}, {template})')

    # Midnight items (hash set with loop compression for consecutive IDs)
    midnight = addon_data.get('midnight_items', [])
    lines.append('local midnightItems = {}')
    if midnight:
        lines.append('local function SetMidnightRange(lo, hi) for i = lo, hi do midnightItems[i] = true end end')
        i = 0
        while i < len(midnight):
            j = i + 1
            while j < len(midnight) and midnight[j] == midnight[j - 1] + 1:
                j += 1
            if j - i >= 3:
                lines.append(f'SetMidnightRange({midnight[i]}, {midnight[j - 1]})')
            else:
                for k in range(i, j):
                    lines.append(f'midnightItems[{midnight[k]}] = true')
            i = j

    # Pass data to LibBonusId
    lines.append('Lib.LoadData({')
    lines.append('\tversion = DATA_VERSION,')
    lines.append('\tbuild = BUILD,')

    lines.append(f'\tsquishMax = {squish_max_lua},')
    lines.append('\tsquishCurve = squishCurve,')
    lines.append('\tbonuses = bonuses,')
    lines.append('\tcurves = curves,')
    lines.append('\tcontentTuning = contentTuning,')
    lines.append('\titems = items,')
    lines.append('\tmidnightItems = midnightItems,')
    lines.append('})')

    newline = '\r\n' if crlf else '\n'
    with open(path, 'w', newline='') as f:
        f.write(newline.join(lines) + newline)


def _to_cbor_data(addon_data):
    """Convert addon_data dict to the Lua-compatible structure for CBOR serialization.

    Converts snake_case keys/values to camelCase, string keys to numeric where possible,
    and expands the content tuning remap table inline.
    """
    def convert_key(k):
        try:
            return int(k)
        except ValueError:
            try:
                return float(k)
            except ValueError:
                return _snake_to_camel(k)

    def convert(v):
        if isinstance(v, dict):
            return {convert_key(k) if isinstance(k, str) else k: convert(val) for k, val in v.items()}
        if isinstance(v, list):
            return [convert(x) for x in v]
        if isinstance(v, str):
            return _snake_to_camel(v)
        return v

    bonuses = {int(k): convert(v) for k, v in addon_data['bonuses'].items()}
    curves = [convert(curve) for curve in addon_data['curves']]

    # Content tuning with remap expansion (so no post-load fixup needed in Lua)
    content_tuning = {int(k): convert(v) for k, v in addon_data['content_tuning'].items()}
    for src, dst in addon_data.get('content_tuning_remap', {}).items():
        dst_int = int(dst)
        if dst_int in content_tuning:
            content_tuning[int(src)] = content_tuning[dst_int]

    items = {int(k): v for k, v in addon_data.get('item_levels', {}).items()}
    midnight = {mid: True for mid in addon_data.get('midnight_items', [])}
    squish_idx = addon_data['squish_curve']

    return {
        'version': ADDON_DATA_VERSION,
        'build': addon_data['build'],
        'squishMax': addon_data['squish_max'],
        'squishCurve': curves[squish_idx],
        'bonuses': bonuses,
        'curves': curves,
        'contentTuning': content_tuning,
        'items': items,
        'midnightItems': midnight,
    }


def _write_lua_cbor(addon_data, path, crlf=False):
    """Write addon data as a CBOR-compressed Lua file using C_EncodingUtil APIs."""
    cbor_data = _to_cbor_data(addon_data)
    cbor_bytes = cbor2.dumps(cbor_data)
    co = zlib.compressobj(1, zlib.DEFLATED, -15)
    compressed = co.compress(cbor_bytes) + co.flush()
    b64 = base64.b64encode(compressed).decode('ascii')

    build = addon_data["build"]
    lines = [
        f'local DATA_VERSION = {ADDON_DATA_VERSION}',
        f'local BUILD = "{build}"',
        'local Lib = LibStub("LibBonusId") ---@type LibBonusId',
        'if not Lib.ShouldLoadData(DATA_VERSION, BUILD) then return end',
        f'Lib.LoadData(C_EncodingUtil.DeserializeCBOR(C_EncodingUtil.DecompressString(C_EncodingUtil.DecodeBase64("{b64}"))))',
    ]

    newline = '\r\n' if crlf else '\n'
    with open(path, 'w', newline='') as f:
        f.write(newline.join(lines) + newline)

    logging.info("CBOR %s: %d bytes (cbor=%d, zlib=%d, base64=%d)",
                 path, os.path.getsize(path), len(cbor_bytes), len(compressed), len(b64))


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
                result['content_tuning_key'] = 'scaling_config'
        else:
            result['content_tuning_key'] = 'scaling_config2'
            result['content_tuning_default_only'] = True
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
            'content_tuning_key': 'stat',
        }
        if entry.Value_2:
            result['content_tuning_id'] = entry.Value_2
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
            data['sort_priority'] = sp
        if indirect:
            data['indirect'] = True
        if len(ops) > 1:
            other = ops[1]
            assert other['op'] == 'add', f"Unexpected other op: {other['op']} for bonus {parent_id}"
            data['extra_amount'] = other['amount']
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
        points = _export_curve_points(dbc, curve_id)
        if points:
            curves[str(curve_id)] = points

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
            for key in ('sort_priority', 'indirect', 'extra_amount'):
                if key in bonus:
                    simplified[key] = bonus[key]
            bonuses[bid] = simplified

    # Remove curves no longer referenced by any bonus
    still_referenced = set()
    for bonus in bonuses.values():
        if bonus.get('op') == 'scale':
            still_referenced.add(str(bonus['curve_id']))
    still_referenced.add(str(squish_curve_id))
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

    squish_curve_index = curve_index_map[str(squish_curve_id)]

    curves = curves_list
    logging.info("Curves: %d unique curves (%d total points)",
                 len(curves), sum(len(v) for v in curves))

    # Pre-compute content tuning operations per bonus-type group.
    # Groups: 'scaling_config', 'scaling_config2', 'stat'
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
            # max_scaling_offset == 0 (default) — differs by bonus type group
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

    # Compact content tuning: use 'op' as default when all three keys are present
    for ct_id, ct_data in list(content_tuning.items()):
        if set(ct_data.keys()) != {'scaling_config', 'scaling_config2', 'stat'}:
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
            for b in [bonus] + ([bonus['other']] if isinstance(bonus.get('other'), dict) else []):
                if 'content_tuning_id' in b:
                    ct_str = str(b['content_tuning_id'])
                    if ct_str in ct_remap:
                        b['content_tuning_id'] = int(ct_remap[ct_str])

        # Remove non-canonical entries
        for ct_id in ct_remap:
            del content_tuning[ct_id]

        # Convert remap to int keys/values for compact storage
        ct_remap_int = {int(k): int(v) for k, v in ct_remap.items()}
        logging.info("CT dedup: removed %d duplicate entries (%d remaining), remap table: %d entries",
                     len(ct_remap), len(content_tuning), len(ct_remap_int))
    else:
        ct_remap_int = {}

    # Build per-item data
    item_levels, midnight_items = _build_item_data(dbc)
    logging.info("Item data: %d items, %d midnight items",
                 len(item_levels), len(midnight_items))

    # Assemble and write
    addon_data = {
        "build": build,
        "squish_curve": squish_curve_index,
        "squish_max": int(max(float(k) for k in curves[squish_curve_index])),
        "bonuses": bonuses,
        "curves": curves,
        "content_tuning": content_tuning,
        "item_levels": item_levels,
        "midnight_items": midnight_items,
    }
    if ct_remap_int:
        addon_data["content_tuning_remap"] = ct_remap_int

    output_path = os.path.join('.cache', build, 'addon_data.json')
    with open(output_path, 'w') as f:
        json.dump(addon_data, f, indent=2)

    lua_cache_path = os.path.join('.cache', build, 'addon_data.lua')
    _write_lua(addon_data, lua_cache_path)

    lua_root_path = 'Data.lua'
    _write_lua_cbor(addon_data, lua_root_path, crlf=True)

    logging.info("Wrote addon data to %s, %s, and %s", output_path, lua_cache_path, lua_root_path)
    logging.info("Bonuses: %d bonus list IDs", len(bonuses))
    logging.info("Curves: %d curves (%d total points)",
                 len(curves), sum(len(v) for v in curves))
    logging.info("Squish curve: index %d", squish_curve_index)
    logging.info("Content tuning: %d entries", len(content_tuning))
    logging.info("Item data: %d items, %d midnight items", len(item_levels), len(midnight_items))
