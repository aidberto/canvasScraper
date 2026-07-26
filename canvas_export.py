#!/usr/bin/env python3
"""Export one Canvas course's modules + assignments to a local folder tree.

Usage: CANVAS_BASE_URL=https://school.instructure.com CANVAS_API_TOKEN=xxx \
       python canvas_export.py <course_id>
"""
import io
import os
import re
import sys
import time

import requests
from markitdown import MarkItDown, StreamInfo

DELAY = 0.4  # seconds between requests
_md = MarkItDown()


def load_env(path=".env"):
    # ponytail: 6-line .env parser; swap for python-dotenv if quoting/multiline ever matters
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def slugify(text, maxlen=80):
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:maxlen] or "untitled"


def html_to_md(html):
    if not html:
        return ""
    try:
        result = _md.convert_stream(
            io.BytesIO(html.encode("utf-8")),
            stream_info=StreamInfo(extension=".html", mimetype="text/html"),
        )
        return result.text_content
    except Exception as e:
        print(f"  WARNING: html->md conversion failed: {e}")
        return html  # raw html beats nothing


class Canvas:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _request(self, url, params=None, stream=False):
        backoff = 1
        while True:
            time.sleep(DELAY)
            r = self.s.get(url, params=params, stream=stream, timeout=60)
            if r.status_code == 403 and "Rate Limit" in r.text:
                print(f"  rate limited, backing off {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            remaining = r.headers.get("X-Rate-Limit-Remaining")
            if remaining and float(remaining) < 50:
                time.sleep(1)  # ponytail: crude brake, enough for one course
            return r

    def get(self, path, params=None):
        url = path if path.startswith("http") else f"{self.base}/api/v1{path}"
        r = self._request(url, params)
        r.raise_for_status()
        return r.json()

    def get_paginated(self, path, params=None):
        params = dict(params or {}, per_page=100)
        url = f"{self.base}/api/v1{path}"
        results = []
        while url:
            r = self._request(url, params)
            r.raise_for_status()
            results.extend(r.json())
            url = r.links.get("next", {}).get("url")
            params = None  # next url already carries query params
        return results

    def download(self, url, dest):
        r = self._request(url, stream=True)
        r.raise_for_status()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def stub(title, item_type, url, note=""):
    lines = [f"# {title}", "", f"- **Type:** {item_type}"]
    if url:
        lines.append(f"- **Canvas URL:** {url}")
    if note:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


class Exporter:
    def __init__(self, canvas, course_id):
        self.canvas = canvas
        self.course_id = course_id
        self.folder_paths = None  # folder_id -> relative path under /files/
        self.downloaded_files = {}  # file_id -> local relative path
        self.assignment_files = {}  # assignment_id -> path under /assignments/

    def run(self):
        try:
            course = self.canvas.get(f"/courses/{self.course_id}")
        except requests.HTTPError as e:
            code = e.response.status_code
            if code in (401, 403):
                die("token invalid or has no access to this course")
            if code == 404:
                die(f"course {self.course_id} not found")
            raise
        self.root = slugify(course["name"])
        print(f"Course: {course['name']} -> ./{self.root}/")

        self.export_assignments()
        self.export_modules()
        print("Done.")

    # ---- files ----

    def _load_folders(self):
        if self.folder_paths is not None:
            return
        self.folder_paths = {}
        try:
            folders = self.canvas.get_paginated(f"/courses/{self.course_id}/folders")
            for f in folders:
                # full_name is like "course files/Week 1/Slides"
                parts = f["full_name"].split("/")[1:]  # drop "course files" root
                self.folder_paths[f["id"]] = "/".join(parts)
        except requests.HTTPError as e:
            print(f"  WARNING: could not list course folders: {e}")

    def download_file(self, file_id):
        """Download a Canvas file into /files/, mirroring its folder. Returns relative path or None."""
        if file_id in self.downloaded_files:
            return self.downloaded_files[file_id]
        self._load_folders()
        try:
            info = self.canvas.get(f"/courses/{self.course_id}/files/{file_id}")
        except requests.HTTPError as e:
            print(f"  WARNING: file {file_id} inaccessible: {e}")
            return None
        subdir = self.folder_paths.get(info.get("folder_id"), "")
        rel = os.path.join("files", subdir, info["display_name"]) if subdir else os.path.join("files", info["display_name"])
        dest = os.path.join(self.root, rel)
        if not os.path.exists(dest):
            print(f"    downloading {rel}")
            try:
                self.canvas.download(info["url"], dest)
            except Exception as e:
                print(f"  WARNING: download failed for {info['display_name']}: {e}")
                return None
        self.downloaded_files[file_id] = rel
        return rel

    def download_embedded_files(self, html):
        """Grab files linked inside HTML bodies (e.g. /courses/X/files/123)."""
        for file_id in set(re.findall(r"/files/(\d+)", html or "")):
            self.download_file(int(file_id))

    # ---- assignments ----

    def export_assignments(self):
        print("Assignments:")
        assignments = self.canvas.get_paginated(f"/courses/{self.course_id}/assignments")
        for a in assignments:
            try:
                name = slugify(a["name"])
                rel = os.path.join("assignments", f"{name}.md")
                print(f"  assignments -> {a['name']}")
                body = html_to_md(a.get("description"))
                text = f"# {a['name']}\n\n"
                if a.get("due_at"):
                    text += f"- **Due:** {a['due_at']}\n"
                if a.get("points_possible") is not None:
                    text += f"- **Points:** {a['points_possible']}\n"
                text += f"- **Canvas URL:** {a.get('html_url', '')}\n\n{body}\n"
                write_text(os.path.join(self.root, rel), text)
                self.assignment_files[a["id"]] = rel
                self.download_embedded_files(a.get("description"))
            except Exception as e:
                print(f"  WARNING: assignment '{a.get('name')}' failed: {e}")

    # ---- modules ----

    def export_modules(self):
        print("Modules:")
        modules = self.canvas.get_paginated(f"/courses/{self.course_id}/modules")
        for m_idx, module in enumerate(modules, 1):
            mod_dir = os.path.join(self.root, "modules", f"{m_idx:02d}-{slugify(module['name'])}")
            print(f"  modules -> {m_idx:02d}-{module['name']}")
            items = self.canvas.get_paginated(
                f"/courses/{self.course_id}/modules/{module['id']}/items"
            )
            for i_idx, item in enumerate(items, 1):
                try:
                    self.export_item(item, mod_dir, i_idx)
                except Exception as e:
                    print(f"  WARNING: item '{item.get('title')}' failed: {e}")

    def export_item(self, item, mod_dir, i_idx):
        title = item.get("title") or "untitled"
        itype = item["type"]
        fname = f"{i_idx:02d}-{slugify(title)}.md"
        path = os.path.join(mod_dir, fname)
        print(f"    {fname} ({itype})")

        if itype == "Page":
            page = self.canvas.get(f"/courses/{self.course_id}/pages/{item['page_url']}")
            body = html_to_md(page.get("body"))
            write_text(path, f"# {title}\n\n{body}\n")
            self.download_embedded_files(page.get("body"))
        elif itype == "Assignment":
            rel = self.assignment_files.get(item.get("content_id"))
            note = f"Full content: [../../{rel}](../../{rel})" if rel else "Assignment not found in course assignment list."
            write_text(path, stub(title, "Assignment", item.get("html_url"), note))
        elif itype == "File":
            rel = self.download_file(item["content_id"])
            note = f"Downloaded to: `{rel}`" if rel else "Download failed."
            write_text(path, stub(title, "File", item.get("html_url"), note))
        elif itype == "SubHeader":
            write_text(path, f"# {title}\n\n*(module section header)*\n")
        else:
            # Discussion, Quiz, ExternalUrl, ExternalTool, ... -> stub only, no content fetch
            url = item.get("html_url") or item.get("external_url")
            write_text(path, stub(title, itype, url, "Content not exported (out of scope)."))


def self_test():
    assert slugify("Week 1: Intro & Setup!") == "week-1-intro-setup"
    assert slugify("   ") == "untitled"
    assert slugify("a" * 200) == "a" * 80
    assert set(re.findall(r"/files/(\d+)", '<a href="/courses/1/files/42/download">x</a>')) == {"42"}
    print("self-test OK")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        return self_test()
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        die("usage: python canvas_export.py <course_id>")
    load_env()
    base_url = os.environ.get("CANVAS_BASE_URL")
    token = os.environ.get("CANVAS_API_TOKEN")
    if not base_url or not token:
        die("set CANVAS_BASE_URL and CANVAS_API_TOKEN in .env (see .env.example) or the environment")
    Exporter(Canvas(base_url, token), sys.argv[1]).run()


if __name__ == "__main__":
    main()
