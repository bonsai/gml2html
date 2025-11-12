#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
変換.py
都市（PLATEAU 等）データを HTML5（WebGL/three.js）で扱いやすい glb/glTF 形式に変換して、
簡単な viewer HTML を出力するスクリプト。

できること（このスクリプトの範囲）：
- URL またはローカルのファイル/ディレクトリを受け取りダウンロード/展開
- .glb / .gltf があれば出力ディレクトリにコピー
- .obj/.ply/.stl 等のメッシュファイルは trimesh を使って glb に変換
- 3D Tiles の b3dm ファイルから埋め込まれた glb を抽出して保存
- 変換結果を表示するシンプルな three.js ビューア (index.html) を生成

注記：
- CityGML (.gml/.xml) から直接 glb に変換する処理は本スクリプトでは含めていません。
  CityGML の変換は専用ツール（FME、Blender + CityGML add-on、citygml-tools 等）を使うことを推奨します。
- このスクリプトはローカル変換の簡易化を目的としています。大規模データは処理時間／メモリに注意してください。

必要な Python ライブラリ:
pip install requests tqdm trimesh pygltflib
（ trimesh は更に numpy, networkx 等を依存します。OS によっては追加のライブラリが必要です）
"""

import argparse
import os
import sys
import shutil
import tempfile
import zipfile
import struct
import logging
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    from tqdm import tqdm
    import trimesh
except Exception as e:
    print("必要なライブラリが見つかりません。以下をインストールしてください：")
    print("  pip install requests tqdm trimesh pygltflib")
    raise

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')


def download_url(url: str, dest_folder: Path) -> Path:
    """URL をダウンロードして dest_folder に保存。ファイル名は URL 由来。"""
    logging.info(f"Downloading: {url}")
    dest_folder.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path) or "downloaded"
    dest = dest_folder / filename

    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(total=total, unit='B', unit_scale=True, desc=filename) as pbar:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))
    logging.info(f"Saved to {dest}")
    return dest


def unzip_if_needed(path: Path, extract_to: Path) -> Path:
    """ZIP なら展開してディレクトリを返す。ZIP でなければ元の path を返す."""
    if zipfile.is_zipfile(path):
        logging.info(f"Unzipping {path} -> {extract_to}")
        extract_to.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'r') as z:
            z.extractall(extract_to)
        return extract_to
    else:
        return path


def find_files(root: Path, exts=None):
    """指定拡張子リストに合致するファイルを列挙。exts は小文字で '.' を含む例: ['.obj','.b3dm']"""
    if exts is None:
        exts = ['.glb', '.gltf', '.obj', '.ply', '.stl', '.fbx', '.b3dm']
    results = []
    if root.is_file():
        if root.suffix.lower() in exts:
            results.append(root)
        return results
    for p in root.rglob('*'):
        if p.is_file() and p.suffix.lower() in exts:
            results.append(p)
    return results


def extract_b3dm_glb(b3dm_path: Path, out_path: Path) -> Path:
    """
    b3dm から埋め込まれた GLB を抽出して保存する。
    b3dm ヘッダの仕様に従う (magic, version, byteLength, featureTableJsonLength, featureTableBinaryLength, batchTableJsonLength, batchTableBinaryLength)
    """
    logging.info(f"Extracting GLB from b3dm: {b3dm_path}")
    data = b3dm_path.read_bytes()
    if len(data) < 28:
        raise ValueError("b3dm file too small to be valid.")
    magic = data[0:4].decode('ascii', errors='ignore')
    if magic != 'b3dm':
        raise ValueError("This is not a b3dm file (magic mismatch).")
    # header structure: 4s 3I 3I -> but easier to unpack little endian ints from offsets
    # offsets: magic(0-3), version(4-7), byteLength(8-11), ftJsonLen(12-15), ftBinLen(16-19), btJsonLen(20-23), btBinLen(24-27)
    version = struct.unpack_from('<I', data, 4)[0]
    byteLength = struct.unpack_from('<I', data, 8)[0]
    ftJsonLen = struct.unpack_from('<I', data, 12)[0]
    ftBinLen = struct.unpack_from('<I', data, 16)[0]
    btJsonLen = struct.unpack_from('<I', data, 20)[0]
    btBinLen = struct.unpack_from('<I', data, 24)[0]

    glb_offset = 28 + ftJsonLen + ftBinLen + btJsonLen + btBinLen
    logging.debug(f"version={version} byteLength={byteLength} glb_offset={glb_offset}")
    if glb_offset >= len(data):
        raise ValueError("Calculated GLB offset beyond file length.")
    glb_data = data[glb_offset:byteLength] if byteLength <= len(data) else data[glb_offset:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(glb_data)
    logging.info(f"Extracted GLB saved to {out_path}")
    return out_path


def convert_mesh_to_glb(input_path: Path, out_path: Path) -> Path:
    """
    trimesh を使って OBJ/PLY/STL -> glb に変換する。複数メッシュがある場合は統合して一つの glb にする。
    """
    logging.info(f"Converting mesh to GLB: {input_path} -> {out_path}")
    try:
        mesh = trimesh.load(input_path, force='mesh')
        if mesh is None:
            raise RuntimeError("trimesh failed to load mesh.")
        # mesh.export(file_obj, file_type='glb') だと file_obj が必要になるので直接 bytes を取得
        glb_bytes = mesh.export(file_type='glb')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'wb') as f:
            if isinstance(glb_bytes, bytes):
                f.write(glb_bytes)
            else:
                # numpy array 等が来る場合
                f.write(bytes(glb_bytes))
        logging.info(f"Saved GLB: {out_path}")
        return out_path
    except Exception as e:
        logging.error(f"Failed to convert {input_path} via trimesh: {e}")
        raise


def copy_file(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logging.info(f"Copied {src} -> {dst}")
    return dst


def generate_viewer_html(glb_files, out_dir: Path):
    """
    simple three.js viewer を生成。外部 CDN を使用（インターネット接続必要）。
    出力: out_dir/index.html と glb は out_dir/models/ に配置済みであることを期待。
    """
    logging.info("Generating index.html viewer")
    models = []
    for f in glb_files:
        rel = os.path.relpath(f, out_dir).replace("\\", "/")
        models.append(rel)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>GLB Viewer</title>
<style>
  body {{ margin:0; overflow:hidden; }}
  #menu {{ position: absolute; top: 10px; left: 10px; z-index:10; background: rgba(255,255,255,0.8); padding:8px; border-radius:4px; }}
  #canvas {{ width:100vw; height:100vh; display:block; }}
</style>
</head>
<body>
<div id="menu">
  <select id="modelSelect"></select>
  <button id="fitBtn">Fit</button>
</div>
<canvas id="canvas"></canvas>
<script type="module">
import * as THREE from 'https://unpkg.com/three@0.154.0/build/three.module.js';
import {{ OrbitControls }} from 'https://unpkg.com/three@0.154.0/examples/jsm/controls/OrbitControls.js';
import {{ GLTFLoader }} from 'https://unpkg.com/three@0.154.0/examples/jsm/loaders/GLTFLoader.js';

const canvas = document.getElementById('canvas');
const renderer = new THREE.WebGLRenderer({canvas: canvas, antialias:true});
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xcccccc);

const camera = new THREE.PerspectiveCamera(45, window.innerWidth/window.innerHeight, 0.1, 10000);
camera.position.set(100,100,100);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0,0,0);
controls.update();

const hemi = new THREE.HemisphereLight(0xffffff, 0x444444, 1.0);
hemi.position.set(0,200,0);
scene.add(hemi);
const dir = new THREE.DirectionalLight(0xffffff, 0.8);
dir.position.set(-1,2,1);
scene.add(dir);

const loader = new GLTFLoader();
let currentModel = null;

const models = {models_list};

function loadModel(url){
  if(currentModel){ scene.remove(currentModel); currentModel.traverse((o)=>{ if(o.isMesh){ o.geometry.dispose(); o.material.dispose(); } }); currentModel = null; }
  loader.load(url, (g)=>{ currentModel = g.scene; scene.add(currentModel); fitToView(currentModel); }, undefined, (e)=>{ console.error(e); });
}

function fitToView(obj){
  const box = new THREE.Box3().setFromObject(obj);
  const size = new THREE.Vector3();
  box.getSize(size);
  const center = new THREE.Vector3();
  box.getCenter(center);
  const maxDim = Math.max(size.x, size.y, size.z);
  const fov = camera.fov * (Math.PI/180);
  let camZ = Math.abs(maxDim / 2 * 1.5 / Math.tan(fov / 2));
  camera.position.set(center.x + camZ, center.y + camZ, center.z + camZ);
  camera.lookAt(center);
  controls.target.copy(center);
  controls.update();
}

window.addEventListener('resize', ()=>{ camera.aspect = window.innerWidth/window.innerHeight; camera.updateProjectionMatrix(); renderer.setSize(window.innerWidth, window.innerHeight); });

function animate(){ requestAnimationFrame(animate); renderer.render(scene, camera); }
animate();

// setup select
const select = document.getElementById('modelSelect');
const fitBtn = document.getElementById('fitBtn');
const modelEntries = {models_json};
modelEntries.forEach((m, i)=>{ const opt = document.createElement('option'); opt.value = m; opt.text = m; select.appendChild(opt); });
select.addEventListener('change', ()=>{ loadModel(select.value); });
fitBtn.addEventListener('click', ()=>{ if(currentModel) fitToView(currentModel); });

if(modelEntries.length>0){ select.value = modelEntries[0]; loadModel(select.value); }
</script>
</body>
</html>
"""
    # inject model list JSON
    import json
    html = html.replace("{models_list}", json.dumps(models))
    html = html.replace("{models_json}", json.dumps(models))
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding='utf-8')
    logging.info(f"Viewer generated at {out_file}")
    return out_file


