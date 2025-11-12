# Paste and run this in a Kaggle Python cell
import os, sys, json, struct, shutil, subprocess
from pathlib import Path

DATASET_ID = "plateau-13113-shibuya-ku-2023"
TARGET_BBOX = (139.6996, 35.6588, 139.7014, 35.6602)  # 必要に応じて調整
OUT_DIR = Path("/kaggle/working/output_glb")
ZIP_NAME = Path("/kaggle/working/output_glb.zip")

def bbox_intersect(region, target_bbox):
    west, south, east, north = region[0], region[1], region[2], region[3]
    lon_min, lat_min, lon_max, lat_max = target_bbox
    if lon_max < west or lon_min > east: return False
    if lat_max < south or lat_min > north: return False
    return True

def extract_b3dm_to_glb_bytes(data: bytes):
    if len(data) < 28: raise ValueError("b3dm too small")
    magic = data[0:4].decode('ascii', errors='ignore')
    if magic != 'b3dm': raise ValueError("not b3dm")
    ftJsonLen = struct.unpack_from('<I', data, 12)[0]
    ftBinLen = struct.unpack_from('<I', data, 16)[0]
    btJsonLen = struct.unpack_from('<I', data, 20)[0]
    btBinLen = struct.unpack_from('<I', data, 24)[0]
    glb_offset = 28 + ftJsonLen + ftBinLen + btJsonLen + btBinLen
    byteLength = struct.unpack_from('<I', data, 8)[0]
    end = byteLength if (byteLength <= len(data) and byteLength > glb_offset) else len(data)
    return data[glb_offset:end]

OUT_DIR.mkdir(parents=True, exist_ok=True)

# try to locate dataset root
from plateaukit import load_dataset
ds = load_dataset(DATASET_ID)
dataset_root = None
for attr in ("root","path","root_path","_root"):
    if hasattr(ds, attr):
        candidate = getattr(ds, attr)
        if isinstance(candidate, str): dataset_root = Path(candidate).resolve(); break
        if isinstance(candidate, Path): dataset_root = candidate.resolve(); break

if dataset_root is None:
    # fallback search
    home = Path.home()
    for p in home.rglob("tileset.json"):
        if DATASET_ID in str(p):
            dataset_root = p.parent
            break

if dataset_root is None:
    raise SystemExit("dataset root not found. Check plateaukit install or plateaukit info")

print("Dataset root:", dataset_root)

tileset_paths = list(dataset_root.rglob("tileset.json"))
print("Found tileset.json:", len(tileset_paths))

extracted = 0
processed = set()

def process_tile(tile, base_path):
    global extracted
    bv = tile.get("boundingVolume", {})
    region = bv.get("region")
    intersects = True
    if region:
        intersects = bbox_intersect(region, TARGET_BBOX)
    content = tile.get("content")
    if intersects and content:
        uri = content.get("uri") or content.get("url")
        if uri:
            absolute = (base_path / uri).resolve()
            if absolute.exists():
                if absolute.suffix.lower() == ".b3dm" and absolute not in processed:
                    processed.add(absolute)
                    try:
                        data = absolute.read_bytes()
                        glb = extract_b3dm_to_glb_bytes(data)
                        outp = OUT_DIR / (absolute.stem + ".glb")
                        outp.write_bytes(glb)
                        print("Extracted:", outp)
                        extracted += 1
                    except Exception as e:
                        print("Error extracting", absolute, e)
                elif absolute.suffix.lower() in (".glb", ".gltf") and absolute not in processed:
                    processed.add(absolute)
                    dst = OUT_DIR / absolute.name
                    shutil.copy2(absolute, dst)
                    print("Copied:", dst)
                    extracted += 1
    for child in tile.get("children", []):
        process_tile(child, base_path)

for ts in tileset_paths:
    try:
        js = json.loads(ts.read_text(encoding='utf-8'))
    except Exception as e:
        print("Parse error", ts, e); continue
    root = js.get("root")
    if root:
        process_tile(root, ts.parent)
    else:
        for t in js.get("tiles", []):
            process_tile(t, ts.parent)

print("Extracted count:", extracted)

if ZIP_NAME.exists(): ZIP_NAME.unlink()
shutil.make_archive(str(ZIP_NAME.with_suffix('')), 'zip', root_dir=OUT_DIR)
print("Zipped output to:", ZIP_NAME)
