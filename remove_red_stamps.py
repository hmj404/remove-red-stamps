#!/usr/bin/env python3
"""
移除 PDF 中一个或多个红色印章，并输出新的 PDF。

处理策略：
1. 对红色电子签名/印章批注，直接删除其独立外观层，完整保留下方内容。
2. 对已经压平到页面内容中的红章，识别大型红色像素簇，生成透明修补层；
   原 PDF 的文字、矢量和图片对象仍被保留，修补层只覆盖识别到的红色像素。

依赖安装：
    pip install numpy pillow pypdf pypdfium2 reportlab

示例：
    python remove_red_stamps.py input.pdf
    python remove_red_stamps.py input.pdf -o output.pdf
    python remove_red_stamps.py input.pdf --debug-dir debug --overwrite
"""

from __future__ import annotations

import argparse
import io
import math
import os
import shutil
import sys
import tempfile
from collections import deque
from ctypes import c_uint
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import numpy as np
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from PIL import Image, ImageDraw
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"缺少依赖 {exc.name!r}。请运行：\n"
        "pip install numpy pillow pypdf pypdfium2 reportlab"
    ) from exc


OUTPUT_SUFFIX = "_去除红章"


@dataclass(frozen=True)
class Settings:
    dpi: int
    red_min: int
    red_dominance: int
    red_ratio: float
    cluster_gap_mm: float
    min_size_mm: float
    min_red_pixels: int
    max_density: float
    all_red: bool
    edge_padding_px: int
    fill_mode: str


@dataclass(frozen=True)
class StampRegion:
    page_index: int
    bbox: tuple[int, int, int, int]
    red_pixels: int
    density: float


def ref_key(value: object) -> tuple[int, int] | None:
    if isinstance(value, IndirectObject):
        return value.idnum, value.generation
    reference = getattr(value, "indirect_reference", None)
    if isinstance(reference, IndirectObject):
        return reference.idnum, reference.generation
    return None


def resolve(value: object) -> DictionaryObject:
    obj = value.get_object() if hasattr(value, "get_object") else value
    if not isinstance(obj, DictionaryObject):
        raise TypeError("PDF 批注对象格式异常。")
    return obj


def effective_field_type(annotation: DictionaryObject) -> object:
    field_type = annotation.get("/FT")
    if field_type is not None:
        return field_type
    parent_ref = annotation.get("/Parent")
    if parent_ref is None:
        return None
    return resolve(parent_ref).get("/FT")


def is_stamp_annotation(annotation: DictionaryObject) -> bool:
    """只把签名、印章或常见 CA 自定义印章批注视为候选。"""
    subtype = str(annotation.get("/Subtype", ""))
    if subtype == "/Stamp":
        return True
    if effective_field_type(annotation) == "/Sig":
        return True
    return subtype.startswith("/BJCA:")


def make_red_mask(rgb: np.ndarray, settings: Settings) -> np.ndarray:
    data = rgb.astype(np.int16, copy=False)
    red = data[:, :, 0]
    green = data[:, :, 1]
    blue = data[:, :, 2]
    other = np.maximum(green, blue)
    return (
        (red >= settings.red_min)
        & ((red - other) >= settings.red_dominance)
        & (red >= other * settings.red_ratio)
    )


