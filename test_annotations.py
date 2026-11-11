"""Run with: python test_annotations.py"""
import subprocess
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PIL import Image

import annotations as ann
import setup_google
from drive import quote
from setup_google import parse_client_secret


def box(x1, y1, x2, y2, label="a"):
    return {"type": "box", "label": label, "points": [[x1, y1], [x2, y2]]}


def poly(points, label="a"):
    return {"type": "polygon", "label": label, "points": points}


def raises(function, *args):
    try:
        function(*args)
    except ValueError:
        return True
    return False


def test_folder_links():
    expected = "1kTVr2h11Xl-_abc"
    for text in (
        f"https://drive.google.com/drive/folders/{expected}?usp=sharing",
        f"https://drive.google.com/drive/u/1/folders/{expected}",
        f"https://drive.google.com/open?id={expected}",
        f"  {expected} ",
    ):
        assert ann.parse_folder_id(text) == expected
    assert raises(ann.parse_folder_id, "x' or name contains '")


def test_labels():
    labels = ann.parse_labels_text("Car #FF8800\n\nTraffic light\n")
    assert labels[0] == {"name": "Car", "color": "#FF8800"}
    assert labels[1]["name"] == "Traffic light" and labels[1]["color"] == ann.PALETTE[1]
    assert ann.parse_labels_text(ann.format_labels_text(labels)) == labels
    assert raises(ann.parse_labels_text, "a\na")
    assert raises(ann.parse_labels_text, "  \n")
    assert raises(ann.validate_labels, {"labels": [{"name": "a", "color": "red; bad"}]})


def test_shape_validation():
    assert ann.is_valid_shape(box(0, 0, 5, 5))
    assert ann.is_valid_shape(poly([[0, 0], [5, 0], [5, 5]]))
    assert not ann.is_valid_shape(poly([[0, 0], [5, 0]]))
    assert not ann.is_valid_shape({"type": "circle", "label": "a", "points": [[0, 0], [1, 1]]})
    assert not ann.is_valid_shape({"label": "a"})
    assert not ann.is_valid_shape("junk")


def test_box_points():
    assert ann.box_points([9, 8], [1, 2]) == [[1, 2], [9, 8]]
    assert ann.box_points([1, 1], [2, 9]) is None


def test_iou():
    assert ann.iou(box(0, 0, 10, 10), box(0, 0, 10, 10)) == 1.0
    assert ann.iou(box(0, 0, 10, 10), box(50, 50, 60, 60)) == 0.0
    assert abs(ann.iou(box(0, 0, 9, 9), box(5, 0, 14, 9)) - 1 / 3) < 0.02
    triangle = poly([[0, 0], [20, 0], [0, 20]])
    assert 0.4 < ann.iou(triangle, box(0, 0, 20, 20)) < 0.6


def test_common_count():
    one = box(0, 0, 10, 10)
    far = box(50, 50, 60, 60)
    assert ann.common_count({"x": [one, far], "y": [one], "z": [one, far]}) == 1
    # one shape must not be matched twice
    assert ann.common_count({"x": [one, one], "y": [one]}) == 1
    assert ann.common_count({"x": [], "y": [one]}) == 0
    assert ann.common_count({"x": [poly([[0, 0], [10, 0], [10, 10], [0, 10]])], "y": [one]}) == 1


def test_agreement_rows():
    shapes = {"i.png": {"x": [box(0, 0, 9, 9), box(50, 50, 60, 60)], "y": [box(0, 0, 9, 9), box(0, 0, 9, 9, "b")]}}
    assert ann.agreement_rows(shapes, "a") == [("i.png", 1, {"x": 1, "y": 0})]
    assert ann.agreement_rows(shapes, "b") == [("i.png", 0, {"x": 0, "y": 1})]
    assert ann.agreement_rows(shapes, "missing") == []


def test_shape_at_prefers_smallest():
    big, small = box(0, 0, 100, 100), box(10, 10, 20, 20)
    triangle = poly([[0, 200], [100, 200], [0, 300]])
    shapes = [big, small, triangle]
    assert ann.shape_at(shapes, 15, 15) is small
    assert ann.shape_at(shapes, 50, 50) is big
    assert ann.shape_at(shapes, 20, 220) is triangle
    assert ann.shape_at(shapes, 90, 290) is None


def test_to_rgb_stretches_16_bit():
    pixels = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    result = np.asarray(ann.to_rgb(Image.fromarray(pixels)))
    assert result.shape == (2, 2, 3)
    assert result[0, 0, 0] == 0 and result[1, 1, 0] == 255
    flat = Image.fromarray(np.full((2, 2), 7, dtype=np.uint16))
    assert np.asarray(ann.to_rgb(flat)).max() == 0


def test_drive_query_quoting():
    assert quote("o'brien\\x") == "o\\'brien\\\\x"


def test_client_secret():
    good = '{"installed": {"client_id": "a", "client_secret": "b"}}'
    assert parse_client_secret(good) == good
    assert raises(parse_client_secret, '{"web": {"client_id": "a", "client_secret": "b"}}')
    assert raises(parse_client_secret, "{not json")
    assert raises(parse_client_secret, "/no/such/file.json")


def run_create_project(fail_on=None):
    """Run the wizard's gcloud step for an already-saved project; return its result and commands."""
    commands = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        if fail_on and fail_on in args:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout="me@example.com")

    with mock.patch.object(setup_google, "load_config", return_value={"gcloud_project": "p1"}), \
            mock.patch.object(setup_google.shutil, "which", return_value="gcloud"), \
            mock.patch.object(setup_google.subprocess, "run", side_effect=fake_run):
        return setup_google.create_project(), commands


def test_saved_project_still_turns_on_drive():
    result, commands = run_create_project()
    assert result == ("p1", True)
    assert any("enable" in command and "--project=p1" in command for command in commands)
    assert not any("create" in command for command in commands)


def test_failed_enable_falls_back_to_browser_for_same_project():
    assert run_create_project(fail_on="enable")[0] == ("p1", False)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
