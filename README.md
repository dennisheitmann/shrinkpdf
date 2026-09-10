# 📄 ShrinkPDF

A Streamlit web app to sanitize and compress PDF files. It remove locks, signatures, metadata, and embedded JavaScript, then shrink file size by recompressing all images and streams.

---

## Features

- **Sanitize** — strips everything that prevents clean editing or saving:
  - Usage-rights locks (`/Perms`, UR3)
  - Digital signatures and signature flags
  - XFA (LiveCycle dynamic form definitions)
  - Embedded JavaScript
  - Authoring metadata (`/Info`, `/Metadata`)
  - Pushbutton action fields (print/clear buttons)
  - RichText flags on text fields

- **Shrink** — reduces file size without changing the visual layout:
  - Recompresses all PDF streams with flate (zlib)
  - Converts all embedded images to RGB JPEG
  - Downsamples images above a target DPI cap
  - Applies chroma subsampling (4:1:1) at higher compression levels
  - Finds images everywhere they can hide: page resources, nested Form XObjects, Pattern resources, and soft-mask (`/SMask`) references

- **Remove images** — replaces all embedded images with 1×1 white placeholders, keeping the page layout intact

- **5 compression presets**:

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

1. Upload a PDF
2. Select the operations to apply (sanitize / shrink / remove images)
3. If shrinking, choose a compression level (1–5)
4. Click **Process PDF**
5. Download the result

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
- Remove images and shrink are mutually exclusive — if remove images is selected, shrink is skipped
- Images are decoded to RGB before recompression, handling RGB, CMYK, grayscale, and palette color spaces
- The page layout is never modified — only image data and stream compression are changed

---

## License

MIT
