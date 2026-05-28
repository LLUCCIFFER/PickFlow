"""极速模式质量评估：零模型依赖，纯 numpy + opencv。

与 quality.py（专家模式）的差异：
- 不调 mediapipe、不依赖任何深度学习权重
- 多锐度指标融合：拉普拉斯 + Tenengrad + FFT 高频比 + 边缘宽度（Marziliano）
- 区分失焦 vs 运动模糊：FFT 谱的方向性 + 边缘宽度
- 9 宫格局部曝光（替代单一全图直方图）
- 简单构图分：saliency 重心三分线偏离 + Hough 水平线倾斜

返回 QualityInfo（复用 quality.py 的 dataclass），face_* 字段保持 None / 0，
flags 里可能多出 motion_blur / horizon_tilt / bad_composition。
"""

from __future__ import annotations

import math
from typing import Literal, Optional

import cv2  # 极速模式硬依赖；缺失就让本模块导入失败，启动期暴露
import numpy as np
from PIL import Image

from pic_selecter.quality import (
    QualityInfo,
    REASON_LABELS,
    _laplacian_variance,
    _entropy,
    _center_crop,
)

Strength = Literal["standard", "aggressive"]


# 极速模式独有的 flag / 原因
EXTRA_REASONS = {
    "motion_blur": "运动模糊 · 手抖或模特动",
    "subject_blurry": "主体不够清晰",
    "horizon_tilt": "地平线明显歪斜",
    "horizon_severe": "歪斜严重 · 失控构图",
    "score_too_low": "综合质量不达标",
}
for k, v in EXTRA_REASONS.items():
    REASON_LABELS.setdefault(k, v)


PROFILES: dict[str, dict[str, float]] = {
    # 阈值标定基于 pic_test (143 张真实大相机文件) 的 salient_sharp 分布：
    #   p10=485 p25=729 p50=1094 p75=2265
    # 设计目标：
    #   - very_subject_sharp 在 ~p15-p20 → 命中明显糊片
    #   - subject_sharp 在 ~p35 → 软提示扣分
    #   - horizon_severe >15° → hard reject（人眼难以接受这级别歪斜）
    "standard": {
        # 主体锐度：salient_sharp 的拉普拉斯方差
        "subject_sharp": 750.0,        # 软扣分（≈ pic_test p25-p30）
        "very_subject_sharp": 550.0,   # 硬拒（≈ pic_test p15）
        # 整图融合锐度兜底：saliency 失败时才生效（smap.std() < 0.01 等极端）
        "very_blur_combined": 0.28,
        # 运动模糊：4 条件 AND
        "motion_anisotropy": 0.62,
        "edge_width_pix": 5.0,
        # 曝光
        "dark_mean": 22.0,
        "bright_mean": 235.0,
        "dead_shadow": 0.82,
        "dead_highlight": 0.82,
        # 内容
        "low_contrast": 10.0,
        "low_entropy": 0.85,
        # 文件本身
        "min_long_side": 640.0,
        "min_file_size": 25_000.0,
        # 地平线倾斜
        "horizon_tilt_deg": 4.5,       # 软提示
        "horizon_severe_deg": 15.0,    # 硬拒
        # 评分总线
        "score_adjust": 0.0,
        "score_floor": 35.0,
    },
    "aggressive": {
        "subject_sharp": 1100.0,
        "very_subject_sharp": 650.0,   # ≈ p20-p25，比 standard 严但避免误杀中位线（p50=963）
        "very_blur_combined": 0.35,
        "motion_anisotropy": 0.55,
        "edge_width_pix": 4.0,
        "dark_mean": 28.0,
        "bright_mean": 228.0,
        "dead_shadow": 0.70,
        "dead_highlight": 0.70,
        "low_contrast": 14.0,
        "low_entropy": 1.20,
        "min_long_side": 900.0,
        "min_file_size": 40_000.0,
        "horizon_tilt_deg": 3.0,
        "horizon_severe_deg": 12.0,    # 进阶档：12° 硬拒
        "score_adjust": -6.0,
        "score_floor": 45.0,
    },
}
# 前端用 "advanced"，这里历史 key 是 "aggressive"；alias 避免静默 fallback。
PROFILES["advanced"] = PROFILES["aggressive"]


