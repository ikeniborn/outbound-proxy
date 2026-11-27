"""Install the verified OCI image and exercise it on a clean, disposable Linux Engine."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:18890"
TAG = "outbound-proxy-smoke:candidate"
SERVICE = ("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=outbound-proxy")


def run(*command):
    return subprocess.check_output(command, text=True, cwd=ROOT).strip()


def amd64_image_id(layout):
    """Select the config digest from the layout already validated by verify_oci.py."""
    def blob(digest):
        return json.loads((layout / "blobs" / "sha256" / digest.removeprefix("sha256:")).read_bytes())

    root, = json.loads((layout / "index.json").read_bytes())["manifests"]
    manifest, = (item for item in blob(root["digest"])["manifests"]
                 if item["platform"]["os"] == "linux" and item["platform"]["architecture"] == "amd64")
    return blob(manifest["digest"])["config"]["digest"]


def deploy(*args):
    prefix = ["sudo", "-n"] if os.geteuid() != 0 else []
    return run(*prefix, "sh", "scripts/docker-proxy.sh", *args)


def browser_check():
    with sync_playwright() as playwright:
        executable = shutil.which("google-chrome") or shutil.which("chromium")
        with playwright.chromium.launch(executable_path=executable) as browser:
            page = browser.new_page()
            with page.expect_response(URL + "/api/session") as session:
                page.goto(URL)
            assert session.value.status == 200
            page.get_by_text("Stopped", exact=True).wait_for()
            assert not page.get_by_label("Pairing code").is_visible()
            assert page.request.post(URL + "/api/action", data={"action": "failover", "value": True},
                                     headers={"Origin": URL}).status == 403
            with page.expect_response(URL + "/api/action") as changed:
                page.get_by_label("Automatic failover").check()
            assert changed.value.status == 200 and changed.value.json()["failover"] is True
            assert changed.value.request.header_value("x-csrf-token")


def smoke(layout):
    if run(*SERVICE) or "outbound-proxy-catalog" in run("docker", "volume", "ls", "-q").splitlines():
        raise RuntimeError("The OCI smoke test requires a disposable Engine with an empty catalog.")
    if json.loads(deploy("status"))["settings"] is not None:
        raise RuntimeError("The OCI smoke test requires a fresh deployment.")
    candidate = amd64_image_id(layout)
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "candidate.tar"
        run("skopeo", "--override-os", "linux", "--override-arch", "amd64", "copy",
            "oci:" + str(layout), f"docker-archive:{archive}:{TAG}")
        run("docker", "image", "load", "--input", str(archive))
    assert run("docker", "image", "inspect", "--format", "{{.Id}}", TAG) == candidate
    try:
        deploy("install", "--local-only", "--image", candidate)
        cid, = run(*SERVICE).splitlines()
        assert run("docker", "inspect", "--format", "{{.Image}}", cid) == candidate
        assert run("docker", "port", cid, "18890/tcp") == "127.0.0.1:18890"
        browser_check()
        status = json.loads(run("docker", "exec", cid, "outbound-proxy", "status"))
        assert status["deployment"]["image"] == candidate and status["failover"] is True
        assert status["running"] is False and status["recovery_pending"] is False
    finally:
        deploy("remove")
    assert not run(*SERVICE)
    assert json.loads(deploy("status"))["settings"] is None
    assert "outbound-proxy-catalog" in run("docker", "volume", "ls", "-q").splitlines()
    print(f"Installed, exercised and removed linux/amd64 image {candidate}")


if __name__ == "__main__":
    smoke(Path(sys.argv[1]).resolve())
