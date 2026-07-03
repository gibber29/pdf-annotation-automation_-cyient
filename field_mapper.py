import copy


# ── Canonical schema ──────────────────────────────────────────────────────────

CANONICAL_SCHEMA = {
    "certificate_title":        None,
    "supplier_name":            None,
    "supplier_address":         None,
    "customer_name":            None,
    "customer_address":         None,
    "certificate_number":       None,
    "part_number_12nc":         None,
    "revision":                 None,
    "part_description":         None,
    "purchase_order_number":    None,
    "quantity":                 None,
    "measured_value":           None,
    "statement_of_conformance": None,
    "sign_off_date":            None,
    "approver_identification":  None,
    "approver_role":            None,
    "confirmation":             None,
    "tables":                   [],
}


# ── Aliases ───────────────────────────────────────────────────────────────────

FIELD_ALIASES = {
    "certificate_title": [
        "certificate of analysis",
        "certificate of conformance",
        "certificate of compliance",
        "certificate of conformity",
        "übereinstimmungszeugnis",
        "übereinstimmungserklärung",
        "werksbescheinigung",
        "konformitätszertifikat",
    ],
    "supplier_name": [
        "supplier",
        "supplier name",
        "vendor",
        "manufacturer",
        "manufactured by",
        "lieferant",
    ],
    "supplier_address": [
        "supplier address",
        "vendor address",
        "manufacturer address",
        "address",
    ],
    "customer_name": [
        "customer",
        "customer name",
        "consignee",
        "bill to",
        "sold to",
        "kunde",
        "company",
        "firma",
    ],
    "customer_address": [
        "customer address",
        "delivery address",
        "ship to",
    ],
    "certificate_number": [
        "certificate no",
        "certificate number",
        "cert no",
        "coa no",
        "coc no",
        "report number",
        "report no",
        "document number",
        "doc no",
        "ticket no",
        "unser auftrag",
        "auftragsnummer",
        "dokumentennummer",
        "lieferschein nummer",
        "nummer / datum",
        "coa no:",
    ],
    "part_number_12nc": [
        "12nc",
        "12 nc",
        "part number",
        "part no",
        "item number",
        "item no",
        "material number",
        "article number",
        "article no",
        "teilenummer",
        "philips part number",
        "philips p/n",
        "p/n",
        "kundenmaterial-nr",
        "kundenmaterial-nr.",
        "ihre artikelnummer",
    ],
    "revision": [
        "revision",
        "rev",
        "version",
        "issue",
        "rev no",
        "revision no",
        "cust item rev",
    ],
    "part_description": [
        "part description",
        "description",
        "product description",
        "item description",
        "material description",
        "article description",
        "bezeichnung",
        "artikelbezeichnung",
    ],
    "purchase_order_number": [
        "purchase order",
        "purchase order number",
        "purchase order no",
        "po number",
        "po no",
        "po#",
        "order number",
        "order no",
        "bestellnummer",
        "customer purchase order",
        "customer purchase order no",
        "ihre bestellung",
        "kundenbestellung",
        "bestellung",
        "kundenbestellung / datum",
    ],
    "quantity": [
        "quantity",
        "qty",
        "amount",
        "menge",
        "quantity shipped",
        "quantity in shipment",
        "ship quantity",
        "liefermenge",
        "liefermenge:",
    ],
    "measured_value": [
        "measured value",
        "measured values",
        "measurement",
        "test result",
        "test results",
        "messwert",
    ],
    "statement_of_conformance": [
        "statement of conformance",
        "conformance statement",
        "declaration of conformance",
        "we hereby certify",
        "we certify",
        "this is to certify",
        "konformitätserklärung",
        "hiermit wird bestätigt",
    ],
    "sign_off_date": [
        "sign off date",
        "sign-off date",
        "date",
        "issue date",
        "release date",
        "datum",
        "ship date",
        "manufacture date",
        "order date",
        "produktionsdatum",
    ],
    "approver_identification": [
        "approver identification",
        "approver id",
        "approved by",
        "authorised by",
        "authorized by",
        "signatory",
        "verified by",
        "name",
    ],
    "approver_role": [
        "approver role",
        "role",
        "position",
        "function",
        "funktion",
        "reason",
        "title",
    ],
    "confirmation": [
        "confirmation",
        "confirm",
        "verified by",
        "checked by",
        "validated by",
        "signature",
    ],
}


# ── Matching logic ────────────────────────────────────────────────────────────

# Normalizes a field label for case-insensitive alias comparison.
def normalize(text):
    return text.lower().strip().rstrip(":").strip()


# Maps a document label to a canonical schema field using exact aliases first,
# then a more permissive substring match as a fallback.
def find_canonical_field(label):
    label_norm = normalize(label)

    # Exact match
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if label_norm == alias:
                return field

    # Substring match fallback
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in label_norm or label_norm in alias:
                return field

    return None


# Resolves extracted label-value pairs into a fresh canonical schema, giving
# higher-priority extraction methods the first opportunity to fill each field.
def map_to_schema(raw_pairs):
    schema = copy.deepcopy(CANONICAL_SCHEMA)

    source_priority = {
        "header_detection":       0,
        "native_kv":              1,
        "inline_colon":           2,
        "date_name_detection":    3,
        "address_block_detection":4,
        "spatial_alias":          5,
        "line_colon":             6,
        "free_text_detection":    7,
    }

    ordered = sorted(
        raw_pairs,
        key=lambda x: source_priority.get(x.get("source"), 99)
    )

    for pair in ordered:
        field = find_canonical_field(pair["label"])

        if not field or field == "tables":
            continue

        # certificate_title must only come from header_detection
        if field == "certificate_title" and pair.get("source") != "header_detection":
            continue

        if schema.get(field) is None:
            schema[field] = pair["value"]

    return schema


# Fills still-empty schema fields from table rows and treats an applicable
# third column as a possible revision value.
def extract_from_tables(tables, schema):
    for table in tables:
        data = table.get("data", [])

        for row in data:
            if len(row) < 2:
                continue

            label = row[0].strip()
            value = row[1].strip()

            if not label or not value:
                continue

            field = find_canonical_field(label)
            if field and field != "tables" and schema.get(field) is None:
                schema[field] = value

            # 3-column tables: third column often holds revision
            if len(row) >= 3 and row[2].strip():
                if field in ("part_number_12nc", "part_description"):
                    rev_val = row[2].strip()
                    if (
                        schema.get("revision") is None
                        and rev_val.lower() not in ("not applicable", "n/a", "")
                    ):
                        schema["revision"] = rev_val

    return schema
