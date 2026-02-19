from abc import ABC, abstractmethod
from csv import DictReader
from enum import Enum, EnumMeta
import inspect
import logging
import os
import pickle
import requests
from typing import Any, Generic, Type, TypeVar

_DUMMY_CLASS_MEMBERS = dir(type('dummy', (ABC,), {}))

def download_csv(table_name: str, build: str) -> list[dict[str,str]]:
    cache_dir_path = os.path.join('.cache', build)
    os.makedirs(cache_dir_path, exist_ok=True)
    csv_path = os.path.join(cache_dir_path, f'{table_name}.csv')
    if os.path.exists(csv_path):
        logging.info("Building from cached CSV (table_name=%s, build=%s)", table_name, build)
        with open(csv_path, 'r', encoding='utf-8') as f:
            data = f.read()
    else:
        logging.info("Downloading DBC data (table_name=%s, build=%s)", table_name, build)
        res = requests.get(f"https://wago.tools/db2/{table_name}/csv?build={build}")
        assert res.status_code == 200
        data = res.content.decode('utf-8')
        with open(csv_path, 'w', encoding='utf-8') as f:
            f.write(data)
    return list(DictReader(data.splitlines(), delimiter=','))


def get_latest_build() -> str:
    res = requests.get("https://wago.tools/api/builds/wow/latest")
    res.raise_for_status()
    return res.json()["version"]


class DBCType(ABC):
    ID: int

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        members_def = {item[0]: item[1] for item in inspect.getmembers(cls) if item[0] not in _DUMMY_CLASS_MEMBERS}
        annotations = members_def.pop('__annotations__')
        assert 'ID' not in annotations
        cls.__annotations = annotations

    def __init__(self, values: dict[str,str]):
        self.ID = int(values['ID'])
        for field, field_type in self._get_annotations().items():
            value = values[field]
            if field_type.__class__ == EnumMeta:
                value = int(value)
            value = field_type(value)
            setattr(self, field, value)

    def __repr__(self):
        fields_str = ', '.join([f"ID={self.ID}"] + [f"{field}={getattr(self, field)}" for field in self._get_annotations()])
        return f"{self.__class__.__name__}({fields_str})"

    @classmethod
    def _get_annotations(cls) -> dict[str,Any]:
        return cls.__annotations

class DBCTypeOneToOne(DBCType, ABC):
    pass

class DBCTypeOneToMany(DBCType, ABC):
    class Index(int):
        pass

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        index_fields = [k for k, v in cls._get_annotations().items() if v == DBCTypeOneToMany.Index]
        assert len(index_fields) == 1
        cls.__index_field = index_fields[0]

    def _get_index_value(self) -> Index:
        return getattr(self, self.__index_field)

_EntryType = TypeVar('_EntryType', bound='DBCType')
class DBCFile(Generic[_EntryType], ABC):
    EntryType: Type[_EntryType]

    def __init__(self, build: str):
        table_name = self.EntryType.__name__
        cache_dir_path = os.path.join('.cache', build)
        os.makedirs(cache_dir_path, exist_ok=True)
        pickle_path = os.path.join(cache_dir_path, f'{table_name}.pkl')
        if os.path.exists(pickle_path):
            with open(pickle_path, 'rb') as f:
                self._entries = pickle.load(f)
        else:
            entries = [self.EntryType(r) for r in download_csv(table_name, build)]
            self._entries = self._build_entries(entries)
            with open(pickle_path, 'wb') as f:
                pickle.dump(self._entries, f)

    @abstractmethod
    def _build_entries(self, entries: list[_EntryType]) -> Any:
        pass

_EntryTypeOneToOne = TypeVar('_EntryTypeOneToOne', bound='DBCTypeOneToOne')
class DBCFileOneToOne(DBCFile[_EntryTypeOneToOne], Generic[_EntryTypeOneToOne], ABC):
    EntryType: Type[_EntryTypeOneToOne]

    def __init_subclass__(cls, **kwargs):
        assert issubclass(cls.EntryType, DBCTypeOneToOne)
        super().__init_subclass__(**kwargs)

    def _build_entries(self, entries):
        return {entry.ID: entry for entry in entries}

    def get(self, id: int) -> _EntryTypeOneToOne:
        return self._entries[id]

    def has(self, id: int) -> bool:
        return id in self._entries

_EntryTypeOneToMany = TypeVar('_EntryTypeOneToMany', bound='DBCTypeOneToMany')
class DBCFileOneToMany(DBCFile[_EntryTypeOneToMany], Generic[_EntryTypeOneToMany], ABC):
    EntryType: Type[_EntryTypeOneToMany]

    def __init_subclass__(cls, **kwargs):
        assert issubclass(cls.EntryType, DBCTypeOneToMany)
        super().__init_subclass__(**kwargs)

    def _build_entries(self, entries):
        result: dict[int,list[_EntryTypeOneToMany]] = {}
        for entry in entries:
            index_value = entry._get_index_value()
            if index_value not in result:
                result[index_value] = []
            result[index_value].append(entry)
        return result

    def get(self, id: int) -> list[_EntryTypeOneToMany]:
        return self._entries[id] if id in self._entries else []

