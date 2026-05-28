"""K-style photobooth filters for exported winner photos."""

from __future__ import annotations

import io
import math
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


@dataclass
class PhotoboothConfig:
    """Frontend JSON config for the winner beautify export."""

    style: str = "seoul_booth"

    @classmethod
    def from_dict(cls, data: dict) -> "PhotoboothConfig":
        style = str(data.get("style") or data.get("template") or "seoul_booth")
        if style not in _STYLE_RENDERERS:
            style = "seoul_booth"
        return cls(style=style)


def list_styles() -> list[dict]:
    return [
        {
            "id": "seoul_booth",
            "name": "韩式大头贴",
            "desc": "奶白相纸边框 + 爱心贴纸 + 柔粉肤色",
        },
        {
            "id": "milk_peach",
            "name": "蜜桃滤镜",
            "desc": "轻提亮、低对比、偏粉白的清透网感",
        },
        {
            "id": "clean_flash",
            "name": "清透闪光",
            "desc": "亮面闪光感 + 轻冷白，适合人像和自拍",
        },
        {
            "id": "berry_sticker",
            "name": "莓果贴纸",
            "desc": "莓粉色调 + 星星贴纸 + 甜感边框",
        },
    ]


def _font_path(weight: str = "regular") -> Optional[str]:
    system = platform.system()
    candidates: list[str]
    if system == "Windows":
        root = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            str(Path(root) / "Fonts" / "msyh.ttc"),
            str(Path(root) / "Fonts" / "segoeui.ttf"),
            str(Path(root) / "Fonts" / "arial.ttf"),
        ]
        if weight in {"bold", "heavy"}:
            candidates.insert(1, str(Path(root) / "Fonts" / "msyhbd.ttc"))
            candidates.insert(2, str(Path(root) / "Fonts" / "segoeuib.ttf"))
    elif system == "Darwin":
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _font(size: int, weight: str = "regular") -> ImageFont.ImageFont:
    path = _font_path(weight)
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return ImageFont.load_default()


def _resize_preview(img: Image.Image, max_side: Optional[int]) -> Image.Image:
    if max_side and max(img.size) > max_side:
        scale = max_side / max(img.size)
        return img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
    return img


def _blend_color(img: Image.Image, color: tuple[int, int, int], alpha: float) -> Image.Image:
    overlay = Image.new("RGB", img.size, color)
    return Image.blend(img, overlay, max(0.0, min(1.0, alpha)))


def _screen(img: Image.Image, color: tuple[int, int, int], alpha: float) -> Image.Image:
    layer = Image.new("RGB", img.size, color)
    screened = ImageChops.screen(img, layer)
    return Image.blend(img, screened, max(0.0, min(1.0, alpha)))


def _soften(img: Image.Image, blur: float = 1.2, alpha: float = 0.18) -> Image.Image:
    blurred = img.filter(ImageFilter.GaussianBlur(blur))
    return Image.blend(img, blurred, alpha)


def _add_grain(img: Image.Image, opacity: float = 0.035) -> Image.Image:
    noise = Image.effect_noise(img.size, 9).convert("L")
    noise = ImageOps.autocontrast(noise)
    grain = Image.merge("RGB", (noise, noise, noise))
    return Image.blend(img, grain, opacity)


def _vignette(img: Image.Image, color: tuple[int, int, int], opacity: float = 0.18) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    pad = int(min(w, h) * 0.06)
    draw.ellipse((-pad, -pad, w + pad, h + pad), fill=255)
    mask = ImageOps.invert(mask.filter(ImageFilter.GaussianBlur(max(10, int(min(w, h) * 0.08)))))
    overlay = Image.new("RGB", img.size, color)
    mixed = Image.composite(overlay, img, mask.point(lambda p: int(p * opacity)))
    return mixed


