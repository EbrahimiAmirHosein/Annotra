"""Annotation rules with no Drive or UI code, so they can be tested on their own."""
import re

import numpy as np
from PIL import Image, ImageDraw

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
PALETTE = ["#E6194B", "#3CB44B", "#4363D8", "#F58231", "#911EB4", "#008080", "#9A6324", "#800000"]
UNKNOWN_COLOR = "#888888"
IOU_THRESHOLD = 0.1
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def parse_folder_id(text):
    """Accept a Drive folder link or a bare folder ID."""
    text = text.strip()
    match = (
        re.search(r"/folders/([\w-]+)", text)
        or re.search(r"[?&]id=([\w-]+)", text)
        or re.fullmatch(r"([\w-]{10,})", text)
    )
    if not match:
        raise ValueError("That doesn't look like a Google Drive folder link.")
    return match.group(1)


def parse_labels_text(text):
    """Read one label per line, each with an optional #RRGGBB color."""
    labels, seen = [], set()
    for line in text.splitlines():
        parts = line.strip().rsplit(None, 1)
        if not parts:
            continue
        name, color = " ".join(parts), None
        if len(parts) == 2 and _COLOR.fullmatch(parts[1]):
            name, color = parts
        if name in seen:
            raise ValueError(f"Label '{name}' is listed twice.")
        seen.add(name)
        labels.append({"name": name, "color": color or PALETTE[len(labels) % len(PALETTE)]})
    if not labels:
        raise ValueError("Enter at least one label.")
    return labels


def format_labels_text(labels):
    return "\n".join(f"{label['name']} {label['color']}" for label in labels)


def validate_labels(data):
    """Check a project file read from Drive, which anyone with folder access can edit."""
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list) or not labels:
        raise ValueError("The project file has no labels.")
    for label in labels:
        if (
            not isinstance(label, dict)
            or not isinstance(label.get("name"), str)
            or not _COLOR.fullmatch(str(label.get("color")))
        ):
            raise ValueError("The project file has an invalid label.")
    return labels


def is_valid_shape(shape):
    try:
        points = shape["points"]
        well_formed = isinstance(shape["label"], str) and all(
            len(point) == 2 and all(isinstance(value, int) for value in point) for point in points
        )
        return well_formed and (
            (shape["type"] == "box" and len(points) == 2)
            or (shape["type"] == "polygon" and len(points) >= 3)
        )
    except (KeyError, TypeError):
        return False


def box_points(start, end):
    """Return a box's corners in order, or None if the drag was too small to be a box."""
    x1, x2 = sorted((start[0], end[0]))
    y1, y2 = sorted((start[1], end[1]))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return [[x1, y1], [x2, y2]]


def to_rgb(image):
    """Convert any Pillow image to 8-bit RGB, stretching high-bit-depth scientific images."""
    if image.mode.startswith("I") or image.mode == "F":
        pixels = np.asarray(image, dtype=np.float64)
        low, high = pixels.min(), pixels.max()
        scaled = (pixels - low) / (high - low) * 255 if high > low else np.zeros_like(pixels)
        image = Image.fromarray(scaled.astype(np.uint8))
    return image.convert("RGB")


def _outline(shape):
    if shape["type"] == "box":
        (x1, y1), (x2, y2) = shape["points"]
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return [tuple(point) for point in shape["points"]]


def _area(outline):
    return abs(
        sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]))
    ) / 2


def _contains(outline, x, y):
    if any(point == (x, y) for point in outline):
        return True
    inside = False
    for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]):
        if (y1 > y) != (y2 > y) and x <= (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def shape_at(shapes, x, y):
    """Return the smallest shape under a point, so nested shapes stay removable."""
    hits = [shape for shape in shapes if _contains(_outline(shape), x, y)]
    return min(hits, key=lambda shape: _area(_outline(shape)), default=None)


def iou(a, b):
    """Intersection over union, rasterized because it handles any mix of boxes and polygons."""
    outline_a, outline_b = _outline(a), _outline(b)
    a_xs, a_ys = zip(*outline_a)
    b_xs, b_ys = zip(*outline_b)
    if max(a_xs) < min(b_xs) or max(b_xs) < min(a_xs) or max(a_ys) < min(b_ys) or max(b_ys) < min(a_ys):
        return 0.0
    left, top = min(a_xs + b_xs), min(a_ys + b_ys)
    size = (max(a_xs + b_xs) - left + 1, max(a_ys + b_ys) - top + 1)

    def mask(outline):
        canvas = Image.new("1", size)
        ImageDraw.Draw(canvas).polygon([(x - left, y - top) for x, y in outline], fill=1)
        return np.array(canvas, dtype=bool)

    mask_a, mask_b = mask(outline_a), mask(outline_b)
    union = np.logical_or(mask_a, mask_b).sum()
    return float(np.logical_and(mask_a, mask_b).sum() / union) if union else 0.0


def common_count(shapes_by_annotator):
    """Count the shapes every annotator agrees on, matching each shape at most once."""
    annotators = list(shapes_by_annotator)
    common = list(shapes_by_annotator[annotators[0]])
    for annotator in annotators[1:]:
        unused = list(shapes_by_annotator[annotator])
        kept = []
        for shape in common:
            score, index = max(((iou(shape, other), i) for i, other in enumerate(unused)), default=(0, -1))
            if score > IOU_THRESHOLD:
                kept.append(shape)
                unused.pop(index)
        common = kept
    return len(common)


def agreement_rows(shapes_per_image, label):
    """For one label, list (image, common count, {annotator: shapes only they drew}) per image."""
    rows = []
    for image, by_annotator in sorted(shapes_per_image.items()):
        selected = {
            annotator: [shape for shape in shapes if shape["label"] == label]
            for annotator, shapes in by_annotator.items()
        }
        common = common_count(selected)
        unique = {annotator: len(shapes) - common for annotator, shapes in selected.items()}
        if common or any(unique.values()):
            rows.append((image, common, unique))
    return rows
