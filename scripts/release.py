"""Build a verified package and publish it through the GitHub REST API."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "release/config.json").read_text(encoding="utf-8"))
GUI = "来酷pro14控制中心.exe"
API_ROOT = "https://api.github.com/repos/" + CONFIG["repository"]


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if redirected and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(new_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


OPENER = urllib.request.build_opener(SafeRedirect())


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def request(method, path, body=None, data=None, content_type="application/json", accept="application/vnd.github+json"):
    url = path if path.startswith("https://") else API_ROOT + path
    if urllib.parse.urlsplit(url).hostname not in {"api.github.com", "uploads.github.com"}:
        raise RuntimeError("Unexpected GitHub API host")
    headers = {"Accept": accept, "User-Agent": "lecoo-pro14-release", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with OPENER.open(req, timeout=180) as response:
        raw = response.read()
    if accept == "application/octet-stream":
        return raw
    return json.loads(raw) if raw else None


def release_for(tag):
    try:
        return request("GET", "/releases/tags/" + urllib.parse.quote(tag, safe=""))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def asset_bytes(asset):
    return request("GET", asset["url"], accept="application/octet-stream")


def download_bootstrap(directory):
    current = release_for(CONFIG["tag"])
    asset = next((a for a in (current or {}).get("assets", []) if a["name"] == CONFIG["archive"]), None)
    if asset:
        path = directory / CONFIG["archive"]
    else:
        previous = release_for(CONFIG["bootstrap"]["tag"])
        if not previous:
            raise RuntimeError("No verified GUI bootstrap release is available")
        asset = next(a for a in previous["assets"] if a["name"] == CONFIG["bootstrap"]["asset"])
        path = directory / CONFIG["bootstrap"]["asset"]
    path.write_bytes(asset_bytes(asset))
    if path.suffix == ".7z" and sha256(path) != CONFIG["bootstrap"]["sha256"]:
        raise RuntimeError("Bootstrap archive checksum mismatch")
    return path


def extract_gui(archive, destination):
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            matches = [name for name in zipped.namelist() if Path(name).name == GUI]
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one GUI executable")
            destination.write_bytes(zipped.read(matches[0]))
    else:
        if sha256(archive) != CONFIG["bootstrap"]["sha256"]:
            raise RuntimeError("Bootstrap archive checksum mismatch")
        with destination.open("wb") as output:
            subprocess.run(["tar", "-xOf", str(archive), GUI], stdout=output, check=True)
    if sha256(destination) != CONFIG["gui_sha256"]:
        raise RuntimeError("GUI executable differs from the verified current GUI")


def package(runtime, output, bootstrap=None):
    runtime, output = Path(runtime), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    build = json.loads((runtime / "build-info.json").read_text(encoding="utf-8-sig"))
    for key in ["core_version", "upstream_commit", "interprocess_version", "rust_version"]:
        if build[key] != CONFIG[key]:
            raise RuntimeError("Runtime build metadata mismatch: " + key)
    if build["regression_tests_passed"] != 2:
        raise RuntimeError("Windows regression checks did not pass")
    if sha256(runtime / "inpoutx64.dll") != CONFIG["driver_sha256"]:
        raise RuntimeError("Driver checksum mismatch")

    with tempfile.TemporaryDirectory(prefix="lecoo-package-") as temporary:
        staging = Path(temporary)
        archive = Path(bootstrap) if bootstrap else download_bootstrap(staging)
        extract_gui(archive, staging / GUI)
        for name in ["lecoo-ec-daemon.exe", "lecoo-ctrl.exe", "inpoutx64.dll"]:
            shutil.copy2(runtime / name, staging / name)
        for name in ["README.md", "NOTICE.md", "LICENSE", "logo.png", "install.bat", "uninstall.bat", "repair-service-recovery.bat"]:
            shutil.copy2(ROOT / name, staging / name)
        manifest = {"release": CONFIG["tag"], "source_commit": os.environ.get("GITHUB_SHA"), "runtime": build,
                    "sha256": {p.name: sha256(p) for p in staging.iterdir() if p.is_file() and p != archive}}
        (staging / "version.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        package_path = output / CONFIG["archive"]
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(staging.iterdir()):
                if path.is_file() and path != archive:
                    zipped.write(path, path.name)
            if zipped.testzip() is not None:
                raise RuntimeError("Package ZIP verification failed")
    (output / "SHA256SUMS.txt").write_text(sha256(package_path) + "  " + package_path.name + "\n", encoding="ascii")
    print(json.dumps({"package": str(package_path), "sha256": sha256(package_path)}))


def verify_published(release):
    assets = {a["name"]: a for a in release["assets"]}
    if not {CONFIG["archive"], "SHA256SUMS.txt"}.issubset(assets):
        raise RuntimeError("Published release is missing required assets")
    checksum = asset_bytes(assets["SHA256SUMS.txt"]).decode("ascii").split()
    if len(checksum) != 2 or checksum[1] != CONFIG["archive"]:
        raise RuntimeError("Published checksum file is invalid")
    archive = assets[CONFIG["archive"]]
    digest = archive.get("digest") or "sha256:" + hashlib.sha256(asset_bytes(archive)).hexdigest()
    if digest != "sha256:" + checksum[0]:
        raise RuntimeError("Published asset checksum mismatch")


def withdraw_previous():
    previous = release_for(CONFIG["bootstrap"]["tag"])
    if previous:
        if previous["id"] != CONFIG["bootstrap"]["release_id"]:
            raise RuntimeError("Previous release changed; refusing to remove it")
        original = next((a for a in previous["assets"] if a["name"] == CONFIG["bootstrap"]["asset"]), None)
        if not original or original.get("digest") != "sha256:" + CONFIG["bootstrap"]["sha256"]:
            raise RuntimeError("Previous release asset changed; refusing to remove it")
        request("DELETE", "/releases/" + str(previous["id"]))
        print("Withdrew superseded release: " + CONFIG["bootstrap"]["tag"])


def publish(output):
    if os.environ.get("GITHUB_REPOSITORY") != CONFIG["repository"] or not os.environ.get("GH_TOKEN"):
        raise RuntimeError("Publishing requires this repository's Actions token")
    commit = os.environ["GITHUB_SHA"]
    head = request("GET", "/git/ref/heads/main")["object"]["sha"]
    if head != commit:
        raise RuntimeError("Main advanced during the build; refusing a stale release")
    release = release_for(CONFIG["tag"])
    if release and not release["draft"]:
        tag = request("GET", "/git/ref/tags/" + CONFIG["tag"])
        if tag["object"]["sha"] != commit:
            raise RuntimeError("Published tag points to another commit; choose a new version")
        verify_published(release)
        withdraw_previous()
        print(release["html_url"])
        return
    body = (ROOT / "release/notes.md").read_text(encoding="utf-8")
    if release is None:
        release = request("POST", "/releases", {"tag_name": CONFIG["tag"], "target_commitish": commit,
                          "name": CONFIG["name"], "body": body, "draft": True, "prerelease": False})
    elif release["target_commitish"] != commit:
        raise RuntimeError("Draft release belongs to another commit")
    existing = {a["name"]: a for a in release["assets"]}
    for name in [CONFIG["archive"], "SHA256SUMS.txt"]:
        path = Path(output) / name
        expected = "sha256:" + sha256(path)
        if name in existing:
            if existing[name].get("digest") == expected:
                continue
            request("DELETE", "/releases/assets/" + str(existing[name]["id"]))
        uploaded = request("POST", release["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(name),
                           data=path.read_bytes(), content_type="application/octet-stream")
        if uploaded.get("digest") and uploaded["digest"] != expected:
            raise RuntimeError("Uploaded asset checksum mismatch")
    release = request("PATCH", "/releases/" + str(release["id"]),
                      {"draft": False, "prerelease": False, "make_latest": "true", "name": CONFIG["name"], "body": body})
    verify_published(release)
    withdraw_previous()
    print(release["html_url"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["package", "publish"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime")
    parser.add_argument("--bootstrap-archive")
    args = parser.parse_args()
    if args.command == "package":
        if not args.runtime:
            parser.error("package requires --runtime")
        package(args.runtime, args.output, args.bootstrap_archive)
    else:
        publish(args.output)
