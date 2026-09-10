"""
app.py — Streamlit UI for PDF sanitization and shrinking.
"""

import streamlit as st
from pdf_helpers import sanitize_pdf, shrink_pdf, remove_images, pdf_info, COMPRESSION_PRESETS

st.set_page_config(page_title="ShrinkPDF", page_icon="📄")
st.title("📄 ShrinkPDF")

st.write("**Sanitize and compress PDF files**")

uploaded = st.file_uploader("Upload a PDF", type="pdf")

if uploaded:
    original_bytes = uploaded.read()
    info = pdf_info(original_bytes)

    st.info(f"**{uploaded.name}** — {info['pages']} page(s), {info['size_kb']} KB")

    st.subheader("Options")
    do_sanitize    = st.checkbox("Sanitize (remove locks, signatures, metadata)", value=True)
    do_remove_imgs = st.checkbox("Remove all images", value=False)
    do_shrink      = st.checkbox("Shrink (recompress streams & images)", value=True)

    if do_shrink:
        level = st.slider(
            "Compression level", 1, 5, 3,
            help="1 = light (best quality) · 3 = balanced · 5 = maximum (smallest file)"
        )
        q, d, s = COMPRESSION_PRESETS[level]
        st.caption(f"Level {level}: JPEG quality {q}, max {d} DPI, "
                   f"chroma subsampling {'4:1:1 (aggressive)' if s == 2 else '4:4:4 (full)'}")

    if st.button("Process PDF", type="primary"):
        result = original_bytes

        with st.spinner("Processing…"):
            if do_sanitize:
                result = sanitize_pdf(result)
            if do_remove_imgs:
                result = remove_images(result)
            elif do_shrink:
                result = shrink_pdf(result, image_quality=q, dpi=d, subsampling=s)

        out_info = pdf_info(result)
        saved_kb = info['size_kb'] - out_info['size_kb']
        saved_pct = saved_kb / info['size_kb'] * 100 if info['size_kb'] else 0

        if saved_kb > 0:
            st.success(f"Done! {info['size_kb']} KB → {out_info['size_kb']} KB "
                       f"(saved {saved_kb:.1f} KB / {saved_pct:.0f}%)")
        else:
            st.info(f"Done! Output: {out_info['size_kb']} KB "
                    f"(no size reduction — PDF was already compact)")

        out_name = uploaded.name.replace(".pdf", "_processed.pdf")
        st.download_button(
            label="⬇️ Download processed PDF",
            data=result,
            file_name=out_name,
            mime="application/pdf",
        )