def make_cleanup_red_mask(rgb: np.ndarray, settings: Settings) -> np.ndarray:
    """在已确认的印章框内使用更宽松阈值，覆盖粉红色抗锯齿边缘。"""
    data = rgb.astype(np.int16, copy=False)
    red = data[:, :, 0]
    green = data[:, :, 1]
    blue = data[:, :, 2]
    other = np.maximum(green, blue)
    soft_min = max(45, settings.red_min - 55)
    soft_dominance = max(6, settings.red_dominance // 4)
    soft_ratio = 1.0 + max(0.02, (settings.red_ratio - 1.0) * 0.20)
    return (
        (red >= soft_min)
        & ((red - other) >= soft_dominance)
        & (red >= other * soft_ratio)
    )


def cancelled_rotation(reader_page: DictionaryObject) -> int:
    """抵消页面 /Rotate，使渲染像素与未旋转 PDF 坐标一致。"""
    rotation = int(getattr(reader_page, "rotation", 0) or 0) % 360
    return (-rotation) % 360


def render_page(
    pdf_page: object,
    reader_page: DictionaryObject,
    dpi: int,
    *,
    draw_annots: bool,
    draw_forms: bool,
) -> tuple[np.ndarray, object]:
    bitmap = pdf_page.render(
        scale=dpi / 72.0,
        rotation=cancelled_rotation(reader_page),
        may_draw_forms=draw_forms,
        draw_annots=draw_annots,
        rev_byteorder=True,
        fill_color=(255, 255, 255, 255),
    )
    image = np.array(bitmap.to_pil().convert("RGB"), dtype=np.uint8)
    return image, bitmap


def rect_to_bitmap_bbox(
    rect: Iterable[object], bitmap: object, pdf_page: object
) -> tuple[int, int, int, int]:
    values = [float(value) for value in rect]
    if len(values) != 4:
        raise ValueError("无效的批注矩形。")
    left, bottom, right, top = values
    converter = bitmap.get_posconv(pdf_page)
    points = [
        converter.to_bitmap(x, y)
        for x, y in (
            (left, bottom),
            (left, top),
            (right, bottom),
            (right, top),
        )
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        math.floor(min(xs)),
        math.floor(min(ys)),
        math.ceil(max(xs)),
        math.ceil(max(ys)),
    )


def clip_bbox(
    bbox: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, min(width, x0)),
        max(0, min(height, y0)),
        max(0, min(width, x1)),
        max(0, min(height, y1)),
    )


def detect_red_stamp_annotations(
    input_path: Path,
    reader: PdfReader,
    selected_pages: set[int],
    settings: Settings,
) -> dict[int, set[int]]:
    """通过“显示批注/隐藏批注”的渲染差异，确认候选批注确实是红色。"""
    removals: dict[int, set[int]] = {}
    document = pdfium.PdfDocument(str(input_path))
    try:
        try:
            document.init_forms()
        except Exception:
            # 某些 PDF 没有标准表单环境，但普通 /Stamp 批注仍可渲染。
            pass

        for page_index, reader_page in enumerate(reader.pages):
            if page_index not in selected_pages:
                continue
            annots = reader_page.get("/Annots", [])
            candidates = [
                index
                for index, annot_ref in enumerate(annots)
                if is_stamp_annotation(resolve(annot_ref))
            ]
            if not candidates:
                continue

            pdf_page = document[page_index]
            with_annots, bitmap = render_page(
                pdf_page,
                reader_page,
                settings.dpi,
                draw_annots=True,
                draw_forms=True,
            )
            without_annots, plain_bitmap = render_page(
                pdf_page,
                reader_page,
                settings.dpi,
                draw_annots=False,
                draw_forms=False,
            )
            red_with = make_red_mask(with_annots, settings)
            red_without = make_red_mask(without_annots, settings)
            visual_delta = np.max(
                np.abs(with_annots.astype(np.int16) - without_annots.astype(np.int16)),
                axis=2,
            )
            annotation_red = red_with & ((~red_without) | (visual_delta >= 20))
            height, width = annotation_red.shape
            threshold = max(12, round(30 * (settings.dpi / 200.0) ** 2))

            for annot_index in candidates:
                annotation = resolve(annots[annot_index])
                rect = annotation.get("/Rect")
                if rect is None:
                    continue
                bbox = clip_bbox(
                    rect_to_bitmap_bbox(rect, bitmap, pdf_page), width, height
                )
                x0, y0, x1, y1 = bbox
                if x1 <= x0 or y1 <= y0:
                    continue
                if int(annotation_red[y0:y1, x0:x1].sum()) >= threshold:
                    removals.setdefault(page_index, set()).add(annot_index)
            plain_bitmap.close()
            bitmap.close()
            pdf_page.close()
    finally:
        document.close()
    return removals


def prune_field_tree(
    fields: Iterable[object], removed: set[tuple[int, int]]
) -> ArrayObject:
    kept = ArrayObject()
    for field_ref in fields:
        if ref_key(field_ref) in removed:
            continue
        field = resolve(field_ref)
        kids = field.get("/Kids")
        if kids is not None:
            new_kids = prune_field_tree(kids, removed)
            if new_kids:
                field[NameObject("/Kids")] = new_kids
            else:
                field.pop(NameObject("/Kids"), None)
                if field.get("/FT") is None:
                    continue
        kept.append(field_ref)
    return kept


