"""相机水印渲染模块。

11 个样式 ID。详尽程度直接拆进样式名里（xx-详尽 / xx-极简），不再单独切换：

  A          标准底栏        （只有一个版本）
  B_full     极简底栏-详尽
  B_clean    极简底栏-极简
  C_full     毛玻璃悬浮-详尽
  C_clean    毛玻璃悬浮-极简
  D_full     经典白边相框-详尽
  D_clean    经典白边相框-极简
  F_full     杂志风-详尽
  F_clean    杂志风-极简
  G          极简白边        （无任何信息）
  H          相机回放        （无任何信息）

「详尽」= 带镜头·参数·时间；「极简」= 仅品牌 Logo + 机型。

对外接口：
  - WatermarkConfig          前端 JSON → 配置对象
  - ExifInfo / parse_exif    给前端 EXIF 卡片用
  - list_templates()         给前端样式选择器用
  - render()                 给 /api/watermark/preview 用
  - batch_export()           给 /api/watermark/start 用
  - available_logos()        给前端调试用
"""

from __future__ import annotations

import io
import logging
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, ImageOps)
from PIL.ExifTags import TAGS

logger = logging.getLogger("pic_selecter")


# ============================================================
# 路径
# ============================================================

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGOS_DIR = _PROJECT_ROOT / "assets" / "logos"
# 样式 H 的相机素材
CAMERA_BACK_PATH = _PROJECT_ROOT / "assets" / "camera_xt5_back.png"
# 相机 LCD 屏 / 取景器玻璃在原 PNG 中的相对坐标（基于像素分析）。
# 注意：渲染时会先把 PNG 紧裁剪到相机本体 bbox，所以下面的比例会在 _camera_back_rgba
# 里换算成相对裁剪图像的比例后再用。
_CAMERA_SCREEN_RATIO_PNG = (0.1566, 0.4206, 0.6109, 0.7955)  # (l, t, r, b)
_CAMERA_VIEWFINDER_RATIO_PNG = (0.385, 0.238, 0.490, 0.318)

_LOGO_MAP: list[tuple[str, str]] = [
    ("fuji", "fujifilm.png"),
    ("canon", "canon.png"),
    ("nikon", "nikon.png"),
    ("sony", "sony.png"),
    ("leica", "leica_logo.png"),
    ("hasselblad", "hasselblad.png"),
    ("olympus", "olympus_blue_gold.png"),
    ("om digital", "olympus_blue_gold.png"),
    ("om system", "olympus_blue_gold.png"),
    ("panasonic", "panasonic.png"),
    ("pentax", "pentax.png"),
    ("ricoh", "ricoh.png"),
    ("apple", "apple.png"),
    ("xiaomi", "xmage.png"),
]


# ============================================================
# 配置
# ============================================================

@dataclass
class WatermarkConfig:
    """前端 JSON → 这个对象。

    template: 见模块 docstring 的 11 个样式 ID 之一。
    详尽程度已经编码在样式 ID 里（B_full / B_clean 等），不再额外切换。
    """
    template: str = "A"

    @classmethod
    def from_dict(cls, d: dict) -> "WatermarkConfig":
        kwargs = {}
        for f in cls.__dataclass_fields__:
            if f in d:
                kwargs[f] = d[f]
        if "template" in kwargs:
            kwargs["template"] = str(kwargs["template"])
        # 旧 template 名 / 旧 base 名 → 新 ID（base 名默认到 _full 变体）
        legacy_map = {
            "classic_white": "A", "fuji_bar": "A",
            "white_frame": "D_full", "minimal": "G", "overlay": "C_full",
            "B": "B_full", "C": "C_full", "D": "D_full", "F": "F_full",
        }
        if kwargs.get("template") in legacy_map:
            kwargs["template"] = legacy_map[kwargs["template"]]
        return cls(**kwargs)


# ============================================================
# 字体
# ============================================================

def _font_path(style: str, weight: str) -> tuple[str, int]:
    """返回 (path, index)。"""
    system = platform.system()
    if system != "Darwin":
        # 简化：非 mac 全部退到默认 sans
        candidates = {
            "regular": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        }
        return (candidates.get(weight, candidates["regular"]), 0)

    # macOS — HelveticaNeue.ttc 索引：0=Regular, 1=Bold, 7=Light, 9=Heavy, 10=Medium
    HV = "/System/Library/Fonts/HelveticaNeue.ttc"
    PF = "/System/Library/Fonts/PingFang.ttc"

    if style == "en":
        return {
            "light":   (HV, 7),
            "regular": (HV, 0),
            "medium":  (HV, 10),
            "bold":    (HV, 1),
            "heavy":   (HV, 9),
        }.get(weight, (HV, 0))
    if style == "sans":  # 含中文兜底
        return {
            "light":   (PF, 2),
            "regular": (PF, 3),
            "medium":  (PF, 4),
            "bold":    (PF, 5),
            "heavy":   (PF, 5),
        }.get(weight, (PF, 3))
    return (HV, 0)


def _font(size: int, weight: str = "regular", style: str = "en") -> ImageFont.FreeTypeFont:
    """每次直接 truetype，不缓存（验证脚本沿用，避免缓存 bug）。"""
    path, idx = _font_path(style, weight)
    if not os.path.exists(path):
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, max(8, int(size)), index=idx)
    except Exception:
        try:
            return ImageFont.truetype(path, max(8, int(size)))
        except Exception:
            return ImageFont.load_default()


# ============================================================
# EXIF
# ============================================================

@dataclass
class ExifInfo:
    make: str = ""
    model: str = ""
    lens: str = ""
    focal_length: str = ""
    f_number: str = ""
    exposure: str = ""
    iso: str = ""
    datetime_str: str = ""


def _fmt_exposure(v) -> str:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            f = Fraction(v[0], v[1])
        else:
            f = Fraction(v).limit_denominator(8000)
        if f >= 1:
            return f"{float(f):.1f}s"
        return f"1/{int(round(1 / float(f)))}s"
    except Exception:
        return ""


