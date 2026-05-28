import importlib
import sys
import types

from PIL import Image, ImageFilter, ImageDraw

from pic_selecter.quality import analyze_image


def checkerboard(size=256, block=8):
    img = Image.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(img)
    for y in range(0, size, block):
        for x in range(0, size, block):
            if (x // block + y // block) % 2 == 0:
                draw.rectangle((x, y, x + block - 1, y + block - 1), fill="black")
    return img


def test_blurry_image_is_rejected_more_than_sharp_image():
    sharp = checkerboard()
    blurry = sharp.filter(ImageFilter.GaussianBlur(radius=8))

    sharp_q = analyze_image(sharp, file_size=200_000, strength="standard")
    blurry_q = analyze_image(blurry, file_size=200_000, strength="standard")

    assert sharp_q.blur_score > blurry_q.blur_score
    assert "blurry" not in sharp_q.flags
    assert {"blurry", "very_blurry"} & set(blurry_q.flags)
    assert blurry_q.auto_reject is True


def test_under_and_over_exposed_images_are_rejected():
    dark = Image.new("RGB", (256, 256), (4, 4, 4))
    bright = Image.new("RGB", (256, 256), (252, 252, 252))

    dark_q = analyze_image(dark, file_size=200_000, strength="standard")
    bright_q = analyze_image(bright, file_size=200_000, strength="standard")

    assert "underexposed" in dark_q.flags
    assert dark_q.auto_reject is True
    assert "overexposed" in bright_q.flags
    assert bright_q.auto_reject is True


def test_low_information_image_is_rejected():
    flat = Image.new("RGB", (256, 256), (128, 128, 128))

    q = analyze_image(flat, file_size=200_000, strength="standard")

    assert "low_information" in q.flags
    assert q.auto_reject is True
    assert q.reject_reason


def test_prescreen_strength_changes_blur_strictness():
    mildly_blurry = checkerboard(block=16).filter(ImageFilter.GaussianBlur(radius=2.2))

    conservative = analyze_image(mildly_blurry, file_size=200_000, strength="conservative")
    aggressive = analyze_image(mildly_blurry, file_size=200_000, strength="aggressive")

    assert aggressive.blur_score == conservative.blur_score
    assert aggressive.quality_score <= conservative.quality_score
    assert ("blurry" in aggressive.flags) or aggressive.auto_reject


def test_quality_info_can_be_serialized_to_plain_dict():
    q = analyze_image(checkerboard(), file_size=123_456, strength="standard")
    data = q.to_dict()

    assert data["blur_score"] == q.blur_score
    assert data["file_size"] == 123_456
    assert isinstance(data["flags"], list)


def test_grouper_cache_round_trips_quality_metrics(tmp_path):
    sys.modules.setdefault("imagehash", types.SimpleNamespace(phash=lambda *args, **kwargs: "0" * 16))
    grouper = importlib.import_module("pic_selecter.grouper")
    ImageInfo = grouper.ImageInfo
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"fake")
    info = ImageInfo(
        path=str(photo),
        phash="0" * 16,
        size=photo.stat().st_size,
        mtime=photo.stat().st_mtime,
        quality={"quality_score": 72.5, "flags": ["blurry"], "reject_reason": "画面模糊"},
    )

    grouper._save_cache(str(tmp_path), {str(photo): info})
    loaded = grouper._load_cache(str(tmp_path))

    assert loaded[str(photo)].quality["quality_score"] == 72.5
    assert loaded[str(photo)].quality["flags"] == ["blurry"]
