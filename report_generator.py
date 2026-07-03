"""Create a PowerPoint callout report from OCR geometry and annotations."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from difflib import SequenceMatcher


SLIDE_W, SLIDE_H = 13.333, 7.5
PDF_X, PDF_Y, PDF_W, PDF_H = 3.55, 0.16, 6.20, 7.18
LABEL_W, LABEL_H = 1.88, 0.31
LEFT_X, RIGHT_X = 0.18, SLIDE_W - 0.18 - LABEL_W
DPI = 200

# Only the annotation schema is fixed. Placement and routing are derived from OCR.
FIELDS = [
    ("supplier_name", "Supplier name"),
    ("customer_name", "Customer name"),
    ("purchase_order_number", "Purchase Order number"),
    ("part_number_12nc", "Part number (12NC)"),
    ("revision", "Revision"),
    ("part_description", "Part description"),
    ("statement_of_conformance", "Statement of conformance"),
    ("certificate_number", "Certificate number"),
    ("customer_address", "Customer address"),
    ("measured_value", "Measured Value"),
    ("certificate_title", "Certificate title"),
    ("quantity", "Quantity"),
    ("approver_identification", "Approver Identification"),
    ("confirmation", "Confirmation"),
    ("approver_role", "Approver Role"),
    ("sign_off_date", "Sign-off date"),
    ("supplier_address", "Supplier address"),
]

FIELD_ANCHORS = {
    "certificate_title": ("certificate of analysis", "certificate of compliance", "certificate of conformity"),
    "certificate_number": ("coa no", "coc no", "certificate no", "certificate number"),
    "supplier_name": ("supplier name", "company name", "manufacturer name"),
    "supplier_address": ("supplier address", "manufacturer address"),
    "customer_name": ("customer name", "customer", "company"),
    "customer_address": ("customer address", "ship to", "sold to"),
    "purchase_order_number": ("purchase order number", "purchase order", "po number", "po no"),
    "part_number_12nc": ("philips part number", "part number", "12nc"),
    "revision": ("revision", "rev"),
    "part_description": ("part description", "description"),
    "quantity": ("quantity shipped", "ship quantity", "quantity"),
    "statement_of_conformance": ("statement of conformance", "statement of conformity", "certifies that", "confirms that"),
    "approver_identification": ("approved by", "verified by", "prepared by", "name"),
    "approver_role": ("title", "role", "designation"),
    "confirmation": ("signature", "confirmation"),
    "sign_off_date": ("sign off date", "date"),
    "measured_value": ("measured value", "result", "test result"),
}


def _norm(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _box(polygon):
    if not polygon:
        return None
    xs = [float(p["x"]) for p in polygon]
    ys = [float(p["y"]) for p in polygon]
    return {"minx": min(xs), "miny": min(ys), "maxx": max(xs), "maxy": max(ys)}


def render_pdf(pdf_path):
    try:
        import fitz
    except ImportError:
        sys.exit("PyMuPDF not installed: pip install pymupdf")
    doc = fitz.open(pdf_path)
    if not doc.page_count:
        sys.exit("The PDF contains no pages")
    pages = []
    try:
        for page in doc:
            width, height = page.rect.width / 72.0, page.rect.height / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            pix.save(tmp.name)
            pages.append({"path": tmp.name, "width": width, "height": height})
    finally:
        doc.close()
    return pages


def _table_values(annotations):
    """Recover reliable values from the annotation engine's table payload."""
    rows = []
    for table in annotations.get("tables") or []:
        if int(table.get("page_number", 1) or 1) != 1:
            continue
        rows.extend(table.get("data") or [])

    values = {}
    aliases = {
        "supplier name": "supplier_name",
        "supplier address": "supplier_address",
        "customer name": "customer_name",
        "customer address": "customer_address",
        "purchase order number": "purchase_order_number",
        "quantity shipped": "quantity",
        "certificate number": "certificate_number",
        "coa no": "certificate_number",
        "coc no": "certificate_number",
    }
    for row in rows:
        cells = [str(c or "").strip() for c in row]
        if len(cells) < 2:
            continue
        key = aliases.get(_norm(cells[0]))
        if key and cells[1]:
            values[key] = cells[1]

        label = _norm(cells[0])
        if label == "philips part number":
            values["part_number_12nc"] = cells[1]
            if len(cells) > 2 and cells[2]:
                values["revision"] = cells[2]
        elif label == "part description":
            values["part_description"] = cells[1]
    return values


