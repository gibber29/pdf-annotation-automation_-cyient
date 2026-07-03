# PDF Annotation Automation

Extract structured fields from Certificate of Conformance (CoC) and Certificate of Analysis (CoA) PDFs, then generate an annotated PowerPoint report showing where the extracted values appear in the document.

## What it does

1. Sends a PDF to Azure AI Document Intelligence using the `prebuilt-layout` model.
2. Collects page text, tables, key-value pairs, confidence scores, and coordinates.
3. Maps extracted values into a canonical certificate schema using rules, aliases, table parsing, and validation.
4. Optionally asks a local Ollama model to recover fields that the deterministic extraction could not find.
5. Writes the raw Azure response and normalized annotations as JSON.
6. Renders the source PDF and creates an annotated `.pptx` report with PptxGenJS.

## Requirements

- Python 3.10 or newer
- Node.js and npm
- An Azure AI Document Intelligence resource
- Optional: [Ollama](https://ollama.com/) with the `qwen3:8b` model for missing-field fallback

## Installation

Clone the repository and enter the project directory:

```bash
git clone https://github.com/gibber29/pdf-annotation-automation_-cyient.git
cd pdf-annotation-automation_-cyient
```

Create and activate a Python virtual environment:

```bash
python -m venv venv
```

On Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
```

Install the Python and Node.js dependencies:

```bash
pip install -r requirements.txt
npm install
```

## Configuration

Create a `.env` file in the project root:

```env
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://YOUR-RESOURCE.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=YOUR_KEY
```

The `.env` file is ignored by Git. Never commit Azure credentials.

### Optional Ollama fallback

The annotation pipeline uses Ollama only when deterministic extraction leaves fields empty. To enable it, start Ollama locally and install the configured model:

```bash
ollama pull qwen3:8b
ollama serve
```

Ollama is expected at `http://localhost:11434`. If it is unavailable, the pipeline logs the fallback error and retains the rule-based results.

## Usage

Place the PDF to process in `inputs/`. Then update `PDF_PATH` in `app.py` to point to that file:

```python
PDF_PATH = "inputs/your-certificate.pdf"
```

Run the complete pipeline:

```bash
python app.py
```

The application creates the `outputs/` directory automatically and writes:

- `outputs/raw_azure_output.json` — Azure layout, table, and key-value data
- `outputs/annotations.json` — normalized certificate fields
- `outputs/report.pptx` — annotated presentation report

## Generate a report from existing JSON

If extraction has already been completed, the report can be regenerated independently:

```bash
python report_generator.py \
  --pdf inputs/your-certificate.pdf \
  --annotations outputs/annotations.json \
  --raw outputs/raw_azure_output.json \
  --out outputs/report.pptx
```

In PowerShell, use backticks instead of backslashes for multiline commands, or place the command on one line.

## Project structure

```text
app.py                 Orchestrates the complete pipeline
extractor.py           Azure Document Intelligence integration
annotation_engine.py   Extraction rules, validation, and Ollama fallback
field_mapper.py        Canonical field aliases and schema mapping
json_writer.py         JSON output helper
report_generator.py    PDF rendering and PowerPoint generation
inputs/                 Source PDFs
outputs/                Generated JSON and PowerPoint files
```

## Notes

- The extractor currently analyzes documents with Azure's `prebuilt-layout` model.
- The LLM fallback is configured in code for `qwen3:8b` and runs with temperature `0.0`.
- The report generator requires the local `pptxgenjs` package installed by `npm install`.
- Review generated output files before committing them, as they may contain document data.

## Security

Keep `.env`, cloud credentials, virtual environments, dependency directories, and cache files out of source control. If a credential is exposed, rotate it immediately in Azure.
