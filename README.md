# 📄 ShrinkPDF

A Streamlit web app to sanitize and compress PDF files. It removes locks, signatures, metadata and active content such as JavaScript, then reduces file size by recompressing streams and re-encoding or downsampling images where that makes them smaller.

---

## Features

- **Sanitize** strips everything that prevents clean editing or saving:
  - Usage-rights locks (`/Perms`, UR3)
  - Digital signatures and signature flags
  - XFA (LiveCycle dynamic form definitions)
  - JavaScript in the document, on pages, on annotations and on form fields
  - Actions that run programs, send form data or import data (`/Launch`, `/SubmitForm`, `/ImportData`). Links that used them stay but do nothing when clicked
  - Authoring metadata (`/Info`, `/Metadata`), optional: can be kept
  - Signature and pushbutton widgets, and RichText flags on text fields
  - Optional: remove embedded files (attachments)
  - Optional: flatten form fields and annotations into the page so they can no longer be edited

- **Shrink** reduces file size without changing the visual layout:
  - Recompresses all PDF streams with flate (zlib)
  - Re-encodes images as JPEG, keeping the original when the JPEG would not be smaller
  - Downsamples images whose resolution at their printed size exceeds the target DPI
  - Leaves stencil masks, soft masks and colour-key masks untouched, and keeps transparency
  - Applies chroma subsampling (4:1:1) at higher compression levels
  - Optionally converts images to grayscale
  - Finds images in page resources, nested Form XObjects, patterns, soft masks and annotation appearances

- **Remove images** replaces all embedded images with 1×1 white placeholders, keeping the page layout intact

- **Linearize output** reorganizes the file for fast web viewing. This does not make the file PDF/A-compliant

- **Password-protected PDFs:** enter the password when prompted. The output is saved without encryption

- **5 compression presets:**

  | Level | JPEG quality | Max DPI | Chroma subsampling |
  |-------|-------------|---------|-------------------|
  | 1 | 85 | 150 | 4:4:4 (full) |
  | 2 | 60 | 120 | 4:4:4 (full) |
  | 3 | 35 | 96  | 4:1:1 (aggressive) |
  | 4 | 15 | 72  | 4:1:1 (aggressive) |
  | 5 | 5  | 60  | 4:1:1 (aggressive) |

---

## Requirements

- Python 3.11+
- See `requirements.txt` for full pinned dependencies

Core dependencies:

```
streamlit
pikepdf
Pillow
```

---

## Installation

```bash
git clone https://github.com/dennisheitmann/shrinkpdf.git
cd shrinkpdf

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

---

## Usage

```bash
streamlit run app.py
```

Then open [http://localhost:8501](http://localhost:8501) in your browser.

1. Upload a PDF. If it is password-protected, enter the password when prompted
2. Select the operations to apply (sanitize / shrink / remove images)
3. Optionally keep metadata, remove attachments, flatten form fields, convert to grayscale, or linearize the output
4. If shrinking, choose a compression level (1–5)
5. Click **Process PDF**
6. Download the result

---

## Project structure

```
shrinkpdf/
├── app.py             # Streamlit UI
├── pdf_helpers.py     # Core PDF processing logic
└── requirements.txt
```

---

## Notes

- Operations are applied in order: **sanitize → remove images → shrink**
- Remove images and shrink are mutually exclusive. If remove images is selected, shrink is skipped
- Linearization is still applied when remove images is selected
- Images smaller than 50×50 px are skipped during shrink
- Images are decoded to RGB before re-encoding. CMYK is converted without an ICC profile, so colours can shift slightly
- Sanitize changes what is visible only where it removes widgets or flattens form fields. Shrink and Linearize do not change the page layout
- Inline images (embedded directly in a page's content stream) are not processed

---

## License

MIT