def resolved_annotations(annotations):
    result = {key: annotations.get(key) for key, _ in FIELDS}
    # Tables are stronger evidence than a generic/label-only LLM result.
    result.update(_table_values(annotations))
    return result


def build_candidates(raw):
    candidates = []
    for page in raw.get("pages") or []:
        page_no = int(page.get("page_number", 1) or 1)
        if page_no != 1:  # the report displays page one
            continue
        for index, line in enumerate(page.get("lines") or []):
            text = str(line.get("text") or "").strip()
            box = _box(line.get("polygon"))
            if text and box:
                candidates.append({"text": text, "norm": _norm(text), "box": box, "index": index})
    return candidates


def _match_score(value, candidate):
    v, c = _norm(value), candidate["norm"]
    if not v or not c:
        return 0.0
    if v == c:
        return 1.0
    if v in c:
        return 0.97 if len(v) >= 4 else 0.75
    if c in v:
        coverage = len(c) / max(len(v), 1)
        return 0.72 + 0.24 * coverage
    vt, ct = set(v.split()), set(c.split())
    overlap = len(vt & ct)
    token_score = (2 * overlap / (len(vt) + len(ct))) if overlap else 0.0
    char_score = SequenceMatcher(None, v, c).ratio()
    return 0.62 * token_score + 0.38 * char_score


def find_target(key, value, candidates):
    if value is None or not str(value).strip():
        return None
    anchors = {_norm(a) for a in FIELD_ANCHORS.get(key, ())}
    ranked = []
    for candidate in candidates:
        score = _match_score(value, candidate)
        # Never annotate a field-name line as its value.
        if candidate["norm"] in anchors and _norm(value) != candidate["norm"]:
            score = 0
        # Prefer a candidate immediately following the field label in reading order.
        for anchor in candidates:
            if anchor["norm"] in anchors and 0 < candidate["index"] - anchor["index"] <= 2:
                score += 0.08
                break
        ranked.append((score, candidate))
    score, best = max(ranked, key=lambda item: item[0], default=(0, None))
    if key == "confirmation":
        # Some extractors store the signed date as "confirmation". The visual
        # confirmation is the handwritten/typed value following Signature.
        for anchor in candidates:
            if anchor["norm"].startswith("signature"):
                following = [c for c in candidates if 0 < c["index"] - anchor["index"] <= 2]
                if following:
                    return following[0]
    if score >= 0.48:
        return best
    if key == "statement_of_conformance":
        for anchor in candidates:
            if anchor["norm"] in anchors:
                following = [c for c in candidates if c["index"] == anchor["index"] + 1]
                return following[0] if following else anchor
    return None


def _fit_document(page_w, page_h):
    scale = min(PDF_W / page_w, PDF_H / page_h)
    width, height = page_w * scale, page_h * scale
    return PDF_X + (PDF_W - width) / 2, PDF_Y + (PDF_H - height) / 2, width, height


def _label_positions(items):
    """Lay labels out in target order, preserving non-crossing connector order."""
    found = sorted(
        (item for item in items if item["target"]),
        key=lambda item: item["target"]["y"] + item["target"]["h"] / 2,
    )
    missing = [item for item in items if not item["target"]]
    top, bottom = 0.20, SLIDE_H - LABEL_H - 0.20
    gap = 0.10
    pitch = LABEL_H + gap
    missing_pitch = LABEL_H + 0.22
    found_bottom = bottom - len(missing) * missing_pitch

    # Start at each target's natural Y, then resolve collisions without ever
    # changing order. Equal ordering on both ends guarantees no line crossings.
    for item in found:
        target_mid = item["target"]["y"] + item["target"]["h"] / 2
        item["ly"] = max(top, min(target_mid - LABEL_H / 2, found_bottom))
    for i in range(1, len(found)):
        found[i]["ly"] = max(found[i]["ly"], found[i - 1]["ly"] + pitch)
    if found and found[-1]["ly"] > found_bottom:
        found[-1]["ly"] = found_bottom
        for i in range(len(found) - 2, -1, -1):
            found[i]["ly"] = min(found[i]["ly"], found[i + 1]["ly"] - pitch)
    for item in found:
        item["ly"] = round(max(top, item["ly"]), 4)
    for i, item in enumerate(missing):
        item["ly"] = round(bottom - (len(missing) - 1 - i) * missing_pitch, 4)
    return found + missing


