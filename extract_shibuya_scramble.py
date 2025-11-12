#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_shibuya_scramble.py

目的：
- PLATEAU の渋谷区データセット (plateau-13113-shibuya-ku-2023) をインストール（plateaukit）
- tileset.json を再帰的に走査し、スクランブル交差点付近のタイルと重なる .b3dm/.glb を抽出
- .b3dm から埋め込み GLB を取り出して ./output_glb/ に保存
- 最後に output_glb.zip を作成

使い方（Colab 推奨）：
1) Colab の最初のセルでこのファイルをアップロードするか、内容を新しいセルに貼る。
2) 実行： python3 extract_shibuya_scramble.py
   （Colab の場合は !python3 extract_shibuya_scramble.py として実行）
注意：
- Colab 環境で実行する例を想定しています。ローカルでも動きます。
- 実行に時間がかかります（データのダウンロードや prebuild によって10分〜数十分）。
"""
import os
import sys
import json
import struct
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urljoin

# ======== 設定 ========
DATASET_ID = "plateau-13113-shibuya-ku-2023"
# スクランブル交差点周辺の検索 bbox（よく使う目安）
# lon_min, lat_min, lon_max, lat_max
TARGET_BBOX = (139.6996, 35.6588, 139.7014, 35.6602)
OUT_DIR = Path.cwd() / "output_glb"
ZIP_NAME = Path.cwd() / "output_glb.zip"

# ======== ユーティリティ ========
def must(cmd):
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd)

def bbox_intersect(region, target_bbox):
    # region: [west, south, east, north, minZ, maxZ] (Cesium 3D Tiles region)
    # target_bbox: (lon_min, lat_min, lon_max, lat_max)
    west, south, east, north = region[0], region[1], region[2], region[3]
    lon_min, lat_min, lon_max, lat_max = target_bbox
    # 矩形同士の当たり判定（経度・緯度が同一座標系）
    if lon_max < west or lon_min > east: return False
    if lat_max < south or lat_min > north: return False
    return True

def extract_b3dm_to_glb_bytes(data: bytes):
    # b3dm header parse and return glb bytes
    if len(data) < 28:
        raise ValueError("b3dm too small")
    magic = data[0:4].decode('ascii', errors='ignore')
    if magic != 'b3dm':
        raise ValueError("not b3dm")
    version = struct.unpack_from('<I', data, 4)[0]
    byteLength = struct.unpack_from('<I', data, 8)[0]
    ftJsonLen = struct.unpack_from('<I', data, 12)[0]
    ftBinLen = struct.unpack_from('<I', data, 16)[0]
    btJsonLen = struct.unpack_from('<I', data, 20)[0]
    btBinLen = struct.unpack_from('<I', data, 24)[0]
    glb_offset = 28 + ftJsonLen + ftBinLen + btJsonLen + btBinLen
    end = byteLength if (byteLength <= len(data) and byteLength > glb_offset) else len(data)
    return data[glb_offset:end]

# ======== メイン処理 ========
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) plateaukit をインストール（Colab など初回実行で必要）
    try:
        import plateaukit
    except Exception:
        print("plateaukit が見つからないためインストールします...")
        must([sys.executable, "-m", "pip", "install", "-q", "plateaukit[all]"])
        # re-import
        try:
            import plateaukit
        except Exception as e:
            print("plateaukit のインポートに失敗しました:", e)
            sys.exit(1)

    # 2) plateaukit コマンドでデータセットをインストール / prebuild
    # (インストール済みならスキップされます)
    try:
        print("Installing dataset (this may take some minutes)...")
        must(["plateaukit", "install", DATASET_ID, "-y"])
        # prebuild bldg/tran/brid を作る（なくても .b3dm は存在するはず）
        print("Running prebuild (bldg/tran/brid)...")
        must(["plateaukit", "prebuild", DATASET_ID, "-t", "bldg", "-t", "tran", "-t", "brid", "-y"])
    except subprocess.CalledProcessError as e:
        print("plateaukit コマンドの実行でエラーが発生しました:", e)
        print("既にインストール済みなら続行します...")

    # 3) Python API で dataset オブジェクトを取得して、実ファイル配置ディレクトリを探す
    from plateaukit import load_dataset
    print("Loading dataset via plateaukit API...")
    ds = load_dataset(DATASET_ID)
    # try to detect root path from dataset object
    dataset_root = None
    for attr in ("root", "path", "root_path", "_root"):
        if hasattr(ds, attr):
            candidate = getattr(ds, attr)
            if isinstance(candidate, str):
                dataset_root = Path(candidate).expanduser().resolve()
                break
            elif isinstance(candidate, Path):
                dataset_root = candidate.resolve()
                break
    if dataset_root is None:
        # fallback: try common install locations printed by plateaukit config OR search home dir for dataset id
        print("dataset.root not found on object. Searching filesystem for tileset.json under approximate locations...")
        home = Path.home()
        # known possible roots: ~/.plateaukit, ~/.cache/plateaukit, /root/.plateaukit
        candidates = [
            home / ".plateaukit",
            home / ".cache" / "plateaukit",
            Path("/root/.plateaukit"),
            Path.cwd(),
        ]
        found = None
        for base in candidates:
            if not base.exists():
                continue
            # search for tileset.json under this base (limit depth)
            for p in base.rglob("tileset.json"):
                # check the path contains dataset id
                if DATASET_ID in str(p):
                    found = p.parent
                    break
            if found:
                dataset_root = found
                break
        if dataset_root is None:
            # brute-force search under home (may be slow)
            for p in home.rglob("tileset.json"):
                if DATASET_ID in str(p):
                    dataset_root = p.parent
                    break

    if dataset_root is None:
        print("dataset の配置場所を見つけられませんでした。`plateaukit info` の出力やインストール先を教えてください。")
        sys.exit(1)

    print("Detected dataset root:", dataset_root)

    # 4) tileset.json を再帰的に探し、対象 bbox と交差するタイルを列挙して .b3dm/.glb を抽出
    tileset_paths = list(dataset_root.rglob("tileset.json"))
    print(f"Found {len(tileset_paths)} tileset.json under dataset root.")
    extracted = 0
    processed_files = set()

    def process_tile(tile, base_path):
        nonlocal extracted
        # tile may have 'content' with 'uri', and optional 'boundingVolume'
        bv = tile.get("boundingVolume", {})
        region = bv.get("region")
        intersects = True
        if region:
            intersects = bbox_intersect(region, TARGET_BBOX)
        # If intersects, process content
        content = tile.get("content")
        if intersects and content:
            uri = content.get("uri") or content.get("url") or content.get("uri")
            if uri:
                # resolve relative to base_path (tileset.json directory)
                absolute = (base_path / uri).resolve()
                # if points to a directory (e.g., tilesets/<id>/...), handle
                if absolute.exists():
                    # if it's a b3dm file, extract; if it's glb/gltf, copy
                    if absolute.suffix.lower() == ".b3dm":
                        if absolute not in processed_files:
                            processed_files.add(absolute)
                            try:
                                data = absolute.read_bytes()
                                glb_bytes = extract_b3dm_to_glb_bytes(data)
                                out_path = OUT_DIR / (absolute.stem + ".glb")
                                out_path.write_bytes(glb_bytes)
                                print("Extracted:", out_path)
                                extracted += 1
                            except Exception as e:
                                print("Failed to extract from", absolute, e)
                    elif absolute.suffix.lower() in (".glb", ".gltf"):
                        if absolute not in processed_files:
                            processed_files.add(absolute)
                            dst = OUT_DIR / absolute.name
                            shutil.copy2(absolute, dst)
                            print("Copied:", dst)
                            extracted += 1
                else:
                    # maybe uri is an absolute URL or relative path using ../ ; try to normalize
                    candidate = (base_path / uri).resolve()
                    if candidate.exists():
                        # handle above
                        if candidate.suffix.lower() == ".b3dm":
                            if candidate not in processed_files:
                                processed_files.add(candidate)
                                try:
                                    data = candidate.read_bytes()
                                    glb_bytes = extract_b3dm_to_glb_bytes(data)
                                    out_path = OUT_DIR / (candidate.stem + ".glb")
                                    out_path.write_bytes(glb_bytes)
                                    print("Extracted:", out_path)
                                    extracted += 1
                                except Exception as e:
                                    print("Failed to extract from", candidate, e)
                        elif candidate.suffix.lower() in (".glb", ".gltf"):
                            if candidate not in processed_files:
                                processed_files.add(candidate)
                                dst = OUT_DIR / candidate.name
                                shutil.copy2(candidate, dst)
                                print("Copied:", dst)
                                extracted += 1
                    else:
                        # Could be remote URL -- skip (plateaukit stores locally, so rarely here)
                        pass

        # recursive children
        for child in tile.get("children", []):
            process_tile(child, base_path)

    for ts_path in tileset_paths:
        try:
            js = json.loads(ts_path.read_text(encoding='utf-8'))
        except Exception as e:
            print("Failed to parse tileset:", ts_path, e)
            continue
        root_tile = js.get("root")
        if not root_tile:
            # older tileset uses 'tiles' top-level
            if "tiles" in js:
                # handle each tile object in tiles list
                for tile in js.get("tiles", []):
                    process_tile(tile, ts_path.parent)
            continue
        # process recursively starting at root
        process_tile(root_tile, ts_path.parent)

    print(f"Extraction finished. {extracted} glb files saved to {OUT_DIR}")

    # 5) zip the output
    if ZIP_NAME.exists():
        ZIP_NAME.unlink()
    shutil.make_archive(str(ZIP_NAME.with_suffix('')), 'zip', root_dir=OUT_DIR)
    print("Zipped to:", ZIP_NAME)

    print("Done. Download output_glb.zip from the runtime filesystem.")
    print("If you want, upload the zip here and I can inspect and (必要なら) convert to .gltf or merge models into one glb.")

if __name__ == "__main__":
    main()