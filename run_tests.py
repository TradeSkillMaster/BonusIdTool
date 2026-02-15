#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
import logging
import os
import re
import subprocess
from typing import Self

from lib.algorithm import Algorithm
from lib.addon_data_algorithm import AddonDataAlgorithm
from lib.direct_dbc_algorithm import DirectDBCAlgorithm
from lib.dbc_file import DBC

MAX_BONUS_ID = 13671
MAX_CONTENT_TUNING_ID = 7000


class GameTestData:
    def __init__(self, link_pattern: str, data: dict[int,int]):
        self._link_pattern = link_pattern
        self._data = data

    @classmethod
    def from_file(cls, path: str) -> list[Self]:
        result = []
        with open(path) as f:
            link_pattern = None
            data = {}
            for line in f.readlines():
                line = line.strip()
                if not line:
                    continue
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

    def get_link(self, id: int) -> int:
        return self._link_pattern.format(id)

    def get_value(self, id: int, default: int) -> int:
        return self._data.get(id, default)


@dataclass
class GameTestLink:
    link: str
    expected: int

    @classmethod
    def from_file(cls, path: str) -> list[Self]:
        result = []
        with open(path) as f:
            for line in f.readlines():
                line = line.strip()
                if not line:
                    continue
                expected, link = re.search(r"^([0-9]+),(.+)$", line).groups()
                result.append(cls(link, int(expected)))
        return result


class LuaAlgorithm(Algorithm):
    def __init__(self, dbc: DBC, build: str):
        self._dbc = dbc
        lua_data_path = os.path.join('.cache', build, 'addon_data.lua')
        self._proc = subprocess.Popen(
            ['lua', 'lua/test_runner.lua', lua_data_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def process_item(self, link: str) -> int:
        base_item_level, has_midnight_scaling = self._dbc.item_sparse.get_info(self.get_item_id_from_link(link))
        line = f"{link}\t{base_item_level}\t{1 if has_midnight_scaling else 0}\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        return int(self._proc.stdout.readline().strip())

    def close(self):
        self._proc.stdin.close()
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
            addon_algorithm = AddonDataAlgorithm(self._dbc, self._build)
            self._test_algorithm(addon_algorithm)

        if 'lua' in algorithms:
            logging.info("Testing Lua algorithm...")
            lua_algorithm = LuaAlgorithm(self._dbc, self._build)
            self._test_algorithm(lua_algorithm)
            lua_algorithm.close()

    def _test_algorithm(self, algorithm: Algorithm):
        logging.info("Checking single bonus ID test data...")
        for data in GameTestData.from_file("test_data/single_bonus_id.txt"):
            default_item_level = algorithm.process_item(data.get_link(0))
            for bonus_id in range(MAX_BONUS_ID + 1):
                # There's a bug in the DBC where 3524 has a "No Bonus" bonus but in reality is a "Scaling Config"
                # bonus which acts the same as 7753.
                if bonus_id == 3524:
                    bonus_id = 7753
                item_level = algorithm.process_item(data.get_link(bonus_id))
                expected = data.get_value(bonus_id, default_item_level)
                if item_level != expected:
                    logging.error("bonusId=%d, itemLevel=%d, expected=%d", bonus_id, item_level, expected)

        logging.info("Checking content tuning ID test data...")
        for data in GameTestData.from_file("test_data/content_tuning_id.txt"):
            default_item_level = algorithm.process_item(data.get_link(0))
            for content_tuning_id in range(MAX_CONTENT_TUNING_ID + 1):
                item_level = algorithm.process_item(data.get_link(content_tuning_id))
                expected = data.get_value(content_tuning_id, default_item_level)
                if item_level != expected:
                    logging.error("contentTuningId=%d, itemLevel=%d, expected=%d", content_tuning_id, item_level, expected)

        logging.info("Checking test links...")
        for data in GameTestLink.from_file("test_data/links.txt"):
            item_level = algorithm.process_item(data.link)
            if item_level != data.expected:
                logging.error("link='%s', itemLevel=%d, expected=%d", data.link, item_level, data.expected)


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
