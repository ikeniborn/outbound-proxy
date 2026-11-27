"""Copy regular OCI files into a new layout and verify local, revision-labelled images."""

import hashlib
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path


def verify(archive, layout, revision):
    layout.mkdir()
    blobs = layout / "blobs/sha256"
    blobs.mkdir(parents=True)
    with tarfile.open(archive, "r|") as tar:
        for member in tar:
            name = member.name.removeprefix("./")
            if name not in ("oci-layout", "index.json") and not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
                continue
            if not member.isfile():
                raise ValueError("Expected a regular OCI file")
            with (layout / name).open("xb") as output:
                shutil.copyfileobj(tar.extractfile(member), output)

    if json.loads((layout / "oci-layout").read_bytes())["imageLayoutVersion"] != "1.0.0":
        raise ValueError("Unknown OCI layout")

    def blob(descriptor):
        digest = descriptor["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or "urls" in descriptor or "data" in descriptor:
            raise ValueError("Expected a local SHA-256 descriptor")
        path = blobs / digest[7:]
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != descriptor["size"] or checksum != digest[7:]:
            raise ValueError("OCI blob does not match its descriptor")
        return path

    def document(descriptor, kind):
        if descriptor["mediaType"] != "application/vnd.oci.image." + kind + ".v1+json":
            raise ValueError("Unexpected OCI media type")
        return json.loads(blob(descriptor).read_bytes())

    root, = json.loads((layout / "index.json").read_bytes())["manifests"]
    index = document(root, "index")
    platforms = []
    for image in index["manifests"]:
        manifest = document(image, "manifest")
        config = document(manifest["config"], "config")
        system, arch = image["platform"]["os"], image["platform"]["architecture"]
        if (config["os"], config["architecture"]) != (system, arch):
            raise ValueError("Platform descriptor does not match image configuration")
        if config["config"]["Labels"]["org.opencontainers.image.revision"] != revision:
            raise ValueError("Image revision does not match the checked commit")
        platforms.append((system, arch))
        for layer in manifest["layers"]:
            blob(layer)
    if sorted(platforms) != [("linux", "amd64"), ("linux", "arm64")]:
        raise ValueError("Expected linux/amd64 and linux/arm64")
    return root["digest"]


if __name__ == "__main__":
    print(verify(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