def write_without_annotations(
    source_path: Path,
    reader: PdfReader,
    removals: dict[int, set[int]],
    output_path: Path,
) -> int:
    if not removals:
        shutil.copyfile(source_path, output_path)
        return 0

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    removed_refs: set[tuple[int, int]] = set()
    removed_count = 0

    for page_index, page in enumerate(writer.pages):
        selected = removals.get(page_index)
        if not selected:
            continue
        annots = page.get("/Annots", [])
        kept = ArrayObject()
        for annot_index, annot_ref in enumerate(annots):
            if annot_index in selected:
                key = ref_key(annot_ref)
                if key is not None:
                    removed_refs.add(key)
                removed_count += 1
            else:
                kept.append(annot_ref)
        if kept:
            page[NameObject("/Annots")] = kept
        else:
            page.pop(NameObject("/Annots"), None)

    acroform_ref = writer.root_object.get("/AcroForm")
    if acroform_ref is not None and removed_refs:
        acroform = resolve(acroform_ref)
        fields = acroform.get("/Fields")
        if fields is not None:
            acroform[NameObject("/Fields")] = prune_field_tree(fields, removed_refs)

    with output_path.open("wb") as stream:
        writer.write(stream)
    return removed_count


def block_reduce_any(mask: np.ndarray, block: int) -> np.ndarray:
    height, width = mask.shape
    padded_height = math.ceil(height / block) * block
    padded_width = math.ceil(width / block) * block
    padded = np.zeros((padded_height, padded_width), dtype=bool)
    padded[:height, :width] = mask
    return padded.reshape(
        padded_height // block, block, padded_width // block, block
    ).any(axis=(1, 3))


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    result = mask.copy()
    height, width = result.shape
    for _ in range(radius):
        padded = np.pad(result, 1, mode="constant", constant_values=False)
        result = np.zeros_like(result)
        for dy in range(3):
            for dx in range(3):
                result |= padded[dy : dy + height, dx : dx + width]
    return result


def connected_bboxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    height, width = mask.shape
    seen = np.zeros_like(mask)
    boxes: list[tuple[int, int, int, int]] = []
    for seed_y, seed_x in zip(*np.nonzero(mask)):
        if seen[seed_y, seed_x]:
            continue
        queue = deque([(int(seed_y), int(seed_x))])
        seen[seed_y, seed_x] = True
        min_x = max_x = int(seed_x)
        min_y = max_y = int(seed_y)
        while queue:
            y, x = queue.popleft()
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for ny in range(max(0, y - 1), min(height, y + 2)):
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
        boxes.append((min_x, min_y, max_x + 1, max_y + 1))
    return boxes


def detect_stamp_regions(
    red_mask: np.ndarray,
    page_index: int,
    settings: Settings,
) -> list[StampRegion]:
    if not red_mask.any():
        return []

    # 约 0.45 mm/格，在较低内存占用下把同一个章的分离笔画聚成一组。
    block = max(2, round(settings.dpi * 0.45 / 25.4))
    reduced = block_reduce_any(red_mask, block)
    gap_pixels = settings.cluster_gap_mm * settings.dpi / 25.4
    radius = max(0, math.ceil(gap_pixels / block))
    grouped = dilate(reduced, radius)

    height, width = red_mask.shape
    regions: list[StampRegion] = []
    for grid_box in connected_bboxes(grouped):
        gx0, gy0, gx1, gy1 = grid_box
        search_x0 = max(0, gx0 * block)
        search_y0 = max(0, gy0 * block)
        search_x1 = min(width, gx1 * block)
        search_y1 = min(height, gy1 * block)
        local = red_mask[search_y0:search_y1, search_x0:search_x1]
        ys, xs = np.nonzero(local)
        if not len(xs):
            continue
        x0 = search_x0 + int(xs.min())
        y0 = search_y0 + int(ys.min())
        x1 = search_x0 + int(xs.max()) + 1
        y1 = search_y0 + int(ys.max()) + 1
        region_mask = red_mask[y0:y1, x0:x1]
        red_pixels = int(region_mask.sum())
        area = max(1, (x1 - x0) * (y1 - y0))
        density = red_pixels / area
        width_mm = (x1 - x0) * 25.4 / settings.dpi
        height_mm = (y1 - y0) * 25.4 / settings.dpi
        aspect = width_mm / max(height_mm, 1e-6)

        min_pixels = round(
            settings.min_red_pixels * (settings.dpi / 200.0) ** 2
        )
        accepted = settings.all_red or (
            width_mm >= settings.min_size_mm
            and height_mm >= settings.min_size_mm
            and red_pixels >= min_pixels
            and density <= settings.max_density
            and 0.15 <= aspect <= 6.5
        )
        if accepted:
            regions.append(
                StampRegion(
                    page_index=page_index,
                    bbox=(x0, y0, x1, y1),
                    red_pixels=red_pixels,
                    density=density,
                )
            )
    return regions