def _filter_milk_peach(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Brightness(img).enhance(1.07)
    img = ImageEnhance.Contrast(img).enhance(0.92)
    img = ImageEnhance.Color(img).enhance(1.08)
    img = _screen(img, (255, 228, 220), 0.16)
    img = _blend_color(img, (255, 240, 244), 0.08)
    return _add_grain(_soften(img, 0.8, 0.08), 0.025)


def _filter_clean_flash(img: Image.Image) -> Image.Image:
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Brightness(img).enhance(1.08)
    img = ImageEnhance.Contrast(img).enhance(1.04)
    img = _screen(img, (226, 238, 255), 0.12)

    w, h = img.size
    glow = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(glow)
    r = int(min(w, h) * 0.44)
    d.ellipse((w - r, -r // 2, w + r, r + r // 2), fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(max(16, r // 4)))
    layer = Image.new("RGB", img.size, (255, 255, 255))
    img = Image.composite(layer, img, glow.point(lambda p: int(p * 0.34)))
    return _add_grain(img, 0.018)


def _filter_berry(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Color(img).enhance(1.18)
    img = ImageEnhance.Brightness(img).enhance(1.03)
    img = ImageEnhance.Contrast(img).enhance(0.98)
    img = _blend_color(img, (255, 214, 232), 0.11)
    return _vignette(_add_grain(img, 0.025), (95, 54, 80), 0.12)


def _heart_points(cx: int, cy: int, size: int) -> list[tuple[int, int]]:
    pts = []
    for i in range(0, 360, 10):
        t = math.radians(i)
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((int(cx + x * size / 32), int(cy + y * size / 32)))
    return pts


def _draw_heart(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int,
                fill: tuple[int, int, int, int], outline: tuple[int, int, int, int]) -> None:
    draw.polygon(_heart_points(cx, cy, size), fill=fill)
    draw.line(_heart_points(cx, cy, size) + [_heart_points(cx, cy, size)[0]], fill=outline, width=max(1, size // 18))


def _star_points(cx: int, cy: int, r_outer: int, r_inner: int) -> list[tuple[int, int]]:
    pts = []
    for i in range(10):
        r = r_outer if i % 2 == 0 else r_inner
        a = math.radians(-90 + i * 36)
        pts.append((int(cx + math.cos(a) * r), int(cy + math.sin(a) * r)))
    return pts


def _draw_bow(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int,
              fill: tuple[int, int, int, int]) -> None:
    half = size // 2
    draw.polygon([(cx, cy), (cx - half, cy - half // 2), (cx - half, cy + half // 2)], fill=fill)
    draw.polygon([(cx, cy), (cx + half, cy - half // 2), (cx + half, cy + half // 2)], fill=fill)
    draw.rounded_rectangle((cx - size // 8, cy - size // 5, cx + size // 8, cy + size // 5),
                           radius=size // 10, fill=(255, 255, 255, 230))


def _add_stickers(canvas: Image.Image, palette: str = "pink") -> Image.Image:
    out = canvas.convert("RGBA")
    draw = ImageDraw.Draw(out)
    w, h = out.size
    ref = max(1, min(w, h))
    pink = (255, 126, 169, 230)
    peach = (255, 190, 160, 230)
    lilac = (186, 160, 255, 220)
    ink = (108, 75, 91, 230)
    if palette == "berry":
        pink, peach, lilac, ink = (222, 80, 128, 230), (255, 178, 203, 230), (140, 116, 210, 220), (91, 46, 72, 230)

    _draw_heart(draw, int(w * 0.09), int(h * 0.14), int(ref * 0.09), pink, (255, 255, 255, 220))
    draw.polygon(_star_points(int(w * 0.90), int(h * 0.16), int(ref * 0.055), int(ref * 0.024)),
                 fill=(255, 224, 116, 235))
    _draw_bow(draw, int(w * 0.12), int(h * 0.87), int(ref * 0.09), lilac)
    _draw_heart(draw, int(w * 0.90), int(h * 0.86), int(ref * 0.07), peach, (255, 255, 255, 220))

    font = _font(max(10, int(ref * 0.022)), "bold")
    tag = "SEOUL SNAP"
    tag_w = int(draw.textlength(tag, font=font))
    pad_x = max(10, int(ref * 0.022))
    pad_y = max(5, int(ref * 0.010))
    x = int(w * 0.50 - tag_w / 2 - pad_x)
    y = int(h * 0.055)
    draw.rounded_rectangle((x, y, x + tag_w + pad_x * 2, y + int(ref * 0.045)),
                           radius=int(ref * 0.024), fill=(255, 255, 255, 210),
                           outline=(255, 202, 219, 220), width=max(1, int(ref * 0.004)))
    draw.text((x + pad_x, y + pad_y), tag, font=font, fill=ink)
    return out.convert("RGB")


def _frame(img: Image.Image, bg: tuple[int, int, int], bottom_ratio: float = 0.12) -> Image.Image:
    w, h = img.size
    border = max(18, int(min(w, h) * 0.045))
    bottom = max(border, int(h * bottom_ratio))
    canvas = Image.new("RGB", (w + border * 2, h + border + bottom), bg)
    canvas.paste(img, (border, border))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (border // 2, border // 2, canvas.width - border // 2, canvas.height - border // 2),
        radius=max(10, border // 2),
        outline=(255, 255, 255),
        width=max(2, border // 8),
    )
    label = datetime.now().strftime("%Y.%m.%d  soft mood")
    font = _font(max(12, int(min(canvas.size) * 0.028)), "regular")
    tw = draw.textlength(label, font=font)
    font_size = getattr(font, "size", max(12, int(min(canvas.size) * 0.028)))
    draw.text(((canvas.width - tw) / 2, h + border + (bottom - font_size) / 2 - border * 0.05),
              label, font=font, fill=(125, 96, 104))
    return canvas


def _render_seoul_booth(img: Image.Image) -> Image.Image:
    img = _filter_milk_peach(img)
    canvas = _frame(img, (255, 249, 249), bottom_ratio=0.16)
    return _add_stickers(canvas, "pink")


def _render_milk_peach(img: Image.Image) -> Image.Image:
    return _filter_milk_peach(img)


def _render_clean_flash(img: Image.Image) -> Image.Image:
    img = _filter_clean_flash(img)
    return _vignette(img, (220, 228, 250), 0.10)


def _render_berry_sticker(img: Image.Image) -> Image.Image:
    img = _filter_berry(img)
    canvas = _frame(img, (255, 246, 251), bottom_ratio=0.13)
    return _add_stickers(canvas, "berry")


_STYLE_RENDERERS = {
    "seoul_booth": _render_seoul_booth,
    "milk_peach": _render_milk_peach,
    "clean_flash": _render_clean_flash,
    "berry_sticker": _render_berry_sticker,
}


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


def render(img_path: str | Path, cfg: PhotoboothConfig,
           preview_max_side: Optional[int] = None) -> bytes:
    src = Image.open(img_path)
    exif_bytes = src.info.get("exif", b"")
    img = ImageOps.exif_transpose(src).convert("RGB")
    img = _resize_preview(img, preview_max_side)
    renderer = _STYLE_RENDERERS.get(cfg.style, _render_seoul_booth)
    canvas = renderer(img)

    buf = io.BytesIO()
    save_kwargs = {
        "format": "JPEG",
        "quality": 94,
        "optimize": True,
        "progressive": True,
        "subsampling": 1,
    }
    sanitized = _sanitize_exif_orientation(exif_bytes)
    if sanitized:
        save_kwargs["exif"] = sanitized
    canvas.save(buf, **save_kwargs)
    return buf.getvalue()


def batch_export(
    src_paths: list[str | Path],
    dst_dir: str | Path,
    cfg: PhotoboothConfig,
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
            out_name = src_p.stem + "_kstyle.jpg"
            (dst / out_name).write_bytes(data)
            ok += 1
        except Exception as e:
            failed.append((src_p.name, f"{type(e).__name__}: {e}"))
        if progress_cb:
            try:
                progress_cb(i, total, src_p.name)
            except Exception:
                pass
    return {"ok": ok, "failed": failed, "total": total}
