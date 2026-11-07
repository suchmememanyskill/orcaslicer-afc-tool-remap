"""Build the dependency-free Orca wheel, bundling Python and the HTML template.

Usage: python3 build_plugin.py --output-dir /path/to/output
The installed development copy can also use the two files side by side.
"""
import argparse
import base64
import csv
import hashlib
import io
from pathlib import Path
import re
import zipfile


def build(output_dir):
    source = Path(__file__).parent
    code = (source / "afc_pre_send.py").read_bytes()
    version = re.search(rb'^# version = "([^"]+)"$', code, re.MULTILINE)[1].decode()
    info = f"afc_tool_remap-{version}.dist-info"
    files = {
        "afc_pre_send/__init__.py": code,
        "afc_pre_send/mapping.html": (source / "mapping.html").read_bytes(),
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: AFC-tool-remap\nVersion: {version}\n"
            "Summary: AFC physical lane mapping before Orca upload\n"
            "Author: Local\nRequires-Python: >=3.12\n\n").encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: afc-tool-remap\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/top_level.txt": b"afc_pre_send\n",
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow((name, "sha256=" + digest, len(data)))
    writer.writerow((f"{info}/RECORD", "", ""))
    files[f"{info}/RECORD"] = record.getvalue().encode()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"afc_tool_remap-{version}-py3-none-any.whl"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    print(build(parser.parse_args().output_dir))