def diffuse_inpaint(
    rgb: np.ndarray, mask: np.ndarray, fallback: np.ndarray
) -> np.ndarray:
    """从红色区域边缘向内传播邻近底色，不依赖 OpenCV/SciPy。"""
    work = rgb.astype(np.float32)
    known = ~mask.copy()
    unknown = mask.copy()
    work[unknown] = 0
    height, width = mask.shape

    # 印章笔画通常很薄；128 轮足以覆盖异常粗的实心区域。
    for _ in range(min(128, max(height, width))):
        if not unknown.any():
            break
        padded_values = np.pad(work, ((1, 1), (1, 1), (0, 0)))
        padded_known = np.pad(known, 1, mode="constant", constant_values=False)
        sums = np.zeros_like(work)
        counts = np.zeros((height, width), dtype=np.float32)
        for dy in range(3):
            for dx in range(3):
                if dx == 1 and dy == 1:
                    continue
                valid = padded_known[dy : dy + height, dx : dx + width]
                values = padded_values[dy : dy + height, dx : dx + width]
                sums += values * valid[:, :, None]
                counts += valid
        fillable = unknown & (counts > 0)
        if not fillable.any():
            break
        work[fillable] = sums[fillable] / counts[fillable, None]
        known[fillable] = True
        unknown[fillable] = False

    if unknown.any():
        work[unknown] = fallback
    return np.clip(np.rint(work), 0, 255).astype(np.uint8)


