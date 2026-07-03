import os
from dotenv import load_dotenv
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential

load_dotenv()

client = DocumentIntelligenceClient(
    endpoint=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
    credential=AzureKeyCredential(
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    ),
)


# Sends a PDF to Azure's layout model and converts its pages, tables, and
# key-value results into the plain dictionary structure used by this project.
def extract_document(pdf_path):
    with open(pdf_path, "rb") as f:
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=f
        )

    result = poller.result()

    output = {
        "pages": [],
        "tables": [],
        "key_value_pairs": []
    }

    # ── Pages & Lines ──────────────────────────────────────────
    for page in result.pages:
        page_data = {
            "page_number": page.page_number,
            "lines": []
        }

        for line in page.lines:
            polygon = []
            if line.polygon:
                flat = line.polygon
                polygon = [
                    {"x": flat[i], "y": flat[i + 1]}
                    for i in range(0, len(flat), 2)
                ]

            page_data["lines"].append({
                "text":        line.content.strip(),
                "page_number": page.page_number,
                "polygon":     polygon
            })

        output["pages"].append(page_data)

    # ── Tables ────────────────────────────────────────────────
    for table in result.tables or []:
        matrix = [
            [""] * table.column_count
            for _ in range(table.row_count)
        ]
        for cell in table.cells:
            matrix[cell.row_index][cell.column_index] = cell.content

        page_ref = None
        if table.bounding_regions:
            page_ref = table.bounding_regions[0].page_number

        output["tables"].append({
            "page_number": page_ref,
            "data":        matrix
        })

    # ── Key-Value Pairs ───────────────────────────────────────
    for kv in result.key_value_pairs or []:
        key_text   = kv.key.content.strip()   if kv.key   else ""
        value_text = kv.value.content.strip() if kv.value else ""
        confidence = kv.confidence            if kv.confidence else 0.0

        output["key_value_pairs"].append({
            "key":        key_text,
            "value":      value_text,
            "confidence": confidence
        })

    return output
