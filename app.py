"""Annotra: label images in a Google Drive folder with boxes and polygons."""
import io
import math
import os
import sys
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox

import plotly.graph_objects as go
from googleapiclient.errors import HttpError
from PIL import Image, ImageOps, ImageTk

import annotations as ann
import setup_google
from drive import ANNOTATIONS_DIR, CLIENT_FILE, FOLDER_MIME, Drive, load_config, save_config, sign_in
from ui_shell import BRAND, COMMON_COLOR, PLOT_COLORS, AnnotatorUIMixin

PROJECT_FILE = "annotator_project.json"
UPLOAD_START_DIR = "/data"  # where docker-compose.yml mounts the user's images
CLOSE_POLYGON_PIXELS = 10
DOWNLOAD_WORKERS = 8


def describe(error):
    return error.reason if isinstance(error, HttpError) else str(error)


class App(AnnotatorUIMixin):
    def __init__(self, root, drive):
        self.root, self.drive = root, drive
        self.root.report_callback_exception = self._on_error
        self.configure_root_window()
        self.setup_theme()
        self.annotator = drive.account_email()
        self.tool = tk.StringVar(value="box")
        self.current_label = tk.StringVar()
        self.images, self.done, self.shapes, self.draft = [], set(), [], []
        self.index = 0
        self.dirty = False
        self.image = None
        self.scale, self.fit_scale, self.pan_x, self.pan_y = 1.0, 1.0, 0.0, 0.0
        self.drag_start = self.pan_start = self.rubber_band = None
        self.project_id = self.choose_project()
        self.labels = self.project_id and self.load_labels()
        if not self.labels:
            self.root.destroy()
            return
        annotations_id = self.drive.get_or_create_folder(ANNOTATIONS_DIR, self.project_id)
        self.annotations_id = annotations_id
        self.my_folder_id = self.drive.get_or_create_folder(self.annotator, annotations_id)
        self.build_app_shell()
        self.build_label_chips()
        self.bind_shortcuts()
        self._bind_canvas()
        self.show_loading("Loading image list from Google Drive…")
        self.refresh_images()
        self.index = self.first_undone() or 0
        self.hide_loading()
        self.update_chrome()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.start)

    def start(self):
        if self.images:
            self.load_current()
        else:
            self.set_status("This folder has no images yet. Click Upload images to add some.")

    def _on_error(self, _type, error, trace):
        traceback.print_exception(_type, error, trace)
        self.hide_loading()
        messagebox.showerror("Error", describe(error))

    # Project setup

    def choose_project(self):
        while True:
            choice = self.ask_project(load_config().get("last_folder", ""))
            if not choice:
                return None
            kind, value = choice
            try:
                if kind == "create":
                    folder_id = self.drive.create_folder(value)
                else:
                    folder_id = ann.parse_folder_id(value)
                    if not self.drive.is_folder(folder_id):
                        raise ValueError("That link is a file, not a folder.")
            except ValueError as error:
                messagebox.showerror("Project", str(error))
                continue
            except HttpError as error:
                messagebox.showerror(
                    "Project",
                    f"Could not open that folder ({describe(error)}).\n"
                    f"Ask its owner to share it with {self.annotator} as Editor.",
                )
                continue
            save_config(last_folder=folder_id)
            return folder_id

    def load_labels(self):
        project_file = self.drive.find(self.project_id, PROJECT_FILE)
        if project_file:
            return ann.validate_labels(self.drive.read_json(project_file["id"]))
        labels = self.ask_labels()
        if labels:
            self.drive.write_json(self.project_id, PROJECT_FILE, {"labels": labels})
        return labels

    def edit_labels(self):
        labels = self.ask_labels(ann.format_labels_text(self.labels))
        if labels:
            self.drive.write_json(self.project_id, PROJECT_FILE, {"labels": labels})
            self.labels = labels
            self.build_label_chips()
            self.render()

    def refresh_images(self):
        self.images = self.drive.list_images(self.project_id)
        self.done = {
            item["name"][: -len(".json")]
            for item in self.drive.children(self.my_folder_id)
            if item["name"].endswith(".json")
        }

    def first_undone(self):
        return next((i for i, image in enumerate(self.images) if image["id"] not in self.done), None)

    def upload_images(self):
        patterns = " ".join(f"*{ext} *{ext.upper()}" for ext in ann.IMAGE_EXTS)
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Choose images to upload",
            initialdir=UPLOAD_START_DIR if os.path.isdir(UPLOAD_START_DIR) else os.path.expanduser("~"),
            filetypes=[("Images", patterns)],
        )
        if not paths:
            return
        self.save_current(mark_done=False)
        existing = {item["name"] for item in self.drive.children(self.project_id)}
        new = [path for path in paths if os.path.basename(path) not in existing and path.lower().endswith(ann.IMAGE_EXTS)]
        current_id = self.images[self.index]["id"] if self.images else None
        try:
            for number, path in enumerate(new, 1):
                self.show_loading(f"Uploading {number} of {len(new)}: {os.path.basename(path)}")
                self.drive.upload_file(path, self.project_id)
            self.show_loading("Refreshing image list…")
            self.refresh_images()
        finally:
            self.hide_loading()
        ids = [image["id"] for image in self.images]
        self.index = ids.index(current_id) if current_id in ids else 0
        self.update_chrome()
        if current_id not in ids and self.images:
            self.load_current()
        self.set_status(f"Uploaded {len(new)}, skipped {len(paths) - len(new)} (same name or not an image)")

    # Moving between images

    def load_current(self):
        info = self.images[self.index]
        # Cleared first so a failed load can never save old shapes onto this image.
        self.image, self.shapes, self.draft, self.dirty = None, [], [], False
        self.canvas.delete("all")
        self.update_chrome()
        self.show_loading("Downloading image…")
        try:
            data = self.drive.read_bytes(info["id"])
            saved = self.drive.find(self.my_folder_id, f"{info['id']}.json") if info["id"] in self.done else None
            record = self.drive.read_json(saved["id"]) if saved else {}
        finally:
            self.hide_loading()
        try:
            with Image.open(io.BytesIO(data)) as opened:
                self.image = ann.to_rgb(ImageOps.exif_transpose(opened))
        except OSError as error:
            self.set_status(f"Could not open {info['path']}: {error}. Press N to skip it.")
            return
        raw = record.get("shapes", [])
        self.shapes = [shape for shape in raw if ann.is_valid_shape(shape)]
        skipped = len(raw) - len(self.shapes)
        self.fit()
        self.render()
        self.update_chrome()
        self.set_status(f"Ignored {skipped} damaged saved shapes" if skipped else f"Loaded {info['path']}")

    def save_current(self, mark_done):
        if self.image is None:
            return
        info = self.images[self.index]
        if not self.dirty and not (mark_done and info["id"] not in self.done):
            return
        self.drive.write_json(self.my_folder_id, f"{info['id']}.json", {
            "image_id": info["id"],
            "image_path": info["path"],
            "width": self.image.width,
            "height": self.image.height,
            "annotator": self.annotator,
            "shapes": self.shapes,
        })
        self.done.add(info["id"])
        self.dirty = False

    def go(self, delta):
        if not self.images:
            return
        self.save_current(mark_done=delta > 0)  # if this raises, we stay put and the work is kept
        target = self.index + delta
        if target >= len(self.images):
            target = self.first_undone()
            if target is None:
                self.update_chrome()
                self.set_status(f"All {len(self.images)} images are annotated")
                return
        if target >= 0:
            self.index = target
            self.load_current()

    def on_close(self):
        try:
            self.save_current(mark_done=False)
        except Exception as error:
            if not messagebox.askyesno("Not saved", f"{describe(error)}\n\nClose and lose this image's changes?"):
                return
        self.root.destroy()

    # Canvas view

    def _bind_canvas(self):
        bind = self.canvas.bind
        bind("<ButtonPress-1>", self.on_press)
        bind("<B1-Motion>", self.on_drag)
        bind("<ButtonRelease-1>", self.on_release)
        bind("<Double-Button-1>", self.on_double_click)
        for button in (2, 3):  # right-click is button 2 on macOS and 3 elsewhere
            bind(f"<ButtonPress-{button}>", self.on_pan_start)
            bind(f"<B{button}-Motion>", self.on_pan_move)
        bind("<MouseWheel>", lambda event: self.zoom(1.1 if event.delta > 0 else 1 / 1.1, event.x, event.y))
        bind("<Button-4>", lambda event: self.zoom(1.1, event.x, event.y))
        bind("<Button-5>", lambda event: self.zoom(1 / 1.1, event.x, event.y))
        bind("<Configure>", lambda _event: self.on_resize())

    def on_resize(self):
        if self.image:
            self.fit()
            self.render()

    def fit(self):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.fit_scale = self.scale = min(width / self.image.width, height / self.image.height)
        self.pan_x = (width - self.image.width * self.scale) / 2
        self.pan_y = (height - self.image.height * self.scale) / 2

    def zoom(self, factor, x, y):
        if not self.image:
            return
        new_scale = min(max(self.scale * factor, self.fit_scale * 0.5), max(self.fit_scale * 40, 20))
        self.pan_x = x - (x - self.pan_x) * new_scale / self.scale
        self.pan_y = y - (y - self.pan_y) * new_scale / self.scale
        self.scale = new_scale
        self.render()

    def on_pan_start(self, event):
        self.pan_start = (event.x, event.y, self.pan_x, self.pan_y)

    def on_pan_move(self, event):
        if self.pan_start and self.image:
            start_x, start_y, pan_x, pan_y = self.pan_start
            self.pan_x, self.pan_y = pan_x + event.x - start_x, pan_y + event.y - start_y
            self.render()

    def to_image(self, x, y):
        return [
            min(max(round((x - self.pan_x) / self.scale), 0), self.image.width - 1),
            min(max(round((y - self.pan_y) / self.scale), 0), self.image.height - 1),
        ]

    def to_canvas(self, point):
        return point[0] * self.scale + self.pan_x, point[1] * self.scale + self.pan_y

    def color_of(self, name):
        return next((label["color"] for label in self.labels if label["name"] == name), ann.UNKNOWN_COLOR)

    def render(self):
        canvas = self.canvas
        canvas.delete("all")
        if not self.image:
            return
        # Only the visible region is resampled, which keeps zooming on large images fast.
        left = max(math.floor(-self.pan_x / self.scale), 0)
        top = max(math.floor(-self.pan_y / self.scale), 0)
        right = min(math.ceil((canvas.winfo_width() - self.pan_x) / self.scale), self.image.width)
        bottom = min(math.ceil((canvas.winfo_height() - self.pan_y) / self.scale), self.image.height)
        if right > left and bottom > top:
            size = (max(round((right - left) * self.scale), 1), max(round((bottom - top) * self.scale), 1))
            method = Image.NEAREST if self.scale >= 2 else Image.BILINEAR
            self.photo = ImageTk.PhotoImage(self.image.crop((left, top, right, bottom)).resize(size, method))
            canvas.create_image(*self.to_canvas((left, top)), anchor=tk.NW, image=self.photo)
        for shape in self.shapes:
            color = self.color_of(shape["label"])
            points = [self.to_canvas(point) for point in shape["points"]]
            if shape["type"] == "box":
                canvas.create_rectangle(*points[0], *points[1], outline=color, width=2)
                anchor = points[0]
            else:
                canvas.create_polygon(*[value for point in points for value in point], outline=color, fill="", width=2)
                anchor = min(points, key=lambda point: point[1])
            canvas.create_text(anchor[0], anchor[1] - 3, text=shape["label"], anchor=tk.SW, fill=color, font=self.ui_font_small)
        self._render_draft()

    def _render_draft(self):
        color = self.color_of(self.current_label.get())
        points = [self.to_canvas(point) for point in self.draft]
        if len(points) > 1:
            self.canvas.create_line(*[value for point in points for value in point], fill=color, width=2)
        for x, y in points:
            self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline="white")

    # Drawing

    def on_press(self, event):
        if not self.image:
            return
        tool = self.tool.get()
        if tool == "box":
            self.drag_start = (event.x, event.y)
        elif tool == "remove":
            shape = ann.shape_at(self.shapes, *self.to_image(event.x, event.y))
            if shape:
                self.shapes.remove(shape)
                self._changed()
        else:
            self._add_polygon_point(event)

    def on_double_click(self, event):
        # Tk sends a fast second click only here, so other tools must still treat it as a press.
        if self.tool.get() == "polygon":
            self.close_polygon()
        else:
            self.on_press(event)

    def _add_polygon_point(self, event):
        if len(self.draft) >= 3:
            first_x, first_y = self.to_canvas(self.draft[0])
            if math.hypot(event.x - first_x, event.y - first_y) <= CLOSE_POLYGON_PIXELS:
                self.close_polygon()
                return
        point = self.to_image(event.x, event.y)
        if not self.draft or point != self.draft[-1]:
            self.draft.append(point)
            self.render()

    def on_drag(self, event):
        if self.drag_start:
            self.canvas.delete(self.rubber_band)
            self.rubber_band = self.canvas.create_rectangle(
                *self.drag_start, event.x, event.y, outline=self.color_of(self.current_label.get()), width=2
            )

    def on_release(self, event):
        if not self.drag_start or not self.image:
            return
        points = ann.box_points(self.to_image(*self.drag_start), self.to_image(event.x, event.y))
        self.drag_start = None
        self.canvas.delete(self.rubber_band)
        if points:
            self.shapes.append({"type": "box", "label": self.current_label.get(), "points": points})
            self._changed()

    def close_polygon(self):
        if self.tool.get() == "polygon" and len(self.draft) >= 3:
            self.shapes.append({"type": "polygon", "label": self.current_label.get(), "points": self.draft})
            self.draft = []
            self._changed()

    def cancel_draft(self):
        self.draft = []
        self.render()

    def undo_point(self):
        if self.draft:
            self.draft.pop()
            self.render()

    def undo_shape(self):
        if self.shapes:
            self.shapes.pop()
            self._changed()

    def _changed(self):
        self.dirty = True
        self.render()
        self.update_chrome()

    # Comparing annotators

    def show_agreement(self):
        self.save_current(mark_done=False)
        self.show_loading("Reading everyone's annotations…")
        try:
            charts = self._build_charts()
        finally:
            self.hide_loading()
        if charts:
            self.open_agreement_window(charts)

    def _build_charts(self):
        folders = [item for item in self.drive.children(self.annotations_id) if item["mimeType"] == FOLDER_MIME]
        if len(folders) < 2:
            messagebox.showinfo("Compare annotators", f"Need at least 2 annotators. Found {len(folders)}.")
            return {}
        with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
            listings = list(pool.map(lambda folder: self.drive.children(folder["id"]), folders))
        files = {
            folder["name"]: {item["name"]: item["id"] for item in listing if item["name"].endswith(".json")}
            for folder, listing in zip(folders, listings)
        }
        common = set.intersection(*(set(names) for names in files.values()))
        if not common:
            messagebox.showinfo("Compare annotators", "No image has been annotated by every annotator yet.")
            return {}
        jobs = [(name, annotator, ids[name]) for annotator, ids in files.items() for name in common]
        with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
            records = list(pool.map(lambda job: self.drive.read_json(job[2]), jobs))
        paths = {f"{image['id']}.json": image["path"] for image in self.images}
        shapes_per_image = {}
        for (name, annotator, _), record in zip(jobs, records):
            valid = [shape for shape in record.get("shapes", []) if ann.is_valid_shape(shape)]
            shapes_per_image.setdefault(paths.get(name, name), {})[annotator] = valid
        names = [label["name"] for label in self.labels]
        names += sorted({s["label"] for per in shapes_per_image.values() for shapes in per.values() for s in shapes} - set(names))
        charts = {}
        for label in names:
            rows = ann.agreement_rows(shapes_per_image, label)
            if rows:
                charts[label] = self._chart_png(label, rows, list(files))
        if not charts:
            messagebox.showinfo("Compare annotators", "The shared images have no annotations to compare.")
        return charts

    def _chart_png(self, label, rows, annotators):
        indexes = list(range(len(rows)))
        totals = [common + sum(unique.values()) for _, common, unique in rows]
        series = [("Common", COMMON_COLOR, [common for _, common, _ in rows])]
        for number, annotator in enumerate(annotators):
            color = PLOT_COLORS[number % len(PLOT_COLORS)]
            series.append((f"Only {annotator}", color, [unique[annotator] for _, _, unique in rows]))
        figure = go.Figure()
        for name, color, counts in series:
            figure.add_trace(go.Bar(
                x=indexes, y=[100 * count / total for count, total in zip(counts, totals)],
                name=name, marker_color=color,
            ))
        figure.update_layout(
            template="plotly_white", barmode="stack", width=1200, height=600,
            title=f"Annotator agreement: {label}", xaxis_title="Image", yaxis_title="Share of shapes (%)",
            xaxis={"type": "category"}, yaxis={"range": [0, 100]}, paper_bgcolor="#FFFFFF", plot_bgcolor=BRAND["bg"],
        )
        return figure.to_image(format="png", scale=2)


def main():
    if not os.path.exists(CLIENT_FILE):
        setup_google.run()
    drive = Drive(sign_in())
    root = tk.Tk(className="Annotra")  # the name docks and taskbars show instead of "Tk"
    try:
        App(root, drive)
    except Exception as error:
        traceback.print_exc()
        messagebox.showerror("Could not start", describe(error))
        sys.exit(1)
    root.mainloop()


if __name__ == "__main__":
    main()