def _fmt_aperture(v) -> str:
    try:
        f = float(v[0]) / float(v[1]) if isinstance(v, tuple) else float(v)
        return f"f/{int(round(f))}" if abs(f - round(f)) < 0.05 else f"f/{f:.1f}"
    except Exception:
        return ""


def _fmt_focal(v) -> str:
    try:
        f = float(v[0]) / float(v[1]) if isinstance(v, tuple) else float(v)
        return f"{int(round(f))}mm"
    except Exception:
        return ""


def _fmt_iso(v) -> str:
    try:
        if isinstance(v, (list, tuple)):
            v = v[0]
        return f"ISO{int(v)}"
    except Exception:
        return ""


def _fmt_datetime(v) -> str:
    if not v:
        return ""
    s = str(v).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y.%m.%d %H:%M")
        except ValueError:
            continue
    return s


def _clean(v) -> str:
    return str(v).replace("\x00", "").strip() if v is not None else ""


def parse_exif(img: Image.Image) -> ExifInfo:
    """从 PIL Image 解析 EXIF。注意：传入的 img 必须是 Image.open 的原 handle，
    不是 exif_transpose 之后的副本——后者会丢 EXIF。"""
    e = img.getexif() if hasattr(img, "getexif") else None
    info = ExifInfo()
    if not e:
        return info
    info.make = _clean(e.get(271, ""))    # Make
    model = _clean(e.get(272, ""))         # Model
    if model and info.make:
        mk_low = info.make.lower().split()[0] if info.make else ""
        if mk_low and model.lower().startswith(mk_low):
            model = model[len(mk_low):].strip()
    info.model = model
    info.datetime_str = _fmt_datetime(e.get(306, ""))
    try:
        ifd = e.get_ifd(0x8769)
        info.lens = _clean(ifd.get(42036, ""))
        info.focal_length = _fmt_focal(ifd.get(37386, ""))
        info.f_number = _fmt_aperture(ifd.get(33437, ""))
        info.exposure = _fmt_exposure(ifd.get(33434, ""))
        info.iso = _fmt_iso(ifd.get(34855, "") or ifd.get(34867, ""))
    except Exception:
        pass
    return info


# ============================================================
# Logo（抠白底 + trim 透明边 + 缩放）
# ============================================================

def _logo_for_make(make: str) -> Optional[Path]:
    if not make:
        return None
    m = make.lower().strip()
    for key, fname in _LOGO_MAP:
        if key in m:
            p = LOGOS_DIR / fname
            if p.exists():
                return p
    return None


