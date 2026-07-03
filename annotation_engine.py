from field_mapper import map_to_schema, extract_from_tables, find_canonical_field
import requests
import json
import re


# Converts polygon points into axis-aligned bounds and useful vertical metrics.
def get_bbox(polygon):
    xs = [p["x"] for p in polygon]
    ys = [p["y"] for p in polygon]
    return {
        "left":     min(xs),
        "right":    max(xs),
        "top":      min(ys),
        "bottom":   max(ys),
        "center_y": sum(ys) / len(ys),
        "height":   max(ys) - min(ys),
    }


# Reports whether two polygons have sufficiently close vertical centers.
def same_row(poly1, poly2, threshold=0.015):
    b1 = get_bbox(poly1)
    b2 = get_bbox(poly2)
    return abs(b1["center_y"] - b2["center_y"]) <= threshold


# Divides OCR lines into left and right groups using their horizontal position.
def split_into_columns(lines, threshold=0.45):
    left_col, right_col = [], []
    for line in lines:
        if not line["polygon"]:
            left_col.append(line)
            continue
        bbox = get_bbox(line["polygon"])
        if bbox["left"] < threshold:
            left_col.append(line)
        else:
            right_col.append(line)
    return left_col, right_col


# Limits field detection to the leading pages where certificate data is expected.
def get_relevant_pages(extracted_output, max_pages=2):
    """
    Only first 1-2 pages contain certificate fields.
    Rest are annexes, serial number lists etc.
    """
    pages = extracted_output.get("pages", [])
    return pages[:max_pages]


# Returns only tables belonging to the certificate's relevant leading pages.
def get_relevant_tables(extracted_output, max_page=2):
    """
    Only use tables from the first 2 pages.
    """
    return [
        t for t in extracted_output.get("tables", [])
        if t.get("page_number") and t["page_number"] <= max_page
    ]


# Extracts colon-terminated labels and collects their value either from the
# same row or from the nearby lines immediately below the label.
def extract_pairs_from_lines(lines):
    pairs = []
    i = 0

    while i < len(lines):
        current = lines[i]
        text    = current["text"]
        poly    = current["polygon"]

        if text.endswith(":"):
            label = text[:-1].strip()
            value = ""

            # CASE 1 — value on same row
            if (
                i + 1 < len(lines)
                and lines[i + 1]["polygon"]
                and same_row(poly, lines[i + 1]["polygon"])
            ):
                value = lines[i + 1]["text"]
                i += 2

            # CASE 2 — value below label
            else:
                label_bbox   = get_bbox(poly) if poly else None
                line_height  = label_bbox["height"] if label_bbox else 0.05
                max_distance = max(line_height * 3, 0.08)

                collected = []
                j = i + 1

                while j < len(lines):
                    candidate_text = lines[j]["text"]
                    candidate_poly = lines[j]["polygon"]

                    if candidate_text.endswith(":"):
                        break

                    if label_bbox and candidate_poly:
                        candidate_bbox = get_bbox(candidate_poly)
                        if candidate_bbox["top"] - label_bbox["bottom"] > max_distance:
                            break

                    collected.append(candidate_text)
                    j += 1

                value = "\n".join(collected)
                i = j

            if value:
                pairs.append({
                    "label":      label,
                    "value":      value,
                    "source":     "line_colon",
                    "confidence": None
                })
        else:
            i += 1

    return pairs


# Splits OCR lines containing both a label and value separated by a colon.
def extract_inline_pairs(lines):
    """
    Handles lines where label and value are in the same text block
    e.g. 'Name: Johan Riedl', 'Date: 2/12/2026', 'Philips P/N: 1253-8415'
    """
    pairs = []
    for line in lines:
        text = line["text"].strip().lstrip("·").strip()
        if ":" not in text or text.endswith(":"):
            continue
        parts = text.split(":", 1)
        label = parts[0].strip()
        value = parts[1].strip()
        if label and value and len(label) >= 2:
            pairs.append({
                "label":      label,
                "value":      value,
                "source":     "inline_colon",
                "confidence": None
            })
    return pairs