def _assign_sides(entries, document):
    """Choose the nearer margin from target geometry; balance center targets."""
    dx, _, dw, _ = document
    centre = dx + dw / 2
    left, right, unresolved = [], [], []
    for entry in entries:
        if not entry["target"]:
            unresolved.append(entry)
            continue
        target_mid = entry["target"]["x"] + entry["target"]["w"] / 2
        # A small central band avoids unstable choices for centered text.
        if abs(target_mid - centre) < dw * 0.08:
            (left if len(left) <= len(right) else right).append(entry)
        else:
            (left if target_mid < centre else right).append(entry)
    for entry in unresolved:
        (left if len(left) <= len(right) else right).append(entry)
    for entry in left:
        entry["side"] = "left"
    for entry in right:
        entry["side"] = "right"
    return left, right


def build_entries(annotations, raw, page_w, page_h):
    values, candidates = resolved_annotations(annotations), build_candidates(raw)
    dx, dy, dw, dh = _fit_document(page_w, page_h)
    entries = []
    for key, label in FIELDS:
        value = values.get(key)
        target = find_target(key, value, candidates)
        entry = {"key": key, "label": label, "value": value, "target": None}
        if target:
            b = target["box"]
            entry["target"] = {
                "x": dx + b["minx"] / page_w * dw,
                "y": dy + b["miny"] / page_h * dh,
                "w": max((b["maxx"] - b["minx"]) / page_w * dw, 0.08),
                "h": max((b["maxy"] - b["miny"]) / page_h * dh, 0.07),
            }
        entries.append(entry)
    left, right = _assign_sides(entries, (dx, dy, dw, dh))
    left = _label_positions(left)
    right = _label_positions(right)
    # Each callout gets a separate routing lane in the margin. Lane coordinates
    # are calculated from the fitted document bounds and population, not fields.
    lane_gap = 0.055
    for index, entry in enumerate(left):
        entry["laneX"] = round(dx - 0.08 - index * lane_gap, 4)
    for index, entry in enumerate(right):
        entry["laneX"] = round(dx + dw + 0.08 + index * lane_gap, 4)
    entries = left + right
    for entry in entries:
        entry["lx"] = LEFT_X if entry["side"] == "left" else RIGHT_X
    return entries, (dx, dy, dw, dh)


def _fit_full_slide(page_w, page_h, margin=0.08):
    """Fit an unannotated PDF page onto a widescreen slide."""
    available_w, available_h = SLIDE_W - 2 * margin, SLIDE_H - 2 * margin
    scale = min(available_w / page_w, available_h / page_h)
    width, height = page_w * scale, page_h * scale
    return (SLIDE_W - width) / 2, (SLIDE_H - height) / 2, width, height