def _load_logo(path: Path, target_h: int,
               bg_threshold: int = 30, trim_pad: int = 4) -> Image.Image:
    """加载 logo + 自动抠背景 + trim + 等比缩到 target_h。

    抠背景策略：
      1) P 模式（palette）→ convert("RGBA") 保留 palette 透明色
      2) 已有 RGBA 且 alpha 不是全 255 → 直接用现有 alpha
      3) 否则检测 4 角颜色：
         - 4 角颜色一致（方差小） → 把这个颜色当背景，抠掉所有接近此色的像素
         - 4 角不一致 → 退回到"抠白"策略（兜底）
    """
    raw = Image.open(path)

    # ---- 第 1 步：标准化到 RGBA，初步获取 alpha 通道 ----
    if raw.mode == "P":
        # palette 模式可能带透明色，convert("RGBA") 会正确处理
        logo = raw.convert("RGBA")
    elif raw.mode == "RGBA":
        logo = raw.copy()
    else:
        logo = raw.convert("RGBA")

    # ---- 第 2 步：判断是否需要抠背景 ----
    # 如果 alpha 已经包含足够的透明信息（极值范围明显），不再抠背景
    alpha = logo.split()[-1]
    a_min, a_max = alpha.getextrema()
    needs_keying = (a_min == a_max == 255)  # 全不透明 → 需要算法抠背景

    if needs_keying:
        rgb = logo.convert("RGB")
        w, h = rgb.size
        corners = [rgb.getpixel((0, 0)),
                   rgb.getpixel((w - 1, 0)),
                   rgb.getpixel((0, h - 1)),
                   rgb.getpixel((w - 1, h - 1))]
        rs = [c[0] for c in corners]
        gs = [c[1] for c in corners]
        bs = [c[2] for c in corners]
        # 4 角颜色是否一致（差值小于 20）
        consistent = (max(rs) - min(rs) < 20
                      and max(gs) - min(gs) < 20
                      and max(bs) - min(bs) < 20)
        if consistent:
            bg = (sum(rs) // 4, sum(gs) // 4, sum(bs) // 4)
            # 抠掉所有与 bg 曼哈顿距离 < bg_threshold * 3 的像素
            r, g, b = rgb.split()
            # 计算每像素到 bg 的 L1 距离
            dr = ImageChops.difference(r, Image.new("L", r.size, bg[0]))
            dg = ImageChops.difference(g, Image.new("L", g.size, bg[1]))
            db = ImageChops.difference(b, Image.new("L", b.size, bg[2]))
            # 取三通道最大差异（近似 L∞ 距离）—— 差异大则前景，差异小则背景
            d_max = ImageChops.lighter(ImageChops.lighter(dr, dg), db)
            new_alpha = d_max.point(lambda v: 255 if v > bg_threshold else 0)
            logo = Image.merge("RGBA", (r, g, b, new_alpha))
        else:
            # 4 角不一致 → 退回抠白
            r, g, b = rgb.split()
            min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
            new_alpha = min_rgb.point(lambda v: 255 if v < 240 else 0)
            logo = Image.merge("RGBA", (r, g, b, new_alpha))

    # ---- 第 3 步：trim 透明边 ----
    bbox = logo.split()[-1].getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        x0 = max(0, x0 - trim_pad); y0 = max(0, y0 - trim_pad)
        x1 = min(logo.size[0], x1 + trim_pad); y1 = min(logo.size[1], y1 + trim_pad)
        logo = logo.crop((x0, y0, x1, y1))

    # ---- 第 4 步：等比缩到目标高 ----
    w, h = logo.size
    if h > 0 and h != target_h:
        scale = target_h / h
        logo = logo.resize((max(1, int(w * scale)), target_h), Image.LANCZOS)
    return logo


def _logo_white(logo: Image.Image) -> Image.Image:
    """把 logo 染成白色（保留 alpha）—— 深色背景上用。"""
    if logo.mode != "RGBA":
        logo = logo.convert("RGBA")
    alpha = logo.split()[-1]
    white = Image.new("RGBA", logo.size, (255, 255, 255, 0))
    white.putalpha(alpha)
    return white


# ============================================================
# 文本工具
# ============================================================

def _measure(draw: ImageDraw.ImageDraw, text: str,
             f: ImageFont.FreeTypeFont) -> tuple[int, int]:
    if not text:
        return 0, 0
    bw = draw.textbbox((0, 0), text, font=f)
    bh = draw.textbbox((0, 0), "Hg", font=f)
    return bw[2] - bw[0], bh[3] - bh[1]


def _baseline_offset(draw: ImageDraw.ImageDraw, f: ImageFont.FreeTypeFont) -> int:
    return draw.textbbox((0, 0), "Hg", font=f)[1]


# ============================================================
# 配色（Apple 灰阶）
# ============================================================

INK_BLACK = (29, 29, 31)        # #1d1d1f
INK_DARK = (66, 66, 70)
INK_GREY = (134, 134, 139)      # #86868b
INK_LIGHT = (210, 210, 215)
INK_HAIR = (235, 235, 237)
WHITE = (255, 255, 255)


# ============================================================
# 样式 A：富士底栏（仅 full）
# ============================================================

def _render_A(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    w, h = img.size
    bar_h = max(96, int(w * 0.110))
    canvas = Image.new("RGB", (w, h + bar_h), WHITE)
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)

    bar_top = h
    bar_mid = h + bar_h // 2

    pad_l = int(w * 0.050)
    pad_r = int(w * 0.050)

    f_main = _font(int(bar_h * 0.24), weight="medium", style="en")
    f_sub = _font(int(bar_h * 0.165), weight="regular", style="en")
    line_gap = int(bar_h * 0.085)

    draw.line([(0, bar_top), (w, bar_top)], fill=INK_HAIR, width=1)

    # 左块：机型 / 镜头（或时间）
    left_main = exif.model
    left_sub = (exif.lens or exif.datetime_str) if show_params else ""

    if left_main or left_sub:
        m_w, m_h = _measure(draw, left_main, f_main) if left_main else (0, 0)
        s_w, s_h = _measure(draw, left_sub, f_sub) if left_sub else (0, 0)
        b_m = _baseline_offset(draw, f_main)
        b_s = _baseline_offset(draw, f_sub)
        if left_main and left_sub:
            block_h = m_h + line_gap + s_h
            y0 = bar_mid - block_h // 2
            draw.text((pad_l, y0 - b_m), left_main, font=f_main, fill=INK_BLACK)
            draw.text((pad_l, y0 + m_h + line_gap - b_s),
                      left_sub, font=f_sub, fill=INK_GREY)
        elif left_main:
            draw.text((pad_l, bar_mid - m_h // 2 - b_m),
                      left_main, font=f_main, fill=INK_BLACK)
        elif left_sub:
            draw.text((pad_l, bar_mid - s_h // 2 - b_s),
                      left_sub, font=f_sub, fill=INK_GREY)

    # 右块：参数 / 时间
    if show_params:
        right_main_bits = [b for b in (exif.focal_length, exif.f_number, exif.exposure, exif.iso) if b]
        right_main = "  ".join(right_main_bits)
        right_sub = exif.datetime_str if (left_sub != exif.datetime_str) else ""
    else:
        right_main = right_sub = ""

    rm_w, rm_h = _measure(draw, right_main, f_main) if right_main else (0, 0)
    rs_w, rs_h = _measure(draw, right_sub, f_sub) if right_sub else (0, 0)
    right_text_w = max(rm_w, rs_w)
    right_text_x = w - pad_r - right_text_w

    # Logo + 竖线
    logo_path = _logo_for_make(exif.make)
    if logo_path:
        logo_h = int(bar_h * 0.30)
        try:
            logo_img = _load_logo(logo_path, logo_h)
        except Exception:
            logo_img = None
    else:
        logo_img = None

    divider_w = max(1, int(bar_h * 0.006))
    divider_h = int(bar_h * 0.48)
    div_gap_left = int(bar_h * 0.32)
    div_gap_right = int(bar_h * 0.32)

    if logo_img is not None:
        logo_w = logo_img.size[0]
        block_w = logo_w + div_gap_left + divider_w + div_gap_right
        logo_x = right_text_x - block_w
        logo_y = bar_mid - logo_img.size[1] // 2
        canvas.paste(logo_img, (logo_x, logo_y), logo_img)
        line_x = logo_x + logo_w + div_gap_left
        draw.line([(line_x, bar_mid - divider_h // 2),
                   (line_x, bar_mid + divider_h // 2)],
                  fill=INK_LIGHT, width=divider_w)

    if right_main and right_sub:
        block_h = rm_h + line_gap + rs_h
        y0 = bar_mid - block_h // 2
        b_m = _baseline_offset(draw, f_main)
        b_s = _baseline_offset(draw, f_sub)
        draw.text((right_text_x, y0 - b_m), right_main, font=f_main, fill=INK_BLACK)
        draw.text((right_text_x, y0 + rm_h + line_gap - b_s),
                  right_sub, font=f_sub, fill=INK_GREY)
    elif right_main:
        b_m = _baseline_offset(draw, f_main)
        draw.text((right_text_x, bar_mid - rm_h // 2 - b_m),
                  right_main, font=f_main, fill=INK_BLACK)

    return canvas


# ============================================================
# 样式 B：极简底栏
# ============================================================

def _render_B(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    w, h = img.size
    short = min(w, h)
    pad_side = int(short * 0.022)
    pad_top = int(short * 0.022)
    pad_bot = int(short * 0.135)

    cw = w + pad_side * 2
    ch = h + pad_top + pad_bot
    canvas = Image.new("RGB", (cw, ch), WHITE)
    canvas.paste(img, (pad_side, pad_top))
    draw = ImageDraw.Draw(canvas)

    cx = cw // 2
    band_top = pad_top + h
    band_h = pad_bot
    band_cy = band_top + band_h // 2

    f_model = _font(int(band_h * 0.20), weight="medium", style="en")
    f_meta = _font(int(band_h * 0.13), weight="light", style="en")

    logo_h = int(band_h * 0.24)
    logo_img = None
    if exif.make:
        lp = _logo_for_make(exif.make)
        if lp:
            try:
                logo_img = _load_logo(lp, logo_h)
            except Exception:
                pass

    brand_model = exif.model
    mm_w, mm_h = (_measure(draw, brand_model, f_model) if brand_model else (0, 0))
    logo_w_brand = logo_img.size[0] if logo_img is not None else 0
    gap_brand = int(band_h * 0.10)
    top_w = logo_w_brand + (gap_brand if logo_w_brand and mm_w else 0) + mm_w
    top_h = max(logo_h if logo_img is not None else 0, mm_h)

    meta_bits = []
    if show_params:
        if exif.lens:
            meta_bits.append(exif.lens)
        params = "  ".join(b for b in (exif.focal_length, exif.f_number, exif.exposure, exif.iso) if b)
        if params:
            meta_bits.append(params)
        if exif.datetime_str:
            meta_bits.append(exif.datetime_str)
    meta_line = "   ·   ".join(meta_bits)
    meta_w, meta_h = (_measure(draw, meta_line, f_meta) if meta_line else (0, 0))

    line_gap = int(band_h * 0.15)
    block_h = top_h + (line_gap + meta_h if meta_line else 0)
    y0 = band_cy - block_h // 2

    x_top = cx - top_w // 2
    if logo_img is not None:
        canvas.paste(logo_img,
                     (x_top, y0 + (top_h - logo_img.size[1]) // 2),
                     logo_img)
        x_top += logo_w_brand + (gap_brand if mm_w else 0)
    if brand_model:
        b_m = _baseline_offset(draw, f_model)
        draw.text((x_top, y0 + (top_h - mm_h) // 2 - b_m),
                  brand_model, font=f_model, fill=INK_BLACK)
    if meta_line:
        b_p = _baseline_offset(draw, f_meta)
        draw.text((cx - meta_w // 2, y0 + top_h + line_gap - b_p),
                  meta_line, font=f_meta, fill=INK_GREY)

    return canvas


# ============================================================
# 样式 C：毛玻璃悬浮卡片
# ============================================================

def _render_C(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    w, h = img.size
    ref = min(w, h)

    cw, ch = w, h
    inner_scale = 0.82
    iw = int(w * inner_scale)
    ih = int(h * inner_scale)

    bg = img.copy()
    bg = bg.filter(ImageFilter.GaussianBlur(radius=int(ref * 0.01)))
    bg = ImageEnhance.Brightness(bg).enhance(0.88)
    canvas = bg.convert("RGB")

    fg = img.resize((iw, ih), Image.LANCZOS)
    fg_x = (cw - iw) // 2
    fg_y = int((ch - ih) * 0.42)

    sh_blur = int(ref * 0.025)
    sh_pad = sh_blur * 3
    shadow = Image.new("RGBA", (iw + sh_pad * 2, ih + sh_pad * 2), (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(shadow)
    sh_draw.rectangle([(sh_pad, sh_pad), (sh_pad + iw, sh_pad + ih)],
                      fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=sh_blur))
    sh_x = fg_x - sh_pad
    sh_y = fg_y - sh_pad + int(sh_blur * 0.8)
    canvas.paste(shadow, (sh_x, sh_y), shadow)

    canvas.paste(fg, (fg_x, fg_y))

    draw = ImageDraw.Draw(canvas)

    f_model = _font(int(ref * 0.022), weight="medium", style="en")
    f_meta = _font(int(ref * 0.014), weight="light", style="en")
    logo_h_brand = int(ref * 0.024)
    logo_img = None
    if exif.make:
        lp = _logo_for_make(exif.make)
        if lp:
            try:
                logo_img = _load_logo(lp, logo_h_brand)
            except Exception:
                logo_img = None

    brand_model = exif.model
    mm_w, mm_h = (_measure(draw, brand_model, f_model) if brand_model else (0, 0))
    logo_w_brand = logo_img.size[0] if logo_img is not None else 0
    gap_brand = int(ref * 0.018)
    top_w = logo_w_brand + (gap_brand if logo_w_brand and mm_w else 0) + mm_w
    top_h = max(logo_h_brand if logo_img is not None else 0, mm_h)

    meta_bits = []
    if show_params:
        if exif.lens:
            meta_bits.append(exif.lens)
        params = "  ".join(b for b in (exif.focal_length, exif.f_number, exif.exposure, exif.iso) if b)
        if params:
            meta_bits.append(params)
        if exif.datetime_str:
            meta_bits.append(exif.datetime_str)
    meta_line = "   ·   ".join(meta_bits)

    line_gap = int(ref * 0.012)
    meta_w, meta_h = (_measure(draw, meta_line, f_meta) if meta_line else (0, 0))
    block_h = top_h + (line_gap + meta_h if meta_line else 0)

    fg_bottom = fg_y + ih
    text_band_top = fg_bottom + int(ref * 0.020)
    text_band_bot = ch - int(ref * 0.025)
    block_top = (text_band_top + text_band_bot) // 2 - block_h // 2

    cx = cw // 2
    text_color = (255, 255, 255)

    def shaded_text(x, y, text, f, color):
        draw.text((x, y + 2), text, font=f, fill=(0, 0, 0))
        draw.text((x, y), text, font=f, fill=color)

    x0 = cx - top_w // 2
    if logo_img is not None:
        logo_y = block_top + (top_h - logo_img.size[1]) // 2
        white_logo = _logo_white(logo_img)
        canvas.paste(white_logo, (x0, logo_y), white_logo)
        x0 += logo_w_brand + (gap_brand if mm_w else 0)
    if brand_model:
        b_m = _baseline_offset(draw, f_model)
        shaded_text(x0, block_top + (top_h - mm_h) // 2 - b_m,
                    brand_model, f_model, text_color)
    if meta_line:
        b_p = _baseline_offset(draw, f_meta)
        shaded_text(cx - meta_w // 2, block_top + top_h + line_gap - b_p,
                    meta_line, f_meta, text_color)

    return canvas


# ============================================================
# 样式 D：经典白边相框
# ============================================================

def _render_D(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    w, h = img.size
    ref = min(w, h)
    pad_top = int(ref * 0.040)
    pad_side = int(ref * 0.040)
    pad_bot = int(ref * 0.20)

    cw = w + pad_side * 2
    ch = h + pad_top + pad_bot
    canvas = Image.new("RGB", (cw, ch), WHITE)
    canvas.paste(img, (pad_side, pad_top))
    draw = ImageDraw.Draw(canvas)

    cx = cw // 2
    band_top = pad_top + h
    band_h = pad_bot
    band_cy = band_top + band_h // 2

    f_model = _font(int(band_h * 0.20), weight="regular", style="en")
    f_params = _font(int(band_h * 0.13), weight="light", style="en")

    brand_model = exif.model
    meta_bits = []
    if show_params:
        if exif.lens:
            meta_bits.append(exif.lens)
        params = "  ".join(b for b in (exif.focal_length, exif.f_number, exif.exposure, exif.iso) if b)
        if params:
            meta_bits.append(params)
        if exif.datetime_str:
            meta_bits.append(exif.datetime_str)
    meta_line = "   ·   ".join(meta_bits)

    logo_h_brand = int(band_h * 0.18)
    logo_img = None
    if exif.make:
        lp = _logo_for_make(exif.make)
        if lp:
            try:
                logo_img = _load_logo(lp, logo_h_brand)
            except Exception:
                logo_img = None

    if brand_model:
        mm_w, mm_h = _measure(draw, brand_model, f_model)
    else:
        mm_w = mm_h = 0
    logo_w_brand = logo_img.size[0] if logo_img is not None else 0
    gap_brand = int(ref * 0.028)
    top_block_w = logo_w_brand + (gap_brand if logo_w_brand and mm_w else 0) + mm_w
    top_block_h = max(logo_h_brand if logo_img is not None else 0, mm_h)

    meta_w, meta_h = (_measure(draw, meta_line, f_params) if meta_line else (0, 0))
    line_gap = int(band_h * 0.22)
    block_h = top_block_h + (line_gap + meta_h if meta_line else 0)
    y0 = band_cy - block_h // 2

    x_top = cx - top_block_w // 2
    if logo_img is not None:
        canvas.paste(logo_img,
                     (x_top, y0 + (top_block_h - logo_img.size[1]) // 2),
                     logo_img)
        x_top += logo_w_brand + (gap_brand if mm_w else 0)
    if brand_model:
        b_m = _baseline_offset(draw, f_model)
        draw.text((x_top, y0 + (top_block_h - mm_h) // 2 - b_m),
                  brand_model, font=f_model, fill=INK_BLACK)
    if meta_line:
        b_p = _baseline_offset(draw, f_params)
        draw.text((cx - meta_w // 2, y0 + top_block_h + line_gap - b_p),
                  meta_line, font=f_params, fill=INK_GREY)

    return canvas


# ============================================================
# 样式 F：杂志风（带色卡）
# ============================================================

def _render_F(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    w, h = img.size
    ref = min(w, h)
    pad_top = int(ref * 0.040)
    pad_bot_band = int(ref * 0.18)
    pad_side = int(ref * 0.040)

    cw = w + pad_side * 2
    ch = h + pad_top + pad_bot_band
    canvas = Image.new("RGB", (cw, ch), WHITE)
    canvas.paste(img, (pad_side, pad_top))
    draw = ImageDraw.Draw(canvas)

    bot_y0 = pad_top + h
    bot_h = pad_bot_band

    # 左下色卡
    palette = _extract_palette(img, n=5)
    cell_w = int(w * 0.062)
    cell_h = int(bot_h * 0.22)
    strip_x = pad_side
    strip_cy = bot_y0 + bot_h // 2
    strip_y = strip_cy - cell_h // 2
    for i, color in enumerate(palette):
        draw.rectangle(
            [(strip_x + i * cell_w, strip_y),
             (strip_x + (i + 1) * cell_w, strip_y + cell_h)],
            fill=color,
        )

    # 右下信息
    f_model = _font(int(bot_h * 0.15), weight="regular", style="en")
    f_meta = _font(int(bot_h * 0.10), weight="light", style="en")
    params = ("  ".join(b for b in (exif.focal_length, exif.f_number, exif.exposure, exif.iso) if b)
              if show_params else "")

    logo_h_brand = int(bot_h * 0.14)
    logo_img = None
    if exif.make:
        lp = _logo_for_make(exif.make)
        if lp:
            try:
                logo_img = _load_logo(lp, logo_h_brand)
            except Exception:
                logo_img = None

    brand_model = exif.model
    mm_w, mm_h = (_measure(draw, brand_model, f_model) if brand_model else (0, 0))
    logo_w_brand = logo_img.size[0] if logo_img is not None else 0
    gap_brand = int(ref * 0.020)
    top_w = logo_w_brand + (gap_brand if logo_w_brand and mm_w else 0) + mm_w
    top_h = max(logo_h_brand if logo_img is not None else 0, mm_h)

    meta_bits = []
    if show_params:
        if exif.lens:
            meta_bits.append(exif.lens)
        if params:
            meta_bits.append(params)
        if exif.datetime_str:
            meta_bits.append(exif.datetime_str)
    meta_line = "   ·   ".join(meta_bits)
    meta_w, meta_h = (_measure(draw, meta_line, f_meta) if meta_line else (0, 0))

    line_gap = int(bot_h * 0.18)
    block_h = top_h + (line_gap + meta_h if meta_line else 0)
    block_top = strip_cy - block_h // 2

    right_x_anchor = cw - pad_side
    x_right = right_x_anchor
    if brand_model:
        x_right -= mm_w
        b_m = _baseline_offset(draw, f_model)
        draw.text((x_right, block_top + (top_h - mm_h) // 2 - b_m),
                  brand_model, font=f_model, fill=INK_BLACK)
        if logo_img is not None:
            x_right -= gap_brand
    if logo_img is not None:
        x_right -= logo_w_brand
        canvas.paste(logo_img,
                     (x_right, block_top + (top_h - logo_img.size[1]) // 2),
                     logo_img)
    if meta_line:
        b_p = _baseline_offset(draw, f_meta)
        draw.text((right_x_anchor - meta_w,
                   block_top + top_h + line_gap - b_p),
                  meta_line, font=f_meta, fill=INK_GREY)

    return canvas


def _extract_palette(img: Image.Image, n: int = 5) -> list[tuple[int, int, int]]:
    """从图中提取 n 个主色调（quantize + 按出现频率，按明度排序）。"""
    small = img.copy()
    small.thumbnail((240, 240), Image.LANCZOS)
    q = small.convert("RGB").quantize(colors=n * 6, method=Image.MEDIANCUT)
    pal = q.getpalette() or []
    counts = q.getcolors() or []
    counts.sort(reverse=True)
    rgbs = []
    seen = set()
    for cnt, idx in counts:
        if idx * 3 + 2 >= len(pal):
            continue
        r, g, b = pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]
        if max(r, g, b) < 40 or min(r, g, b) > 240:
            continue
        key = (r // 30, g // 30, b // 30)
        if key in seen:
            continue
        seen.add(key)
        rgbs.append((r, g, b))
        if len(rgbs) >= n:
            break
    while len(rgbs) < n:
        rgbs.append((200, 200, 200))
    rgbs.sort(key=lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2], reverse=True)
    return rgbs


# ============================================================
# 样式 G：极简窄白边（仅 full，无任何信息）
# ============================================================

def _render_G(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    _ = exif, show_params
    w, h = img.size
    pad = max(8, int(min(w, h) * 0.022))
    canvas = Image.new("RGB", (w + pad * 2, h + pad * 2), WHITE)
    canvas.paste(img, (pad, pad))
    return canvas


# ============================================================
# 样式 H：相机回放（上原图 / 下模糊版 + 居中相机 + 屏幕/取景器内嵌缩略）
# ============================================================

# 模块级缓存：(rgba, screen_ratio, viewfinder_ratio)。
# 抠白 + 紧裁剪后 ratio 也跟着变，一起缓存。
_CAMERA_RGBA_CACHE: Optional[tuple[Image.Image, tuple, tuple]] = None


def _camera_back_rgba() -> Optional[tuple[Image.Image, tuple, tuple]]:
    """加载相机 PNG，抠白底 + 紧裁剪到相机本体 bbox。

    返回 (rgba, screen_ratio, viewfinder_ratio)。两个 ratio 都是相对裁剪后图像
    归一的 (l, t, r, b)；失败返回 None。

    白底抠法：min(R,G,B) ≥ 245 → alpha=0（硬切，消除半透明白晕）；
    min(R,G,B) ≤ 220 → alpha=255；之间线性。机身和文字都在 v<100，不会被误伤。
    """
    global _CAMERA_RGBA_CACHE
    if _CAMERA_RGBA_CACHE is not None:
        return _CAMERA_RGBA_CACHE
    if not CAMERA_BACK_PATH.exists():
        logger.warning(f"相机素材缺失: {CAMERA_BACK_PATH}")
        return None
    try:
        src = Image.open(CAMERA_BACK_PATH).convert("RGB")
    except Exception:
        logger.exception(f"加载 {CAMERA_BACK_PATH} 失败")
        return None
    W, H = src.size
    r, g, b = src.split()
    min_rgb = ImageChops.darker(ImageChops.darker(r, g), b)
    # 245 处硬切：v=245→0, v=220→255
    alpha = min_rgb.point(lambda v: max(0, min(255, int((245 - v) * 10.2))))
    rgba = Image.merge("RGBA", (r, g, b, alpha))
    bbox = alpha.getbbox()
    if bbox is None:
        return None
    rgba = rgba.crop(bbox)
    cw, ch = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx0, cy0 = bbox[0], bbox[1]

    def _remap(rt: tuple) -> tuple:
        l, t, r_, b_ = rt
        return ((l * W - cx0) / cw, (t * H - cy0) / ch,
                (r_ * W - cx0) / cw, (b_ * H - cy0) / ch)

    _CAMERA_RGBA_CACHE = (rgba,
                          _remap(_CAMERA_SCREEN_RATIO_PNG),
                          _remap(_CAMERA_VIEWFINDER_RATIO_PNG))
    return _CAMERA_RGBA_CACHE


def _build_drop_shadow(rgba: Image.Image, blur: int, offset_y: int,
                       opacity: int) -> tuple[Image.Image, tuple[int, int]]:
    """从 alpha 通道生成柔和投影。返回 (shadow, (dx, dy))，
    贴到 (cam_x + dx, cam_y + dy) 即可。"""
    pad = blur * 3
    sw, sh = rgba.size
    canvas = Image.new("RGBA", (sw + pad * 2, sh + pad * 2), (0, 0, 0, 0))
    shadow_solid = Image.new("RGBA", (sw, sh), (0, 0, 0, opacity))
    shadow_solid.putalpha(
        ImageChops.multiply(rgba.split()[-1],
                            Image.new("L", (sw, sh), opacity))
    )
    canvas.paste(shadow_solid, (pad, pad), shadow_solid)
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=blur))
    return canvas, (-pad, -pad + offset_y)


def _fit_into(src: Image.Image, target_w: int, target_h: int,
              bg=(10, 10, 12)) -> Image.Image:
    sw, sh = src.size
    scale = min(target_w / sw, target_h / sh)
    new_w = max(1, int(round(sw * scale)))
    new_h = max(1, int(round(sh * scale)))
    resized = src.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), bg)
    canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def _add_screen_depth(canvas: Image.Image, x0: int, y0: int,
                      w: int, h: int) -> None:
    """屏幕内阴影 + 顶部柔光，模拟嵌入机身的 LCD 玻璃感。"""
    ref = min(w, h)
    inset = max(2, int(ref * 0.025))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(inset):
        a = int(140 * (1 - i / inset) ** 2)
        od.rectangle([i, i, w - 1 - i, h - 1 - i], outline=(0, 0, 0, a))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(1, inset // 3)))
    canvas.paste(overlay, (x0, y0), overlay)

    gloss = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gloss)
    steps = max(1, h // 3)
    for i in range(steps):
        a = int(55 * (1 - i / steps) ** 1.6)
        gd.line([(0, i), (int(w * 0.55), i + int(h * 0.15))],
                fill=(255, 255, 255, a), width=2)
    gloss = gloss.filter(ImageFilter.GaussianBlur(radius=max(2, int(ref * 0.012))))
    canvas.paste(gloss, (x0, y0), gloss)


def _paste_viewfinder_inset(canvas: Image.Image, photo: Image.Image,
                            cam_x: int, cam_y: int,
                            cam_w: int, cam_h: int,
                            vf_ratio: tuple) -> None:
    """取景器圆玻璃里嵌入压暗的小照片 + 右上小高光，模拟透过镜筒看到的画面。"""
    vl, vt, vr, vb = vf_ratio
    vx0 = cam_x + int(cam_w * vl)
    vy0 = cam_y + int(cam_h * vt)
    vx1 = cam_x + int(cam_w * vr)
    vy1 = cam_y + int(cam_h * vb)
    vw, vh = vx1 - vx0, vy1 - vy0
    if vw < 8 or vh < 8:
        return

    pw, ph = photo.size
    scale = max(vw / pw, vh / ph)
    rw, rh = max(1, int(pw * scale)), max(1, int(ph * scale))
    resized = photo.resize((rw, rh), Image.LANCZOS)
    cx, cy = rw // 2, rh // 2
    inset = resized.crop((cx - vw // 2, cy - vh // 2,
                          cx - vw // 2 + vw, cy - vh // 2 + vh))
    inset = inset.convert("RGB")
    inset = Image.eval(inset, lambda v: min(255, int(v * 0.78) + 18))

    mask = Image.new("L", (vw, vh), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((1, 1, vw - 2, vh - 2), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(1, vw // 18)))

    inset_rgba = inset.convert("RGBA")
    inset_rgba.putalpha(mask)
    canvas.alpha_composite(inset_rgba, (vx0, vy0))

    glare = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glare)
    gd.ellipse((int(vw * 0.55), int(vh * 0.05),
                int(vw * 0.95), int(vh * 0.45)),
               fill=(255, 255, 255, 70))
    glare = glare.filter(ImageFilter.GaussianBlur(radius=max(2, vw // 12)))
    glare_masked = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    glare_masked.paste(glare, (0, 0), mask)
    canvas.alpha_composite(glare_masked, (vx0, vy0))


def _render_H(img: Image.Image, exif: ExifInfo,
              show_params: bool = True) -> Image.Image:
    _ = exif, show_params
    cache = _camera_back_rgba()
    if cache is None:
        # 没有相机素材就退回 G 极简白边，至少不报错
        return _render_G(img, exif, show_params)
    camera_rgba, screen_ratio, vf_ratio = cache

    pw, ph = img.size
    short = min(pw, ph)

    canvas = Image.new("RGB", (pw, ph * 2), (0, 0, 0))
    # 上半部：原图
    canvas.paste(img, (0, 0))
    # 下半部：轻度模糊版
    blur_radius = max(6, int(short * 0.008))
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    canvas.paste(blurred, (0, ph))

    cam_w0, cam_h0 = camera_rgba.size
    target_h = int(ph * 0.73)
    target_w = int(target_h * cam_w0 / cam_h0)
    max_w = int(pw * 0.88)
    if target_w > max_w:
        target_w = max_w
        target_h = int(target_w * cam_h0 / cam_w0)
    camera = camera_rgba.resize((target_w, target_h), Image.LANCZOS)

    cam_x = (pw - target_w) // 2
    # 贴齐画布底部；接触投影会自然延伸到画面外（被裁剪），呈现"坐在画框边缘"的效果
    cam_y = ph * 2 - target_h

    sl, st, sr, sb = screen_ratio
    scr_x0 = cam_x + int(target_w * sl)
    scr_y0 = cam_y + int(target_h * st)
    scr_x1 = cam_x + int(target_w * sr)
    scr_y1 = cam_y + int(target_h * sb)
    scr_w, scr_h = scr_x1 - scr_x0, scr_y1 - scr_y0
    preview = _fit_into(img, scr_w, scr_h)

    canvas_rgba = canvas.convert("RGBA")

    # 两层投影：环境（大半径低不透明度）+ 接触（小半径高不透明度）
    amb_blur = max(40, int(target_h * 0.08))
    shadow_amb, (dxa, dya) = _build_drop_shadow(
        camera, blur=amb_blur, offset_y=int(target_h * 0.04), opacity=90
    )
    canvas_rgba.alpha_composite(shadow_amb, (cam_x + dxa, cam_y + dya))
    con_blur = max(8, int(target_h * 0.015))
    shadow_con, (dxc, dyc) = _build_drop_shadow(
        camera, blur=con_blur, offset_y=int(target_h * 0.008), opacity=200
    )
    canvas_rgba.alpha_composite(shadow_con, (cam_x + dxc, cam_y + dyc))

    # 相机本体淡晕影（用 alpha 限制只落在机身上）
    vign = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vign)
    for i in range(8):
        k = i / 8
        a = int(45 * k * k)
        pad_x = int(target_w * (0.02 + k * 0.08))
        pad_y = int(target_h * (0.02 + k * 0.08))
        vd.rectangle([pad_x, pad_y, target_w - pad_x, target_h - pad_y],
                     outline=(0, 0, 0, a), width=max(2, target_w // 200))
    vign = vign.filter(ImageFilter.GaussianBlur(radius=max(4, target_w // 60)))
    cam_alpha = camera.split()[-1]
    vign.putalpha(ImageChops.multiply(vign.split()[-1], cam_alpha))

    canvas_rgba.alpha_composite(camera, (cam_x, cam_y))
    canvas_rgba.alpha_composite(vign, (cam_x, cam_y))

    canvas_rgba.paste(preview, (scr_x0, scr_y0))
    _add_screen_depth(canvas_rgba, scr_x0, scr_y0, scr_w, scr_h)
    _paste_viewfinder_inset(canvas_rgba, img,
                            cam_x, cam_y, target_w, target_h,
                            vf_ratio)

    return canvas_rgba.convert("RGB")


# ============================================================
# 调度
# ============================================================

# 每个 ID 对应 (renderer, show_params 绑定值)。详尽与极简拆成不同 ID。
_STYLE_SPECS: dict[str, tuple[Callable, bool]] = {
    "A":       (_render_A, True),
    "B_full":  (_render_B, True),
    "B_clean": (_render_B, False),
    "C_full":  (_render_C, True),
    "C_clean": (_render_C, False),
    "D_full":  (_render_D, True),
    "D_clean": (_render_D, False),
    "F_full":  (_render_F, True),
    "F_clean": (_render_F, False),
    "G":       (_render_G, True),   # show_params 被忽略
    "H":       (_render_H, True),   # show_params 被忽略
}

# 给前端用的友好元数据
_STYLE_META = {
    "A":       {"name": "标准底栏",
                "desc": "白色信息条，左机型/镜头 ｜ 中品牌 Logo ｜ 右参数/时间"},
    "B_full":  {"name": "极简底栏-详尽",
                "desc": "顶/左/右贴边窄白 + 底部居中 Logo + 机型 + 镜头·参数·时间"},
    "B_clean": {"name": "极简底栏-极简",
                "desc": "顶/左/右贴边窄白 + 底部居中 Logo + 机型"},
    "C_full":  {"name": "毛玻璃悬浮-详尽",
                "desc": "原图模糊作背景 + 缩小照片悬浮居中带阴影 + 镜头·参数·时间"},
    "C_clean": {"name": "毛玻璃悬浮-极简",
                "desc": "原图模糊作背景 + 缩小照片悬浮居中带阴影 + 品牌 Logo"},
    "D_full":  {"name": "经典白边相框-详尽",
                "desc": "顶/左/右窄白 + 底大白边居中放品牌 + 镜头·参数·时间"},
    "D_clean": {"name": "经典白边相框-极简",
                "desc": "顶/左/右窄白 + 底大白边居中放品牌 + 机型"},
    "F_full":  {"name": "杂志风-详尽",
                "desc": "左下色卡（从图自动提取）+ 右下品牌 + 镜头·参数·时间"},
    "F_clean": {"name": "杂志风-极简",
                "desc": "左下色卡（从图自动提取）+ 右下品牌 + 机型"},
    "G":       {"name": "极简白边",
                "desc": "照片四周均匀窄白边，无任何文字"},
    "H":       {"name": "相机回放",
                "desc": "上原图 + 下模糊版 + 居中富士 X-T5，LCD 与取景器都显示画面"},
}


def list_templates() -> list[dict]:
    """前端取可用样式列表。"""
    out = []
    for tid in _STYLE_SPECS:
        meta = _STYLE_META[tid]
        out.append({"id": tid, "name": meta["name"], "desc": meta["desc"]})
    return out


# ============================================================
# 入口
# ============================================================

def _sanitize_exif_orientation(exif_bytes: bytes) -> bytes:
    if not exif_bytes:
        return b""
    try:
        import piexif  # type: ignore
        ed = piexif.load(exif_bytes)
        if piexif.ImageIFD.Orientation in ed.get("0th", {}):
            ed["0th"][piexif.ImageIFD.Orientation] = 1
        return piexif.dump(ed)
    except Exception:
        return b""


def render(img_path: str | Path, cfg: WatermarkConfig,
           preview_max_side: Optional[int] = None) -> bytes:
    """主入口：套水印 + 返回 JPEG bytes。"""
    src = Image.open(img_path)
    exif = parse_exif(src)         # 先读 EXIF（exif_transpose 会丢）
    img = ImageOps.exif_transpose(src).convert("RGB")

    if preview_max_side and max(img.size) > preview_max_side:
        scale = preview_max_side / max(img.size)
        img = img.resize(
            (max(1, int(img.size[0] * scale)),
             max(1, int(img.size[1] * scale))),
            Image.LANCZOS,
        )

    spec = _STYLE_SPECS.get(cfg.template)
    if spec is None:
        spec = _STYLE_SPECS["A"]
    fn, show_params = spec
    try:
        canvas = fn(img, exif, show_params=show_params)
    except Exception:
        logger.exception(f"模板 {cfg.template} 渲染失败，回退 A")
        canvas = _render_A(img, exif, show_params=True)

    buf = io.BytesIO()
    exif_bytes = _sanitize_exif_orientation(Image.open(img_path).info.get("exif", b""))
    save_kwargs = {
        "format": "JPEG", "quality": 92, "optimize": True,
        "progressive": True, "subsampling": 1,
    }
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes
    if canvas.mode != "RGB":
        canvas = canvas.convert("RGB")
    canvas.save(buf, **save_kwargs)
    return buf.getvalue()


def batch_export(
    src_paths: list[str | Path],
    dst_dir: str | Path,
    cfg: WatermarkConfig,
    progress_cb=None,
    cancel_check=None,
) -> dict:
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed: list[tuple[str, str]] = []
    total = len(src_paths)
    for i, src in enumerate(src_paths, 1):
        if cancel_check and cancel_check():
            break
        src_p = Path(src)
        try:
            data = render(src_p, cfg, preview_max_side=None)
            out_name = src_p.stem + ".jpg"
            (dst / out_name).write_bytes(data)
            ok += 1
        except Exception as e:
            failed.append((src_p.name, f"{type(e).__name__}: {e}"))
            logger.exception(f"watermark: 处理 {src_p} 失败")
        if progress_cb:
            try:
                progress_cb(i, total, src_p.name)
            except Exception:
                pass
    return {"ok": ok, "failed": failed, "total": total}


def available_logos() -> list[dict]:
    if not LOGOS_DIR.exists():
        return []
    out = []
    for p in sorted(LOGOS_DIR.glob("*.png")):
        out.append({
            "name": p.stem,
            "file": p.name,
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    return out