def estimate_background_color(
    rgb: np.ndarray, mask: np.ndarray, fallback: np.ndarray
) -> np.ndarray:
    """用非印章像素中最常见的量化色块估计局部底色。"""
    pixels = rgb[~mask]
    if not len(pixels):
        return fallback.astype(np.uint8)
    quantized = (pixels // 16).astype(np.uint16)
    keys = (
        quantized[:, 0] * 256
        + quantized[:, 1] * 16
        + quantized[:, 2]
    )
    counts = np.bincount(keys, minlength=4096)
    winning_key = int(counts.argmax())
    selected = pixels[keys == winning_key]
    if not len(selected):
        return fallback.astype(np.uint8)
    return np.clip(np.rint(selected.mean(axis=0)), 0, 255).astype(np.uint8)


def build_overlay(
    rgb: np.ndarray,
    red_mask: np.ndarray,
    regions: list[StampRegion],
    settings: Settings,
) -> np.ndarray:
    height, width, _ = rgb.shape
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    non_red = rgb[~red_mask]
    fallback = (
        np.median(non_red, axis=0).astype(np.float32)
        if len(non_red)
        else np.array([255, 255, 255], dtype=np.float32)
    )
    cleanup_mask = make_cleanup_red_mask(rgb, settings)

    for region in regions:
        x0, y0, x1, y1 = region.bbox
        margin = max(3, round(width / 2000), settings.edge_padding_px + 2)
        px0 = max(0, x0 - margin)
        py0 = max(0, y0 - margin)
        px1 = min(width, x1 + margin)
        py1 = min(height, y1 + margin)
        local_mask = cleanup_mask[py0:py1, px0:px1].copy()
        if settings.edge_padding_px:
            local_mask = dilate(local_mask, settings.edge_padding_px)
        target = overlay[py0:py1, px0:px1]
        source_patch = rgb[py0:py1, px0:px1]
        if settings.fill_mode == "inpaint":
            replacement = diffuse_inpaint(source_patch, local_mask, fallback)
            target[local_mask, :3] = replacement[local_mask]
        else:
            background = estimate_background_color(
                source_patch, local_mask, fallback
            )
            target[local_mask, :3] = background
        target[local_mask, 3] = 255
    return overlay


def object_color(
    page_object: object, *, fill: bool
) -> tuple[int, int, int, int] | None:
    red = c_uint()
    green = c_uint()
    blue = c_uint()
    alpha = c_uint()
    function = (
        pdfium_c.FPDFPageObj_GetFillColor
        if fill
        else pdfium_c.FPDFPageObj_GetStrokeColor
    )
    if not function(page_object, red, green, blue, alpha):
        return None
    return red.value, green.value, blue.value, alpha.value


def color_is_red(
    color: tuple[int, int, int, int] | None, settings: Settings
) -> bool:
    if color is None:
        return False
    red, green, blue, alpha = color
    other = max(green, blue)
    return (
        alpha > 8
        and red >= settings.red_min
        and red - other >= settings.red_dominance
        and red >= other * settings.red_ratio
    )


def pixel_bbox_to_page_bbox(
    bbox: tuple[int, int, int, int], bitmap: object, pdf_page: object
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    converter = bitmap.get_posconv(pdf_page)
    points = [
        converter.to_page(x, y)
        for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    ]
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def remove_red_vector_objects(
    input_path: Path,
    output_path: Path,
    regions_by_page: dict[int, list[StampRegion]],
    settings: Settings,
) -> int:
    """删除印章框内可独立识别的红色路径/文字对象，露出其下方内容。"""
    if not regions_by_page:
        shutil.copyfile(input_path, output_path)
        return 0

    reader = PdfReader(str(input_path))
    document = pdfium.PdfDocument(str(input_path))
    removed = 0
    changed = False
    try:
        for page_index, regions in regions_by_page.items():
            pdf_page = document[page_index]
            _, bitmap = render_page(
                pdf_page,
                reader.pages[page_index],
                settings.dpi,
                draw_annots=False,
                draw_forms=False,
            )
            page_boxes = [
                pixel_bbox_to_page_bbox(region.bbox, bitmap, pdf_page)
                for region in regions
            ]
            bitmap.close()
            removable: list[object] = []
            for page_object in list(pdf_page.get_objects(max_depth=15)):
                if page_object.type not in (
                    pdfium_c.FPDF_PAGEOBJ_TEXT,
                    pdfium_c.FPDF_PAGEOBJ_PATH,
                ):
                    continue
                if not (
                    color_is_red(object_color(page_object, fill=True), settings)
                    or color_is_red(
                        object_color(page_object, fill=False), settings
                    )
                ):
                    continue
                try:
                    object_box = tuple(float(value) for value in page_object.get_bounds())
                except Exception:
                    continue
                if any(bboxes_intersect(object_box, box) for box in page_boxes):
                    removable.append(page_object)

            for page_object in removable:
                pdf_page.remove_obj(page_object)
                page_object.close()
                removed += 1
            if removable:
                pdf_page.gen_content()
                changed = True
            pdf_page.close()

        if changed:
            document.save(str(output_path))
        else:
            shutil.copyfile(input_path, output_path)
    finally:
        document.close()
    return removed


def build_overlays_for_known_regions(
    input_path: Path,
    regions_by_page: dict[int, list[StampRegion]],
    settings: Settings,
) -> tuple[dict[int, np.ndarray], dict[int, list[StampRegion]]]:
    """矢量对象删除后，仅修补原印章框内仍残留的红色图像像素。"""
    reader = PdfReader(str(input_path))
    document = pdfium.PdfDocument(str(input_path))
    overlays: dict[int, np.ndarray] = {}
    remaining_by_page: dict[int, list[StampRegion]] = {}
    try:
        for page_index, original_regions in regions_by_page.items():
            pdf_page = document[page_index]
            rgb, bitmap = render_page(
                pdf_page,
                reader.pages[page_index],
                settings.dpi,
                draw_annots=False,
                draw_forms=False,
            )
            red_mask = make_red_mask(rgb, settings)
            remaining_regions: list[StampRegion] = []
            for region in original_regions:
                x0, y0, x1, y1 = clip_bbox(
                    region.bbox, red_mask.shape[1], red_mask.shape[0]
                )
                count = int(red_mask[y0:y1, x0:x1].sum())
                if count >= max(8, round(region.red_pixels * 0.015)):
                    area = max(1, (x1 - x0) * (y1 - y0))
                    remaining_regions.append(
                        StampRegion(
                            page_index=page_index,
                            bbox=(x0, y0, x1, y1),
                            red_pixels=count,
                            density=count / area,
                        )
                    )
            if remaining_regions:
                overlays[page_index] = build_overlay(
                    rgb, red_mask, remaining_regions, settings
                )
                remaining_by_page[page_index] = remaining_regions
            bitmap.close()
            pdf_page.close()
    finally:
        document.close()
    return overlays, remaining_by_page


def save_debug_images(
    debug_dir: Path,
    page_index: int,
    rgb: np.ndarray,
    red_mask: np.ndarray,
    regions: list[StampRegion],
) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    page_number = page_index + 1
    Image.fromarray(rgb).save(debug_dir / f"page-{page_number:03d}-render.png")
    Image.fromarray((red_mask * 255).astype(np.uint8)).save(
        debug_dir / f"page-{page_number:03d}-red-mask.png"
    )
    preview = Image.fromarray(rgb.copy())
    draw = ImageDraw.Draw(preview)
    for region in regions:
        draw.rectangle(region.bbox, outline=(255, 0, 255), width=4)
    preview.save(debug_dir / f"page-{page_number:03d}-detected.png")


def detect_flattened_stamps(
    base_path: Path,
    selected_pages: set[int],
    settings: Settings,
    debug_dir: Path | None,
) -> tuple[dict[int, np.ndarray], dict[int, list[StampRegion]]]:
    reader = PdfReader(str(base_path))
    document = pdfium.PdfDocument(str(base_path))
    overlays: dict[int, np.ndarray] = {}
    regions_by_page: dict[int, list[StampRegion]] = {}
    try:
        for page_index, reader_page in enumerate(reader.pages):
            if page_index not in selected_pages:
                continue
            pdf_page = document[page_index]
            rgb, bitmap = render_page(
                pdf_page,
                reader_page,
                settings.dpi,
                draw_annots=False,
                draw_forms=False,
            )
            red_mask = make_red_mask(rgb, settings)
            regions = detect_stamp_regions(red_mask, page_index, settings)
            if debug_dir is not None:
                save_debug_images(debug_dir, page_index, rgb, red_mask, regions)
            if regions:
                overlays[page_index] = build_overlay(
                    rgb, red_mask, regions, settings
                )
                regions_by_page[page_index] = regions
            bitmap.close()
            pdf_page.close()
    finally:
        document.close()
    return overlays, regions_by_page


def create_overlay_pdf(
    base_reader: PdfReader,
    overlays: dict[int, np.ndarray],
    output_path: Path,
) -> None:
    pdf_canvas = canvas.Canvas(str(output_path), pagesize=(1, 1))
    buffers: list[io.BytesIO] = []
    for page_index, page in enumerate(base_reader.pages):
        media = page.mediabox
        media_width = float(media.width)
        media_height = float(media.height)
        pdf_canvas.setPageSize((media_width, media_height))
        overlay = overlays.get(page_index)
        if overlay is not None:
            crop = page.cropbox
            buffer = io.BytesIO()
            Image.fromarray(overlay, mode="RGBA").save(buffer, format="PNG")
            buffer.seek(0)
            buffers.append(buffer)
            pdf_canvas.drawImage(
                ImageReader(buffer),
                float(crop.left) - float(media.left),
                float(crop.bottom) - float(media.bottom),
                width=float(crop.width),
                height=float(crop.height),
                mask="auto",
            )
        pdf_canvas.showPage()
    pdf_canvas.save()


def merge_overlays(
    base_path: Path,
    overlay_path: Path,
    overlays: dict[int, np.ndarray],
    output_path: Path,
) -> None:
    base_reader = PdfReader(str(base_path))
    overlay_reader = PdfReader(str(overlay_path))
    writer = PdfWriter()
    writer.clone_document_from_reader(base_reader)

    for page_index in overlays:
        base_page = writer.pages[page_index]
        overlay_page = overlay_reader.pages[page_index]
        media = base_page.mediabox
        if float(media.left) or float(media.bottom):
            overlay_page.add_transformation(
                Transformation().translate(
                    tx=float(media.left), ty=float(media.bottom)
                )
            )
        base_page.merge_page(overlay_page, over=True, expand=False)

    with output_path.open("wb") as stream:
        writer.write(stream)


def validate_output(
    source_reader: PdfReader,
    output_path: Path,
    regions_by_page: dict[int, list[StampRegion]],
    settings: Settings,
) -> None:
    result_reader = PdfReader(str(output_path))
    if len(result_reader.pages) != len(source_reader.pages):
        raise RuntimeError("输出校验失败：页数发生变化。")
    for source_page, result_page in zip(source_reader.pages, result_reader.pages):
        source_box = tuple(float(value) for value in source_page.mediabox)
        result_box = tuple(float(value) for value in result_page.mediabox)
        if any(
            not math.isclose(source, result, abs_tol=0.01)
            for source, result in zip(source_box, result_box)
        ):
            raise RuntimeError("输出校验失败：页面尺寸发生变化。")

    if not regions_by_page:
        return
    document = pdfium.PdfDocument(str(output_path))
    try:
        for page_index, regions in regions_by_page.items():
            pdf_page = document[page_index]
            rgb, bitmap = render_page(
                pdf_page,
                result_reader.pages[page_index],
                settings.dpi,
                draw_annots=True,
                draw_forms=True,
            )
            red_after = make_red_mask(rgb, settings)
            for region in regions:
                x0, y0, x1, y1 = clip_bbox(
                    region.bbox, red_after.shape[1], red_after.shape[0]
                )
                remaining = int(red_after[y0:y1, x0:x1].sum())
                if remaining > max(8, round(region.red_pixels * 0.08)):
                    raise RuntimeError(
                        f"输出校验失败：第 {page_index + 1} 页仍残留较多红色像素。"
                    )
            bitmap.close()
            pdf_page.close()
    finally:
        document.close()


def parse_page_spec(spec: str | None, page_count: int) -> set[int]:
    if spec is None:
        return set(range(page_count))
    selected: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start - 1, end))
        else:
            selected.add(int(token) - 1)
    if not selected or min(selected) < 0 or max(selected) >= page_count:
        raise ValueError(f"--pages 超出范围；该 PDF 共 {page_count} 页。")
    return selected


