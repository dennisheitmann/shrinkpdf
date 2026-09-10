"""
pdf_helpers.py — Sanitize and shrink PDF files.

sanitize_pdf  — strips usage-rights locks, signatures, XFA, JavaScript,
                metadata, and pushbutton fields so the PDF is clean and
                freely editable/saveable.

shrink_pdf    — recompresses streams and optionally downsamples images to
                reduce file size.

remove_images — replaces all images with white placeholders.
"""

import io
import warnings
import pikepdf


def _resolve(obj):
    """Resolve a pikepdf object, following indirect references."""
    if isinstance(obj, pikepdf.Object) and hasattr(obj, 'get_object'):
        return obj.get_object()
    return obj


def _iter_images(pdf, seen=None):
    """
    Yield every image XObject in the PDF exactly once.
    Recurses into:
    - Form XObjects (which have their own /Resources with nested images)
    - /SMask references on images (soft-mask alpha channels are images too)
    - /Pattern resources
    """
    if seen is None:
        seen = set()

    def _from_resources(resources):
        if resources is None:
            return
        resources = _resolve(resources)
        xobjects = resources.get('/XObject')
        if xobjects is not None:
            xobjects = _resolve(xobjects)
            for key in xobjects.keys():
                xobj = _resolve(xobjects[key])
                oid = xobj.objgen
                if oid in seen:
                    continue
                seen.add(oid)
                subtype = xobj.get('/Subtype')
                if subtype == pikepdf.Name('/Image'):
                    yield xobj
                    # Chase /SMask — it is itself an image XObject
                    smask = xobj.stream_dict.get('/SMask')
                    if smask is not None:
                        smask = _resolve(smask)
                        soid = smask.objgen
                        if soid not in seen:
                            seen.add(soid)
                            yield smask
                elif subtype == pikepdf.Name('/Form'):
                    # Recurse into Form XObject's own resources
                    yield from _from_resources(xobj.get('/Resources'))
        # Recurse into Pattern resources
        patterns = resources.get('/Pattern')
        if patterns is not None:
            patterns = _resolve(patterns)
            for key in patterns.keys():
                pat = _resolve(patterns[key])
                oid = pat.objgen
                if oid not in seen:
                    seen.add(oid)
                    yield from _from_resources(pat.get('/Resources'))

    for page in pdf.pages:
        yield from _from_resources(page.get('/Resources'))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize_pdf(pdf_bytes: bytes) -> bytes:
    """
    Strip everything that locks or bloats a PDF:
    - /Perms (UR3 usage-rights / certification lock)
    - /SigFlags and /Sig fields
    - /XFA (LiveCycle dynamic form definition)
    - /Names/JavaScript
    - /Metadata and /Info (authoring tool fingerprints)
    - Pushbutton action fields
    - RichText flag (Ff bit 23) on text fields
    """
    PUSHBUTTON_FF = 1 << 16
    RICHTEXT_FF   = 1 << 23

    def clean_fields(fields_arr):
        result = []
        for ref in fields_arr:
            f = _resolve(ref)
            ft = str(f.get('/FT', ''))
            ff = int(str(f.get('/Ff', '0')))
            if ft == '/Sig':
                continue
            if ft == '/Btn' and (ff & PUSHBUTTON_FF):
                continue
            if ft == '/Tx' and (ff & RICHTEXT_FF):
                f['/Ff'] = ff & ~RICHTEXT_FF
            if '/Kids' in f:
                f['/Kids'] = clean_fields(f['/Kids'])
            result.append(ref)
        return pikepdf.Array(result)

    with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        root = pdf.Root
        if '/Perms' in root:
            del root['/Perms']
        if '/AcroForm' in root:
            af = root['/AcroForm']
            for key in ('/XFA', '/SigFlags'):
                if key in af:
                    del af[key]
            if '/Fields' in af:
                af['/Fields'] = clean_fields(af['/Fields'])
        if '/Names' in root:
            names = root['/Names']
            if '/JavaScript' in names:
                del names['/JavaScript']
            if not list(names.keys()):
                del root['/Names']
        if '/Metadata' in root:
            del root['/Metadata']
        if '/Info' in pdf.trailer:
            del pdf.trailer['/Info']
        pdf.save(buf)
        return buf.getvalue()


# Compression presets: (image_quality, dpi, subsampling)
# subsampling: 0=4:4:4 (best), 2=4:1:1 (most aggressive chroma discard)
COMPRESSION_PRESETS = {
    1: (85, 150, 0),   # light
    2: (60, 120, 0),   # moderate
    3: (35, 96,  2),   # aggressive
    4: (15, 72,  2),   # very aggressive
    5: (5,  60,  2),   # maximum
}


