from extractor import extract_document
from annotation_engine import annotate
from json_writer import save_json
from report_generator import main as generate_report
import sys

PDF_PATH         = "inputs/_320314711044735_05.pdf"
RAW_OUTPUT_PATH  = "outputs/raw_azure_output.json"
ANNOTATIONS_PATH = "outputs/annotations.json"
REPORT_PATH      = "outputs/report.pptx"

print("Running Azure extraction...")
extracted = extract_document(PDF_PATH)

save_json(extracted, RAW_OUTPUT_PATH)
print("Raw Azure output saved.")

print("Running annotation engine...")
annotations = annotate(extracted, use_llm=True)

save_json(annotations, ANNOTATIONS_PATH)
print("Annotations saved.")

print("\nGenerating annotated PPT report...")
sys.argv = [
    "report_generator.py",
    "--pdf",         PDF_PATH,
    "--annotations", ANNOTATIONS_PATH,
    "--raw",         RAW_OUTPUT_PATH,
    "--out",         REPORT_PATH,
]
generate_report()

print(f"\nDone! Check {ANNOTATIONS_PATH} and {REPORT_PATH}")