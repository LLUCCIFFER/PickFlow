"""多信号分组算法：DINOv2 语义 + 时间衰减 + EXIF 签名 + GPS + 主角重叠 + 文件名序号。

核心 API：
- cluster(infos) -> List[List[int]]
  返回每组是 infos 索引列表

设计要点：
1. 第一步硬规则：文件名连号 + sub-second 间隔 → 强制连拍组（这一步绝对优先，不让别的信号否决）。
2. 第二步软相似度：把上一步的连拍块视为"超节点"，再用加权多信号相似矩阵 + complete linkage 聚合。
   complete linkage 的好处：不会出现 A—B—C—D 链式连接 A 和 D（用 max distance 当簇间距）。
3. 第三步：组大小硬上限 25，超了就按时间二分。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

logger = logging.getLogger("pic_selecter")


# ---------- 信号权重（可调；保持总和约等 1） ----------
# BASE 权重档：风景/混合相册（任一方无脸时使用）
W_DINOV2 = 0.35
W_TIME = 0.20
W_EXIF = 0.10
W_FACE = 0.15
W_GPS = 0.10
W_FILENAME = 0.10

# PORTRAIT 权重档：两图都有脸 → face ID 主导
# 原理：人像场景里"同一个人正在被拍"比"构图相似"更能定义同组连拍。
# 解决两个老问题：(a) 同人不同背景该合的不合 (b) 不同人同背景错合
PORTRAIT_W_DINOV2 = 0.20
PORTRAIT_W_TIME = 0.15
PORTRAIT_W_EXIF = 0.05
PORTRAIT_W_FACE = 0.50
PORTRAIT_W_GPS = 0.05
PORTRAIT_W_FILENAME = 0.05

# 时间衰减半衰期（秒）
TIME_HALFLIFE = 150.0

# GPS 半衰期（度，约 100m）
GPS_HALFLIFE = 0.0009

# 强制连拍：连号差 ≤ N + 时间间隔 ≤ S
BURST_NUMBER_DELTA = 3
BURST_TIME_GAP = 1.2
BURST_HASH_MAX = 18       # phash hamming > 此 → 内容差异太大，不算连拍

# 软聚类距离上限——超过这个值不合并；这是 (1 - similarity)
CLUSTER_DISTANCE_THRESHOLD = 0.46

# 单组大小上限
MAX_GROUP_SIZE = 25

# 时间硬切段（秒）：间隔超过此 → 必然不同组
HARD_BREAK_SECONDS = 45 * 60


def _filename_number(name: str) -> Optional[int]:
    """从文件名抽末尾数字段（连号检测用）。`IMG_4321.HEIC` → 4321。"""
    m = re.search(r"(\d+)(?=\.[^.]+$|$)", name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _filename_prefix(name: str) -> str:
    """同前缀连号才算同一序列。`IMG_4321.JPG` → `IMG_`、`B0005084.JPG` → `B`。"""
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
    """优先 EXIF datetime，否则 file mtime（已存于 info.timestamp / mtime）。"""
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


# =================== 第 1 步：强制连拍组 ===================

def detect_bursts(infos) -> list[set[int]]:
    """返回必须强制成一组的 index 集合列表。

    判定：同前缀文件名 + 连号差 ≤ BURST_NUMBER_DELTA + 时间间隔 ≤ BURST_TIME_GAP。
    """
    n = len(infos)
    # (prefix, number, time, idx) 排序
    keyed = []
    for i, info in enumerate(infos):
        from pathlib import Path
        name = Path(info.path).name
        num = _filename_number(name)
        if num is None:
            continue
        prefix = _filename_prefix(name)
        t = _time_for_info(info)
        keyed.append((prefix, num, t, i))
    if not keyed:
        return []

    # 按 (prefix, number) 排序
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
        # 时间间隔（缺失时认为可能是连拍，给放过）
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

    # 收集 union → set
    groups: dict[int, set[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, set()).add(i)
    return [g for g in groups.values() if len(g) >= 2]


# =================== 第 2 步：成对相似度 ===================

def _time_similarity(t1: Optional[float], t2: Optional[float]) -> float:
    if t1 is None or t2 is None:
        return 0.0
    dt = abs(t1 - t2)
    return math.exp(-dt / TIME_HALFLIFE)


def _gps_similarity(meta1: Optional[dict], meta2: Optional[dict]) -> Optional[float]:
    if not meta1 or not meta2:
        return None
    g1 = meta1.get("gps_lat"), meta1.get("gps_lon")
    g2 = meta2.get("gps_lat"), meta2.get("gps_lon")
    if g1[0] is None or g1[1] is None or g2[0] is None or g2[1] is None:
        return None
    try:
        lat1, lon1 = float(g1[0]), float(g1[1])
        lat2, lon2 = float(g2[0]), float(g2[1])
        avg_lat_rad = math.radians((lat1 + lat2) / 2.0)
        d = math.hypot(lat1 - lat2, (lon1 - lon2) * math.cos(avg_lat_rad))
    except (TypeError, ValueError):
        return None
    return math.exp(-d / GPS_HALFLIFE)


def _exif_signature_similarity(meta1: Optional[dict], meta2: Optional[dict]) -> float:
    """同相机 + 同镜头 + 焦距接近 + 光圈接近 + ISO 数量级接近 → 高分。"""
    if not meta1 or not meta2:
        return 0.0
    score = 0.0
    parts = 0
    # 相机
    if meta1.get("camera") and meta2.get("camera"):
        parts += 1
        if meta1["camera"] == meta2["camera"]:
            score += 1.0
    # 镜头
    if meta1.get("lens") and meta2.get("lens"):
        parts += 1
        if meta1["lens"] == meta2["lens"]:
            score += 1.0
    # 焦距
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
        # 焦距接近度：差 < 5mm 全分，差 25mm 衰减到 0
        d = abs(f1 - f2)
        score += max(0.0, 1.0 - d / 25.0)
    # 光圈
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
        score += max(0.0, 1.0 - d / 4.0)  # 差 4 档以内有分
    # ISO 数量级
    def _iso(m):
        s = (m or {}).get("iso")
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    i1, i2 = _iso(meta1), _iso(meta2)
    if i1 is not None and i2 is not None and i1 > 0 and i2 > 0:
        parts += 1
        # log2(ratio) 差 < 1 → 满分；> 3 → 0
        d = abs(math.log2(i1 / i2))
        score += max(0.0, 1.0 - d / 3.0)
    return score / parts if parts > 0 else 0.0


def _face_overlap_similarity(faces1, faces2) -> float:
    """两张照片的人脸 ID 重叠度（Jaccard）。

    faces1/faces2: list of 512 维 float32 numpy 向量（ArcFace via InsightFace）。
    匹配阈值：cosine 相似 > 0.45（ArcFace 嵌入比 dlib 128d 分布更紧凑）。
    """
    if not faces1 or not faces2:
        return 0.0
    n1, n2 = len(faces1), len(faces2)
    matched1 = set()
    matched2 = set()
    for i, f1 in enumerate(faces1):
        if i in matched1:
            continue
        best_j, best_sim = -1, -1.0
        for j, f2 in enumerate(faces2):
            if j in matched2:
                continue
            sim = float(np.dot(f1, f2))
            if sim > best_sim:
                best_sim = sim
                best_j = j
        if best_j >= 0 and best_sim > 0.45:
            matched1.add(i)
            matched2.add(best_j)
    matched = len(matched1)
    return matched / (n1 + n2 - matched) if (n1 + n2 - matched) > 0 else 0.0


def _dinov2_similarity(v1, v2) -> float:
    """专家模式核心信号；缺失即任务失败。

    DINOv2 CLS token 在自然图片上的余弦相似度分布约 [0.0, 1.0]，
    不相关图片 ~0.1-0.3，相似场景 ~0.5-0.7，近乎相同 ~0.8+。
    直接用 cosine 值（clamp 到 [0,1]），不做 (s+1)/2 压缩。
    """
    if v1 is None or v2 is None:
        raise RuntimeError(
            "DINOv2 向量缺失。专家模式应在 _run_job 启动前 prewarm 模型；"
            "若到这里仍为 None，说明 vision.extract_dinov2 静默失败了。"
        )
    s = float(np.dot(v1, v2))
    return max(0.0, min(1.0, s))


def _filename_similarity(name1: str, name2: str) -> float:
    n1, n2 = _filename_number(name1), _filename_number(name2)
    p1, p2 = _filename_prefix(name1), _filename_prefix(name2)
    if p1 != p2 or n1 is None or n2 is None:
        return 0.0
    d = abs(n1 - n2)
    if d == 0:
        return 1.0
    # 差 5 内还很有信号，差 30 几乎没了
    return max(0.0, 1.0 - d / 30.0)


def _pair_similarity(info_a, info_b, meta_a, meta_b) -> float:
    """加权融合多个信号。返回 [0, 1]。

    DINOv2 缺失 → 抛 RuntimeError（专家模式核心信号，不静默降级）。
    """
    from pathlib import Path
    name_a = Path(info_a.path).name
    name_b = Path(info_b.path).name

    sim_vis = _dinov2_similarity(
        getattr(info_a, "dinov2", None),
        getattr(info_b, "dinov2", None),
    )

    ta = _time_for_info(info_a)
    tb = _time_for_info(info_b)
    sim_time = _time_similarity(ta, tb)

    sim_exif = _exif_signature_similarity(meta_a, meta_b)

    sim_gps = _gps_similarity(meta_a, meta_b)
    has_gps = sim_gps is not None
    if sim_gps is None:
        sim_gps = 0.0

    faces_a = getattr(info_a, "face_embeddings", None) or []
    faces_b = getattr(info_b, "face_embeddings", None) or []
    portrait = bool(faces_a) and bool(faces_b)

    sim_face = _face_overlap_similarity(faces_a, faces_b)

    sim_name = _filename_similarity(name_a, name_b)

    # 权重档：两图都有脸 → PORTRAIT（face 主导）；否则 BASE（视觉主导）
    if portrait:
        w_dino, w_time, w_exif, w_face, w_gps, w_name = (
            PORTRAIT_W_DINOV2, PORTRAIT_W_TIME, PORTRAIT_W_EXIF,
            PORTRAIT_W_FACE, PORTRAIT_W_GPS, PORTRAIT_W_FILENAME
        )
    else:
        w_dino, w_time, w_exif, w_face, w_gps, w_name = (
            W_DINOV2, W_TIME, W_EXIF, W_FACE, W_GPS, W_FILENAME
        )
    # GPS 缺失：把它的权重转给 EXIF
    if not has_gps:
        w_exif += w_gps
        w_gps = 0.0
    # 仅在 BASE 档需要处理"任一方无脸"的权重重分配；
    # PORTRAIT 档两边必有脸，face 是 0 也是有效差异信号（不同人脸）
    if not portrait:
        neither_has_face = (not faces_a) or (not faces_b)
        if sim_face == 0.0 and neither_has_face:
            w_dino += w_face * 0.4
            w_time += w_face * 0.3
            w_exif += w_face * 0.3
            w_face = 0.0

    total = (
        w_dino * sim_vis
        + w_time * sim_time
        + w_exif * sim_exif
        + w_face * sim_face
        + w_gps * sim_gps
        + w_name * sim_name
    )

    if logger.isEnabledFor(logging.DEBUG):
        dt = f"{abs(ta - tb):.0f}s" if (ta and tb) else "?"
        fa = len(faces_a)
        fb = len(faces_b)
        logger.debug(
            f"PAIR {name_a} × {name_b} | portrait={portrait} | "
            f"sim={total:.3f} dist={1-total:.3f} | "
            f"dino={sim_vis:.3f}(w{w_dino:.2f}) "
            f"time={sim_time:.3f}(w{w_time:.2f},dt={dt}) "
            f"exif={sim_exif:.3f}(w{w_exif:.2f}) "
            f"face={sim_face:.3f}(w{w_face:.2f},#{fa}v{fb}) "
            f"gps={sim_gps:.3f}(w{w_gps:.2f}) "
            f"name={sim_name:.3f}(w{w_name:.2f})"
        )

    return total


# =================== 时间硬切段 ===================

def _split_by_time_gaps(infos: Sequence) -> list[list[int]]:
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


# =================== 第 3 步：层次聚类 ===================

def _complete_linkage_cluster(
    members: list[int], dist_fn, threshold: float, forced_groups: list[set[int]] = None
) -> list[list[int]]:
    """简易 complete linkage：每步合并簇间最大距离最小的两个簇，前提是 ≤ threshold。

    members：参与聚类的全局索引列表。
    forced_groups：必须保持在同一簇的索引集合（会先 merge 它们，且互相的距离视为 0）。
    """
    if not members:
        return []
    clusters: dict[int, set[int]] = {i: {i} for i in members}

    if forced_groups:
        for grp in forced_groups:
            grp_list = sorted(grp & set(members))
            if len(grp_list) < 2:
                continue
            anchor = grp_list[0]
            for j in grp_list[1:]:
                if j in clusters:
                    clusters[anchor].update(clusters.pop(j))

    # 计算两个簇之间的 complete linkage 距离 = 簇间所有点对最大距离
    cache: dict[tuple[int, int], float] = {}

    def cluster_distance(ca: int, cb: int) -> float:
        key = (min(ca, cb), max(ca, cb))
        if key in cache:
            return cache[key]
        max_d = 0.0
        for i in clusters[ca]:
            for j in clusters[cb]:
                d = dist_fn(i, j)
                if d > max_d:
                    max_d = d
                    # complete linkage：超过阈值就没意义继续，反正用不到
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
                d = cluster_distance(ids[i], ids[j])
                if d < best_d:
                    best_d = d
                    best_pair = (ids[i], ids[j])
        if best_pair is None or best_d > threshold:
            break
        a, b = best_pair
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"MERGE clusters {sorted(clusters[a])} + {sorted(clusters[b])} | "
                f"complete_linkage_dist={best_d:.3f} (threshold={threshold:.2f})"
            )
        clusters[a].update(clusters.pop(b))
        # 清掉 cache 里和 a / b 有关的条目
        for k in list(cache.keys()):
            if a in k or b in k:
                del cache[k]

    return [sorted(c) for c in clusters.values()]


def _split_oversized(groups: list[list[int]], infos, max_size: int = MAX_GROUP_SIZE) -> list[list[int]]:
    """超大组在最大时间间隙处一刀二分，直到所有组 ≤ max_size。"""
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
    """主入口。infos 必须已计算 EXIF / dinov2 / face_embeddings 等字段。

    meta_for(info) -> dict：返回 EXIF 元数据 dict。默认从 info.exif_summary 取。
    """
    n = len(infos)
    if n == 0:
        return []
    if meta_for is None:
        meta_for = lambda info: (info.exif_summary if getattr(info, "exif_summary", None) else None)

    # 1. 强制连拍组
    forced = detect_bursts(infos)
    logger.info(f"clustering: 强制连拍组 {len(forced)} 组（涉及 {sum(len(g) for g in forced)} 张）")

    # 2. 时间硬切段
    segments = _split_by_time_gaps(infos)
    logger.info(f"clustering: 时间硬切 {len(segments)} 段，最大 {max(len(s) for s in segments)} 张")

    # 3. 准备距离函数（lazy 计算 + 缓存）
    pair_cache: dict[tuple[int, int], float] = {}

    metas = [meta_for(info) for info in infos]

    def dist(i: int, j: int) -> float:
        if i == j:
            return 0.0
        key = (i, j) if i < j else (j, i)
        if key in pair_cache:
            return pair_cache[key]
        s = _pair_similarity(infos[i], infos[j], metas[i], metas[j])
        d = 1.0 - s
        pair_cache[key] = d
        return d

    # 4. 段内聚类
    all_groups: list[list[int]] = []
    for seg in segments:
        seg_set = set(seg)
        local_forced = [g & seg_set for g in forced if len(g & seg_set) >= 2]
        sub_groups = _complete_linkage_cluster(
            seg, dist, threshold=CLUSTER_DISTANCE_THRESHOLD,
            forced_groups=local_forced,
        )
        all_groups.extend(sub_groups)
    groups = all_groups

    # 4. 限制大小
    groups = _split_oversized(groups, infos, MAX_GROUP_SIZE)

    # 5. 组内按时间排序（前端 preview 显示更友好）
    for g in groups:
        g.sort(key=lambda i: _time_for_info(infos[i]) or 0.0)

    # 整体按"最早时间"排序，跟现有 build_session 的预期一致
    groups.sort(key=lambda g: _time_for_info(infos[g[0]]) or 0.0)

    logger.info(f"clustering: 最终 {len(groups)} 组，最大 {max((len(g) for g in groups), default=0)} 张")

    if logger.isEnabledFor(logging.DEBUG):
        from pathlib import Path as _P
        for gi, g in enumerate(groups):
            names = [_P(infos[i].path).name for i in g]
            logger.debug(f"GROUP {gi+1} ({len(g)}张): {', '.join(names)}")

    return groups
