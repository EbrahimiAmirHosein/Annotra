"""First-run wizard that connects one person's Google account to the annotator."""
import json
import os
import shutil
import subprocess
import uuid
import webbrowser

from drive import CLIENT_FILE, HEADLESS, load_config, save_config, write_private

CONSOLE = "https://console.cloud.google.com"


def parse_client_secret(text):
    """Accept the downloaded client JSON, either its contents or a path to it."""
    text = text.strip()
    if not text.startswith("{"):
        path = os.path.expanduser(text.strip("'\""))
        if not os.path.isfile(path):
            raise ValueError(
                "Docker can't read files on your computer. In another terminal run\n"
                "  cat ~/Downloads/client_secret_*.json\nand paste its output here."
                if HEADLESS else "That is not a file path or JSON."
            )
        with open(path, encoding="utf-8") as file:
            text = file.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("That is not valid JSON. Paste the whole file.") from None
    client = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("This is not a Desktop app client. Create the client with type 'Desktop app'.")
    return text


def create_project():
    """Return (project ID or None, whether Drive is on); anything gcloud can't do falls to the browser."""
    project = load_config().get("gcloud_project")
    if not shutil.which("gcloud"):
        print("gcloud is not installed, so you'll finish this in your browser.")
        return project, False
    try:
        active = subprocess.run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not active:
            subprocess.run(["gcloud", "auth", "login"] + (["--no-launch-browser"] if HEADLESS else []), check=True)
        if not project:
            new_project = f"annotra-{uuid.uuid4().hex[:8]}"
            subprocess.run(["gcloud", "projects", "create", new_project, "--name=Annotra"], check=True)
            # Saved before enabling the API so a retry reuses it; Google limits projects per account.
            save_config(gcloud_project=new_project)
            project = new_project
        subprocess.run(["gcloud", "services", "enable", "drive.googleapis.com", f"--project={project}"], check=True)
        return project, True
    except subprocess.CalledProcessError:
        print("\ngcloud could not finish, so you'll finish this in your browser.")
        return project, False


def step(title, url, instructions):
    print(f"\n--- {title} ---\nOpen: {url}")
    if not HEADLESS:
        webbrowser.open(url)
    for line in instructions:
        print(f"  - {line}")
    input("Press Enter when done... ")


def guide_console(project, drive_on):
    suffix = f"?project={project}" if project else ""
    if not project:
        step("Create a project", f"{CONSOLE}/projectcreate", ["Name it anything and click Create.", "Select the new project."])
    if not drive_on:
        step("Turn on Google Drive", f"{CONSOLE}/apis/library/drive.googleapis.com{suffix}", ["Click Enable."])
    step("Set up the sign-in screen", f"{CONSOLE}/auth/overview{suffix}", [
        "Click Get started.",
        "Enter any app name and your email.",
        "Choose Audience: External, enter your email again, agree, and click Create.",
    ])
    # Publishing needs a public home page and privacy policy, so stay in Testing by default.
    step("Allow your account", f"{CONSOLE}/auth/audience{suffix}", [
        "Leave the app in Testing. You do not need Publish app.",
        "Under Test users, click Add users, enter your Google email and any teammates' emails, and save.",
        "While in Testing, Google asks you to sign in again about every 7 days.",
    ])
    step("Create your client", f"{CONSOLE}/auth/clients/create{suffix}", [
        "Choose Application type: Desktop app and click Create.",
        "Click Download JSON and keep the file private.",
    ])


def run():
    print("\nGoogle setup (only needed once).")
    print("Your files stay in your own Google Drive and your key stays on this computer.")
    text = input("\nIf a teammate gave you a client JSON, paste it or its path. Otherwise press Enter: ").strip()
    if not text:
        guide_console(*create_project())
        text = input(f"\nPaste the downloaded JSON{'' if HEADLESS else ' or its path'}: ")
    while True:
        try:
            write_private(CLIENT_FILE, parse_client_secret(text))
            break
        except ValueError as error:
            text = input(f"{error}\nTry again: ")
    print("\nSaved. On the next screen Google may say the app isn't verified.")
    print("It is your own app, so choose Advanced, then continue to it.\n")


if __name__ == "__main__":
    run()
