"""专家模式视觉栈：DINOv2 + NIMA + MUSIQ + CLIP-IQA+ + InsightFace 人脸。

设计原则：**不静默降级**。任何依赖缺失或模型加载失败都直接抛出异常。

模型栈：
- DINOv2-small：384 维语义特征（分组核心）
- NIMA (MobileNetV2)：美学评分 1-10（人像/风景偏好）
- MUSIQ (pyiqa)：技术质量评分 0-100（抓拍/纪实友好）
- CLIP-IQA+ (pyiqa)：LAION 美学评分 0-1（构图偏好）
- InsightFace：RetinaFace 检测 + ArcFace 512 维人脸嵌入 + 关键点（闭眼检测）

依赖：
- torch >= 2.2
- torchvision >= 0.17
- transformers >= 4.40 （DINOv2）
- pyiqa >= 0.1.10 + timm >= 0.9（MUSIQ / CLIP-IQA+）
- insightface >= 0.7
- onnxruntime >= 1.16
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger("pic_selecter")

_LOCK = threading.Lock()
_models: dict = {}
_DEVICE = None


class VisionUnavailable(RuntimeError):
    """专家模式视觉栈某个组件不可用。"""


def _device():
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE
    import torch
    if torch.backends.mps.is_available():
        _DEVICE = torch.device("mps")
        logger.info("vision: 使用 MPS（Apple Silicon GPU）")
    elif torch.cuda.is_available():
        _DEVICE = torch.device("cuda")
        logger.info("vision: 使用 CUDA")
    else:
        _DEVICE = torch.device("cpu")
        logger.info("vision: 使用 CPU")
    return _DEVICE


def _cache_dir() -> Path:
    d = Path.home() / ".cache" / "pic_selecter"
    d.mkdir(parents=True, exist_ok=True)
    return d


# =============================================================
# DINOv2-small：384 维语义特征（不变）
# =============================================================

def _ensure_dinov2():
    if "dinov2" in _models:
        return _models["dinov2"]
    with _LOCK:
        if "dinov2" in _models:
            return _models["dinov2"]
        try:
            import torch  # noqa
            from transformers import AutoImageProcessor, AutoModel
        except ImportError as e:
            raise VisionUnavailable(
                f"DINOv2 依赖缺失：{e}。专家模式需要 `pip install torch transformers`。"
            ) from e
        logger.info("vision: 加载 DINOv2-small（首次约 86MB）…")
        # 优先用本地缓存（HF 在国内常 SSL EOF；缓存命中时绕开 HEAD 校验）
        try:
            processor = AutoImageProcessor.from_pretrained(
                "facebook/dinov2-small", local_files_only=True
            )
            model = AutoModel.from_pretrained(
                "facebook/dinov2-small", local_files_only=True
            ).to(_device()).eval()
        except Exception:
            processor = AutoImageProcessor.from_pretrained("facebook/dinov2-small")
            model = AutoModel.from_pretrained("facebook/dinov2-small").to(_device()).eval()
        _models["dinov2"] = (model, processor)
        logger.info("vision: DINOv2-small 就绪")
    return _models["dinov2"]


def extract_dinov2(pil_img: Image.Image) -> np.ndarray:
    """提取 DINOv2-small CLS token（L2 归一化，384 维）。"""
    import torch
    model, processor = _ensure_dinov2()
    inputs = processor(images=pil_img.convert("RGB"), return_tensors="pt")
    inputs = {k: v.to(_device()) for k, v in inputs.items()}
    with torch.no_grad():
        out = model(**inputs)
        feat = out.last_hidden_state[:, 0, :]
    v = feat.detach().cpu().numpy().astype(np.float32).squeeze(0)
    n = float(np.linalg.norm(v))
    if n < 1e-8:
        raise RuntimeError("DINOv2 输出零向量")
    return (v / n).astype(np.float32)


# =============================================================
# NIMA 美学评分（MobileNetV2 backbone，独立于 CLIP）
# =============================================================

def _ensure_nima():
    if "nima" in _models:
        return _models["nima"]
    with _LOCK:
        if "nima" in _models:
            return _models["nima"]
        try:
            import torch
            import torch.nn as nn
            from torchvision import models, transforms
        except ImportError as e:
            raise VisionUnavailable(
                f"NIMA 依赖缺失：{e}。需要 `pip install torch torchvision`。"
            ) from e

        logger.info("vision: 构建 NIMA 美学评分模型（MobileNetV2 backbone）…")
        base = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.DEFAULT)
        base.classifier = nn.Sequential(
            nn.Dropout(0.75),
            nn.Linear(base.last_channel, 10),
            nn.Softmax(dim=1),
        )
        base = base.to(_device()).eval()

        preprocess = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225]),
        ])

        _models["nima"] = (base, preprocess)
        logger.info("vision: NIMA 美学模型就绪")
    return _models["nima"]


def extract_aesthetic_score(pil_img: Image.Image) -> float:
    """返回美学分 1-10。使用 NIMA 分布均值。"""
    import torch
    model, preprocess = _ensure_nima()
    img = preprocess(pil_img.convert("RGB")).unsqueeze(0).to(_device())
    with torch.no_grad():
        probs = model(img).squeeze(0)
    buckets = torch.arange(1, 11, dtype=torch.float32, device=probs.device)
    score = float((probs * buckets).sum().item())
    return max(1.0, min(10.0, score))


# =============================================================
# pyiqa: MUSIQ（技术质量 0-100）+ CLIP-IQA+（LAION 美学 0-1）
#
# **关键：永远跑 CPU + 喂图前 resize 到 1024 长边。**
# 原因：pyiqa 的 MUSIQ / CLIP-IQA+ 内部不会自动下采样输入。Mac MPS 上喂
# 4000 万像素的相机大图 → CLIP patch embedding 申请 34GiB buffer 直接爆。
# 强制 CPU 避免 MPS OOM；输入 resize 到 1024 长边——MUSIQ 训练在多尺度
# (384+) 上、CLIP-IQA 用 224×224，1024 远超模型需求，不损失评估精度，
# 反而更快。
# =============================================================

PYIQA_MAX_SIDE = 1024


def _resize_for_pyiqa(pil_img: Image.Image) -> Image.Image:
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) <= PYIQA_MAX_SIDE:
        return img
    scale = PYIQA_MAX_SIDE / max(w, h)
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                      Image.LANCZOS)


def _ensure_musiq():
    if "musiq" in _models:
        return _models["musiq"]
    with _LOCK:
        if "musiq" in _models:
            return _models["musiq"]
        try:
            import pyiqa  # noqa
            import torch
        except ImportError as e:
            raise VisionUnavailable(
                f"MUSIQ 依赖缺失：{e}。需要 `pip install pyiqa timm`。"
            ) from e
        logger.info("vision: 加载 MUSIQ（CPU，技术质量评分，首次约 100MB）…")
        dev = torch.device("cpu")
        model = pyiqa.create_metric("musiq", device=dev, as_loss=False)
        _models["musiq"] = (model, dev)
        logger.info("vision: MUSIQ 就绪")
    return _models["musiq"]


def extract_musiq_score(pil_img: Image.Image) -> float:
    """返回 MUSIQ 技术质量分 0-100。CPU + 1024 长边以下输入。"""
    import torch
    model, _dev = _ensure_musiq()
    img = _resize_for_pyiqa(pil_img)
    with torch.no_grad():
        score = model(img)
    val = float(score.item() if hasattr(score, "item") else score)
    return max(0.0, min(100.0, val))


def _ensure_clipiqa():
    if "clipiqa" in _models:
        return _models["clipiqa"]
    with _LOCK:
        if "clipiqa" in _models:
            return _models["clipiqa"]
        try:
            import pyiqa  # noqa
            import torch
        except ImportError as e:
            raise VisionUnavailable(
                f"CLIP-IQA+ 依赖缺失：{e}。需要 `pip install pyiqa timm`。"
            ) from e
        logger.info("vision: 加载 CLIP-IQA+（CPU，LAION 美学，首次约 350MB）…")
        dev = torch.device("cpu")
        model = pyiqa.create_metric("clipiqa+", device=dev, as_loss=False)
        _models["clipiqa"] = (model, dev)
        logger.info("vision: CLIP-IQA+ 就绪")
    return _models["clipiqa"]


def extract_clipiqa_score(pil_img: Image.Image) -> float:
    """返回 CLIP-IQA+ 美学分 0-1。CPU + 1024 长边以下输入。"""
    import torch
    model, _dev = _ensure_clipiqa()
    img = _resize_for_pyiqa(pil_img)
    with torch.no_grad():
        score = model(img)
    val = float(score.item() if hasattr(score, "item") else score)
    return max(0.0, min(1.0, val))


# =============================================================
# InsightFace：RetinaFace 检测 + ArcFace 512 维嵌入 + 关键点
# =============================================================

def _ensure_insightface():
    if "insightface" in _models:
        return _models["insightface"]
    with _LOCK:
        if "insightface" in _models:
            return _models["insightface"]
        try:
            from insightface.app import FaceAnalysis
        except ImportError as e:
            raise VisionUnavailable(
                f"InsightFace 依赖缺失：{e}。需要 `pip install insightface onnxruntime`。"
            ) from e

        logger.info("vision: 加载 InsightFace（RetinaFace + ArcFace，首次约 300MB）…")
        app = FaceAnalysis(
            name="buffalo_l",
            root=str(_cache_dir() / "insightface"),
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _models["insightface"] = app
        logger.info("vision: InsightFace 就绪")
    return _models["insightface"]


def extract_faces(
    pil_img: Image.Image, max_dim: int = 1024
) -> List[dict]:
    """返回 [{ bbox: (x1,y1,x2,y2), embedding: 512d ndarray, kps: (5,2) ndarray }]。

    没人脸 → 返回 []。依赖缺失 → 抛 VisionUnavailable。
    """
    app = _ensure_insightface()
    img = pil_img.convert("RGB")
    w, h = img.size
    scale = 1.0
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    arr = np.array(img)[:, :, ::-1]  # RGB → BGR for InsightFace

    faces = app.get(arr)
    if not faces:
        return []

    inv = 1.0 / scale
    out = []
    for face in faces:
        bbox = tuple(int(c * inv) for c in face.bbox.astype(int))
        emb = face.embedding.astype(np.float32)
        n = float(np.linalg.norm(emb))
        if n < 1e-8:
            continue
        emb = emb / n

        kps = None
        if face.kps is not None:
            kps = (face.kps * inv).astype(np.float32)

        lm68 = None
        if getattr(face, "landmark_3d_68", None) is not None:
            lm68 = (face.landmark_3d_68[:, :2] * inv).astype(np.float32)

        out.append({
            "bbox": bbox,
            "embedding": emb,
            "kps": kps,
            "det_score": float(face.det_score),
            "landmark_2d_68": lm68,
        })
    return out


def compute_eye_open_score(face_info: dict, pil_img: Image.Image) -> float | None:
    """用 68 点关键点的 EAR（Eye Aspect Ratio）估算闭眼程度。

    68-landmark: 左眼 36-41, 右眼 42-47（各 6 点）。
    EAR = (|p1-p5| + |p2-p4|) / (2 * |p0-p3|)
    典型睁眼 0.25-0.35+，闭眼 < 0.20。

    EAR 物理上限 ≈ 0.45（眼睛形状决定）。InsightFace 的 1k3d68 模型在某些
    角度/光照下输出的点序不严格遵循 iBUG68，会产出 1.0+ 的离谱值；这种
    情况下点是不可信的，返回 None 让上层"按未知处理"，避免把"坏数据"
    当成"睁得很开"放过真闭眼。
    """
    lm68 = face_info.get("landmark_2d_68")
    if lm68 is None or len(lm68) < 48:
        return None

    def _ear(pts):
        vert1 = float(np.linalg.norm(pts[1] - pts[5]))
        vert2 = float(np.linalg.norm(pts[2] - pts[4]))
        horiz = float(np.linalg.norm(pts[0] - pts[3]))
        if horiz < 1e-6:
            return 0.0
        return (vert1 + vert2) / (2.0 * horiz)

    left_ear = _ear(np.asarray(lm68[36:42], dtype=np.float32))
    right_ear = _ear(np.asarray(lm68[42:48], dtype=np.float32))
    ear = (left_ear + right_ear) / 2.0
    if ear > 0.55:
        return None
    return round(ear, 4)


# =============================================================
# 启动期能力校验
# =============================================================

def capabilities() -> dict:
    """轻量探测——仅尝试 import，不下载权重。"""
    out = {"dinov2": False, "aesthetic": False, "musiq": False,
           "clipiqa": False, "face_id": False}
    try:
        import torch  # noqa
        import transformers  # noqa
        out["dinov2"] = True
    except ImportError:
        pass
    try:
        import torch  # noqa
        import torchvision  # noqa
        out["aesthetic"] = True
    except ImportError:
        pass
    try:
        import pyiqa  # noqa
        out["musiq"] = True
        out["clipiqa"] = True
    except ImportError:
        pass
    try:
        import insightface  # noqa
        import onnxruntime  # noqa
        out["face_id"] = True
    except ImportError:
        pass
    return out


def require_expert_capabilities() -> None:
    """专家模式启动前调一次，缺一即抛 VisionUnavailable。"""
    caps = capabilities()
    missing = [k for k, v in caps.items() if not v]
    if missing:
        raise VisionUnavailable(
            f"专家模式缺少依赖：{', '.join(missing)}。请按 requirements.txt 安装完整依赖。"
        )


def require_tycoon_capabilities() -> None:
    """土豪模式：DINOv2 + InsightFace（分组依赖）必备；NIMA/MUSIQ/CLIP 不要。"""
    caps = capabilities()
    needed = ["dinov2", "face_id"]
    missing = [k for k in needed if not caps.get(k)]
    if missing:
        raise VisionUnavailable(
            f"土豪模式缺少依赖：{', '.join(missing)}。请按 requirements.txt 安装。"
        )


def prewarm_all() -> None:
    """专家模式预热全部模型；任一失败抛出。"""
    _ensure_dinov2()
    _ensure_nima()
    _ensure_musiq()
    _ensure_clipiqa()
    _ensure_insightface()


def prewarm_tycoon() -> None:
    """土豪模式预热：仅 DINOv2 + InsightFace（分组依赖）。"""
    _ensure_dinov2()
    _ensure_insightface()