class ItemBonusType(Enum):
    NONE = 0
    INCREASE_ITEM_LEVEL = 1
    INCREASE_BONUS_STAT = 2
    SET_ITEM_QUALITY = 3
    ADD_TOOLTIP_LINE = 4
    NAME_SUFFIX = 5
    ADD_SOCKETS = 6
    SET_APPEARANCE_MODIFIER = 7
    RECEIVE_TOAST = 9
    MULTIPLY_REPAIR_COST = 10
    STAT_SCALING = 11
    DISENCHANT_LOOT = 12
    STAT_FIXED = 13
    PREVIEW_ITEM_PYRAMID = 14
    CLIENT_PREVIEW = 15
    SET_BIND_TYPE = 16
    SECONDARY_RELIC_POWER_LABEL = 17
    SET_REQUIRED_LEVEL = 18
    AZERITE_TIER_UNLOCK_SET_ID = 19
    SCRAPPING_LOOT_ID = 20
    CAN_DISENCHANT = 21
    ITEM_EFFECT_ID = 23
    UNKNOWN_1 = 24
    MODIFIED_CRAFTING_STAT = 25
    UNKNOWN_2 = 26
    REQ_LEVEL_CURVE = 27
    ICON_FILE_DATA_ID = 28
    UNKNOWN_3 = 29
    ITEM_DESCRIPTION = 30
    ITEM_NAME_DESCRIPTION_ID = 31
    UNKNOWN_4 = 32
    GROUP_ID = 34
    ITEM_LIMIT_CATEGORY_ID = 35
    PVP_ITEM_LEVEL_INCREMENT = 36
    CONVERSION_ID = 37
    ITEM_HISTORY_SLOT = 38
    BASE_ITEM_LEVEL = 42
    PVP_ITEM_LEVEL = 43
    COSMETIC_SLOT = 44
    OVERRIDE_DESCRIPTION_COLOR = 45
    OVERRIDE_CANNOT_TRADE_BOP = 46
    BONDING_WITH_PRIORITY = 47
    OFFSET_CURVE = 48
    SCALING_CONFIG = 49
    APPLY_BONUS = 50
    SCALING_CONFIG_2 = 51
    CRAFTING_QUALITY = 52
    MIDNIGHT_ITEM_LEVEL = 53

    @property
    def sort_priority(self) -> int:
        if self in (ItemBonusType.STAT_SCALING, ItemBonusType.STAT_FIXED):
            return 1
        if self in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
            return 2
        if self == ItemBonusType.OFFSET_CURVE:
            return 3
        if self in (ItemBonusType.MIDNIGHT_ITEM_LEVEL, ItemBonusType.CRAFTING_QUALITY):
            return 4
        return 0

    @property
    def bonus_group(self) -> 'ItemBonusType':
        if self in (ItemBonusType.CRAFTING_QUALITY, ItemBonusType.MIDNIGHT_ITEM_LEVEL):
            return ItemBonusType.CRAFTING_QUALITY
        if self in (ItemBonusType.OFFSET_CURVE, ItemBonusType.SCALING_CONFIG,
                    ItemBonusType.SCALING_CONFIG_2, ItemBonusType.STAT_SCALING,
                    ItemBonusType.STAT_FIXED, ItemBonusType.BASE_ITEM_LEVEL):
            return ItemBonusType.SCALING_CONFIG
        return self

class CurveType(Enum):
    LINEAR = 0
    REQ_LEVEL_AND_ITEM_LEVEL = 5

class ContentTuning(DBCTypeOneToOne):
    Flags: int
    HPScalingCurveID: int
    MinLevelSquish: int
    MaxLevelSquish: int
    MinLevelScalingOffset: int
    MaxLevelScalingOffset: int
    AllowedMinOffset: int
    AllowedMaxOffset: int

class Curve(DBCTypeOneToOne):
    Type: int
    Flags: int

class CurvePoint(DBCTypeOneToMany):
    Pos_0: float
    Pos_1: float
    CurveID: DBCTypeOneToMany.Index

class ItemBonus(DBCTypeOneToMany):
    Value_0: int
    Value_1: int
    Value_2: int
    Value_3: int
    ParentItemBonusListID: DBCTypeOneToMany.Index
    Type: ItemBonusType

    @property
    def bonus_type(self) -> ItemBonusType:
        return ItemBonusType(self.Type)

    @property
    def dbc_priority(self) -> int | None:
        """Per-bonus priority from DBC Value_1, for types that use it. Lower = higher priority."""
        if self.bonus_type in (ItemBonusType.SCALING_CONFIG, ItemBonusType.SCALING_CONFIG_2):
            return self.Value_1
        return None

