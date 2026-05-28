"""极速模式分组：零模型依赖，纯传统 CV。

四阶段渐进：
1. 时间硬切段：间隔 > HARD_BREAK_SECONDS → 不可跨段合并
2. 段内 base 相似度：4 hash 融合 + HSV 分块直方图 + 时间衰减 + EXIF + 文件名连号
3. ORB 几何强验证：对 base_sim ≥ 0.45 的候选对，跑 BFMatcher + RANSAC 单应性
   inlier 数是"是不是同一物理场景"的硬证据，决定 sim 的最终值
4. complete linkage 聚类（保留反链式特性）→ 限制组大小 → 组内按 quality 排序

每张图需要预先在 ImageInfo 上挂的字段（fast 模式专属，由 grouper._process_one 写入）：
- phash / dhash / whash / ahash：4 个 64-bit hex hash
- color_hist：144 维 float32（HSV 3×3 块 × 16 bins，归一化）
- orb_descs：(N, 32) uint8，N≤500；None 表示提取失败
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Optional, Sequence

import cv2  # 极速模式硬依赖：ORB 匹配 / RANSAC；缺失就在模块导入时挂掉
import numpy as np

logger = logging.getLogger("pic_selecter")


# ---- 可调参数 ----

# 时间硬切段：间隔超过这个秒数 → 必然不同组（30 分钟）
HARD_BREAK_SECONDS = 30 * 60

# 时间衰减半衰期（秒）—— 60s 让换 pose / 回看屏幕后仍保留信号
TIME_HALFLIFE = 60.0

# 强制连拍：同前缀连号 + 时间间隔 ≤ S
BURST_NUMBER_DELTA = 3
BURST_TIME_GAP = 1.2
BURST_HASH_MAX = 18       # phash hamming > 此 → 内容差异太大，不算连拍

# 多 hash 融合权重（pHash 主导，其它互补）
W_PHASH = 0.40
W_DHASH = 0.30
W_WHASH = 0.20
W_AHASH = 0.10

# base 综合权重（之和约 1）
W_HASH = 0.32
W_COLOR = 0.22
W_TIME = 0.18
W_EXIF = 0.12
W_NAME = 0.10
W_GPS = 0.06

# ORB 验证候选阈值：base_sim ≥ 此才跑 ORB
ORB_CANDIDATE_BASE = 0.45

# ORB inlier 数 → sim 提升
ORB_INLIERS_STRONG = 80     # ≥ 此：sim 强制 0.95
ORB_INLIERS_MEDIUM = 30     # ≥ 此：sim = max(base, 0.85)
ORB_INLIERS_WEAK = 5        # < 此 + base 高 → 降级（hash 误报）

# 聚类阈值（距离 = 1 - sim）
CLUSTER_DISTANCE_THRESHOLD = 0.38

# 单组大小上限
MAX_GROUP_SIZE = 25


# =================== 工具 ===================

def _filename_number(name: str) -> Optional[int]:
    m = re.search(r"(\d+)(?=\.[^.]+$|$)", name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _filename_prefix(name: str) -> str:
    base = name.rsplit(".", 1)[0]
    m = re.match(r"^(.*?)(\d+)$", base)
    return m.group(1) if m else base


def _parse_iso_dt(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _time_for_info(info) -> Optional[float]:
    if info.exif_summary and info.exif_summary.get("datetime"):
        t = _parse_iso_dt(info.exif_summary["datetime"])
        if t is not None:
            return t
    if getattr(info, "timestamp", None):
        try:
            return float(info.timestamp)
        except (TypeError, ValueError):
            pass
    if getattr(info, "mtime", None):
        try:
            return float(info.mtime)
        except (TypeError, ValueError):
            pass
    return None


def _hex_to_uint64(hex_str: Optional[str]) -> Optional[int]:
    if not hex_str:
        return None
    try:
        return int(hex_str, 16)
    except (TypeError, ValueError):
        return None


def _hash_sim(h1: Optional[str], h2: Optional[str]) -> Optional[float]:
    a = _hex_to_uint64(h1)
    b = _hex_to_uint64(h2)
    if a is None or b is None:
        return None
    dist = bin(a ^ b).count("1")
    return max(0.0, 1.0 - dist / 64.0)


def _hash_combined_sim(a, b) -> float:
    """4 hash 加权融合。缺失的 hash 自动跳过并重新归一化权重。"""
    pairs = [
        (W_PHASH, _hash_sim(getattr(a, "phash", None), getattr(b, "phash", None))),
        (W_DHASH, _hash_sim(getattr(a, "dhash", None), getattr(b, "dhash", None))),
        (W_WHASH, _hash_sim(getattr(a, "whash", None), getattr(b, "whash", None))),
        (W_AHASH, _hash_sim(getattr(a, "ahash", None), getattr(b, "ahash", None))),
    ]
    total_w = 0.0
    total_s = 0.0
    for w, s in pairs:
        if s is None:
            continue
        total_w += w
        total_s += w * s
    if total_w < 1e-6:
        return 0.0
    return total_s / total_w


def _color_sim(a, b) -> Optional[float]:
    ha = getattr(a, "color_hist", None)
    hb = getattr(b, "color_hist", None)
    if ha is None or hb is None:
        return None
    try:
        # 余弦（两边都已 L2 归一化且分量非负）；直方图 cosine 天然在 [0, 1]
        d = float(np.dot(ha, hb))
        return max(0.0, min(1.0, d))
    except Exception:
        return None


def _time_sim(t1: Optional[float], t2: Optional[float]) -> float:
    if t1 is None or t2 is None:
        return 0.0
    return math.exp(-abs(t1 - t2) / TIME_HALFLIFE)


def _exif_sim(meta1: Optional[dict], meta2: Optional[dict]) -> float:
    if not meta1 or not meta2:
        return 0.0
    score = 0.0
    parts = 0
    if meta1.get("camera") and meta2.get("camera"):
        parts += 1
        if meta1["camera"] == meta2["camera"]:
            score += 1.0
    if meta1.get("lens") and meta2.get("lens"):
        parts += 1
        if meta1["lens"] == meta2["lens"]:
            score += 1.0

    def _focal(m):
        s = (m or {}).get("focal_length")
        if not s:
            return None
        try:
            return float(str(s).rstrip("m").rstrip("m"))
        except ValueError:
            return None
    f1, f2 = _focal(meta1), _focal(meta2)
    if f1 is not None and f2 is not None:
        parts += 1
        d = abs(f1 - f2)
        score += max(0.0, 1.0 - d / 25.0)

    def _aper(m):
        s = (m or {}).get("aperture")
        if not s:
            return None
        try:
            return float(str(s).replace("f/", ""))
        except ValueError:
            return None
    a1, a2 = _aper(meta1), _aper(meta2)
    if a1 is not None and a2 is not None:
        parts += 1
        d = abs(a1 - a2)
        score += max(0.0, 1.0 - d / 4.0)
    return score / parts if parts > 0 else 0.0


def _name_sim(name1: str, name2: str) -> float:
    n1, n2 = _filename_number(name1), _filename_number(name2)
    p1, p2 = _filename_prefix(name1), _filename_prefix(name2)
    if p1 != p2 or n1 is None or n2 is None:
        return 0.0
    d = abs(n1 - n2)
    if d == 0:
        return 1.0
    return max(0.0, 1.0 - d / 30.0)


def _gps_sim(meta1: Optional[dict], meta2: Optional[dict]) -> Optional[float]:
    if not meta1 or not meta2:
        return None
    g1 = meta1.get("gps_lat"), meta1.get("gps_lon")
    g2 = meta2.get("gps_lat"), meta2.get("gps_lon")
    if any(v is None for v in g1 + g2):
        return None
    try:
        lat1, lon1 = float(g1[0]), float(g1[1])
        lat2, lon2 = float(g2[0]), float(g2[1])
        avg_lat_rad = math.radians((lat1 + lat2) / 2.0)
        d = math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(avg_lat_rad))
    except (TypeError, ValueError):
        return None
    return math.exp(-d / 0.0009)


# =================== 强制连拍组 ===================

def detect_bursts(infos) -> list[set[int]]:
    n = len(infos)
    keyed = []
    for i, info in enumerate(infos):
        name = Path(info.path).name
        num = _filename_number(name)
        if num is None:
            continue
        prefix = _filename_prefix(name)
        t = _time_for_info(info)
        keyed.append((prefix, num, t, i))
    if not keyed:
        return []
    keyed.sort(key=lambda x: (x[0], x[1]))

    union = list(range(n))

    def find(x):
        while union[x] != x:
            union[x] = union[union[x]]
            x = union[x]
        return x

    def merge(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            union[ra] = rb

    for j in range(1, len(keyed)):
        p0, n0, t0, i0 = keyed[j - 1]
        p1, n1, t1, i1 = keyed[j]
        if p0 != p1:
            continue
        if n1 - n0 > BURST_NUMBER_DELTA:
            continue
        if t0 is not None and t1 is not None and (t1 - t0) > BURST_TIME_GAP:
            continue
        ph0 = getattr(infos[i0], "phash", None)
        ph1 = getattr(infos[i1], "phash", None)
        if ph0 and ph1:
            try:
                ham = bin(int(ph0, 16) ^ int(ph1, 16)).count("1")
                if ham > BURST_HASH_MAX:
                    continue
            except (ValueError, TypeError):
                pass
        merge(i0, i1)

    groups: dict[int, set[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, set()).add(i)
    return [g for g in groups.values() if len(g) >= 2]


# =================== ORB 几何验证 ===================

def _orb_inliers(desc_a, desc_b, kps_a=None, kps_b=None) -> int:
    """返回经过 RANSAC 单应性验证后的内点数。

    desc_a / desc_b：(N, 32) uint8 ORB 描述子。
    kps_a / kps_b：(N, 2) float32 关键点 (x, y) 坐标。若 None，则只算 raw 匹配数 / 2（粗略）。
    """
    # 任一方没算出描述子（图过小 / ORB 找不到关键点）→ 几何无法验证，按 0 处理
    # 这是"数据不足"，不是能力降级
    if desc_a is None or desc_b is None:
        return 0
    if len(desc_a) < 8 or len(desc_b) < 8:
        return 0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(desc_a, desc_b)
    if len(matches) < 8:
        return 0
    # 距离过滤：保留距离 < 60 的（ORB 256bit 描述子的经验阈值）
    matches = [m for m in matches if m.distance < 60]
    if len(matches) < 8:
        return 0
    if kps_a is None or kps_b is None:
        # 没坐标就无法跑 RANSAC——这不该发生，因为 grouper._compute_orb 同时输出
        raise RuntimeError("ORB 描述子存在但关键点坐标缺失，调用方逻辑错误")
    pts_a = np.array([kps_a[m.queryIdx] for m in matches], dtype=np.float32).reshape(-1, 1, 2)
    pts_b = np.array([kps_b[m.trainIdx] for m in matches], dtype=np.float32).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, 4.0)
    if mask is None:
        return 0
    return int(mask.sum())


# =================== 成对相似度 ===================

def _pair_base_sim(info_a, info_b, meta_a, meta_b) -> float:
    """不含 ORB 验证的"快速"相似度。"""
    sim_hash = _hash_combined_sim(info_a, info_b)
    sim_color = _color_sim(info_a, info_b)
    if sim_color is None:
        # 没有颜色直方图 → 转移权重给 hash
        sim_color = 0.0
        has_color = False
    else:
        has_color = True

    ta = _time_for_info(info_a)
    tb = _time_for_info(info_b)
    sim_time = _time_sim(ta, tb)
    sim_exif = _exif_sim(meta_a, meta_b)
    sim_name = _name_sim(Path(info_a.path).name, Path(info_b.path).name)

    sim_gps = _gps_sim(meta_a, meta_b)
    has_gps = sim_gps is not None
    if not has_gps:
        sim_gps = 0.0

    w_hash, w_color, w_time, w_exif, w_name, w_gps = (
        W_HASH, W_COLOR, W_TIME, W_EXIF, W_NAME, W_GPS
    )
    if not has_color:
        w_hash += w_color
        w_color = 0.0
    if not has_gps:
        w_exif += w_gps
        w_gps = 0.0

    return (
        w_hash * sim_hash
        + w_color * sim_color
        + w_time * sim_time
        + w_exif * sim_exif
        + w_name * sim_name
        + w_gps * sim_gps
    )


def _pair_final_sim(info_a, info_b, meta_a, meta_b) -> float:
    """带 ORB 几何强验证的最终相似度。"""
    base = _pair_base_sim(info_a, info_b, meta_a, meta_b)
    hash_sim = _hash_combined_sim(info_a, info_b)
    if base < ORB_CANDIDATE_BASE and hash_sim < 0.65:
        return base

    # 跨段时间太大就别浪费算 ORB
    ta = _time_for_info(info_a)
    tb = _time_for_info(info_b)
    if ta is not None and tb is not None and abs(ta - tb) > HARD_BREAK_SECONDS:
        return base * 0.5  # 强约束：硬切段内不该被合并

    inliers = _orb_inliers(
        getattr(info_a, "orb_descs", None),
        getattr(info_b, "orb_descs", None),
        getattr(info_a, "orb_kps", None),
        getattr(info_b, "orb_kps", None),
    )

    if inliers >= ORB_INLIERS_STRONG:
        return 0.95
    if inliers >= ORB_INLIERS_MEDIUM:
        return max(base, 0.85)
    if inliers < ORB_INLIERS_WEAK and base > 0.55:
        # base 看着像，几何匹配挂了 → hash 误报，降级
        return 0.55
    return base


# =================== 时间硬切段 ===================

def _split_by_time_gaps(infos: Sequence) -> list[list[int]]:
    """按时间间隔切大段。返回每段是 infos 索引的 list。"""
    if not infos:
        return []
    sorted_idx = sorted(range(len(infos)),
                        key=lambda i: _time_for_info(infos[i]) or 0.0)
    segments: list[list[int]] = [[sorted_idx[0]]]
    for i in sorted_idx[1:]:
        t_cur = _time_for_info(infos[i])
        t_prev = _time_for_info(infos[segments[-1][-1]])
        if t_cur is not None and t_prev is not None and (t_cur - t_prev) > HARD_BREAK_SECONDS:
            segments.append([i])
        else:
            segments[-1].append(i)
    return segments


# =================== complete linkage 聚类 ===================

def _complete_linkage(
    members: list[int],
    dist_fn,
    threshold: float,
    forced_groups: list[set[int]] = None,
) -> list[list[int]]:
    """在给定成员上做 complete linkage 聚类。

    members 是全局索引列表；dist_fn(i, j) 接受全局索引。
    forced_groups：要求强制合并的子集（全局索引集合）。
    """
    if not members:
        return []
    clusters: dict[int, set[int]] = {i: {i} for i in members}
    if forced_groups:
        for grp in forced_groups:
            anchor = next(iter(grp & set(members)), None)
            if anchor is None:
                continue
            for j in grp:
                if j == anchor or j not in clusters:
                    continue
                clusters[anchor].update(clusters.pop(j))

    cache: dict[tuple[int, int], float] = {}

    def cluster_dist(ca: int, cb: int) -> float:
        key = (min(ca, cb), max(ca, cb))
        if key in cache:
            return cache[key]
        max_d = 0.0
        for i in clusters[ca]:
            for j in clusters[cb]:
                d = dist_fn(i, j)
                if d > max_d:
                    max_d = d
                    if max_d > threshold:
                        cache[key] = max_d
                        return max_d
        cache[key] = max_d
        return max_d

    while True:
        ids = list(clusters.keys())
        if len(ids) < 2:
            break
        best_pair = None
        best_d = float("inf")
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                d = cluster_dist(ids[i], ids[j])
                if d < best_d:
                    best_d = d
                    best_pair = (ids[i], ids[j])
                if best_d <= 0.0:
                    break
            if best_d <= 0.0:
                break
        if best_pair is None or best_d > threshold:
            break
        a, b = best_pair
        clusters[a].update(clusters.pop(b))
        for k in list(cache.keys()):
            if a in k or b in k:
                del cache[k]

    return [sorted(c) for c in clusters.values()]


def _split_oversized(groups: list[list[int]], infos, max_size: int = MAX_GROUP_SIZE) -> list[list[int]]:
    out: list[list[int]] = []
    stack = list(groups)
    while stack:
        g = stack.pop()
        if len(g) <= max_size:
            out.append(g)
            continue
        timed = sorted((_time_for_info(infos[i]) or 0.0, i) for i in g)
        best_gap, best_k = -1.0, len(timed) // 2
        for k in range(1, len(timed)):
            gap = timed[k][0] - timed[k - 1][0]
            if gap > best_gap:
                best_gap = gap
                best_k = k
        stack.append([i for _, i in timed[:best_k]])
        stack.append([i for _, i in timed[best_k:]])
    return out


# =================== 入口 ===================

def cluster(infos: Sequence, meta_for=None) -> list[list[int]]:
    """主入口。返回每组是 infos 索引的 list。

    infos 必须已计算 phash / dhash / whash / ahash / color_hist / orb_descs / orb_kps
    （fast 模式由 grouper._process_one 写入）。
    """
    n = len(infos)
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    if meta_for is None:
        meta_for = lambda info: (info.exif_summary if getattr(info, "exif_summary", None) else None)

    metas = [meta_for(info) for info in infos]
    bursts = detect_bursts(infos)
    logger.info(f"fast cluster: 强制连拍 {len(bursts)} 组（{sum(len(g) for g in bursts)} 张）")

    # 时间硬切段
    segments = _split_by_time_gaps(infos)
    logger.info(f"fast cluster: 时间硬切 {len(segments)} 段，最大 {max(len(s) for s in segments)} 张")

    pair_cache: dict[tuple[int, int], float] = {}

    def dist(i: int, j: int) -> float:
        if i == j:
            return 0.0
        key = (i, j) if i < j else (j, i)
        if key in pair_cache:
            return pair_cache[key]
        s = _pair_final_sim(infos[i], infos[j], metas[i], metas[j])
        d = 1.0 - s
        pair_cache[key] = d
        return d

    all_groups: list[list[int]] = []
    for seg in segments:
        # 段内的强制连拍组（与全局连拍组求交）
        seg_set = set(seg)
        local_forced = [g & seg_set for g in bursts if len(g & seg_set) >= 2]
        sub_groups = _complete_linkage(
            seg, dist, threshold=CLUSTER_DISTANCE_THRESHOLD,
            forced_groups=local_forced,
        )
        all_groups.extend(sub_groups)

    all_groups = _split_oversized(all_groups, infos, MAX_GROUP_SIZE)

    # 组内按时间排序，组间按最早时间
    for g in all_groups:
        g.sort(key=lambda i: _time_for_info(infos[i]) or 0.0)
    all_groups.sort(key=lambda g: _time_for_info(infos[g[0]]) or 0.0)

    logger.info(
        f"fast cluster: 最终 {len(all_groups)} 组，最大 "
        f"{max((len(g) for g in all_groups), default=0)} 张"
    )
    return all_groups
