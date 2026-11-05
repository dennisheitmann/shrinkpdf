"""
shrinkpdf_app.py — Streamlit UI for PDF sanitization and shrinking.
"""

import os

import streamlit as st
from pdf_helpers import (
    is_valid_pdf, needs_password, unlock_pdf, sanitize_pdf, shrink_pdf,
    remove_images, linearize_pdf, pdf_info, count_images, COMPRESSION_PRESETS,
)


# Cached so the file is not re-parsed on every widget change. The cache key is
# the file bytes, so a new upload or a new password is computed again.
@st.cache_data(show_spinner="Checking PDF…")
def _is_encrypted(pdf_bytes: bytes) -> bool:
    return needs_password(pdf_bytes)


@st.cache_data(show_spinner="Unlocking PDF…")
def _unlock(pdf_bytes: bytes, password: str) -> bytes:
    return unlock_pdf(pdf_bytes, password)


@st.cache_data(show_spinner="Checking PDF…")
def _check_and_info(pdf_bytes: bytes) -> tuple:
    valid, err_msg = is_valid_pdf(pdf_bytes)
    info = pdf_info(pdf_bytes) if valid else None
    return valid, err_msg, info


@st.cache_data
def _count_images(pdf_bytes: bytes) -> int:
    return count_images(pdf_bytes)


# Used when Shrink is disabled but images are still re-encoded (grayscale only):
# (JPEG quality, max DPI, chroma subsampling 4:4:4)
GRAYSCALE_ONLY_PRESET = (90, 300, 0)

st.set_page_config(page_title="ShrinkPDF", page_icon="📄")
st.title("📄 ShrinkPDF")

st.write("**Sanitize and compress PDF files**")

uploaded = st.file_uploader("Upload a PDF", type="pdf")

