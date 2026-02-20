#!/usr/bin/env python3

import base64
import json
import logging
import os
import zlib
from typing import Any

import cbor2

ADDON_DATA_VERSION = 1

_LUA_KEYWORDS = frozenset({
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for',
    'function', 'goto', 'if', 'in', 'local', 'nil', 'not', 'or',
    'repeat', 'return', 'then', 'true', 'until', 'while',
})


def _snake_to_camel(s: str) -> str:
    """Convert snake_case to camelCase."""
    parts = s.split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _lua_key(k: int | str) -> str:
    """Format a Python dict key as a Lua table key."""
    if isinstance(k, int):
        return f'[{k}]'
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
    raise ValueError(f"Cannot format key {k!r} as a Lua table key")


def _find_diffs(a: Any, b: Any, path: tuple[str | int, ...] = ()) -> list[tuple[tuple[str | int, ...], Any, Any]]:
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


def _lua_expr(slope: int, intercept: int) -> str:
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


def _lua_value(
    val: Any,
    subs: dict[tuple[str | int, ...], tuple[int, int]] | None = None,
    path: tuple[str | int, ...] = (),
) -> str:
    """Serialize a Python value to compact inline Lua.

    If subs is provided, substitutes linear expressions (slope*i + intercept)
    at the given paths instead of the literal value.
    """
    if subs and path in subs:
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
        return '{' + ', '.join(_lua_value(v, subs, path + (i,)) for i, v in enumerate(val)) + '}'
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            parts.append(f'{_lua_key(k)}={_lua_value(v, subs, path + (k,))}')
        return '{' + ', '.join(parts) + '}'
    raise TypeError(f"Cannot serialize {type(val)} to Lua")


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_runs(entries: list[tuple[int, Any]], min_run: int = 3) -> list[tuple[Any, ...]]:
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
                assert varying is not None
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


def write_lua(addon_data: dict[str, Any], path: str, crlf: bool = False) -> None:
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
            template = _lua_value(base_val, varying)
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
                template = _lua_value(base_val, varying)
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

    # Item levels (range-based: two parallel arrays for binary search lookup)
    item_range_starts = addon_data.get('item_range_starts', [])
    item_range_levels = addon_data.get('item_range_levels', [])
    lines.append(f'local itemRangeStarts = {_lua_value(item_range_starts)}')
    lines.append(f'local itemRangeLevels = {_lua_value(item_range_levels)}')

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

    # Item tree bonuses (deduplicated lists + compressed index map)
    tree_bonus_lists = addon_data.get('tree_bonus_lists', [])
    lines.append('local treeBonusLists = {')
    for lst in tree_bonus_lists:
        lines.append('\t' + _lua_value(lst) + ',')
    lines.append('}')

    item_tree_bonuses = addon_data.get('item_tree_bonuses', {})
    sorted_tree_ids = sorted(item_tree_bonuses.keys(), key=int)
    # Store 1-based indices for Lua
    tree_entries = [(int(k), item_tree_bonuses[k] + 1) for k in sorted_tree_ids]
    tree_segments = _find_runs(tree_entries)

    lines.append('local itemTreeBonuses = {}')
    for seg in tree_segments:
        if seg[0] == 'single':
            _, iid, val = seg
            lines.append(f'itemTreeBonuses[{iid}] = {_lua_value(val)}')
        else:
            _, start, end, base_val, varying = seg
            template = _lua_value(base_val, varying)
            lines.append(f'for i = {start}, {end} do itemTreeBonuses[i] = {template} end')

    # Bonus string precomputed lookup table
    lines.append(f'local levelToBonusString = {_lua_value(addon_data["level_to_bonus_string"])}')

    # Pass data to LibBonusId
    lines.append('Lib.LoadData({')
    lines.append('\tversion = DATA_VERSION,')
    lines.append('\tbuild = BUILD,')

    lines.append(f'\tsquishMax = {squish_max_lua},')
    lines.append('\tsquishCurve = squishCurve,')
    lines.append('\tbonuses = bonuses,')
    lines.append('\tcurves = curves,')
    lines.append('\tcontentTuning = contentTuning,')
    lines.append('\titemRangeStarts = itemRangeStarts,')
    lines.append('\titemRangeLevels = itemRangeLevels,')
    lines.append('\tmidnightItems = midnightItems,')
    lines.append('\ttreeBonusLists = treeBonusLists,')
    lines.append('\titemTreeBonuses = itemTreeBonuses,')
    lines.append('\tlevelToBonusString = levelToBonusString,')
    lines.append('})')

    newline = '\r\n' if crlf else '\n'
    with open(path, 'w', newline='') as f:
        f.write(newline.join(lines) + newline)


def _to_cbor_data(addon_data: dict[str, Any]) -> dict[str | int, Any]:
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

    item_range_starts = addon_data.get('item_range_starts', [])
    item_range_levels = addon_data.get('item_range_levels', [])
    midnight = {mid: True for mid in addon_data.get('midnight_items', [])}
    squish_idx = addon_data['squish_curve']

    tree_bonus_lists = addon_data.get('tree_bonus_lists', [])
    # Store 1-based indices for Lua
    item_tree_bonuses = {int(k): v + 1 for k, v in addon_data.get('item_tree_bonuses', {}).items()}

    return {
        'version': ADDON_DATA_VERSION,
        'build': addon_data['build'],
        'squishMax': addon_data['squish_max'],
        'squishCurve': curves[squish_idx],
        'bonuses': bonuses,
        'curves': curves,
        'contentTuning': content_tuning,
        'itemRangeStarts': item_range_starts,
        'itemRangeLevels': item_range_levels,
        'midnightItems': midnight,
        'treeBonusLists': tree_bonus_lists,
        'itemTreeBonuses': item_tree_bonuses,
        'levelToBonusString': {int(k): v for k, v in addon_data['level_to_bonus_string'].items()},
    }


def write_lua_cbor(addon_data: dict[str, Any], path: str, crlf: bool = False) -> None:
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
        f'local DATA = "{b64}"',
        'Lib.LoadData(C_EncodingUtil.DeserializeCBOR(C_EncodingUtil.DecompressString(C_EncodingUtil.DecodeBase64(DATA))))',
    ]

    newline = '\r\n' if crlf else '\n'
    with open(path, 'w', newline='') as f:
        f.write(newline.join(lines) + newline)

    logging.info("CBOR %s: %d bytes (cbor=%d, zlib=%d, base64=%d)",
                 path, os.path.getsize(path), len(cbor_bytes), len(compressed), len(b64))