def build_js(rendered_pages, annotations, raw, out_path):
    first_page = rendered_pages[0]
    img_path = first_page["path"]
    page_w, page_h = first_page["width"], first_page["height"]
    entries, document = build_entries(annotations, raw, page_w, page_h)
    img_b64 = base64.b64encode(open(img_path, "rb").read()).decode("ascii")
    out_path = os.path.abspath(out_path).replace("\\", "\\\\").replace('"', '\\"')
    dx, dy, dw, dh = document
    extra_slides = []
    for page in rendered_pages[1:]:
        page_b64 = base64.b64encode(open(page["path"], "rb").read()).decode("ascii")
        x, y, w, h = _fit_full_slide(page["width"], page["height"])
        extra_slides.append(f'''{{
  const pageSlide = pptx.addSlide();
  pageSlide.background = {{ color: "FFFFFF" }};
  pageSlide.addImage({{data:"image/png;base64,{page_b64}", x:{x:.5f}, y:{y:.5f}, w:{w:.5f}, h:{h:.5f}}});
}}''')
    extra_slides_js = "\n".join(extra_slides)
    return f'''const pptxgen = require("pptxgenjs");
const pptx = new pptxgen();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "CoC / CoA annotation pipeline";
const slide = pptx.addSlide();
slide.background = {{ color: "F4F5F3" }};
const entries = {json.dumps(entries, ensure_ascii=False)};
const DOC = {{x:{dx:.5f}, y:{dy:.5f}, w:{dw:.5f}, h:{dh:.5f}}};
slide.addImage({{data:"image/png;base64,{img_b64}", ...DOC}});
slide.addShape(pptx.ShapeType.rect, {{...DOC, fill:{{color:"FFFFFF", transparency:100}}, line:{{color:"B6B6B6", width:0.7}}}});

for (const e of entries) {{
  const found = Boolean(e.target);
  slide.addShape(pptx.ShapeType.rect, {{
    x:e.lx, y:e.ly, w:{LABEL_W}, h:{LABEL_H},
    fill:{{color:found ? "FFFFFF" : "92D050"}},
    line:{{color:found ? "171717" : "92D050", width:0.8}}
  }});
  slide.addText(e.label, {{x:e.lx, y:e.ly, w:{LABEL_W}, h:{LABEL_H}, fontFace:"Arial",
    fontSize:7.3, color:found ? "222222" : "FFFFFF", align:"center", valign:"mid", margin:1}});
  if (!found) continue;

  const left = e.side === "left";
  const labelX = left ? e.lx + {LABEL_W} : e.lx;
  const labelY = e.ly + {LABEL_H}/2;
  const targetX = left ? e.target.x : e.target.x + e.target.w;
  const targetY = e.target.y + e.target.h/2;
  const laneX = e.laneX;
  const line = {{color:"777777", width:0.65, beginArrowType:"none", endArrowType:"none"}};
  // Three segments with numerically identical endpoints form one continuous,
  // field-specific orthogonal connector. No lane is shared by two fields.
  slide.addShape(pptx.ShapeType.line, {{
    x:Math.min(labelX,laneX), y:labelY,
    w:Math.abs(laneX-labelX), h:0, line
  }});
  slide.addShape(pptx.ShapeType.line, {{
    x:laneX, y:Math.min(labelY,targetY),
    w:0, h:Math.abs(targetY-labelY), line
  }});
  slide.addShape(pptx.ShapeType.line, {{
    x:Math.min(laneX,targetX), y:targetY,
    w:Math.abs(targetX-laneX), h:0, line
  }});
  slide.addShape(pptx.ShapeType.ellipse, {{x:labelX-0.052, y:labelY-0.052, w:0.104, h:0.104,
    fill:{{color:"000000"}}, line:{{color:"000000", width:0}}}});
  slide.addShape(pptx.ShapeType.ellipse, {{x:targetX-0.052, y:targetY-0.052, w:0.104, h:0.104,
    fill:{{color:"000000"}}, line:{{color:"000000", width:0}}}});
  slide.addShape(pptx.ShapeType.rect, {{x:e.target.x, y:e.target.y, w:e.target.w, h:e.target.h,
    fill:{{color:"FFFFFF", transparency:100}}, line:{{color:"111111", width:0.8}}}});
}}

{extra_slides_js}

pptx.writeFile({{fileName:"{out_path}"}}).catch(err => {{console.error(err); process.exit(1);}});
'''


def main():
    parser = argparse.ArgumentParser(description="Generate a CoC/CoA annotation PowerPoint")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--annotations", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.annotations, encoding="utf-8") as handle:
        annotations = json.load(handle)
    with open(args.raw, encoding="utf-8") as handle:
        raw = json.load(handle)

    rendered_pages = []
    js_path = None
    try:
        print("Rendering PDF pages...")
        rendered_pages = render_pdf(args.pdf)
        first_page = rendered_pages[0]
        print(
            f'  Rendered {len(rendered_pages)} page(s); first page size: '
            f'{first_page["width"]:.2f}" x {first_page["height"]:.2f}"'
        )
        js_code = build_js(rendered_pages, annotations, raw, args.out)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", encoding="utf-8")
        js_path = tmp.name
        with tmp:
            tmp.write(js_code)
        print("Building PPTX...")
        node_env = os.environ.copy()
        local_modules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules")
        node_env["NODE_PATH"] = local_modules + os.pathsep + node_env.get("NODE_PATH", "")
        result = subprocess.run(
            ["node", js_path], capture_output=True, text=True, env=node_env
        )
        if result.returncode:
            sys.exit("Node/PptxGenJS error:\n" + (result.stderr or result.stdout))
        print(f"Saved to {args.out}")
    finally:
        image_paths = [page["path"] for page in rendered_pages]
        for path in [js_path, *image_paths]:
            if path and os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    main()