# Matches known labels without colons to the nearest value on their right.
def extract_alias_matched_pairs(lines):
    """
    Spatial label-value matching without colons.
    Finds lines matching known field aliases and pairs them
    with the nearest value to the right on the same row.
    """
    pairs = []

    for i, line in enumerate(lines):
        text = line["text"].strip().rstrip(":")
        poly = line["polygon"]

        if not poly or len(text) < 3:
            continue

        field = find_canonical_field(text)
        if not field:
            continue

        bbox = get_bbox(poly)

        candidates = []
        for j, other in enumerate(lines):
            if i == j or not other["polygon"]:
                continue

            other_bbox = get_bbox(other["polygon"])

            if not same_row(poly, other["polygon"]):
                continue

            if other_bbox["left"] <= bbox["right"]:
                continue

            candidates.append((other_bbox["left"], other["text"]))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            value = candidates[0][1]

            pairs.append({
                "label":      text,
                "value":      value,
                "source":     "spatial_alias",
                "confidence": None
            })

    return pairs


# Detects a supported certificate title among the first page-header lines.
def detect_header_fields(lines):
    """
    Looks at the first 10 lines for certificate title keywords
    that appear without a colon label.
    Also detects German certificate types.
    """
    pairs = []
    keywords = [
        "certificate of analysis",
        "certificate of conformance",
        "certificate of compliance",
        "übereinstimmungszeugnis",
        "übereinstimmungserklärung",
        "werksbescheinigung",
    ]
    for line in lines[:10]:
        text       = line["text"].strip()
        text_lower = text.lower()
        if any(kw in text_lower for kw in keywords):
            pairs.append({
                "label":      "certificate_title",
                "value":      text,
                "source":     "header_detection",
                "confidence": None
            })
            break
    return pairs


# Builds a conformance statement from text following a recognized trigger phrase.
def detect_free_text_fields(lines):
    """
    Detects statement of conformance from free-text paragraphs.
    Handles both English and German trigger phrases.
    """
    pairs = []
    trigger_phrases = [
        "we hereby certify",
        "we certify",
        "this is to certify",
        "the above-mentioned",
        "the product listed above",
        "parts were assembled",
        "above mentioned",
        "hiermit wird bestätigt",
        "die oben genannten",
        "wurden gemäß",
    ]
    for i, line in enumerate(lines):
        text_lower = line["text"].lower()
        if any(phrase in text_lower for phrase in trigger_phrases):
            block = []
            for j in range(i, min(i + 5, len(lines))):
                block.append(lines[j]["text"])
            pairs.append({
                "label":      "statement_of_conformance",
                "value":      " ".join(block),
                "source":     "free_text_detection",
                "confidence": None
            })
            break
    return pairs


# Separates a German-style date and possible approver name found on one line.
def detect_date_name_line(lines):
    """
    Detects lines like '05.05.2026  Markus Köhler, QMB'
    where date and approver name appear together on the same line.
    Common in German certificates.
    """
    pairs = []
    date_pattern = re.compile(r'\b(\d{2}\.\d{2}\.\d{4})\b')

    for line in lines:
        text  = line["text"].strip()
        match = date_pattern.search(text)
        if match:
            date_val  = match.group(1)
            remainder = text.replace(date_val, "").strip().strip(",").strip()

            pairs.append({
                "label":      "sign_off_date",
                "value":      date_val,
                "source":     "date_name_detection",
                "confidence": None
            })

            # Only use remainder as approver if it looks like a name
            # (has letters, not too long, not a place name)
            if remainder and 3 <= len(remainder) <= 60:
                pairs.append({
                    "label":      "approver_identification",
                    "value":      remainder,
                    "source":     "date_name_detection",
                    "confidence": None
                })

    return pairs


