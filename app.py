"""
app.py — Streamlit UI for PDF sanitization and shrinking.
"""

import io
import pikepdf
import streamlit as st
from pdf_helpers import (
    is_valid_pdf, sanitize_pdf, shrink_pdf, remove_images,
    pdf_info, COMPRESSION_PRESETS,
)

st.set_page_config(page_title="ShrinkPDF", page_icon="📄")
st.title("📄 ShrinkPDF")

st.write("**Sanitize and compress PDF files**")

uploaded = st.file_uploader("Upload a PDF", type="pdf")

if uploaded:
    original_bytes = uploaded.read()

    # --- Corrupted PDF detection -------------------------------------------
    valid, err_msg = is_valid_pdf(original_bytes)
    if not valid:
        st.error(f"⚠️ Could not open PDF: {err_msg}")
        st.stop()

    info = pdf_info(original_bytes)
    st.info(f"**{uploaded.name}** — {info['pages']} page(s), {info['size_kb']} KB")

    # --- Options --------------------------------------------------------------
    st.subheader("Options")

    do_sanitize    = st.checkbox("Sanitize (remove locks, signatures, JavaScript)", value=True)
    keep_metadata  = st.checkbox("Keep metadata (author, title, creation date)",
                                 value=False, disabled=not do_sanitize,
                                 help="Only relevant when Sanitize is enabled")
    do_remove_imgs = st.checkbox("Remove all images", value=False)
    do_shrink      = st.checkbox("Shrink (recompress streams & images)", value=True,
                                 disabled=do_remove_imgs)
    do_grayscale   = st.checkbox("Convert images to grayscale", value=False,
                                 disabled=do_remove_imgs)
    # Resolve effective values — disabled widgets still return their last value in Streamlit
    eff_shrink    = do_shrink    and not do_remove_imgs
    eff_grayscale = do_grayscale and not do_remove_imgs
    do_pdfa        = st.checkbox("Linearize output (PDF/A-compatible)",
                                 help="Optimizes the file for fast web viewing and archival")

    # Always define q/d/s so they are never undefined when referenced below
    q, d, s = COMPRESSION_PRESETS[3]
    if eff_shrink:
        level = st.slider(
            "Compression level", 1, 5, 3,
            help="1 = light (best quality) · 3 = balanced · 5 = maximum (smallest file)"
        )
        q, d, s = COMPRESSION_PRESETS[level]
        st.caption(f"Level {level}: JPEG quality {q}, max {d} DPI, "
                   f"chroma subsampling {'4:1:1 (aggressive)' if s == 2 else '4:4:4 (full)'}")

    # --- Processing -----------------------------------------------------------
    if st.button("Process PDF", type="primary"):
        result = original_bytes
        progress_bar = st.progress(0, text="Starting…")

        try:
            if do_sanitize:
                progress_bar.progress(0, text="Sanitizing…")
                result = sanitize_pdf(result, keep_metadata=keep_metadata)

            if do_remove_imgs:
                def _prog(done, total):
                    progress_bar.progress(done / total,
                                          text=f"Removing images… {done}/{total}")
                result = remove_images(result, progress_callback=_prog)

            elif eff_shrink or eff_grayscale:
                def _prog(done, total):
                    progress_bar.progress(done / total,
                                          text=f"Processing images… {done}/{total}")
                result = shrink_pdf(
                    result,
                    image_quality=q, dpi=d, subsampling=s,
                    grayscale=eff_grayscale,
                    save_pdfa=do_pdfa,
                    progress_callback=_prog,
                )

            elif do_pdfa:
                # PDF/A linearization only, no image processing
                with io.BytesIO() as buf, io.BytesIO(result) as src, pikepdf.open(src) as pdf:
                    pdf.save(buf, linearize=True)
                    result = buf.getvalue()

        except Exception as e:
            progress_bar.empty()
            st.error(f"⚠️ Processing failed: {e}")
            st.stop()

        progress_bar.progress(1.0, text="Done!")

        out_info  = pdf_info(result)
        saved_kb  = info['size_kb'] - out_info['size_kb']
        saved_pct = saved_kb / info['size_kb'] * 100 if info['size_kb'] else 0

        if saved_kb > 0:
            st.success(f"Done! {info['size_kb']} KB → {out_info['size_kb']} KB "
                       f"(saved {saved_kb:.1f} KB / {saved_pct:.0f}%)")
        else:
            st.info(f"Done! Output: {out_info['size_kb']} KB "
                    f"(no size reduction — PDF was already compact)")

        suffix    = "_pdfa" if do_pdfa else "_processed"
        out_name  = uploaded.name.replace(".pdf", f"{suffix}.pdf")
        st.download_button(
            label="⬇️ Download processed PDF",
            data=result,
            file_name=out_name,
            mime="application/pdf",
        )