if uploaded:
    original_bytes = uploaded.getvalue()

    # --- Password-protected PDF: unlock before any other check ---------------
    if _is_encrypted(original_bytes):
        password = st.text_input(
            "This PDF is password-protected. Enter the password to continue:",
            type="password",
        )
        if not password:
            st.stop()
        try:
            original_bytes = _unlock(original_bytes, password)
        except ValueError as e:
            st.error(f"⚠️ {e}")
            st.stop()

    # --- Corrupted PDF detection and info (cached) ---------------------------
    valid, err_msg, info = _check_and_info(original_bytes)
    if not valid:
        st.error(f"⚠️ Could not open PDF: {err_msg}")
        st.stop()
    st.info(f"**{uploaded.name}** — {info['pages']} page(s), {info['size_kb']} KB")

    # --- Options --------------------------------------------------------------
    st.subheader("Options")

    do_sanitize    = st.checkbox("Sanitize (remove locks, signatures, JavaScript and launch/submit/import actions)", value=True)
    keep_metadata  = st.checkbox("Keep metadata (author, title, creation date)",
                                 value=False, disabled=not do_sanitize,
                                 help="Only relevant when Sanitize is enabled")
    remove_embedded = st.checkbox("Remove embedded files (attachments)",
                                  value=False, disabled=not do_sanitize,
                                  help="Only relevant when Sanitize is enabled")
    flatten_forms   = st.checkbox("Flatten form fields and annotations",
                                  value=False, disabled=not do_sanitize,
                                  help="Bakes fields into the page content so they can no longer be edited. "
                                       "Only relevant when Sanitize is enabled")
    do_remove_imgs = st.checkbox("Remove all images", value=False)
    do_shrink      = st.checkbox("Shrink (recompress streams & images)", value=True,
                                 disabled=do_remove_imgs)
    do_grayscale   = st.checkbox("Convert images to grayscale", value=False,
                                 disabled=do_remove_imgs)
    # Resolve effective values — disabled widgets still return their last value in Streamlit
    eff_shrink    = do_shrink    and not do_remove_imgs
    eff_grayscale = do_grayscale and not do_remove_imgs
    do_linearize   = st.checkbox("Linearize output (fast web view)",
                                 help="Reorganizes the file so the first page can be shown "
                                      "before the whole file has downloaded. Does not make "
                                      "the file PDF/A-compliant.")

    # Always define q/d/s so they are never undefined when referenced below.
    # When Shrink is off (e.g. grayscale-only), use high-quality settings so
    # images aren't heavily degraded: (JPEG quality, max DPI, subsampling 4:4:4).
    q, d, s = GRAYSCALE_ONLY_PRESET
    if eff_shrink:
        level = st.slider(
            "Compression level", 1, 5, 3,
            help="1 = light (best quality) · 3 = balanced · 5 = maximum (smallest file)"
        )
        q, d, s = COMPRESSION_PRESETS[level]
        st.caption(f"Level {level}: JPEG quality {q}, images above {d} DPI at their "
                   f"printed size are downsampled, "
                   f"chroma subsampling {'4:1:1 (aggressive)' if s == 2 else '4:4:4 (full)'}")

    # --- Processing -----------------------------------------------------------
    if st.button("Process PDF", type="primary"):
        result = original_bytes
        # Only show the bar when an image step will report progress
        needs_images = do_remove_imgs or eff_shrink or eff_grayscale
        progress_bar = None
        if needs_images and _count_images(result) > 0:
            progress_bar = st.progress(0, text="Starting…")

        try:
            if do_sanitize:
                # No progress callback exists for sanitize, so use a spinner
                with st.spinner("Sanitizing…"):
                    result = sanitize_pdf(result,
                                          keep_metadata=keep_metadata,
                                          remove_embedded_files=remove_embedded,
                                          flatten_forms=flatten_forms)

            if do_remove_imgs:
                def _prog(done, total):
                    # The file is saved after the last image, so say so
                    text = (f"Removing images… {done}/{total}" if done < total
                            else "Saving…")
                    if progress_bar is not None:
                        progress_bar.progress(done / total, text=text)
                with st.spinner("Processing…"):
                    result = remove_images(result, linearize=do_linearize,
                                           progress_callback=_prog)

            elif eff_shrink or eff_grayscale:
                def _prog(done, total):
                    # The file is saved after the last image, so say so
                    text = (f"Processing images… {done}/{total}" if done < total
                            else "Saving…")
                    if progress_bar is not None:
                        progress_bar.progress(done / total, text=text)
                # The spinner shows activity even when the file has no images,
                # because the progress callback never fires in that case.
                with st.spinner("Processing…"):
                    result = shrink_pdf(
                        result,
                        image_quality=q, dpi=d, subsampling=s,
                        grayscale=eff_grayscale,
                        linearize=do_linearize,
                        progress_callback=_prog,
                    )

            elif do_linearize:
                # Linearization only, no image processing. No progress callback
                # exists here, so use a spinner.
                with st.spinner("Linearizing…"):
                    result = linearize_pdf(result)

        except Exception as e:
            if progress_bar is not None:
                progress_bar.empty()
            st.error(f"⚠️ Processing failed: {e}")
            st.stop()

        if progress_bar is not None:
            progress_bar.progress(1.0, text="Done!")

        out_info  = pdf_info(result)
        saved_kb  = info['size_kb'] - out_info['size_kb']
        saved_pct = saved_kb / info['size_kb'] * 100 if info['size_kb'] else 0

        if saved_kb > 0:
            st.success(f"Done! {info['size_kb']} KB → {out_info['size_kb']} KB "
                       f"(saved {saved_kb:.1f} KB / {saved_pct:.0f}%)")
        elif saved_kb == 0:
            st.info(f"Done! Output: {out_info['size_kb']} KB "
                    f"(no size change — PDF was already compact)")
        else:
            st.warning(f"Done, but the output is larger: {info['size_kb']} KB → "
                       f"{out_info['size_kb']} KB (+{-saved_kb:.1f} KB). "
                       f"Sanitizing or linearizing can add overhead to an "
                       f"already compact PDF. Consider keeping the original.")

        suffix    = "_linear" if do_linearize else "_processed"
        base, _ext = os.path.splitext(uploaded.name)
        out_name  = f"{base}{suffix}.pdf"
        st.download_button(
            label="⬇️ Download processed PDF",
            data=result,
            file_name=out_name,
            mime="application/pdf",
        )