class ItemOffsetCurve(DBCTypeOneToOne):
    CurveID: int
    Offset: int

class ItemScalingConfig(DBCTypeOneToOne):
    ItemOffsetCurveID: int
    ItemLevel: int
    ItemSquishEraID: int
    Flags: int

class ItemSparse(DBCTypeOneToOne):
    Stackable: int
    ItemSquishEraID: int
    ItemLevel: int

class ConditionalContentTuning(DBCTypeOneToMany):
    OrderIndex: int
    RedirectContentTuningID: int
    RedirectFlag: int
    RedirectEnum: int
    ParentContentTuningID: DBCTypeOneToMany.Index

class ItemSquishEra(DBCTypeOneToOne):
    Patch: int
    CurveID: int
    Flags: int

class ConditionalContentTuningDBC(DBCFileOneToMany[ConditionalContentTuning]):
    EntryType = ConditionalContentTuning

class ContentTuningDBC(DBCFileOneToOne[ContentTuning]):
    EntryType = ContentTuning

class CurveDBC(DBCFileOneToOne[Curve]):
    EntryType = Curve

class CurvePointDBC(DBCFileOneToMany[CurvePoint]):
    EntryType = CurvePoint

    def find_points(self, id: int, value: float) -> tuple[CurvePoint, CurvePoint]:
        data = self.get(id)
        lower_bound = next((p for p in reversed(data) if p.Pos_0 <= value), None)
        upper_bound = next((p for p in data if p.Pos_0 >= value), None)
        lower_bound = lower_bound or upper_bound
        upper_bound = upper_bound or lower_bound
        assert lower_bound and upper_bound
        return (lower_bound, upper_bound)

class ItemBonusDBC(DBCFileOneToMany[ItemBonus]):
    EntryType = ItemBonus

class ItemOffsetCurveDBC(DBCFileOneToOne[ItemOffsetCurve]):
    EntryType = ItemOffsetCurve

class ItemScalingConfigDBC(DBCFileOneToOne[ItemScalingConfig]):
    EntryType = ItemScalingConfig

class ItemSparseDBC(DBCFileOneToOne[ItemSparse]):
    EntryType = ItemSparse

    def get_info(self, item_id: int) -> tuple[int, bool]:
        item_sparse = self.get(item_id)
        return (item_sparse.ItemLevel, item_sparse.ItemSquishEraID == 2)

class ItemSquishEraDBC(DBCFileOneToOne[ItemSquishEra]):
    EntryType = ItemSquishEra

    def get_midnight(self) -> ItemSquishEra:
        return self.get(2)

class DBC:
    def __init__(self, build: str):
        self._build = build
        self._conditional_content_tuning = None
        self._content_tuning = None
        self._curve = None
        self._curve_point = None
        self._item_bonus = None
        self._item_offset_curve = None
        self._item_scaling_config = None
        self._item_sparse = None
        self._item_squish_era = None

    @property
    def conditional_content_tuning(self) -> ConditionalContentTuningDBC:
        if not self._conditional_content_tuning:
            self._conditional_content_tuning = ConditionalContentTuningDBC(self._build)
        return self._conditional_content_tuning

    @property
    def content_tuning(self) -> ContentTuningDBC:
        if not self._content_tuning:
            self._content_tuning = ContentTuningDBC(self._build)
        return self._content_tuning

    @property
    def curve(self) -> CurveDBC:
        if not self._curve:
            self._curve = CurveDBC(self._build)
        return self._curve

    @property
    def curve_point(self) -> CurvePointDBC:
        if not self._curve_point:
            self._curve_point = CurvePointDBC(self._build)
        return self._curve_point

    @property
    def item_bonus(self) -> ItemBonusDBC:
        if not self._item_bonus:
            self._item_bonus = ItemBonusDBC(self._build)
        return self._item_bonus

    @property
    def item_offset_curve(self) -> ItemOffsetCurveDBC:
        if not self._item_offset_curve:
            self._item_offset_curve = ItemOffsetCurveDBC(self._build)
        return self._item_offset_curve

    @property
    def item_scaling_config(self) -> ItemScalingConfigDBC:
        if not self._item_scaling_config:
            self._item_scaling_config = ItemScalingConfigDBC(self._build)
        return self._item_scaling_config

    @property
    def item_sparse(self) -> ItemSparseDBC:
        if not self._item_sparse:
            self._item_sparse = ItemSparseDBC(self._build)
        return self._item_sparse

    @property
    def item_squish_era(self) -> ItemSquishEraDBC:
        if not self._item_squish_era:
            self._item_squish_era = ItemSquishEraDBC(self._build)
        return self._item_squish_era