# Infers customer name and address blocks near the top from known customer names.
def detect_unlabelled_address(lines):
    """
    Detects customer address blocks that appear without a label.
    Looks for known customer name patterns near the top of the page.
    Common in German certificates where customer address is just printed.
    """
    known_customers = [
        "philips",
        "philips medizin",
        "philips north america",
    ]
    pairs = []

    for i, line in enumerate(lines[:15]):
        text_lower = line["text"].lower()
        if any(kw in text_lower for kw in known_customers):
            block = []
            for j in range(i, min(i + 6, len(lines))):
                candidate = lines[j]["text"].strip()
                if candidate.endswith(":") or len(candidate) > 80:
                    break
                block.append(candidate)

            if len(block) >= 2:
                pairs.append({
                    "label":      "customer_name",
                    "value":      block[0],
                    "source":     "address_block_detection",
                    "confidence": None
                })
                if len(block) > 1:
                    pairs.append({
                        "label":      "customer_address",
                        "value":      ", ".join(block[1:]),
                        "source":     "address_block_detection",
                        "confidence": None
                    })
            break

    return pairs


# Combines native Azure pairs with all rule-based line extraction strategies.
def extract_raw_pairs(extracted_output):
    raw_pairs = []

    # Strategy 1 — native KV pairs
    for kv in extracted_output.get("key_value_pairs", []):
        if kv["key"] and kv["value"]:
            raw_pairs.append({
                "label":      kv["key"],
                "value":      kv["value"],
                "source":     "native_kv",
                "confidence": kv["confidence"]
            })

    # Strategy 2 — per-page line extraction (first 2 pages only)
    for page in get_relevant_pages(extracted_output):
        lines = page["lines"]

        raw_pairs.extend(detect_header_fields(lines))
        raw_pairs.extend(detect_free_text_fields(lines))
        raw_pairs.extend(extract_inline_pairs(lines))
        raw_pairs.extend(detect_date_name_line(lines))
        raw_pairs.extend(detect_unlabelled_address(lines))

        # Colon-based extraction — column-aware
        left_lines, right_lines = split_into_columns(lines)
        if len(left_lines) > 2 and len(right_lines) > 2:
            raw_pairs.extend(extract_pairs_from_lines(left_lines))
            raw_pairs.extend(extract_pairs_from_lines(right_lines))
        else:
            raw_pairs.extend(extract_pairs_from_lines(lines))

        # Spatial alias matching for no-colon labels
        raw_pairs.extend(extract_alias_matched_pairs(lines))

    return raw_pairs


# ── Validation ────────────────────────────────────────────────

FIELD_VALIDATORS = {
    "certificate_number":      lambda v: (
        len(v) >= 3
        and any(c.isdigit() for c in v)
        and not any(kw in v.lower() for kw in [
            "certificate of", "analysis", "conformance", "compliance"
        ])
    ),
    "part_number_12nc":        lambda v: len(v) >= 3 and any(c.isdigit() for c in v),
    "revision":                lambda v: len(v) <= 10 and v.replace(".", "").replace("-", "").isalnum(),
    "sign_off_date":           lambda v: (
        any(c.isdigit() for c in v)
        and len(v) >= 4
        and any(sep in v for sep in ["/", "-", ".", " "])
    ),
    "quantity":                lambda v: any(c.isdigit() for c in v),
    "supplier_name":           lambda v: (
        3 <= len(v) <= 200
        and not v.endswith(":")
        and not v.replace(",", "").replace(" ", "").isdigit()
    ),
    "customer_name":           lambda v: (
        3 <= len(v) <= 200
        and not v.endswith(":")
        and not v.replace(",", "").replace(" ", "").isdigit()
    ),
    "supplier_address": lambda v: (
        len(v) >= 5
        and not any(kw in v.lower() for kw in [
            "werksbescheinigung", "datum", "certificate", "conformance"
        ])
    ),
    "customer_address": lambda v: (
        len(v) >= 5
        and not any(kw in v.lower() for kw in [
            "werksbescheinigung", "datum", "certificate", "conformance"
        ])
    ),
    "certificate_title":       lambda v: any(kw in v.lower() for kw in [
        "certificate", "analysis", "conformance", "compliance",
        "übereinstimmung", "werksbescheinigung"
    ]),
    "purchase_order_number":   lambda v: len(v) >= 3 and any(c.isdigit() for c in v),
    "approver_identification": lambda v: 3 <= len(v) <= 100,
    "approver_role": lambda v: (
        2 <= len(v) <= 60
        and not any(c.isdigit() for c in v)
        and "print" not in v.lower()
        and "sign" not in v.lower()
        and "certificate" not in v.lower()
        and "übereinstimmung" not in v.lower()
        and "conformance" not in v.lower()
    ),
    "part_description":        lambda v: 3 <= len(v) <= 300,
    "statement_of_conformance":lambda v: len(v) >= 20,
    "measured_value":          lambda v: len(v) >= 1,
    "confirmation":            lambda v: len(v) >= 1,
}


