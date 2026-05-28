#!/usr/bin/env python3
"""分组诊断脚本：不启动 UI，直接运行 DINOv2 特征提取 + 聚类，输出详细分析。

用法：
    python test_grouping.py <folder> [--debug] [--limit N]

不需要 insightface — 只用 DINOv2 + EXIF + 时间 + 文件名信号诊断聚类质量。
"""
import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image, ImageOps
import imagehash

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pic_selecter")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class ImageInfo:
    path: str
    phash: str
    timestamp: Optional[float] = None
    size: int = 0
    mtime: float = 0.0
    exif_summary: Optional[dict] = None
    quality: Optional[dict] = None
    dinov2: Optional[Any] = None
    aesthetic_score: Optional[float] = None
    face_embeddings: Optional[list] = None
    dhash: Optional[str] = None
    whash: Optional[str] = None
    ahash: Optional[str] = None
    color_hist: Optional[Any] = None
    orb_descs: Optional[Any] = None
    orb_kps: Optional[Any] = None


def extract_for_test(path: str) -> Optional[ImageInfo]:
    """提取 DINOv2 + EXIF（跳过 InsightFace 和 NIMA）。"""
    from pic_selecter.grouper import _read_exif_datetime, extract_exif_summary
    try:
        st = os.stat(path)
        with Image.open(path) as img:
            img.load()
            ts_dt = _read_exif_datetime(img)
            exif_sum = extract_exif_summary(img, st.st_size)
            img_t = ImageOps.exif_transpose(img)
            ph = imagehash.phash(img_t, hash_size=8)

            from pic_selecter import vision
            dinov2_vec = vision.extract_dinov2(img_t)

        return ImageInfo(
            path=path,
            phash=str(ph),
            timestamp=ts_dt.timestamp() if ts_dt else None,
            size=st.st_size,
            mtime=st.st_mtime,
            exif_summary=exif_sum,
            dinov2=dinov2_vec,
            face_embeddings=[],
        )
    except Exception as e:
        print(f"  跳过 {Path(path).name}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="分组诊断")
    parser.add_argument("folder", help="图片文件夹路径")
    parser.add_argument("--debug", action="store_true", help="输出 per-pair 相似度明细")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 张")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    folder = args.folder
    print(f"\n{'='*70}")
    print(f"分组诊断  folder={folder}")
    print(f"{'='*70}\n")

    # ---- 1. 扫描文件 ----
    files = []
    for root, _, names in os.walk(folder):
        rel = Path(root).relative_to(folder)
        if rel.parts and rel.parts[0] in {"winners", "losers", "_pic_selecter"}:
            continue
        for n in names:
            if Path(n).suffix.lower() in IMAGE_EXTS:
                files.append(str(Path(root) / n))
    files.sort()
    if args.limit > 0:
        files = files[:args.limit]
    print(f"找到 {len(files)} 张图片")

    # ---- 2. DINOv2 特征提取 ----
    print("\n[1/3] DINOv2 特征提取...")
    t0 = time.time()
    infos = []
    for i, f in enumerate(files):
        info = extract_for_test(f)
        if info:
            infos.append(info)
        if (i + 1) % 10 == 0 or i == len(files) - 1:
            print(f"  {i+1}/{len(files)}...", flush=True)
    t_extract = time.time() - t0
    print(f"  完成：{len(infos)} 张，耗时 {t_extract:.1f}s")

    if len(infos) < 2:
        print("图片不足 2 张，无法聚类。")
        return

    # ---- 3. DINOv2 cosine 分布 ----
    print("\n[2/3] DINOv2 余弦相似度分布...")
    n = len(infos)
    all_cosines = []
    for i in range(n):
        for j in range(i + 1, n):
            cos = float(np.dot(infos[i].dinov2, infos[j].dinov2))
            all_cosines.append(cos)
    all_cosines = np.array(all_cosines)
    print(f"  {len(all_cosines)} 对：")
    print(f"    min={all_cosines.min():.3f}  p5={np.quantile(all_cosines, 0.05):.3f}  "
          f"p10={np.quantile(all_cosines, 0.1):.3f}  p25={np.quantile(all_cosines, 0.25):.3f}  "
          f"p50={np.quantile(all_cosines, 0.5):.3f}")
    print(f"    p75={np.quantile(all_cosines, 0.75):.3f}  p90={np.quantile(all_cosines, 0.9):.3f}  "
          f"p95={np.quantile(all_cosines, 0.95):.3f}  max={all_cosines.max():.3f}")

    # 展示旧映射 vs 新映射的效果
    old_mapped = (all_cosines + 1.0) / 2.0
    new_mapped = np.clip(all_cosines, 0, 1)
    print(f"\n  旧映射 (s+1)/2 分布：")
    print(f"    min={old_mapped.min():.3f}  p10={np.quantile(old_mapped, 0.1):.3f}  "
          f"p50={np.quantile(old_mapped, 0.5):.3f}  p90={np.quantile(old_mapped, 0.9):.3f}")
    print(f"  新映射 max(0,s) 分布：")
    print(f"    min={new_mapped.min():.3f}  p10={np.quantile(new_mapped, 0.1):.3f}  "
          f"p50={np.quantile(new_mapped, 0.5):.3f}  p90={np.quantile(new_mapped, 0.9):.3f}")

    # ---- 4. 模拟旧 vs 新聚类 ----
    print("\n[3/3] 聚类对比（旧映射 vs 新映射）...")

    from pic_selecter import clustering

    # 保存当前代码（已修复）的结果
    print("\n  --- 新映射（已修复）---")
    t0 = time.time()
    groups_new = clustering.cluster(infos)
    t_new = time.time() - t0

    multi_new = [g for g in groups_new if len(g) > 1]
    sizes_new = sorted([len(g) for g in groups_new], reverse=True)
    print(f"    {len(groups_new)} 组（多图组 {len(multi_new)}），耗时 {t_new:.1f}s")
    print(f"    组大小: {sizes_new[:15]}")

    # 用猴子补丁测试旧映射
    original_fn = clustering._dinov2_similarity

    def old_dinov2_similarity(v1, v2):
        if v1 is None or v2 is None:
            raise RuntimeError("DINOv2 missing")
        s = float(np.dot(v1, v2))
        return max(0.0, min(1.0, (s + 1.0) / 2.0))

    clustering._dinov2_similarity = old_dinov2_similarity

    # 也暂时还原旧的人脸权重逻辑来对比
    original_pair_sim = clustering._pair_similarity

    print("\n  --- 旧映射（Bug 原版）---")
    t0 = time.time()
    groups_old = clustering.cluster(infos)
    t_old = time.time() - t0

    multi_old = [g for g in groups_old if len(g) > 1]
    sizes_old = sorted([len(g) for g in groups_old], reverse=True)
    print(f"    {len(groups_old)} 组（多图组 {len(multi_old)}），耗时 {t_old:.1f}s")
    print(f"    组大小: {sizes_old[:15]}")

    # 还原
    clustering._dinov2_similarity = original_fn

    # ---- 5. 详细对比 ----
    print(f"\n{'='*70}")
    print(f"对比总结")
    print(f"{'='*70}")
    print(f"  旧映射: {len(groups_old)} 组（多图 {len(multi_old)}, 最大 {sizes_old[0] if sizes_old else 0}）")
    print(f"  新映射: {len(groups_new)} 组（多图 {len(multi_new)}, 最大 {sizes_new[0] if sizes_new else 0}）")

    if len(multi_old) > len(multi_new):
        print(f"\n  旧版多出了 {len(multi_old) - len(multi_new)} 个多图组 ← 不相关图片被错误合并")
    elif len(multi_new) > len(multi_old):
        print(f"\n  新版多出了 {len(multi_new) - len(multi_old)} 个多图组")
    else:
        print(f"\n  多图组数量相同，但组内成员可能不同")

    # 打印新版多图组详情
    print(f"\n--- 新版多图组详情 ---")
    for gi, g_indices in enumerate(groups_new, 1):
        if len(g_indices) <= 1:
            continue
        print(f"\n  组 {gi}（{len(g_indices)} 张）：")
        for idx in g_indices:
            info = infos[idx]
            name = Path(info.path).name
            ts = ""
            if info.exif_summary and info.exif_summary.get("datetime"):
                ts = f"  {info.exif_summary['datetime'][:19]}"
            cam = ""
            if info.exif_summary:
                cam_s = info.exif_summary.get("camera", "")
                fl = info.exif_summary.get("focal_length", "")
                if cam_s or fl:
                    cam = f"  [{cam_s} {fl}]".rstrip()
            print(f"    {name}{ts}{cam}")

        # 组内 DINOv2 cosine
        if len(g_indices) <= 12:
            vecs = [(infos[idx], infos[idx].dinov2) for idx in g_indices]
            names_short = [Path(v[0].path).stem[-10:] for v in vecs]
            min_cos = 1.0
            max_cos = -1.0
            for i in range(len(vecs)):
                for j in range(i + 1, len(vecs)):
                    cos = float(np.dot(vecs[i][1], vecs[j][1]))
                    min_cos = min(min_cos, cos)
                    max_cos = max(max_cos, cos)
            print(f"    DINOv2 cosine: min={min_cos:.3f} max={max_cos:.3f}")

    print(f"\n总耗时: {t_extract + t_new:.1f}s")


if __name__ == "__main__":
    main()
