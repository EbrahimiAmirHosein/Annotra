"""Google sign-in and the Drive operations the annotator needs."""
import io
import json
import mimetypes
import os
import threading

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaInMemoryUpload, MediaIoBaseDownload

from annotations import IMAGE_EXTS

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".annotra")
CLIENT_FILE = os.path.join(CONFIG_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# The full scope is required to read existing folders; drive.file only sees files this app created.
SCOPES = ["https://www.googleapis.com/auth/drive"]
HEADLESS = os.environ.get("ANNOTATOR_DOCKER") == "1"
DOCKER_AUTH_PORT = 8765
ANNOTATIONS_DIR = "_annotations"
FOLDER_MIME = "application/vnd.google-apps.folder"
RETRIES = 3


def write_private(path, text):
    """Write a credential file only the current user can read."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(**values):
    write_private(CONFIG_FILE, json.dumps({**load_config(), **values}))


def sign_in():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) if os.path.exists(TOKEN_FILE) else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
        # In Docker the published port is limited to the host's loopback by docker-compose.yml.
        creds = flow.run_local_server(
            bind_addr="0.0.0.0" if HEADLESS else None,
            port=DOCKER_AUTH_PORT if HEADLESS else 0,
            open_browser=not HEADLESS,
        )
    write_private(TOKEN_FILE, creds.to_json())
    return creds


def quote(value):
    """Escape a value for a Drive search query; user names and files may contain quotes."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, creds):
        self.creds = creds
        self._local = threading.local()

    @property
    def service(self):
        # The HTTP client underneath is not thread-safe, so each thread gets its own.
        if not hasattr(self._local, "service"):
            self._local.service = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        return self._local.service

    def account_email(self):
        return self.service.about().get(fields="user(emailAddress)").execute(num_retries=RETRIES)["user"]["emailAddress"]

    def _list(self, query):
        files, token = [], None
        while True:
            response = self.service.files().list(
                q=f"{query} and trashed=false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=RETRIES)
            files.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return files

    def children(self, folder_id):
        return self._list(f"'{quote(folder_id)}' in parents")

    def find(self, parent_id, name):
        matches = self._list(f"'{quote(parent_id)}' in parents and name='{quote(name)}'")
        return matches[0] if matches else None

    def is_folder(self, file_id):
        info = self.service.files().get(
            fileId=file_id, fields="mimeType", supportsAllDrives=True
        ).execute(num_retries=RETRIES)
        return info["mimeType"] == FOLDER_MIME

    def create_folder(self, name, parent_id=None):
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        return self.service.files().create(
            body=body, fields="id", supportsAllDrives=True
        ).execute(num_retries=RETRIES)["id"]

    def get_or_create_folder(self, name, parent_id):
        existing = self.find(parent_id, name)
        return existing["id"] if existing else self.create_folder(name, parent_id)

    def list_images(self, folder_id):
        """Find images at any depth, skipping the folder that holds annotations."""
        images, pending = [], [(folder_id, "")]
        while pending:
            current, prefix = pending.pop()
            for item in self.children(current):
                path = f"{prefix}{item['name']}"
                if item["mimeType"] == FOLDER_MIME:
                    if item["name"] != ANNOTATIONS_DIR or prefix:
                        pending.append((item["id"], f"{path}/"))
                elif item["name"].lower().endswith(IMAGE_EXTS):
                    images.append({"id": item["id"], "name": item["name"], "path": path})
        return sorted(images, key=lambda image: image["path"])

    def read_bytes(self, file_id):
        buffer = io.BytesIO()
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=RETRIES)
        return buffer.getvalue()

    def read_json(self, file_id):
        return json.loads(self.read_bytes(file_id))

    def write_json(self, parent_id, name, data):
        media = MediaInMemoryUpload(json.dumps(data, indent=1).encode(), mimetype="application/json")
        existing = self.find(parent_id, name)
        files = self.service.files()
        if existing:
            files.update(fileId=existing["id"], media_body=media, supportsAllDrives=True).execute(num_retries=RETRIES)
        else:
            body = {"name": name, "parents": [parent_id]}
            files.create(body=body, media_body=media, fields="id", supportsAllDrives=True).execute(num_retries=RETRIES)

    def upload_file(self, path, parent_id):
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        media = MediaFileUpload(path, mimetype=mime, resumable=True)
        body = {"name": os.path.basename(path), "parents": [parent_id]}
        self.service.files().create(
            body=body, media_body=media, fields="id", supportsAllDrives=True
        ).execute(num_retries=RETRIES)
