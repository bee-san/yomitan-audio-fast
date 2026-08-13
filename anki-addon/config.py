"""
Source schema:

- type: "jpod" | "nhk" | "forvo" | "ajt_jp" | "ozk5" | "flat"
    - ajt_jp is short for AJT Japanese
    - flat is for sources that ship only audio files, with the file stem as the expression
    - If you ever create a new source, I recommend following the AJT Japanese schema, as it's well defined compared to the others
- id: string id used as the id in the "source" column, as well as the parameter in the url
- path: string, path to the source files
- display: string used to display in Yomichan. Uses %s for the "DISPLAY" column.
"""

import json
from pathlib import Path
from typing import TypedDict, Final, Type

from .source.jpod import JPodAudioSource
from .source.nhk16 import NHK16AudioSource
from .source.forvo import ForvoAudioSource
from .source.ajt_jp import AJTJapaneseSource
from .source.ozk5 import OZK5AudioSource
from .source.flat import FlatDirAudioSource

from .consts import CONFIG_FILE_NAME, DEFAULT_CONFIG_FILE_NAME
from .util import get_program_root_path
from .source.audio_source import AudioSource, AudioSourceData


SOURCE_TYPES: Final[dict[str, Type[AudioSource]]] = {
    "jpod": JPodAudioSource,
    "nhk": NHK16AudioSource,
    "forvo": ForvoAudioSource,
    "ajt_jp": AJTJapaneseSource,
    "ozk5": OZK5AudioSource,
    "flat": FlatDirAudioSource,
}


class JsonConfigSource(TypedDict):
    type: str
    id: str
    path: str
    display: str


class JsonServerConfig(TypedDict):
    port: int
    lookup_mode: str
    response_cache_entries: int
    row_cache_entries: int


class JsonConfig(TypedDict):
    server: JsonServerConfig
    sources: list[JsonConfigSource]


def get_default_config_path():
    return get_program_root_path().joinpath(DEFAULT_CONFIG_FILE_NAME)


def get_config_path():
    return get_program_root_path().joinpath(CONFIG_FILE_NAME)


def merge_sources(
    default_sources: list, user_sources: object
) -> list[JsonConfigSource]:
    """User entries win, but defaults missing from a pinned user list are appended.

    Without this, anyone whose `user_files/config.json` was written before a new
    built-in source existed would never see it, and `entries.db` rows from it would
    look unconfigured.
    """
    if not isinstance(user_sources, list):
        return list(default_sources)
    merged = list(user_sources)
    known = {item.get("id") for item in merged if isinstance(item, dict)}
    merged.extend(item for item in default_sources if item.get("id") not in known)
    return merged


def read_config() -> JsonConfig:
    """
    read default config, unless user config is found
    """
    default_config_path = get_default_config_path()
    with open(default_config_path, encoding="utf-8") as f:
        config = json.load(f)

    default_sources = config.get("sources", [])
    config_path = get_config_path()

    if config_path.is_file():
        with open(config_path, encoding="utf-8") as f:
            user_config = json.load(f)
            for k, v in user_config.items():
                if k == "server":
                    if isinstance(v, dict):
                        config.setdefault("server", {}).update(v)
                    continue
                config[k] = v

    config["sources"] = merge_sources(default_sources, config.get("sources"))

    return config


def get_server_config() -> JsonServerConfig:
    config = read_config().get("server", {})
    if not isinstance(config, dict):
        config = {}
    try:
        port = int(config.get("port", 5050))
    except (TypeError, ValueError):
        port = 5050
    if not 1 <= port <= 65535:
        port = 5050
    lookup_mode = config.get("lookup_mode", "sqlite")
    if lookup_mode not in ("sqlite", "memory"):
        lookup_mode = "sqlite"

    def cache_size(name: str, default: int) -> int:
        try:
            value = int(config.get(name, default))
        except (TypeError, ValueError):
            return default
        return min(262144, max(0, value))

    return JsonServerConfig(
        port=port,
        lookup_mode=lookup_mode,
        response_cache_entries=cache_size("response_cache_entries", 16384),
        row_cache_entries=cache_size("row_cache_entries", 16384),
    )


def get_all_sources() -> dict[str, AudioSource]:
    """
    note: insertion order is important for this to work
    """
    sources = {}
    config = read_config()
    for source_json in config["sources"]:
        id = source_json["id"]
        type = source_json["type"]
        path = source_json["path"]
        display = source_json["display"]

        # checks for source_meta.json
        source_meta_path = get_program_root_path() / path / "source_meta.json"
        if source_meta_path.is_file():
            with open(source_meta_path, encoding="utf-8") as f:
                source_meta = json.load(f)
                meta_type = source_meta.get("type", None)
                if meta_type is not None:
                    type = meta_type

        AudioSourceClass = SOURCE_TYPES.get(type)
        if AudioSourceClass is None:
            # an unknown type must not take the whole add-on down at import time
            print(f"(config) skipping source {id!r} with unknown type {type!r}")
            continue
        data = AudioSourceData(id, path, display)
        source = AudioSourceClass(data)
        sources[id] = source
    return sources


ALL_SOURCES = get_all_sources()
