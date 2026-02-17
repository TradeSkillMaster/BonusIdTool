# Bonus ID Tool

Reverse-engineering World of Warcraft's item level calculation from DBC (Database Client) files. Produces compact addon data that can be embedded in a WoW addon to compute item levels entirely client-side.

## Overview

WoW item levels are determined by a complex system of bonus IDs, scaling configs, content tuning, curve interpolation, and the Midnight item-level squish. This project:

1. **Parses DBC files** fetched from [wago.tools](https://wago.tools) (ItemBonus, CurvePoint, ContentTuning, ItemScalingConfig, etc.)
2. **Implements the item level algorithm** in Python, matching the game client's behavior
3. **Generates compact addon data** (JSON + Lua) for use in the [LibBonusId](https://github.com/TradeSkillMaster/LibBonusId) WoW addon library
4. **Validates correctness** against game-extracted test data

## Usage

### Generate addon data

```bash
python bonus_id_tool.py generate <build>
```

Fetches DBC data for the given build (e.g. `12.0.1.65867`), computes all bonus ID effects, and writes:
- `.cache/<build>/addon_data.json` — full addon data as JSON
- `.cache/<build>/addon_data.lua` — Lua table format
- `../LibBonusId/Data.lua` — CBOR-encoded Lua for the addon

### Run tests

```bash
python bonus_id_tool.py test <build>
```

Tests the algorithm against game-extracted data in `test/data/`. Supports testing individual algorithm implementations:

```bash
python bonus_id_tool.py test <build> -a dbc    # Direct DBC algorithm only
python bonus_id_tool.py test <build> -a addon  # Addon data algorithm only
python bonus_id_tool.py test <build> -a lua    # Lua algorithm only
```

### Calculate item level

From a WoW item link:

```bash
python bonus_id_tool.py calc <build> "|cnIQ4:|Hitem:60211::::::::61:259::5:1:9052:2:9:35:28:2660:::::|h[Bracers of the Dark Pool]|h|r"
```

From separated item info (item ID, bonus IDs, and modifiers):

```bash
python bonus_id_tool.py calc <build> -i 60211 -b 9052 -p 35 -c 2660
```

Options:
- `-i, --item-id` — Item ID
- `-b, --bonus-ids` — Comma-separated bonus IDs
- `-p, --player-level` — Player level modifier (modifier type 9, default: 0)
- `-c, --content-tuning-id` — Content tuning ID modifier (modifier type 28, default: 0)
- `-a, --algorithm` — Algorithm to use: `dbc` (default) or `addon`

## Project Structure

```
├── bonus_id_tool.py              # CLI entry point
├── lib/
│   ├── generate_addon_data.py    # Addon data generator
│   ├── algorithm.py              # Base algorithm interface
│   ├── direct_dbc_algorithm.py   # Algorithm using raw DBC data
│   ├── addon_data_algorithm.py   # Algorithm using generated addon data
│   ├── dbc_file.py               # DBC file parsing and type definitions
│   ├── item.py                   # Item link parsing
│   └── lua_writer.py             # Lua/CBOR output
├── test/
│   ├── test.py                   # Test runner
│   ├── test_runner.lua           # Lua test harness
│   └── data/
│       ├── single_bonus_id.txt   # Per-bonus-ID expected item levels
│       ├── content_tuning_id.txt # Per-content-tuning expected levels
│       └── links.txt             # Full item links with expected levels
└── .cache/                       # Downloaded DBC data (per build)
```

## Requirements

- Python 3.10+
- `requests` (for fetching DBC data)
- `lua` (for running Lua algorithm tests)

## License

MIT — see [LICENSE.txt](LICENSE.txt).