# Applies field-specific validators, clearing and recording values that fail.
def validate_annotations(annotations):
    rejected = {}

    for field, validator in FIELD_VALIDATORS.items():
        value = annotations.get(field)
        if value is None:
            continue
        try:
            passed = validator(value)
        except Exception:
            passed = False

        if not passed:
            rejected[field] = value
            annotations[field] = None
            print(f"  Validation FAILED — cleared: {field} = {repr(value)}")
        else:
            print(f"  Validation OK: {field} = {repr(value)}")

    return annotations, rejected


# ── LLM Fallback ──────────────────────────────────────────────

# Builds a text-only JSON extraction prompt for fields rules could not supply.
def build_llm_prompt(extracted_output, missing_fields, rejected_fields):
    all_text = []
    for page in get_relevant_pages(extracted_output):
        all_text.append(f"--- Page {page['page_number']} ---")
        for line in page["lines"]:
            all_text.append(line["text"])

    doc_text = "\n".join(all_text)

    fields_list   = "\n".join(f"- {f}" for f in missing_fields)
    rejected_note = ""
    if rejected_fields:
        rejected_note = "\nDo NOT use these rejected values:\n"
        for f, v in rejected_fields.items():
            rejected_note += f"  - {f}: {repr(v)}\n"

    prompt = f"""Extract these fields from the certificate text below. Return JSON only, no explanation.
If unsure, set null. Do not invent values. Document may be in English or German.

Fields needed:
{fields_list}
{rejected_note}
Text:
{doc_text}

JSON:"""

    return prompt


# Asks the local Ollama model for missing values and accepts only validated,
# recognized fields that have not already been populated.
def llm_fallback(extracted_output, annotations, rejected_fields):
    target_fields = [
        "certificate_title", "supplier_name", "supplier_address",
        "customer_name", "customer_address", "certificate_number",
        "part_number_12nc", "revision", "part_description",
        "purchase_order_number", "quantity", "statement_of_conformance",
        "sign_off_date", "approver_identification", "approver_role",
    ]

    missing_fields = [f for f in target_fields if annotations.get(f) is None]

    if not missing_fields:
        print("No missing fields — LLM fallback not needed.")
        return annotations

    print(f"\nLLM fallback for {len(missing_fields)} fields: {missing_fields}")

    prompt = build_llm_prompt(extracted_output, missing_fields, rejected_fields)

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model":  "qwen3:8b",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0}
            },
            timeout=600
        )

        raw = response.json().get("response", "").strip()

        if "<think>" in raw:
            raw = raw.split("</think>")[-1].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        llm_result = json.loads(raw)

        for field, value in llm_result.items():
            if field not in target_fields:
                continue
            if annotations.get(field) is not None:
                continue
            if not value:
                continue

            validator = FIELD_VALIDATORS.get(field)
            if validator:
                try:
                    passed = validator(str(value))
                except Exception:
                    passed = False

                if passed:
                    annotations[field] = value
                    print(f"  LLM filled (validated): {field} = {value}")
                else:
                    print(f"  LLM value failed validation — skipped: {field} = {repr(value)}")
            else:
                annotations[field] = value
                print(f"  LLM filled: {field} = {value}")

    except json.JSONDecodeError as e:
        print(f"LLM returned invalid JSON: {e}")
    except Exception as e:
        print(f"LLM fallback error: {e}")

    return annotations


