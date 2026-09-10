#!/usr/bin/env python3

import json
import subprocess
from pathlib import Path

PAGE = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Downloads/neizlechimyh_bolezney_net_book/pages/page-0181.png"
VISION = Path("/tmp/booktext-vision-words.json")
OUT = Path("/tmp/booktext-v18-crops")

TARGETS = {
    "1-клеток,",
    "стралает",
    "кукурузс,",
    "эрготоксинов",
    "подогрейтс",
    "добавьс",
    "спорынь",
    "замстили",
    "попыгайтесь",
}

OUT.mkdir(parents=True, exist_ok=True)

def cmd(args, capture=False):
    p = subprocess.run(
        args,
        text=True,
        capture_output=capture,
        check=True,
    )
    return p.stdout if capture else ""

# Page dimensions.
dims = cmd(
    ["magick", "identify", "-format", "%w %h", str(PAGE)],
    capture=True
).strip()

W, H = map(int, dims.split())

data = json.loads(VISION.read_text(encoding="utf-8"))
words = data["words"]

found = []

for i, w in enumerate(words, 1):
    text = w["text"]

    if text not in TARGETS:
        continue

    b = w["bbox"]

    # Vision coordinates are normalized with origin at lower-left.
    x = b["x"] * W
    y_bottom = b["y"] * H
    bw = b["width"] * W
    bh = b["height"] * H

    y_top = H - (y_bottom + bh)

    # Give OCR meaningful neighboring context.
    pad_x = max(35, bw * 0.65)
    pad_y = max(12, bh * 0.8)

    left = max(0, int(x - pad_x))
    top = max(0, int(y_top - pad_y))
    right = min(W, int(x + bw + pad_x))
    bottom = min(H, int(y_top + bh + pad_y))

    cw = right - left
    ch = bottom - top

    stem = f"{i:03d}_{text.replace('/', '_').replace(',', '').replace('(', '').replace(')', '')}"

    raw = OUT / f"{stem}_raw.png"
    gray = OUT / f"{stem}_gray.png"
    bwimg = OUT / f"{stem}_bw.png"

    cmd([
        "magick", str(PAGE),
        "-crop", f"{cw}x{ch}+{left}+{top}",
        "+repage",
        "-resize", "400%",
        str(raw),
    ])

    cmd([
        "magick", str(raw),
        "-colorspace", "Gray",
        "-contrast-stretch", "0.5%x0.5%",
        "-sharpen", "0x1",
        str(gray),
    ])

    cmd([
        "magick", str(gray),
        "-threshold", "72%",
        str(bwimg),
    ])

    rec = {
        "index": i,
        "vision": text,
        "confidence": w.get("confidence"),
        "crop": {
            "left": left,
            "top": top,
            "width": cw,
            "height": ch,
        },
        "files": {
            "raw": str(raw),
            "gray": str(gray),
            "bw": str(bwimg),
        },
        "ocr": {},
    }

    for version_name, image in [
        ("raw", raw),
        ("gray", gray),
        ("bw", bwimg),
    ]:
        rec["ocr"][version_name] = {}

        for psm in (6, 7, 8, 13):
            with open(image, "rb") as fh:
                p = subprocess.run(
                    [
                        "tesseract",
                        "stdin",
                        "stdout",
                        "-l", "rus+eng",
                        "--psm", str(psm),
                        "--dpi", "300",
                    ],
                    stdin=fh,
                    text=False,
                    capture_output=True,
                )

            stdout = p.stdout.decode("utf-8", errors="replace")

            txt = " ".join(
                x.strip()
                for x in stdout.splitlines()
                if x.strip()
            )

            rec["ocr"][version_name][str(psm)] = txt

    found.append(rec)

json_out = OUT / "v18-results.json"
json_out.write_text(
    json.dumps(found, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print("===== VISUAL RECHECK V18 =====")
print("PAGE:", PAGE)
print("SIZE:", W, "x", H)
print("TARGETS FOUND:", len(found))

for r in found:
    print()
    print("=" * 72)
    print(
        f"VISION {r['index']:03d}:",
        r["vision"],
        f"conf={r['confidence']}"
    )
    print("CROP:", r["crop"])

    for variant in ("raw", "gray", "bw"):
        print(f"{variant.upper()}:")

        for psm in ("6", "7", "8", "13"):
            print(
                f"  PSM {psm:>2}:",
                repr(r["ocr"][variant][psm])
            )

print()
print("===== FILES =====")
print("CROPS:", OUT)
print("JSON: ", json_out)