def process_pdf(args: argparse.Namespace) -> tuple[Path, int, int, int]:
    input_path = args.input.resolve()
    if not input_path.is_file() or input_path.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"找不到有效的输入 PDF：{input_path}")
    output_path = (
        args.output.resolve()
        if args.output is not None
        else input_path.with_name(f"{input_path.stem}{OUTPUT_SUFFIX}.pdf")
    )
    if output_path == input_path:
        raise ValueError("输出路径不能与输入文件相同。")
    if output_path.exists() and not args.overwrite:
        raise FileExistsError(f"输出文件已存在；如需覆盖请加 --overwrite：{output_path}")

    settings = Settings(
        dpi=args.dpi,
        red_min=args.red_min,
        red_dominance=args.red_dominance,
        red_ratio=args.red_ratio,
        cluster_gap_mm=args.cluster_gap_mm,
        min_size_mm=args.min_size_mm,
        min_red_pixels=args.min_red_pixels,
        max_density=args.max_density,
        all_red=args.all_red,
        edge_padding_px=args.edge_padding_px,
        fill_mode=args.fill_mode,
    )
    reader = PdfReader(str(input_path))
    if reader.is_encrypted:
        raise RuntimeError("输入 PDF 已加密，请先解密。")
    selected_pages = parse_page_spec(args.pages, len(reader.pages))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir = args.debug_dir.resolve() if args.debug_dir is not None else None
    with tempfile.TemporaryDirectory(prefix="remove-red-stamps-") as temp_name:
        temp_dir = Path(temp_name)
        base_path = temp_dir / "base.pdf"
        vector_path = temp_dir / "vector-cleaned.pdf"
        overlay_path = temp_dir / "overlay.pdf"

        removals: dict[int, set[int]] = {}
        if args.mode != "flattened-only":
            removals = detect_red_stamp_annotations(
                input_path, reader, selected_pages, settings
            )
        removed_annotations = write_without_annotations(
            input_path, reader, removals, base_path
        )

        overlays: dict[int, np.ndarray] = {}
        regions_by_page: dict[int, list[StampRegion]] = {}
        vector_objects = 0
        if args.mode != "annotations-only":
            overlays, regions_by_page = detect_flattened_stamps(
                base_path, selected_pages, settings, debug_dir
            )

        region_count = sum(len(items) for items in regions_by_page.values())
        if removed_annotations == 0 and region_count == 0:
            raise RuntimeError(
                "没有检测到符合条件的红章。可尝试降低 --min-size-mm，"
                "或用 --all-red 移除所有红色像素。"
            )

        effective_base_path = base_path
        remaining_regions_by_page = regions_by_page
        if regions_by_page:
            try:
                vector_objects = remove_red_vector_objects(
                    base_path, vector_path, regions_by_page, settings
                )
            except Exception as exc:
                print(
                    f"警告：红色矢量对象分离失败，将改用像素修补：{exc}",
                    file=sys.stderr,
                )
                vector_objects = 0
            else:
                if vector_objects:
                    effective_base_path = vector_path
                    overlays, remaining_regions_by_page = (
                        build_overlays_for_known_regions(
                            vector_path, regions_by_page, settings
                        )
                    )

        candidate_path = temp_dir / "result.pdf"
        if overlays:
            base_reader = PdfReader(str(effective_base_path))
            create_overlay_pdf(base_reader, overlays, overlay_path)
            merge_overlays(
                effective_base_path, overlay_path, overlays, candidate_path
            )
        else:
            shutil.copyfile(effective_base_path, candidate_path)

        validate_output(reader, candidate_path, regions_by_page, settings)

        # 先写入同目录临时文件，再原子替换，避免覆盖模式下损坏已有成品。
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as publish_stream:
            publish_temp = Path(publish_stream.name)
        try:
            shutil.copyfile(candidate_path, publish_temp)
            os.replace(publish_temp, output_path)
        finally:
            publish_temp.unlink(missing_ok=True)
    raster_region_count = sum(
        len(items) for items in remaining_regions_by_page.values()
    )
    return output_path, removed_annotations, vector_objects, raster_region_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="移除 PDF 中一个或多个红色印章，保留其他页面内容。"
    )
    parser.add_argument("input", type=Path, help="输入 PDF")
    parser.add_argument("-o", "--output", type=Path, help="输出 PDF；默认与输入同目录")
    parser.add_argument(
        "--mode",
        choices=("auto", "annotations-only", "flattened-only"),
        default="auto",
        help="处理模式；默认同时处理独立签章层和已压平红章",
    )
    parser.add_argument("--pages", help="仅处理指定页，如 1,3-5")
    parser.add_argument("--dpi", type=int, default=240, help="检测分辨率，默认 240")
    parser.add_argument("--red-min", type=int, default=105, help="红通道最低值")
    parser.add_argument(
        "--red-dominance", type=int, default=30, help="红色相对绿/蓝通道的最小差值"
    )
    parser.add_argument(
        "--red-ratio", type=float, default=1.15, help="红色相对绿/蓝通道的最小倍数"
    )
    parser.add_argument(
        "--cluster-gap-mm", type=float, default=4.5, help="归并印章笔画的最大间隙毫米数"
    )
    parser.add_argument(
        "--min-size-mm", type=float, default=7.0, help="自动红章区域的最小宽和高"
    )
    parser.add_argument(
        "--min-red-pixels", type=int, default=70, help="在 200 DPI 下的最少红色像素"
    )
    parser.add_argument(
        "--max-density", type=float, default=0.72, help="区域最大红色覆盖密度"
    )
    parser.add_argument(
        "--all-red",
        action="store_true",
        help="处理所有检测到的红色像素；可能同时移除红色文字或图标",
    )
    parser.add_argument(
        "--debug-dir", type=Path, help="保存渲染图、红色掩膜和检测框，便于调参"
    )
    parser.add_argument(
        "--edge-padding-px",
        type=int,
        default=2,
        help="在检测分辨率下覆盖印章抗锯齿边缘的像素数，默认 2",
    )
    parser.add_argument(
        "--fill-mode",
        choices=("background", "inpaint"),
        default="background",
        help="压平红章填充方式；background 使用局部主底色，inpaint 传播邻近颜色",
    )
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的输出文件")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not 72 <= args.dpi <= 600:
        parser.error("--dpi 必须在 72 到 600 之间。")
    if not 0 < args.red_ratio <= 5:
        parser.error("--red-ratio 必须大于 0。")
    if args.min_size_mm < 0 or args.cluster_gap_mm < 0:
        parser.error("尺寸参数不能为负数。")
    if not 0 <= args.edge_padding_px <= 8:
        parser.error("--edge-padding-px 必须在 0 到 8 之间。")
    if not 0 < args.max_density <= 1:
        parser.error("--max-density 必须在 0 到 1 之间。")

    try:
        (
            output_path,
            removed_annotations,
            vector_objects,
            raster_region_count,
        ) = process_pdf(args)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"完成：移除 {removed_annotations} 个红色签章批注层。")
    print(f"完成：移除 {vector_objects} 个红色矢量对象。")
    print(f"完成：修补 {raster_region_count} 个红色图像区域。")
    print(f"输出：{output_path}")
    if raster_region_count:
        print("提示：已压平红章遮住的原始细节无法被完全恢复，程序使用邻近底色修补。")
    print("提示：任何 PDF 修改都会使原数字签名失效，请保留原文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
