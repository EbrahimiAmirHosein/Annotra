"""Window layout, theme, and dialogs for the annotator."""
import io
import os
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import annotations as ann

APP_NAME = "Annotra"
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "Annotra-single-logo.png")
BRAND = {"bg": "#F8FAFC", "header": "#1B4965", "accent": "#2A9D8F", "text": "#1D3557", "muted": "#64748B"}
PLOT_COLORS = ["#1B4965", "#2A9D8F", "#E76F51", "#264653", "#457B9D", "#8D5A97"]
COMMON_COLOR = "#E9C46A"


def text_on(color):
    """Pick black or white text for legibility on a label's color."""
    red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "black" if 0.299 * red + 0.587 * green + 0.114 * blue > 150 else "white"


class AnnotatorUIMixin:
    def setup_theme(self):
        self.style = ttk.Style(self.root)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        family = "Segoe UI" if os.name == "nt" else "DejaVu Sans"
        self.ui_font = (family, 11)
        self.ui_font_small = (family, 9)
        self.ui_font_bold = (family, 11, "bold")
        self.ui_font_title = (family, 14, "bold")
        configure = self.style.configure
        configure(".", font=self.ui_font, background=BRAND["bg"])
        configure("TFrame", background=BRAND["bg"])
        configure("Header.TFrame", background=BRAND["header"])
        configure("Header.TLabel", background=BRAND["header"], foreground="white", font=self.ui_font_title)
        configure("HeaderSub.TLabel", background=BRAND["header"], foreground="#CAE9FF", font=self.ui_font)
        configure("Sidebar.TFrame", background="#E8EEF4")
        configure("Sidebar.TLabel", background="#E8EEF4", foreground=BRAND["text"], font=self.ui_font_small)
        configure("SidebarTitle.TLabel", background="#E8EEF4", foreground=BRAND["text"], font=self.ui_font_bold)
        configure("Sidebar.TRadiobutton", background="#E8EEF4")
        configure("Status.TLabel", background="#E2E8F0", foreground=BRAND["text"], font=(family, 10))
        configure("Accent.TButton", font=self.ui_font_bold)
        self.root.configure(bg=BRAND["bg"])

    def configure_root_window(self):
        self.root.title(APP_NAME)
        if os.path.exists(ICON_PATH):
            self.icon = ImageTk.PhotoImage(file=ICON_PATH)
            self.root.iconphoto(True, self.icon)  # True applies it to dialogs too
        self.root.minsize(1000, 650)
        self._center_window(self.root, 1400, 900)

    def _center_window(self, window, width, height):
        window.update_idletasks()
        x = max(0, (window.winfo_screenwidth() - width) // 2)
        y = max(0, (window.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def build_app_shell(self):
        shell = ttk.Frame(self.root)
        shell.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(shell, style="Header.TFrame")
        header.pack(fill=tk.X)
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(anchor=tk.W, padx=16, pady=(10, 2))
        meta = ttk.Frame(header, style="Header.TFrame")
        meta.pack(fill=tk.X, padx=16, pady=(0, 10))
        self.header_user_label = ttk.Label(meta, style="HeaderSub.TLabel")
        self.header_user_label.pack(side=tk.LEFT)
        self.header_progress_label = ttk.Label(meta, style="HeaderSub.TLabel")
        self.header_progress_label.pack(side=tk.LEFT, padx=(20, 0))
        self.header_file_label = ttk.Label(meta, style="HeaderSub.TLabel")
        self.header_file_label.pack(side=tk.RIGHT)
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(shell, variable=self.progress_var, maximum=100).pack(fill=tk.X, padx=16, pady=(4, 0))

        status = ttk.Frame(shell)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT, padx=12, pady=6)

        body = ttk.Frame(shell)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        sidebar = ttk.Frame(body, style="Sidebar.TFrame", width=250)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        sidebar.pack_propagate(False)
        inner = ttk.Frame(sidebar, style="Sidebar.TFrame")
        inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        self._build_sidebar(inner)

        self.content_frame = ttk.Frame(body)
        self.content_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(
            self.content_frame, bg="#F1F5F9", highlightthickness=1,
            highlightbackground="#94A3B8", cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.loading_frame = ttk.Frame(self.content_frame)
        self.loading_label = ttk.Label(self.loading_frame, text="Working…")
        self.loading_label.pack(padx=16, pady=12)

    def _build_sidebar(self, parent):
        ttk.Label(parent, text="Tool", style="SidebarTitle.TLabel").pack(anchor=tk.W, pady=(4, 2))
        for text, value in (("Box (B)", "box"), ("Polygon (G)", "polygon"), ("Remove (X)", "remove")):
            ttk.Radiobutton(
                parent, text=text, variable=self.tool, value=value,
                style="Sidebar.TRadiobutton", command=self.cancel_draft,
            ).pack(anchor=tk.W)
        ttk.Label(parent, text="Labels", style="SidebarTitle.TLabel").pack(anchor=tk.W, pady=(12, 2))
        self.chip_frame = ttk.Frame(parent, style="Sidebar.TFrame")
        self.chip_frame.pack(fill=tk.X)
        self.shape_count_label = ttk.Label(parent, text="Shapes: 0", style="Sidebar.TLabel")
        self.shape_count_label.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(parent, text="Actions", style="SidebarTitle.TLabel").pack(anchor=tk.W, pady=(12, 2))
        for text, command, style in (
            ("Next (N)", lambda: self.go(1), "Accent.TButton"),
            ("Previous (P)", lambda: self.go(-1), "TButton"),
            ("Upload images", self.upload_images, "TButton"),
            ("Edit labels", self.edit_labels, "TButton"),
            ("Compare annotators", self.show_agreement, "TButton"),
        ):
            ttk.Button(parent, text=text, command=command, style=style).pack(fill=tk.X, pady=3)
        help_text = (
            "Polygon: click points, then click the first point, double-click, or press Enter.\n"
            "Esc cancels · Backspace undoes a point · Ctrl+Z undoes a shape.\n"
            "Scroll to zoom · Right-drag to pan."
        )
        ttk.Label(parent, text=help_text, style="Sidebar.TLabel", wraplength=225).pack(anchor=tk.W, pady=(12, 0))

    def build_label_chips(self):
        for widget in self.chip_frame.winfo_children():
            widget.destroy()
        self.chips = {}
        for number, label in enumerate(self.labels, 1):
            text = f"{number}  {label['name']}" if number <= 9 else label["name"]
            chip = tk.Button(
                self.chip_frame, text=text, anchor=tk.W, fg=text_on(label["color"]), bg=label["color"],
                activebackground=label["color"], activeforeground=text_on(label["color"]), relief=tk.FLAT,
                padx=6, pady=3, command=lambda name=label["name"]: self.select_label(name),
            )
            chip.pack(fill=tk.X, pady=2)
            self.chips[label["name"]] = chip
        names = [label["name"] for label in self.labels]
        self.select_label(self.current_label.get() if self.current_label.get() in names else names[0])

    def select_label(self, name):
        self.current_label.set(name)
        for chip_name, chip in self.chips.items():
            chip.config(relief=tk.SUNKEN if chip_name == name else tk.FLAT, bd=3 if chip_name == name else 0)

    def _select_label_number(self, number):
        if number < len(self.labels):
            self.select_label(self.labels[number]["name"])

    def _set_tool(self, tool):
        self.tool.set(tool)
        self.cancel_draft()

    def bind_shortcuts(self):
        bindings = {
            "n": lambda: self.go(1), "p": lambda: self.go(-1),
            "b": lambda: self._set_tool("box"), "g": lambda: self._set_tool("polygon"),
            "x": lambda: self._set_tool("remove"),
        }
        for key, action in bindings.items():
            self.root.bind(f"<{key}>", lambda _event, action=action: action())
            self.root.bind(f"<{key.upper()}>", lambda _event, action=action: action())
        for number in range(1, 10):
            self.root.bind(f"<Key-{number}>", lambda _event, n=number: self._select_label_number(n - 1))
        self.root.bind("<Return>", lambda _event: self.close_polygon())
        self.root.bind("<Escape>", lambda _event: self.cancel_draft())
        self.root.bind("<BackSpace>", lambda _event: self.undo_point())
        self.root.bind("<Control-z>", lambda _event: self.undo_shape())

    def set_status(self, message):
        self.status_var.set(message)

    def show_loading(self, message):
        self.loading_label.config(text=message)
        self.loading_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.root.update_idletasks()

    def hide_loading(self):
        self.loading_frame.place_forget()

    def update_chrome(self):
        total = len(self.images)
        self.progress_var.set(100 * len(self.done & {image["id"] for image in self.images}) / total if total else 0)
        self.header_user_label.config(text=self.annotator)
        self.header_progress_label.config(text=f"Image {self.index + 1} / {total}" if total else "No images")
        self.header_file_label.config(text=self.images[self.index]["path"] if total else "")
        self.shape_count_label.config(text=f"Shapes: {len(self.shapes)}")

    def _dialog(self, title, width, height):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.configure(bg=BRAND["bg"])
        self._center_window(dialog, width, height)
        dialog.wait_visibility()
        dialog.grab_set()
        return dialog

    def ask_project(self, remembered):
        """Return ('open', link), ('create', name), or None if the window was closed."""
        dialog = self._dialog(APP_NAME, 560, 340)
        result = []

        def finish(kind, value):
            if not value.strip():
                messagebox.showwarning("Required", "Please fill in this field first.", parent=dialog)
                return
            result.append((kind, value.strip()))
            dialog.destroy()

        ttk.Label(dialog, text="Open a project", font=self.ui_font_title).pack(pady=(18, 4))
        ttk.Label(dialog, text="Paste a Google Drive folder link. Images inside it will be annotated.").pack()
        link = ttk.Entry(dialog, width=60)
        link.insert(0, remembered)
        link.pack(pady=8)
        ttk.Button(
            dialog, text="Open folder", style="Accent.TButton", command=lambda: finish("open", link.get())
        ).pack()
        ttk.Separator(dialog).pack(fill=tk.X, padx=20, pady=16)
        ttk.Label(dialog, text="Or create a new project folder in your Drive:").pack()
        name = ttk.Entry(dialog, width=60)
        name.pack(pady=8)
        ttk.Button(dialog, text="Create project", command=lambda: finish("create", name.get())).pack()
        link.focus_set()
        dialog.wait_window()
        return result[0] if result else None

    def ask_labels(self, initial=""):
        """Return a validated label list, or None if the window was closed."""
        dialog = self._dialog("Labels", 480, 400)
        ttk.Label(dialog, text="What will you annotate?", font=self.ui_font_title).pack(pady=(16, 4))
        ttk.Label(dialog, text="One label per line. Optionally add a color, like: Car #FF8800").pack()
        box = tk.Text(dialog, width=48, height=11, font=self.ui_font)
        box.insert("1.0", initial)
        box.pack(padx=16, pady=10)
        result = []

        def save():
            try:
                result.append(ann.parse_labels_text(box.get("1.0", tk.END)))
            except ValueError as error:
                messagebox.showwarning("Labels", str(error), parent=dialog)
                return
            dialog.destroy()

        ttk.Button(dialog, text="Save labels", style="Accent.TButton", command=save).pack()
        box.focus_set()
        dialog.wait_window()
        return result[0] if result else None

    def open_agreement_window(self, charts):
        """Show one PNG chart per label, as {label: png bytes}."""
        window = tk.Toplevel(self.root)
        window.title("Annotator agreement")
        window.configure(bg=BRAND["bg"])
        self._center_window(window, 1280, 760)
        top = ttk.Frame(window)
        top.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(top, text="Label:", font=self.ui_font_bold).pack(side=tk.LEFT)
        selector = ttk.Combobox(top, values=list(charts), state="readonly", width=32)
        selector.pack(side=tk.LEFT, padx=8)
        selector.set(next(iter(charts)))
        picture = ttk.Label(window)
        picture.pack(padx=12, pady=4)

        def show(*_):
            image = Image.open(io.BytesIO(charts[selector.get()]))
            image.thumbnail((1240, 660), Image.LANCZOS)
            picture.photo = ImageTk.PhotoImage(image)
            picture.config(image=picture.photo)

        selector.bind("<<ComboboxSelected>>", show)
        show()