def shrink_pdf(pdf_bytes: bytes, image_quality: int = 35, dpi: int = 96,
               subsampling: int = 2) -> bytes:
    """
    Reduce PDF file size by:
    - Recompressing all streams with flate (zlib)
    - Downsampling, chroma subsampling, and JPEG recompression of all images
    """
    from PIL import Image

    def _to_pil(image_obj):
        """Decode any PDF image to a Pillow RGB image, or return None."""
        cs = str(image_obj.stream_dict.get('/ColorSpace', ''))
        if 'CMYK' in cs and '/SMask' in image_obj.stream_dict:
            del image_obj.stream_dict['/SMask']
        try:
            return pikepdf.PdfImage(image_obj).as_pil_image().convert('RGB')
        except (pikepdf.PdfError, ValueError, NotImplementedError, OSError) as e:
            warnings.warn(f'pikepdf decoder failed, trying raw fallback: {e}', stacklevel=2)
        try:
            w  = int(image_obj.stream_dict.get('/Width',  0))
            h  = int(image_obj.stream_dict.get('/Height', 0))
            cs = str(image_obj.stream_dict.get('/ColorSpace', ''))
            if w < 1 or h < 1:
                return None
            mode = ('RGB'  if 'RGB'  in cs else
                    'CMYK' if 'CMYK' in cs else
                    'L'    if 'Gray' in cs else None)
            if mode is None:
                return None
            return Image.frombytes(mode, (w, h), image_obj.read_bytes()).convert('RGB')
        except (pikepdf.PdfError, ValueError, OSError) as e:
            warnings.warn(f'raw fallback also failed, skipping image: {e}', stacklevel=2)
            return None

    def _write_jpeg(image_obj, pil_img):
        with io.BytesIO() as out:
            pil_img.save(out, format='JPEG', quality=image_quality,
                         optimize=True, subsampling=subsampling)
            jpeg_bytes = out.getvalue()
        image_obj.stream_dict['/Filter'] = pikepdf.Name('/DCTDecode')
        image_obj.stream_dict['/ColorSpace'] = pikepdf.Name('/DeviceRGB')
        image_obj.stream_dict['/BitsPerComponent'] = 8
        image_obj.stream_dict['/Width'] = pil_img.width
        image_obj.stream_dict['/Height'] = pil_img.height
        for key in ('/DecodeParms', '/SMask', '/Mask'):
            if key in image_obj.stream_dict:
                del image_obj.stream_dict[key]
        image_obj.write(jpeg_bytes, filter=pikepdf.Name('/DCTDecode'))

    with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        for image_obj in _iter_images(pdf):
            pil_img = _to_pil(image_obj)
            if pil_img is None or pil_img.width < 50 or pil_img.height < 50:
                continue
            max_px = dpi * 9
            if pil_img.width > max_px or pil_img.height > max_px:
                scale = min(max_px / pil_img.width, max_px / pil_img.height)
                pil_img = pil_img.resize(
                    (max(1, int(pil_img.width * scale)), max(1, int(pil_img.height * scale))),
                    Image.LANCZOS,
                )
            _write_jpeg(image_obj, pil_img)
        pdf.save(buf, compress_streams=True, recompress_flate=True)
        return buf.getvalue()


def remove_images(pdf_bytes: bytes) -> bytes:
    """Replace all image XObjects with 1x1 white JPEG placeholders."""
    from PIL import Image as _Image
    with io.BytesIO() as blank_buf, _Image.new('RGB', (1, 1), (255, 255, 255)) as blank_img, \
         io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        blank_img.save(blank_buf, format='JPEG')
        blank_jpeg = blank_buf.getvalue()
        for image_obj in _iter_images(pdf):
            for key in ('/DecodeParms', '/SMask', '/Mask'):
                if key in image_obj.stream_dict:
                    del image_obj.stream_dict[key]
            image_obj.stream_dict['/Filter'] = pikepdf.Name('/DCTDecode')
            image_obj.stream_dict['/ColorSpace'] = pikepdf.Name('/DeviceRGB')
            image_obj.stream_dict['/BitsPerComponent'] = 8
            image_obj.stream_dict['/Width'] = 1
            image_obj.stream_dict['/Height'] = 1
            image_obj.write(blank_jpeg, filter=pikepdf.Name('/DCTDecode'))
        pdf.save(buf, compress_streams=True, recompress_flate=True)
        return buf.getvalue()


def pdf_info(pdf_bytes: bytes) -> dict:
    """Return basic stats about a PDF for display in the UI."""
    with io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        return {
            'pages': len(pdf.pages),
            'size_kb': round(len(pdf_bytes) / 1024, 1),
        }
