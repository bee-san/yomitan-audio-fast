from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys

from pathlib import Path


PACKAGE_NAME = "_local_audio_fast_addon"


def load_addon(root: Path):
    root = root.resolve(strict=True)
    os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
    specification = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load add-on package from {root}")
    package = importlib.util.module_from_spec(specification)
    sys.modules[PACKAGE_NAME] = package
    specification.loader.exec_module(package)
    return package


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the exact optimized Anki add-on server without starting Anki."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--lookup-mode", choices=("sqlite", "memory"))
    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument("--build-pack", action="store_true")
    build_group.add_argument(
        "--import-rust-bundle",
        type=Path,
        help="Build the add-on row index and hardlink a compiled Rust bundle pack.",
    )
    parser.add_argument("--workers", type=int)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    load_addon(args.root)
    config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
    server_module = importlib.import_module(f"{PACKAGE_NAME}.server")
    pack_module = importlib.import_module(f"{PACKAGE_NAME}.fast_pack")
    util_module = importlib.import_module(f"{PACKAGE_NAME}.util")
    config = config_module.get_server_config()
    db_path = (args.db or util_module.get_db_path()).resolve()
    pack_root = args.root.resolve() / "user_files" / "fast_audio"
    if args.build_pack:
        result = pack_module.build_audio_pack(
            db_path,
            pack_root,
            config_module.ALL_SOURCES,
            callback=lambda message: print(message, flush=True),
            workers=args.workers,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    elif args.import_rust_bundle is not None:
        result = pack_module.import_rust_bundle(
            db_path,
            pack_root,
            args.import_rust_bundle,
            callback=lambda message: print(message, flush=True),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if args.build_only:
        return
    port = args.port if args.port is not None else config["port"]
    runtime = server_module.ServerRuntime(
        host=args.host,
        port=port,
        db_path=db_path,
        pack_root=pack_root,
        lookup_mode=args.lookup_mode or config["lookup_mode"],
        response_cache_size=config["response_cache_entries"],
        row_cache_size=config["row_cache_entries"],
    )
    print(f"Local Audio Fast ready: {runtime.base_url}/", flush=True)
    print(f"Health: {runtime.base_url}/healthz", flush=True)
    print(
        f"Yomitan URL: {runtime.base_url}/?term={{term}}&reading={{reading}}",
        flush=True,
    )
    try:
        runtime.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