# ── Main entry point ──────────────────────────────────────────

# Runs the complete annotation pipeline: rule extraction, table enrichment,
# post-processing, validation, and optional local-LLM recovery.
def annotate(extracted_output, use_llm=True):
    print("\n── Step 1: Rule-based extraction ──")
    raw_pairs   = extract_raw_pairs(extracted_output)
    annotations = map_to_schema(raw_pairs)

    # Table extraction — relevant pages only
    annotations = extract_from_tables(
        get_relevant_tables(extracted_output),
        annotations
    )

    annotations["tables"] = get_relevant_tables(extracted_output)

    # ── Post-processing ───────────────────────────────────────

    # 12NC: only use regex as last resort
    if not annotations.get("part_number_12nc"):
        found = extract_12nc_by_regex(extracted_output)
        if found:
            annotations["part_number_12nc"] = found
            print(f"  12NC regex found: {found}")

    # Supplier address block
    all_lines = []
    for page in get_relevant_pages(extracted_output):
        all_lines.extend(page["lines"])

    if not annotations.get("supplier_address"):
        addr = extract_address_block(all_lines, "Supplier")
        if addr:
            annotations["supplier_address"] = addr

    if not annotations.get("customer_address"):
        addr = extract_address_block(all_lines, "Customer Address")
        if addr:
            annotations["customer_address"] = addr

    # Certificate title sanity check
    title = annotations.get("certificate_title")
    if title and not any(kw in title.lower() for kw in [
        "certificate of analysis", "certificate of conformance",
        "certificate of compliance", "certificate of conformity",
        "übereinstimmung", "werksbescheinigung"
    ]):
        annotations["certificate_title"] = None

    print("\n── Step 2: Validating rule-based results ──")
    annotations, rejected_fields = validate_annotations(annotations)

    if use_llm:
        print("\n── Step 3: LLM fallback ──")
        annotations = llm_fallback(extracted_output, annotations, rejected_fields)

    return annotations


# Finds the first standalone 12-digit part number on a relevant page.
def extract_12nc_by_regex(extracted_output):
    """12NC is exactly 12 consecutive digits."""
    pattern = re.compile(r'\b(\d{12})\b')
    for page in get_relevant_pages(extracted_output):
        for line in page["lines"]:
            match = pattern.search(line["text"])
            if match:
                return match.group(1)
    return None


# Locates an address label and joins nearby lines positioned to its right.
def extract_address_block(lines, label_text):
    """
    Finds a label then collects multi-line address below/right of it.
    """
    for i, line in enumerate(lines):
        if line["text"].strip().rstrip(":").lower() != label_text.lower():
            continue
        if not line["polygon"]:
            continue

        label_bbox = get_bbox(line["polygon"])
        parts = []

        for other in lines:
            if not other["polygon"]:
                continue
            other_bbox = get_bbox(other["polygon"])

            if other_bbox["left"] <= label_bbox["right"]:
                continue
            if other_bbox["top"] < label_bbox["top"] - 0.005:
                continue
            if other_bbox["top"] > label_bbox["top"] + 0.15:
                break

            parts.append((other_bbox["top"], other["text"]))

        if parts:
            parts.sort(key=lambda x: x[0])
            return ", ".join(p[1] for p in parts)

    return None
