"""Local image quality analysis for automatic prescreening.

The scores here are deliberately technical and explainable. They catch obvious
failures such as severe blur, dead exposure, tiny files, and low-information
frames; they do not try to judge expression, pose, or composition.

When running in expert mode, face signals come from InsightFace (via vision.py)
which provides face bbox + 5-point landmarks for sharpness and eye-open analysis.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

import numpy as np
from PIL import Image


Strength = Literal["standard", "aggressive"]


@dataclass
class QualityInfo:
    blur_score: float                       # 拉普拉斯方差（中心 60% 区域，对人像更友好）
    brightness_mean: float
    brightness_std: float
    contrast_score: float
    overexposed_ratio: float
    underexposed_ratio: float
    entropy: float
    width: int
    height: int
    file_size: int
    quality_score: float
    flags: list[str]
    auto_reject: bool
    reject_reason: str | None = None
    # 人脸感知信号（InsightFace 不可用或没检测到时为 None / 0）
    face_count: int = 0
    face_sharpness: Optional[float] = None  # 最大脸的拉普拉斯方差
    eyes_open_score: Optional[float] = None  # 眼睑开合比，越小越闭
    face_clipped: bool = False              # 主脸是否贴边

    # 新增：显著性区域锐度（替代整图锐度，对虚化主体更友好）
    salient_sharpness: Optional[float] = None
    # NIMA 美学（MobileNetV2，1-10）；None = 视觉模型未启用
    aesthetic_score: Optional[float] = None
    # MUSIQ 技术质量（pyiqa，0-100）；与 NIMA 互补——抓拍/纪实友好
    musiq_score: Optional[float] = None
    # CLIP-IQA+ LAION 美学（pyiqa，0-1）；与 NIMA 互补——构图偏好
    clipiqa_score: Optional[float] = None
    # 每张脸的明细：bbox/sharpness/eye_score/det_score/area_ratio；用于多脸硬拒规则
    faces_detail: list[dict] = field(default_factory=list)
    # 土豪模式：LLM 给的判定 + 中文短理由（其他模式恒为 None）
    llm_verdict: Optional[str] = None         # "pass" | "reject"
    llm_reason: Optional[str] = None
    # 极速模式专属：fast_quality 产出的中间量；expert 模式恒为 None
    blur_combined: Optional[float] = None     # 0-1，归一化综合锐度
    motion_anisotropy: Optional[float] = None # 0-1，FFT 方向集中度
    edge_width_pix: Optional[float] = None    # Marziliano 平均边宽
    focus_ratio: Optional[float] = None       # 主体锐度 / 背景锐度
    horizon_tilt_deg: Optional[float] = None  # 主导直线与水平/垂直的最小偏差
    composition: Optional[float] = None       # 0-1，构图分

    def to_dict(self) -> dict:
        return asdict(self)


PROFILES: dict[str, dict[str, float]] = {
    "standard": {
        "blur": 110.0,
        "very_blur": 60.0,
        "dark_mean": 22.0,
        "bright_mean": 235.0,
        "dead_shadow": 0.80,
        "dead_highlight": 0.80,
        "low_contrast": 10.0,
        "low_entropy": 0.85,
        "min_long_side": 640.0,
        "min_file_size": 25_000.0,
        "score_adjust": -2.0,
        "score_floor": 50.0,
        "face_blur": 170.0,
        "face_very_blur": 80.0,
        # 0.21-0.22 之间是"明显将闭未闭"的实测下沿；0.19 太保守、抓不到
        # 任何东西。
        "eyes_closed_ear": 0.22,
        # 美学三模型阈值（≈ pic_test p30-p35，明显低于中位线才算"偏低"）。
        # 判定规则 2-of-3（任意两个低）；low_aesthetic 在 hard 列表会真的拒。
        # 上一版 4.7 / 42 / 0.45 比实测分布下沿还低，等于不参与判定。
        "nima_low": 5.0,
        "musiq_low": 55.0,
        "clipiqa_low": 0.55,
    },
    "aggressive": {
        "blur": 200.0,
        "very_blur": 90.0,
        "dark_mean": 30.0,
        "bright_mean": 228.0,
        "dead_shadow": 0.68,
        "dead_highlight": 0.68,
        "low_contrast": 15.0,
        "low_entropy": 1.25,
        "min_long_side": 900.0,
        "min_file_size": 40_000.0,
        "score_adjust": -10.0,
        "score_floor": 65.0,
        "face_blur": 280.0,
        "face_very_blur": 120.0,
        # EAR > 0.55 已在 vision 侧兜成 None，这里不会把"坏数据"当成
        # "睁得很开"。0.25 能抓到"半睁/低头垂目"。
        "eyes_closed_ear": 0.25,
        # 进阶档对标"组内 p40-p50"：摄影师相册里 MUSIQ 一般 60-75，定
        # 在 68；CLIP-IQA 一般 0.55-0.75，定在 0.65。这两个分布跨度小，
        # 卡在中位以下才能让"平庸"图至少 2 项落败。
        "nima_low": 5.55,
        "musiq_low": 68.0,
        "clipiqa_low": 0.65,
    },
}
# 前端 prescreen_strength 用的是 "standard" / "advanced"，但这里历史上叫 "aggressive"。
# 加 alias 保证 PROFILES["advanced"] 真的拿到严格档（之前 .get("advanced") 落空 →
# 静默 fallback 到 standard，导致"进阶档"完全没生效）。
PROFILES["advanced"] = PROFILES["aggressive"]


REASON_LABELS = {
    "too_small": "非拍摄文件（疑似截图）",
    "tiny_file": "文件异常小",
    "very_blurry": "主体失焦",
    "blurry": "焦点偏软",
    "underexposed": "曝光严重不足",
    "overexposed": "高光溢出 · 细节流失",
    "low_contrast": "反差不足",
    "low_information": "画面缺少内容",
    "face_very_blurry": "人脸严重失焦",
    "face_blurry": "人脸焦点未跟上",
    "eyes_closed": "主体闭眼",
    "all_eyes_closed": "合影全员闭眼",
    "face_clipped": "人脸被切到边缘",
    "low_aesthetic": "美学评分偏低",
    "llm_reject": "AI 初筛判定为废片",
    "score_too_low": "综合质量不达标",
}


def has_face_support() -> bool:
    try:
        import insightface  # noqa
        return True
    except ImportError:
        return False


def analyze_image(
    img: Image.Image,
    file_size: int,
    strength: Strength | str = "standard",
    face_aware: bool = True,
    face_data: list[dict] | None = None,
    aesthetic_score: float | None = None,
    musiq_score: float | None = None,
    clipiqa_score: float | None = None,
) -> QualityInfo:
    """Return explainable technical quality metrics for one image.

    face_data: 预计算的人脸数据（来自 vision.extract_faces），每项含
      bbox=(x1,y1,x2,y2), kps, det_score。传入时直接用，不再重复检测。
    face_aware=False 时完全跳过人脸检测。
    aesthetic_score / musiq_score / clipiqa_score: 三个互补美学分。
      三个都低于 profile 阈值才会判 low_aesthetic（OR 救回）；
      任一缺失则跳过该规则。
    """
    profile = PROFILES.get(strength, PROFILES["standard"])
    width, height = img.size
    gray = img.convert("L")
    if max(gray.size) > 768:
        gray.thumbnail((768, 768), Image.Resampling.LANCZOS)

    arr = np.asarray(gray, dtype=np.float32)
    if arr.size == 0:
        arr = np.zeros((1, 1), dtype=np.float32)

    brightness_mean = float(arr.mean())
    brightness_std = float(arr.std())
    contrast_score = brightness_std
    underexposed_ratio = float((arr <= 8).mean())
    overexposed_ratio = float((arr >= 247).mean())
    entropy = _entropy(arr)
    blur_score = max(_laplacian_variance(arr), _laplacian_variance(_center_crop(arr, 0.6)))
    salient_sharp = _saliency_region_sharpness(arr)

    face_signals = {}
    if face_aware and face_data is not None:
        face_signals = _face_signals_from_data(face_data, img)
    elif face_aware:
        face_signals = _compute_face_signals(img)

    flags: list[str] = []
    if max(width, height) < profile["min_long_side"]:
        flags.append("too_small")
    if file_size and file_size < profile["min_file_size"]:
        flags.append("tiny_file")
    if blur_score < profile["very_blur"]:
        flags.append("very_blurry")
    elif blur_score < profile["blur"]:
        flags.append("blurry")
    if brightness_mean < profile["dark_mean"] or underexposed_ratio >= profile["dead_shadow"]:
        flags.append("underexposed")
    if brightness_mean > profile["bright_mean"] or overexposed_ratio >= profile["dead_highlight"]:
        flags.append("overexposed")
    if contrast_score < profile["low_contrast"]:
        flags.append("low_contrast")
    if entropy < profile["low_entropy"]:
        flags.append("low_information")

    face_count = face_signals.get("face_count", 0)
    face_sharp = face_signals.get("face_sharpness")
    eyes_score = face_signals.get("eyes_open_score")
    face_area_ratio = face_signals.get("face_area_ratio", 0)
    faces_detail = face_signals.get("faces_detail") or []

    # ---- 多脸硬拒规则（优先用 faces_detail，空时落 single-face 兜底）----
    if faces_detail:
        # 主脸 = area_ratio 最大、且 det_score >= 0.5 的脸。
        # 之前阈值 0.7 太严：侧脸 / 微糊的真脸 det_score 经常落在 0.55-0.7，
        # 直接排除掉就漏判 face_blurry——而那种"低置信但确实是脸"的恰恰最
        # 该拒。0.5 仍然能挡掉绝大多数背景/服饰被误判为脸的假阳。
        mains = [f for f in faces_detail if f["det_score"] >= 0.5]
        main = max(mains, key=lambda f: f["area_ratio"], default=None)

        # 主脸严重糊 → face_very_blurry；中度糊 → face_blurry
        if main is not None:
            if main["sharpness"] < profile["face_very_blur"]:
                flags.append("face_very_blurry")
            elif main["sharpness"] < profile["face_blur"]:
                flags.append("face_blurry")
            # 主脸清晰但整图被判 blurry → 救回（人像背景虚化）
            if (main["sharpness"] >= profile["face_blur"]
                    and "blurry" in flags and "very_blurry" not in flags
                    and main["area_ratio"] >= 0.01):
                flags.remove("blurry")

        # 主脸闭眼（高置信 + 面积够大）
        if (main is not None and main["eye_score"] is not None
                and main["eye_score"] < profile["eyes_closed_ear"]
                and main["area_ratio"] >= 0.005):
            if "eyes_closed" not in flags:
                flags.append("eyes_closed")

        # 全员闭眼（≥2 张高置信脸都闭眼）—— 合影场景废片
        if len(mains) >= 2 and all(
                f["eye_score"] is not None
                and f["eye_score"] < profile["eyes_closed_ear"]
                for f in mains):
            if "all_eyes_closed" not in flags:
                flags.append("all_eyes_closed")
    else:
        # 兜底：face_data 为空但通过 _compute_face_signals 拿到了 main 信号
        if face_count > 0 and face_sharp is not None:
            if face_sharp < profile["face_very_blur"]:
                flags.append("face_very_blurry")
            elif face_sharp < profile["face_blur"]:
                flags.append("face_blurry")
            if (face_sharp >= profile["face_blur"] and "blurry" in flags
                    and "very_blurry" not in flags and face_area_ratio >= 0.01):
                flags.remove("blurry")
        if (face_count > 0 and eyes_score is not None
                and eyes_score < profile["eyes_closed_ear"]
                and face_signals.get("det_score", 1.0) >= 0.5
                and face_area_ratio >= 0.005):
            flags.append("eyes_closed")

    if face_signals.get("face_clipped"):
        flags.append("face_clipped")

    # 三模型联合美学拒：NIMA + MUSIQ + CLIP-IQA+ 中**至少两个**低于阈值即拒。
    # 之前是 3-of-3（AND 全部低）→ 实际几乎拒不到任何图，加上 low_aesthetic 又没进
    # hard 列表，等于美学信号完全没参与决策。改 2-of-3 + 进 hard 之后，
    # 美学分明显偏低的"平庸但技术 OK"的图也会被拒——这是 expert 模式应有的能力。
    nima_low = profile.get("nima_low", 4.7)
    musiq_low = profile.get("musiq_low", 42.0)
    clipiqa_low = profile.get("clipiqa_low", 0.45)
    aesthetic_lows = sum([
        aesthetic_score is not None and aesthetic_score < nima_low,
        musiq_score is not None and musiq_score < musiq_low,
        clipiqa_score is not None and clipiqa_score < clipiqa_low,
    ])
    aesthetic_available = sum(s is not None for s in
                              (aesthetic_score, musiq_score, clipiqa_score))
    # 至少两个分可用（避免单分误杀），其中 ≥ 2 个低
    if aesthetic_available >= 2 and aesthetic_lows >= 2:
        flags.append("low_aesthetic")

    if face_count == 0 and salient_sharp is not None:
        if salient_sharp >= profile["blur"] and "blurry" in flags and "very_blurry" not in flags:
            flags.remove("blurry")

    quality_score = _quality_score(
        blur_score=blur_score,
        brightness_mean=brightness_mean,
        contrast_score=contrast_score,
        entropy=entropy,
        flags=flags,
        face_sharpness=face_sharp,
        score_adjust=profile["score_adjust"],
    )
    auto_reject_flags = _rejecting_flags(flags)
    score_floor = profile.get("score_floor", 30.0)
    auto_reject = bool(auto_reject_flags) or quality_score < score_floor
    reject_reason = _reason_for(flags) if auto_reject_flags else None
    if auto_reject and reject_reason is None:
        reject_reason = REASON_LABELS["score_too_low"]

    return QualityInfo(
        salient_sharpness=round(salient_sharp, 3) if salient_sharp is not None else None,
        blur_score=round(blur_score, 3),
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
        face_count=face_count,
        face_sharpness=round(face_sharp, 3) if face_sharp is not None else None,
        eyes_open_score=round(eyes_score, 4) if eyes_score is not None else None,
        face_clipped=bool(face_signals.get("face_clipped", False)),
        aesthetic_score=round(aesthetic_score, 2) if aesthetic_score is not None else None,
        musiq_score=round(musiq_score, 2) if musiq_score is not None else None,
        clipiqa_score=round(clipiqa_score, 4) if clipiqa_score is not None else None,
        faces_detail=faces_detail,
    )


def analyze_basic(
    img: Image.Image,
    file_size: int,
    llm_verdict: Optional[str] = None,
    llm_reason: Optional[str] = None,
) -> QualityInfo:
    """土豪模式专用：仅算公共基础指标 + 接收 LLM 判定，不跑任何本地拒片规则。

    返回的 QualityInfo：
      - 基础信号（尺寸、亮度、对比度、曝光比、entropy、blur_score）正常填写，
        供锦标赛卡片显示和质量分排序使用；
      - 拒片决策完全由 LLM 接管：auto_reject = (llm_verdict == "reject")；
      - flags 仅含 "llm_reject"（如果被 LLM 拒）；
      - 不跑 NIMA / MUSIQ / CLIP-IQA / 多脸 Laplacian 判定（这些 tycoon 不需要）。
    """
    width, height = img.size
    gray = img.convert("L")
    if max(gray.size) > 768:
        gray.thumbnail((768, 768), Image.Resampling.LANCZOS)
    arr = np.asarray(gray, dtype=np.float32)
    if arr.size == 0:
        arr = np.zeros((1, 1), dtype=np.float32)

    brightness_mean = float(arr.mean())
    brightness_std = float(arr.std())
    contrast_score = brightness_std
    underexposed_ratio = float((arr <= 8).mean())
    overexposed_ratio = float((arr >= 247).mean())
    entropy = _entropy(arr)
    blur_score = max(_laplacian_variance(arr), _laplacian_variance(_center_crop(arr, 0.6)))

    is_reject = (llm_verdict == "reject")
    flags = ["llm_reject"] if is_reject else []
    reason = llm_reason if is_reject else None

    # quality_score 简化算法：基础分 + LLM 拒一刀切扣分
    base_score = min(35.0, np.log1p(max(0.0, blur_score)) * 5.0)
    base_score += max(0.0, 25.0 - abs(brightness_mean - 128.0) / 128.0 * 25.0)
    base_score += min(25.0, contrast_score / 64.0 * 25.0)
    base_score += min(15.0, entropy / 7.0 * 15.0)
    if is_reject:
        base_score -= 30.0
    quality_score = float(max(0.0, min(100.0, base_score)))

    return QualityInfo(
        blur_score=round(blur_score, 3),
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
        auto_reject=is_reject,
        reject_reason=reason,
        llm_verdict=llm_verdict,
        llm_reason=llm_reason,
    )


def _center_crop(arr: np.ndarray, ratio: float) -> np.ndarray:
    h, w = arr.shape[:2]
    ch = max(1, int(h * ratio))
    cw = max(1, int(w * ratio))
    y0 = (h - ch) // 2
    x0 = (w - cw) // 2
    return arr[y0:y0 + ch, x0:x0 + cw]


def _entropy(arr: np.ndarray) -> float:
    hist, _ = np.histogram(arr, bins=256, range=(0, 255), density=False)
    total = hist.sum()
    if total <= 0:
        return 0.0
    p = hist.astype(np.float64) / total
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def _saliency_region_sharpness(arr: np.ndarray) -> Optional[float]:
    """显著性区域内的拉普拉斯方差。

    取主体（saliency 高响应区）前 20% 像素作 mask，算 Laplacian variance。
    对"主体在角落 / 主体很小"的照片比"整图 + 中心 60%"准。

    实现复用 fast_quality 里的 numpy FFT spectral residual saliency
    （cv2.saliency 在 opencv-contrib 4.10+ 已被移除，不再可用）。
    """
    from pic_selecter.fast_quality import _saliency_map, _salient_region_sharpness
    smap = _saliency_map(arr)
    return _salient_region_sharpness(arr, smap)


def _laplacian_variance(arr: np.ndarray) -> float:
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    center = arr[1:-1, 1:-1] * 4
    lap = center - arr[:-2, 1:-1] - arr[2:, 1:-1] - arr[1:-1, :-2] - arr[1:-1, 2:]
    return float(lap.var())


def _quality_score(
    *,
    blur_score: float,
    brightness_mean: float,
    contrast_score: float,
    entropy: float,
    flags: list[str],
    face_sharpness: Optional[float],
    score_adjust: float,
) -> float:
    # 有脸时脸部锐度替代整图模糊评分（更符合人像的实际质量）
    effective_blur = face_sharpness if face_sharpness is not None else blur_score
    blur_component = min(35.0, np.log1p(max(0.0, effective_blur)) * 5.0)
    exposure_component = max(0.0, 25.0 - abs(brightness_mean - 128.0) / 128.0 * 25.0)
    contrast_component = min(25.0, contrast_score / 64.0 * 25.0)
    entropy_component = min(15.0, entropy / 7.0 * 15.0)
    score = blur_component + exposure_component + contrast_component + entropy_component + score_adjust
    for flag in flags:
        if flag in {"very_blurry", "underexposed", "overexposed", "low_information",
                    "face_very_blurry", "eyes_closed", "all_eyes_closed",
                    "llm_reject"}:
            score -= 18.0
        elif flag in {"blurry", "low_contrast", "face_blurry"}:
            score -= 10.0
        elif flag in {"too_small", "tiny_file", "face_clipped", "low_aesthetic"}:
            score -= 8.0
    return float(max(0.0, min(100.0, score)))


def _rejecting_flags(flags: list[str]) -> list[str]:
    hard = {
        "very_blurry",
        "underexposed",
        "overexposed",
        "low_information",
        "too_small",
        "tiny_file",
        "face_very_blurry",
        "face_blurry",
        "eyes_closed",
        "all_eyes_closed",
        "llm_reject",
        # 美学三模型 2-of-3 低 → 真的拒；之前不在 hard 里，导致 expert
        # 模式独有的美学信号完全不参与拒片决策
        "low_aesthetic",
    }
    return [f for f in flags if f in hard]


def _reason_for(flags: list[str]) -> str | None:
    # 优先级：LLM>脸>整体；闭眼第一（客户最敏感）
    for flag in (
        "llm_reject",
        "all_eyes_closed",
        "eyes_closed",
        "face_very_blurry",
        "face_blurry",
        "very_blurry",
        "underexposed",
        "overexposed",
        "low_information",
        "too_small",
        "tiny_file",
        "blurry",
        "low_contrast",
        "face_clipped",
        "low_aesthetic",
    ):
        if flag in flags:
            return REASON_LABELS[flag]
    return None


# ---------------- 人脸信号计算 ----------------

def _face_signals_from_data(face_data: list[dict], img: Image.Image) -> dict:
    """从 vision.extract_faces() 的输出计算质量信号。

    返回字段：
      - face_count / face_sharpness / face_clipped / eyes_open_score
        / face_area_ratio / det_score：兼容字段，对应"主脸"（面积最大）。
      - faces_detail: 每张脸的 {bbox, sharpness, eye_score, det_score,
        area_ratio} 列表——供多脸硬拒规则使用。
    """
    if not face_data:
        return {"face_count": 0, "faces_detail": []}

    full_w, full_h = img.size

    def face_area(f):
        x1, y1, x2, y2 = f["bbox"]
        return max(0, x2 - x1) * max(0, y2 - y1)

    # A3 修复：之前这里有 try: import vision except: vision = None 兜底——
    # 启动期 require_expert_capabilities 已校验过，运行时再 import 失败属于
    # 异常状态，应当向上抛而不是悄悄把 eye_score 全置 None。
    from pic_selecter import vision

    faces_detail: list[dict] = []
    for f in face_data:
        x1, y1, x2, y2 = f["bbox"]
        cx1, cy1 = max(0, x1), max(0, y1)
        cx2, cy2 = min(full_w, x2), min(full_h, y2)
        if cx2 <= cx1 or cy2 <= cy1:
            continue
        crop = np.asarray(img.crop((cx1, cy1, cx2, cy2)).convert("L"), dtype=np.float32)
        if max(crop.shape) > 256:
            crop_pil = Image.fromarray(crop.astype(np.uint8))
            crop_pil.thumbnail((256, 256), Image.Resampling.LANCZOS)
            crop = np.asarray(crop_pil, dtype=np.float32)
        sharp = _laplacian_variance(crop)
        # A4 修复：以前 eye_score 异常被静默吞成 None，现在至少打 warning。
        # 单张图的 EAR 计算偶发失败（landmark 缺失）属"数据不足"，可继续；
        # 但 silently 全静默 → 调阈值时根本看不见信号丢失。
        try:
            eye_score = vision.compute_eye_open_score(f, img)
        except Exception as _eye_exc:
            import logging as _logging
            _logging.getLogger("pic_selecter").warning(
                f"compute_eye_open_score 失败: {type(_eye_exc).__name__}: {_eye_exc}"
            )
            eye_score = None
        fw = cx2 - cx1
        fh = cy2 - cy1
        area_ratio = (fw * fh) / max(1, full_w * full_h)
        faces_detail.append({
            "bbox": [int(cx1), int(cy1), int(cx2), int(cy2)],
            "sharpness": round(float(sharp), 3),
            "eye_score": round(float(eye_score), 4) if eye_score is not None else None,
            "det_score": float(f.get("det_score", 1.0)),
            "area_ratio": round(float(area_ratio), 5),
        })

    if not faces_detail:
        return {"face_count": len(face_data), "faces_detail": []}

    # 主脸 = 面积最大的（与历史行为一致）
    main_idx = max(range(len(faces_detail)),
                   key=lambda i: faces_detail[i]["area_ratio"])
    main = faces_detail[main_idx]
    main_raw = face_data[face_data.index(face_data[main_idx])] if main_idx < len(face_data) else face_data[0]
    x1, y1, x2, y2 = main["bbox"]

    margin = max(2.0, min(full_w, full_h) * 0.008)
    clipped = (x1 <= margin or x2 >= full_w - margin or y2 >= full_h - margin)

    return {
        "face_count": len(faces_detail),
        "face_sharpness": main["sharpness"],
        "face_clipped": bool(clipped),
        "eyes_open_score": main["eye_score"],
        "face_area_ratio": main["area_ratio"],
        "det_score": main["det_score"],
        "faces_detail": faces_detail,
    }


def _compute_face_signals(img: Image.Image) -> dict:
    """独立人脸检测（非 expert 模式、或 face_data 未传入时的后备路径）。
    用 InsightFace 直接检测。

    **不再静默吞异常**（A2 修复）：
    - vision.extract_faces 返回 [] = "图里没人脸" → 合法，返回 {face_count: 0}
    - 抛 VisionUnavailable / 任何其他异常 → 向上抛，由 worker 区分是
      整图 skip 还是把任务挂掉。以前 `except Exception: return {}` 会让
      expert 模式跑到一半 InsightFace 崩了也假装"这张没人脸"，废片放过。
    """
    from pic_selecter import vision
    faces = vision.extract_faces(img)
    return _face_signals_from_data(faces, img)