# ---------------- 辅助 ----------------

def _resize_for_analysis(img: Image.Image, long_side: int = 768) -> Image.Image:
    if max(img.size) <= long_side:
        return img
    out = img.copy()
    out.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
    return out


def _tenengrad(arr: np.ndarray) -> float:
    """Sobel 梯度平方和。比拉普拉斯方差对噪点更鲁棒，对边缘量更敏感。"""
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    gx = arr[1:-1, 2:] - arr[1:-1, :-2]
    gy = arr[2:, 1:-1] - arr[:-2, 1:-1]
    return float((gx * gx + gy * gy).mean())


def _fft_high_freq_ratio(arr: np.ndarray) -> tuple[float, float]:
    """返回 (高频能量比, 方向各向异性)。

    - 高频能量比：频谱半径 > 0.3 * Nyquist 的能量占总能量的比，越大越锐。
    - 方向各向异性：把频谱按 12 个方向角 bin 聚合，max/mean。运动模糊的频谱
      沿垂直运动方向的方向上有规则零线 → 某方向能量明显偏低 → 各向异性高。
    """
    if arr.shape[0] < 16 or arr.shape[1] < 16:
        return 0.0, 0.0
    # 取中央方形区域，避免长宽比影响频谱形状
    h, w = arr.shape
    s = min(h, w)
    y0 = (h - s) // 2
    x0 = (w - s) // 2
    crop = arr[y0:y0 + s, x0:x0 + s]
    # 降到 256 加快
    if s > 256:
        crop = cv2.resize(crop.astype(np.float32), (256, 256), interpolation=cv2.INTER_AREA)
        s = 256
    crop = crop - crop.mean()
    # 加 Hann 窗减少边缘泄漏
    win = np.outer(np.hanning(s), np.hanning(s)).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft2(crop * win))
    mag = np.abs(spec).astype(np.float32)
    mag[s // 2, s // 2] = 0.0  # 去 DC

    yy, xx = np.mgrid[:s, :s].astype(np.float32)
    cy, cx = s / 2.0, s / 2.0
    dy = yy - cy
    dx = xx - cx
    r = np.sqrt(dy * dy + dx * dx)
    r_max = s / 2.0
    # 高频能量比
    high_mask = r > 0.30 * r_max
    total = float(mag.sum() + 1e-8)
    high_e = float(mag[high_mask].sum())
    high_ratio = high_e / total

    # 方向各向异性：只在中频环带（避免 DC 和极高频）
    band = (r > 0.10 * r_max) & (r < 0.50 * r_max)
    if band.sum() < 50:
        return high_ratio, 0.0
    theta = np.arctan2(dy, dx)  # -pi..pi
    # 折叠到 0..pi（频谱中心对称）
    theta = np.where(theta < 0, theta + math.pi, theta)
    n_bins = 12
    bin_idx = np.minimum(n_bins - 1, (theta / math.pi * n_bins).astype(np.int32))
    band_mag = mag[band]
    band_bin = bin_idx[band]
    sums = np.bincount(band_bin, weights=band_mag, minlength=n_bins)
    counts = np.bincount(band_bin, minlength=n_bins).astype(np.float32) + 1e-6
    avg = sums / counts
    mean_e = float(avg.mean() + 1e-8)
    aniso = float((avg.max() - avg.min()) / (avg.max() + 1e-8))
    return high_ratio, aniso


def _edge_width_marziliano(arr: np.ndarray) -> Optional[float]:
    """Marziliano 边缘宽度：沿水平方向找垂直边缘，测局部从极小到极大的横向距离。

    越大 → 边缘越宽 → 越模糊。失焦边缘宽度对称，运动模糊会受方向影响。
    返回 None 表示数据不足（图过小 / 边缘点太少），不是能力降级。
    """
    if arr.shape[0] < 16 or arr.shape[1] < 16:
        return None
    a = arr.astype(np.float32)
    # Canny 找边缘像素
    a_u8 = np.clip(a, 0, 255).astype(np.uint8)
    edges = cv2.Canny(a_u8, 50, 150)
    ys, xs = np.where(edges > 0)
    if len(xs) < 50:
        return None
    # 太多边缘点 → 抽样以加速
    if len(xs) > 800:
        idx = np.random.default_rng(0).choice(len(xs), 800, replace=False)
        ys, xs = ys[idx], xs[idx]
    widths = []
    h, w = a.shape
    for y, x in zip(ys, xs):
        if x < 3 or x > w - 4:
            continue
        # 沿水平方向找局部极小到极大的距离，限制 12 像素半径
        left = x
        for k in range(1, 12):
            if x - k < 1:
                break
            if a[y, x - k] >= a[y, x - k + 1]:
                left = x - k
            else:
                break
        right = x
        for k in range(1, 12):
            if x + k > w - 2:
                break
            if a[y, x + k] <= a[y, x + k - 1]:
                right = x + k
            else:
                break
        wpx = right - left
        if 1 <= wpx <= 25:
            widths.append(wpx)
    if len(widths) < 30:
        return None
    return float(np.mean(widths))


def _nine_grid_exposure(arr: np.ndarray) -> dict:
    """9 宫格曝光分析。返回每格的 mean / clip ratio 以及"最差格"指标。"""
    h, w = arr.shape
    if h < 9 or w < 9:
        return {"worst_dark": 0.0, "worst_bright": 0.0, "worst_clip_dark": 0.0,
                "worst_clip_bright": 0.0}
    ys = [0, h // 3, 2 * h // 3, h]
    xs = [0, w // 3, 2 * w // 3, w]
    worst_dark = 255.0
    worst_bright = 0.0
    worst_clip_dark = 0.0
    worst_clip_bright = 0.0
    for i in range(3):
        for j in range(3):
            block = arr[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            if block.size == 0:
                continue
            m = float(block.mean())
            cd = float((block <= 8).mean())
            cb = float((block >= 247).mean())
            worst_dark = min(worst_dark, m)
            worst_bright = max(worst_bright, m)
            worst_clip_dark = max(worst_clip_dark, cd)
            worst_clip_bright = max(worst_clip_bright, cb)
    return {
        "worst_dark": worst_dark,
        "worst_bright": worst_bright,
        "worst_clip_dark": worst_clip_dark,
        "worst_clip_bright": worst_clip_bright,
    }


def _horizon_tilt_degrees(arr: np.ndarray) -> Optional[float]:
    """Hough 找主导直线，返回其与水平/垂直的最小偏差角度（degrees）。

    None 表示找不到强主导直线（无明显地平线等参考），数据不足非降级。
    """
    if arr.shape[0] < 64 or arr.shape[1] < 64:
        return None
    a_u8 = np.clip(arr, 0, 255).astype(np.uint8)
    edges = cv2.Canny(a_u8, 50, 150)
    min_len = max(40, int(min(arr.shape) * 0.35))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                            minLineLength=min_len, maxLineGap=10)
    if lines is None or len(lines) == 0:
        return None
    # 按长度加权找最强方向；返回与水平/垂直的最小偏差
    angles = []
    weights = []
    for ln in lines[:200]:
        x1, y1, x2, y2 = ln[0]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length < min_len:
            continue
        theta = math.degrees(math.atan2(dy, dx))
        # 折到 [-90, 90]
        if theta > 90:
            theta -= 180
        elif theta < -90:
            theta += 180
        # 与水平/垂直的最小偏差
        dev = min(abs(theta), abs(90 - abs(theta)))
        angles.append(dev)
        weights.append(length)
    if not angles:
        return None
    # 找"最像水平/垂直"的强线
    arr_a = np.array(angles)
    arr_w = np.array(weights)
    # 找偏差最小的前 1/3 线，加权平均
    n_take = max(1, len(arr_a) // 3)
    idx = np.argsort(arr_a)[:n_take]
    avg = float(np.average(arr_a[idx], weights=arr_w[idx]))
    return avg


def _composition_score(arr: np.ndarray, saliency_map: Optional[np.ndarray]) -> float:
    """构图分（0-1）：主体重心是否在三分线交点附近 + 主体大小合理。

    saliency_map 为 None 时退化为"中心居中"分。
    """
    h, w = arr.shape
    if saliency_map is None:
        return 0.5
    smap = saliency_map.astype(np.float32)
    total = float(smap.sum() + 1e-8)
    if total < 1e-3:
        return 0.4
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    cy = float((yy * smap).sum() / total) / max(h - 1, 1)
    cx = float((xx * smap).sum() / total) / max(w - 1, 1)
    # 三分线 4 个交点
    grid_pts = [(1 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1 / 3), (2 / 3, 2 / 3)]
    center_pt = (0.5, 0.5)
    d_grid = min(math.hypot(cy - py, cx - px) for py, px in grid_pts)
    d_center = math.hypot(cy - center_pt[0], cx - center_pt[1])
    # 取两者最小（三分线和居中都算合理构图）
    d = min(d_grid, d_center)
    # 距离 0 → 1.0，距离 0.35 → 0
    pos_score = max(0.0, 1.0 - d / 0.35)

    # 主体大小：响应 > 80% 分位的像素占比
    thr = np.quantile(smap, 0.80)
    mask = smap >= thr
    frac = float(mask.mean())
    # 主体占 8%-40% → 最好；过小或过大都扣分
    if frac < 0.04:
        size_score = frac / 0.04
    elif frac > 0.55:
        size_score = max(0.0, 1.0 - (frac - 0.55) / 0.45)
    else:
        size_score = 1.0

    # 主体是否贴边：边缘 5% 区域里的主体占比 > 30% → 扣分
    edge_mask = np.zeros_like(mask)
    eh = max(2, int(h * 0.05))
    ew = max(2, int(w * 0.05))
    edge_mask[:eh] = True
    edge_mask[-eh:] = True
    edge_mask[:, :ew] = True
    edge_mask[:, -ew:] = True
    total_subject = float(mask.sum() + 1e-6)
    edge_subject = float((mask & edge_mask).sum())
    edge_frac = edge_subject / total_subject
    edge_score = 1.0 if edge_frac < 0.25 else max(0.0, 1.0 - (edge_frac - 0.25) / 0.5)

    return 0.5 * pos_score + 0.3 * size_score + 0.2 * edge_score


def _saliency_map(arr: np.ndarray):
    """返回 saliency map（float32 0-1），用 numpy FFT 实现 spectral residual。

    Hou & Zhang (2007) 算法：log 幅度谱减去平滑后的谱 → 逆 FFT → 高斯模糊。
    不依赖 cv2.saliency（该模块在 opencv-contrib 4.10+ 中被移除）。
    """
    h, w = arr.shape[:2]
    if h < 8 or w < 8:
        return None
    # 缩到 64x64 计算，再放大回原尺寸
    small = cv2.resize(arr, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    f = np.fft.fft2(small)
    log_amp = np.log(np.abs(f) + 1e-8)
    # 平均滤波器平滑 log 幅度谱（3x3）
    kernel = np.ones((3, 3), np.float32) / 9.0
    smooth = cv2.filter2D(log_amp, -1, kernel)
    # spectral residual
    residual = log_amp - smooth
    # 重建：exp(R) * 原始相位 → 逆 FFT → 平方 → 高斯模糊
    phase = np.angle(f)
    recon = np.fft.ifft2(np.exp(residual + 1j * phase))
    smap = np.abs(recon) ** 2
    smap = cv2.GaussianBlur(smap.astype(np.float32), (9, 9), 2.5)
    smap = cv2.resize(smap, (w, h), interpolation=cv2.INTER_LINEAR)
    mn, mx = smap.min(), smap.max()
    if mx - mn < 1e-8:
        return None
    smap = (smap - mn) / (mx - mn)
    if smap.std() < 0.01:
        return None
    return smap


def _salient_region_sharpness(arr: np.ndarray, smap: Optional[np.ndarray]) -> Optional[float]:
    """主体（saliency 高响应区）内的拉普拉斯方差——衡量"主体糊不糊"。

    本地实现，不依赖 cv2.saliency（该模块在 OpenCV-contrib 4.10+ 已移除）。
    smap 已经是 numpy FFT 算出来的 spectral residual saliency map。
    取响应最高的前 20% 像素作 mask，在原图灰度上算 Laplacian variance。
    """
    if smap is None:
        return None
    if arr.shape[0] < 16 or arr.shape[1] < 16:
        return None
    try:
        sm = smap.astype(np.float32)
        if sm.std() < 0.01:
            return None
        # smap 可能比 arr 小（_saliency_map 内部下采样到 256）；resize 回 arr 大小
        if sm.shape != arr.shape:
            try:
                import cv2
                sm = cv2.resize(sm, (arr.shape[1], arr.shape[0]),
                                interpolation=cv2.INTER_LINEAR)
            except ImportError:
                return None
        thr = np.quantile(sm, 0.80)
        mask = sm >= thr
        if mask.sum() < 100:
            return None
        if arr.shape[0] < 3 or arr.shape[1] < 3:
            return None
        center = arr[1:-1, 1:-1] * 4
        lap = center - arr[:-2, 1:-1] - arr[2:, 1:-1] - arr[1:-1, :-2] - arr[1:-1, 2:]
        m = mask[1:-1, 1:-1]
        sel = lap[m]
        if sel.size < 100:
            return None
        return float(sel.var())
    except Exception:
        return None


def _saliency_focus_consistency(arr: np.ndarray, smap: Optional[np.ndarray]) -> Optional[float]:
    """主体锐度 / 背景锐度的比值。> 1 表示焦点在主体上；< 1 表示焦点错位。

    返回 None 表示样本不足。
    """
    if smap is None:
        return None
    if arr.shape[0] < 32 or arr.shape[1] < 32:
        return None
    thr = np.quantile(smap, 0.80)
    sub_mask = smap >= thr
    bg_mask = smap <= np.quantile(smap, 0.30)
    if sub_mask.sum() < 100 or bg_mask.sum() < 100:
        return None
    # 拉普拉斯
    a = arr.astype(np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return None
    center = a[1:-1, 1:-1] * 4
    lap = center - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    sm = sub_mask[1:-1, 1:-1]
    bm = bg_mask[1:-1, 1:-1]
    sub_var = float(lap[sm].var()) if sm.any() else 0.0
    bg_var = float(lap[bm].var()) if bm.any() else 1e-6
    return sub_var / (bg_var + 1e-6)


# ---------------- 主入口 ----------------

def analyze_image_fast(
    img: Image.Image,
    file_size: int,
    strength: Strength | str = "standard",
) -> QualityInfo:
    """极速模式：纯传统 CV，返回与 QualityInfo 字段兼容的结果。

    face_* 字段恒为 None / 0 / False。
    """
    profile = PROFILES.get(strength, PROFILES["standard"])
    width, height = img.size

    work = _resize_for_analysis(img.convert("L"), 768)
    arr = np.asarray(work, dtype=np.float32)
    if arr.size == 0:
        arr = np.zeros((1, 1), dtype=np.float32)

    brightness_mean = float(arr.mean())
    brightness_std = float(arr.std())
    contrast_score = brightness_std
    underexposed_ratio = float((arr <= 8).mean())
    overexposed_ratio = float((arr >= 247).mean())
    entropy = _entropy(arr)

    # ---- 多锐度指标 ----
    # 拉普拉斯（中心 60% 取大者，与专家模式保持一致的 baseline）
    lap = max(_laplacian_variance(arr), _laplacian_variance(_center_crop(arr, 0.6)))
    teng = _tenengrad(arr)
    high_ratio, motion_aniso = _fft_high_freq_ratio(arr)
    edge_width = _edge_width_marziliano(arr)

    # 归一化到 [0, 1]：log 压缩 + 软上限
    lap_norm = min(1.0, math.log1p(max(0.0, lap)) / math.log1p(900.0))
    teng_norm = min(1.0, math.log1p(max(0.0, teng)) / math.log1p(2000.0))
    high_norm = min(1.0, max(0.0, high_ratio) / 0.40)
    # 边缘宽度反向：3 像素 → 1.0；10 像素 → 0
    if edge_width is None:
        ew_norm = None
    else:
        ew_norm = max(0.0, min(1.0, (10.0 - edge_width) / 7.0))

    # 综合锐度（0-1）：融合 4 个指标
    parts = [lap_norm, teng_norm, high_norm]
    if ew_norm is not None:
        parts.append(ew_norm)
    blur_combined = float(np.mean(parts))

    # ---- saliency 相关 ----
    smap = _saliency_map(arr)
    salient_sharp = None
    focus_ratio = None
    composition = None
    if smap is not None:
        salient_sharp = _salient_region_sharpness(arr, smap)
        focus_ratio = _saliency_focus_consistency(arr, smap)
        composition = _composition_score(arr, smap)

    nine = _nine_grid_exposure(arr)
    horizon_tilt = _horizon_tilt_degrees(arr)

    # ---- 生成 flags ----
    flags: list[str] = []
    if max(width, height) < profile["min_long_side"]:
        flags.append("too_small")
    if file_size and file_size < profile["min_file_size"]:
        flags.append("tiny_file")

    # 主体锐度（saliency 区域内拉普拉斯方差）是替代"人脸锐度"的核心信号——
    # 大相机文件里背景细节多，整图 sharp 不可靠；主体区域真实锐度才反映主体糊不糊
    if salient_sharp is not None:
        if salient_sharp < profile["very_subject_sharp"]:
            flags.append("very_blurry")
        elif salient_sharp < profile["subject_sharp"]:
            flags.append("subject_blurry")

    # 整图融合锐度兜底：仅在 saliency 算不出（极低对比图等）时补上 very_blurry
    # 注：之前还有 blurry 软扣分一路，对 pic_test 0 命中、与 saliency 重叠，删掉
    if "very_blurry" not in flags and blur_combined < profile["very_blur_combined"]:
        flags.append("very_blurry")

    # 运动模糊：FFT 方向各向异性高 + 边宽大 + 主体真的糊
    # focus_ratio 必须不能远大于 1——远大于 1 说明只是浅景深（背景虚化），不是 motion blur
    is_motion = (
        motion_aniso > profile["motion_anisotropy"]
        and edge_width is not None and edge_width > profile["edge_width_pix"]
        and salient_sharp is not None and salient_sharp < profile["subject_sharp"]
        and (focus_ratio is None or focus_ratio < 5.0)
    )
    if is_motion:
        for f in ("blurry", "subject_blurry"):
            if f in flags:
                flags.remove(f)
        if "very_blurry" not in flags:
            flags.append("motion_blur")

    # （之前的 off_subject_focus 判据已删——pic_test 上 0 命中，focus_ratio 几乎都 >1）

    # 曝光：全局为主；九宫格仅当全图也偏向同方向时才加持（排除单角落创意光效）
    if brightness_mean < profile["dark_mean"] or underexposed_ratio >= profile["dead_shadow"]:
        flags.append("underexposed")
    elif (nine["worst_clip_dark"] >= profile["dead_shadow"]
          and brightness_mean < profile["dark_mean"] * 3):
        flags.append("underexposed")
    if brightness_mean > profile["bright_mean"] or overexposed_ratio >= profile["dead_highlight"]:
        flags.append("overexposed")
    elif (nine["worst_clip_bright"] >= profile["dead_highlight"]
          and brightness_mean > profile["bright_mean"] - 20):
        flags.append("overexposed")
    if contrast_score < profile["low_contrast"]:
        flags.append("low_contrast")
    if entropy < profile["low_entropy"]:
        flags.append("low_information")

    # （之前的 bad_composition 软提示已删——主观判据，0 命中）

    # 地平线倾斜：
    # - 软扣分（horizon_tilt）：超过 profile["horizon_tilt_deg"]（默认 4.5°）
    # - 硬拒（horizon_severe）：超过 profile["horizon_severe_deg"]（默认 15°）
    #   15°+ 几乎肯定是手持失误，人眼难以接受；不接受"创意倾斜"狡辩——
    #   真要倾斜构图也不会到 15°+ 还把地平线留在画面里。
    if horizon_tilt is not None:
        sev = profile.get("horizon_severe_deg", 15.0)
        if horizon_tilt > sev:
            flags.append("horizon_severe")
        elif horizon_tilt > profile["horizon_tilt_deg"]:
            flags.append("horizon_tilt")

    quality_score = _compute_score(
        blur_combined=blur_combined,
        brightness_mean=brightness_mean,
        contrast_score=contrast_score,
        entropy=entropy,
        composition=composition,
        flags=flags,
        score_adjust=profile["score_adjust"],
    )
    auto_reject_flags = _rejecting_flags_fast(flags)
    score_floor = profile.get("score_floor", 30.0)
    auto_reject = bool(auto_reject_flags) or quality_score < score_floor
    reject_reason = _reason_for_fast(flags) if auto_reject_flags else None
    if auto_reject and reject_reason is None:
        reject_reason = EXTRA_REASONS["score_too_low"]

    return QualityInfo(
        blur_score=round(lap, 3),
        brightness_mean=round(brightness_mean, 3),
        brightness_std=round(brightness_std, 3),
        contrast_score=round(contrast_score, 3),
        overexposed_ratio=round(overexposed_ratio, 5),
        underexposed_ratio=round(underexposed_ratio, 5),
        entropy=round(entropy, 5),
        width=width,
        height=height,
        file_size=int(file_size or 0),
        quality_score=round(quality_score, 3),
        flags=flags,
        auto_reject=auto_reject,
        reject_reason=reject_reason,
        face_count=0,
        face_sharpness=None,
        eyes_open_score=None,
        face_clipped=False,
        salient_sharpness=round(salient_sharp, 3) if salient_sharp is not None else None,
        aesthetic_score=None,
        # fast-only 中间量（便于 log 复盘与调参）
        blur_combined=round(blur_combined, 3),
        motion_anisotropy=round(motion_aniso, 3),
        edge_width_pix=round(edge_width, 2) if edge_width is not None else None,
        focus_ratio=round(focus_ratio, 3) if focus_ratio is not None else None,
        horizon_tilt_deg=round(horizon_tilt, 2) if horizon_tilt is not None else None,
        composition=round(composition, 3) if composition is not None else None,
    )


def _compute_score(
    *,
    blur_combined: float,
    brightness_mean: float,
    contrast_score: float,
    entropy: float,
    composition: Optional[float],
    flags: list[str],
    score_adjust: float,
) -> float:
    """0-100 综合分。"""
    blur_component = blur_combined * 35.0
    exposure_component = max(0.0, 25.0 - abs(brightness_mean - 128.0) / 128.0 * 25.0)
    contrast_component = min(20.0, contrast_score / 64.0 * 20.0)
    entropy_component = min(10.0, entropy / 7.0 * 10.0)
    comp_component = (composition if composition is not None else 0.5) * 10.0
    score = (blur_component + exposure_component + contrast_component
             + entropy_component + comp_component + score_adjust)
    for flag in flags:
        if flag in {"very_blurry", "motion_blur", "underexposed", "overexposed",
                    "low_information", "horizon_severe"}:
            score -= 22.0
        elif flag in {"subject_blurry", "low_contrast", "horizon_tilt"}:
            score -= 12.0
        elif flag in {"too_small", "tiny_file"}:
            score -= 6.0
    return float(max(0.0, min(100.0, score)))


def _rejecting_flags_fast(flags: list[str]) -> list[str]:
    """哪些 flag 触发自动淘汰。

    soft（仅扣分、不自动 reject）：subject_blurry / horizon_tilt / low_contrast
    hard（自动 reject）：very_blurry / motion_blur / 严重曝光 /
                       严重歪斜（horizon_severe）/ 尺寸太小 / 内容信息量太低
    """
    hard = {
        "very_blurry", "motion_blur",
        "underexposed", "overexposed", "low_information",
        "too_small", "tiny_file",
        "horizon_severe",
    }
    return [f for f in flags if f in hard]


def _reason_for_fast(flags: list[str]) -> Optional[str]:
    for flag in (
        "motion_blur",
        "very_blurry",
        "subject_blurry",
        "horizon_severe",
        "underexposed",
        "overexposed",
        "low_information",
        "too_small",
        "tiny_file",
        "horizon_tilt",
        "low_contrast",
    ):
        if flag in flags:
            return REASON_LABELS.get(flag, flag)
    return None
