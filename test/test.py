#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
import logging
import os
import re
import subprocess
import sys
from typing import Self

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

from lib.algorithm import Algorithm
from lib.addon_data_algorithm import AddonDataAlgorithm
from lib.direct_dbc_algorithm import DirectDBCAlgorithm
from lib.dbc_file import DBC


def _read_test_data_lines(file_name: str):
    with open(os.path.join(SCRIPT_DIR, 'data', file_name)) as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


class GameTestData:
    def __init__(self, link_pattern: str, data: dict[int,int]):
        self._link_pattern = link_pattern
        self._data = data

    @classmethod
    def from_file(cls, file_name: str) -> list[Self]:
        result = []
        link_pattern = None
        data = {}
        for line in _read_test_data_lines(file_name):
            if line.startswith("|"):
                if link_pattern:
                    result.append(cls(link_pattern, data))
                link_pattern = line
                data = {}
            else:
                id, value = line.split(',')
                data[int(id)] = int(value)
        if link_pattern:
            result.append(cls(link_pattern, data))
        return result

    def get_link(self, id: int) -> str:
        return self._link_pattern.format(id)

    def get_value(self, id: int, default: int) -> int:
        return self._data.get(id, default)


@dataclass
class GameTestLink:
    link: str
    expected: int

    @classmethod
    def from_file(cls, file_name: str) -> list[Self]:
        result = []
        for line in _read_test_data_lines(file_name):
            match = re.search(r"^([0-9]+),(.+)$", line)
            assert match
            expected, link = match.groups()
            result.append(cls(link, int(expected)))
        return result


class LuaAlgorithm(Algorithm):
    def __init__(self, build: str):
        lua_data_path = os.path.join('.cache', build, 'addon_data.lua')
        self._proc = subprocess.Popen(
            ['lua', os.path.join(SCRIPT_DIR, 'test_runner.lua'), lua_data_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
        )
        assert self._proc.stdin and self._proc.stdout
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout

    def _get_item_info(self, item_id):
        raise NotImplementedError

    def _process(self, item):
        raise NotImplementedError

    def process_item(self, link: str) -> int:
        self._stdin.write(link + "\n")
        self._stdin.flush()
        return int(self._stdout.readline().strip())

    def bonus_string_round_trip(self, link: str) -> tuple[int, str | None, int | None]:
        self._stdin.write(f"BONUS_RT:{link}\n")
        self._stdin.flush()
        parts = self._stdout.readline().strip().split("\t")
        item_level = int(parts[0])
        if parts[1] == "NIL":
            return item_level, None, None
        return item_level, parts[1], int(parts[2])

    def close(self):
        self._stdin.close()
        self._proc.wait()


class Test:
    def __init__(self, build: str):
        self._build = build
        self._dbc = DBC(build)

    def main(self, algorithms: list[str]):
        if 'dbc' in algorithms:
            logging.info("Testing direct DBC algorithm...")
            dbc_algorithm = DirectDBCAlgorithm(self._dbc)
            self._test_algorithm(dbc_algorithm)

        if 'addon' in algorithms:
            logging.info("Testing addon data algorithm...")
            addon_algorithm = AddonDataAlgorithm(self._build)
            self._test_algorithm(addon_algorithm)

        if 'lua' in algorithms:
            logging.info("Testing Lua algorithm...")
            lua_algorithm = LuaAlgorithm(self._build)
            self._test_algorithm(lua_algorithm)
            self._test_bonus_string(lua_algorithm)
            lua_algorithm.close()

    def _test_algorithm(self, algorithm: Algorithm):
        logging.info("Checking single bonus ID test data...")
        for data in GameTestData.from_file("single_bonus_id.txt"):
            default_item_level = algorithm.process_item(data.get_link(0))
            for bonus_id in range(14000):
                # There's a bug in the DBC where 3524 has a "No Bonus" bonus but in reality is a "Scaling Config"
                # bonus which acts the same as 7753.
                if bonus_id == 3524:
                    bonus_id = 7753
                item_level = algorithm.process_item(data.get_link(bonus_id))
                expected = data.get_value(bonus_id, default_item_level)
                if item_level != expected:
                    logging.error("bonusId=%d, itemLevel=%d, expected=%d", bonus_id, item_level, expected)

        logging.info("Checking content tuning ID test data...")
        for data in GameTestData.from_file("content_tuning_id.txt"):
            default_item_level = algorithm.process_item(data.get_link(0))
            for content_tuning_id in range(7000):
                item_level = algorithm.process_item(data.get_link(content_tuning_id))
                expected = data.get_value(content_tuning_id, default_item_level)
                if item_level != expected:
                    logging.error("contentTuningId=%d, itemLevel=%d, expected=%d", content_tuning_id, item_level, expected)

        logging.info("Checking test links...")
        for data in GameTestLink.from_file("links.txt"):
            item_level = algorithm.process_item(data.link)
            if item_level != data.expected:
                logging.error("link='%s', itemLevel=%d, expected=%d", data.link, item_level, data.expected)

    def _test_bonus_string(self, lua_algorithm: LuaAlgorithm):
        logging.info("Checking bonus string round-trip...")
        for data in GameTestLink.from_file("links.txt"):
            item_level, bonus_string, rt_level = lua_algorithm.bonus_string_round_trip(data.link)
            if bonus_string is None:
                logging.error("GetBonusStringForLevel(%d) returned nil, link='%s'", item_level, data.link)
            elif rt_level != item_level:
                logging.error("bonusString=%s, level=%d, got=%d, link='%s'", bonus_string, item_level, rt_level, data.link)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test item level algorithms against game data')
    parser.add_argument('build', help='Game build version (e.g. 12.0.1.65893)')
    parser.add_argument('-a', '--algorithm', choices=['dbc', 'addon', 'lua', 'all'], default='all',
                        help='Which algorithm to test (default: all)')
    args = parser.parse_args()

    logging.basicConfig(
        level = logging.INFO,
        format = '%(asctime)s.%(msecs)03d %(levelname)s [%(module)s:%(lineno)d] %(message)s',
        datefmt = '%H:%M:%S'
    )

    algorithms = ['dbc', 'addon', 'lua'] if args.algorithm == 'all' else [args.algorithm]
    test = Test(args.build)
    test.main(algorithms)