def main():
    parser = argparse.ArgumentParser(description="都市データを HTML5 (glb/gltf + three.js) 用に変換するツール")
    parser.add_argument("input", help="URL or local file/directory to process (例: https://.../resource/xxxxx または /path/to/file)")
    parser.add_argument("--out", "-o", default="output_view", help="出力ディレクトリ")
    parser.add_argument("--keep-temp", action="store_true", help="一時ディレクトリを削除せず保持する")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="plateau_convert_"))
    logging.info(f"Temp dir: {tmp}")
    try:
        # 入力のダウンロード／コピー
        input_src = args.input
        if input_src.startswith("http://") or input_src.startswith("https://"):
            downloaded = download_url(input_src, tmp)
            working_root = unzip_if_needed(downloaded, tmp / "unzipped")
        else:
            p = Path(input_src).expanduser().resolve()
            if not p.exists():
                logging.error(f"入力パスが存在しません: {p}")
                sys.exit(1)
            if p.is_file() and zipfile.is_zipfile(p):
                working_root = unzip_if_needed(p, tmp / "unzipped")
            else:
                working_root = p

        out_dir = Path(args.out).resolve()
        models_out = out_dir / "models"
        models_out.mkdir(parents=True, exist_ok=True)

        # 対応する拡張子を探索
        exts = ['.glb', '.gltf', '.obj', '.ply', '.stl', '.fbx', '.b3dm']
        files = find_files(Path(working_root), exts=exts)
        logging.info(f"Found {len(files)} candidate files")

        converted = []
        for f in files:
            suffix = f.suffix.lower()
            name = f.stem
            try:
                if suffix in ['.glb', '.gltf']:
                    dest = models_out / f.name
                    copy_file(f, dest)
                    converted.append(dest)
                elif suffix == '.b3dm':
                    out_glb = models_out / (name + ".glb")
                    extract_b3dm_glb(f, out_glb)
                    converted.append(out_glb)
                elif suffix in ['.obj', '.ply', '.stl', '.fbx']:
                    # trimesh で対応できるか試す（fbx は環境によっては不可）
                    out_glb = models_out / (name + ".glb")
                    convert_mesh_to_glb(f, out_glb)
                    converted.append(out_glb)
                else:
                    logging.warning(f"Unsupported file type (skipped): {f}")
            except Exception as e:
                logging.error(f"Failed to process {f}: {e}")

        if len(converted) == 0:
            logging.warning("変換または抽出できたモデルがありません。CityGML 等の場合は別ツールが必要です。")
        else:
            # Viewer を生成
            viewer = generate_viewer_html(converted, out_dir)
            logging.info(f"Conversion finished. Open {viewer} in a browser (static file hosting を推奨).")

    finally:
        if args.keep_temp:
            logging.info(f"Temp dir kept: {tmp}")
        else:
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass


if __name__ == "__main__":
    main()
