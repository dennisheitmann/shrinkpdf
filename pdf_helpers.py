"""
pdf_helpers.py — Sanitize and shrink PDF files.

sanitize_pdf  — strips usage-rights locks, signatures, XFA, JavaScript,
                launch/submit/import actions, metadata, and pushbutton
                fields so the PDF is clean and freely editable/saveable.

shrink_pdf    — recompresses streams and optionally downsamples images to
                reduce file size.

remove_images — replaces all images with white placeholders.
"""

import io
import warnings
import zlib
import pikepdf
from pikepdf import PageCopyWarning

warnings.filterwarnings('ignore', category=PageCopyWarning)


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
    - annotation appearance streams (/AP /N, /D and /R)
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
                    smask = xobj.stream_dict.get('/SMask')
                    if smask is not None:
                        smask = _resolve(smask)
                        soid = smask.objgen
                        if soid not in seen:
                            seen.add(soid)
                            yield smask
                elif subtype == pikepdf.Name('/Form'):
                    yield from _from_resources(xobj.get('/Resources'))
        patterns = resources.get('/Pattern')
        if patterns is not None:
            patterns = _resolve(patterns)
            for key in patterns.keys():
                pat = _resolve(patterns[key])
                oid = pat.objgen
                if oid not in seen:
                    seen.add(oid)
                    yield from _from_resources(pat.get('/Resources'))

    def _from_annotations(page):
        # Annotation appearance streams (stamps, widgets, etc.) are Form XObjects
        # with their own /Resources, and can contain images.
        annots = page.obj.get('/Annots')
        if annots is None:
            return
        for ref in annots:
            annot = _resolve(ref)
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            ap = annot.get('/AP')
            if ap is None:
                continue
            ap = _resolve(ap)
            for key in ('/N', '/D', '/R'):
                entry = ap.get(key)
                if entry is None:
                    continue
                entry = _resolve(entry)
                if isinstance(entry, pikepdf.Stream):
                    yield from _from_resources(entry.get('/Resources'))
                elif isinstance(entry, pikepdf.Dictionary):
                    # Appearance state dictionary: one stream per state
                    for state in entry.keys():
                        sub = _resolve(entry[state])
                        if isinstance(sub, pikepdf.Stream):
                            yield from _from_resources(sub.get('/Resources'))

    for page in pdf.pages:
        yield from _from_resources(page.get('/Resources'))
        yield from _from_annotations(page)


_MAX_FORM_DEPTH = 16


def _image_placements(pdf) -> dict:
    """
    Map image objgen -> (max_width_pt, max_height_pt): the largest size at
    which each image is drawn on any page, in PDF points (1/72 inch).

    Tracks the current transformation matrix through q/Q/cm operators and
    recurses into Form XObjects. Images that are never drawn through a page
    content stream (patterns, inline images, unused objects) are not listed.
    """
    placements = {}

    def walk(stream_obj, resources, ctm, stack):
        if len(stack) > _MAX_FORM_DEPTH:
            return
        try:
            instructions = pikepdf.parse_content_stream(stream_obj)
        except (pikepdf.PdfError, ValueError, TypeError):
            return
        xobjects = None
        if resources is not None:
            xobjects = _resolve(_resolve(resources).get('/XObject'))
        saved = []
        for instr in instructions:
            op = str(getattr(instr, 'operator', ''))
            if op == 'q':
                saved.append(ctm)
            elif op == 'Q':
                if saved:
                    ctm = saved.pop()
            elif op == 'cm':
                try:
                    ctm = pikepdf.Matrix(*[float(x) for x in instr.operands]) @ ctm
                except (TypeError, ValueError):
                    pass
            elif op == 'Do' and xobjects is not None and len(instr.operands) == 1:
                name = str(instr.operands[0])
                if name not in xobjects:
                    continue
                xobj = _resolve(xobjects[name])
                subtype = xobj.get('/Subtype')
                if subtype == pikepdf.Name('/Image'):
                    w_pt = (ctm.a ** 2 + ctm.b ** 2) ** 0.5
                    h_pt = (ctm.c ** 2 + ctm.d ** 2) ** 0.5
                    old_w, old_h = placements.get(xobj.objgen, (0.0, 0.0))
                    placements[xobj.objgen] = (max(old_w, w_pt), max(old_h, h_pt))
                elif subtype == pikepdf.Name('/Form') and xobj.objgen not in stack:
                    form_ctm = ctm
                    if '/Matrix' in xobj:
                        try:
                            form_ctm = pikepdf.Matrix(*[float(x) for x in xobj.Matrix]) @ ctm
                        except (TypeError, ValueError):
                            pass
                    # A form without its own /Resources uses the caller's
                    form_res = xobj.get('/Resources', resources)
                    walk(xobj, form_res, form_ctm, stack + (xobj.objgen,))

    for page in pdf.pages:
        walk(page, page.get('/Resources'), pikepdf.Matrix(), ())

    return placements


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_valid_pdf(pdf_bytes: bytes) -> tuple[bool, str]:
    """
    Try to open the PDF and read its pages.
    Returns (True, '') on success or (False, error_message) on failure.
    """
    try:
        with io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
            _ = len(pdf.pages)
        return True, ''
    except pikepdf.PdfError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001 — intentional catch-all for upload validation
        return False, str(e)


def _soft_mask_ids(images) -> set:
    """objgens of all images that are used as the /SMask of another image."""
    ids = set()
    for img in images:
        smask = img.stream_dict.get('/SMask')
        if smask is not None:
            ids.add(_resolve(smask).objgen)
    return ids


def _stored_size(image_obj) -> int:
    """
    Approximate size in bytes of the image stream in the saved file.
    Streams without a filter are flate-compressed on save, so their
    compressed size is estimated instead of the raw length.
    """
    raw = image_obj.read_raw_bytes()
    if '/Filter' in image_obj.stream_dict:
        return len(raw)
    return len(zlib.compress(raw, 6))


def _reencodable(image_obj, smask_ids) -> bool:
    """
    True if the image can safely be re-encoded as a lossy RGB/Gray JPEG.
    Skips soft masks themselves, stencil masks, colour-key masks (their pixel
    ranges would no longer match after lossy compression) and images whose
    soft mask has a /Matte entry (pre-blended colours).
    """
    d = image_obj.stream_dict
    if image_obj.objgen in smask_ids:
        return False
    if bool(d.get('/ImageMask', False)):
        return False
    if isinstance(d.get('/Mask'), pikepdf.Array):
        return False
    smask = d.get('/SMask')
    if smask is not None and '/Matte' in _resolve(smask).stream_dict:
        return False
    return True


_MAX_ACTION_DEPTH = 32


# Action types that run code, send data or change state outside the reader.
# /URI and /GoTo are harmless and are kept.
_DROPPED_ACTIONS = {
    pikepdf.Name('/JavaScript'),
    pikepdf.Name('/Launch'),
    pikepdf.Name('/SubmitForm'),
    pikepdf.Name('/ImportData'),
}


def _is_dropped_action(action) -> bool:
    """True if `action` is an action dictionary of a type in _DROPPED_ACTIONS."""
    return (isinstance(action, pikepdf.Dictionary)
            and action.get('/S') in _DROPPED_ACTIONS)


def _scrub_action(holder, key, depth=0):
    """
    Remove a dropped action (see _DROPPED_ACTIONS) stored at holder[key]. Also
    removes dropped actions hidden in the /Next chain of an otherwise harmless
    action.
    """
    if depth > _MAX_ACTION_DEPTH or key not in holder:
        return
    action = holder[key]
    if _is_dropped_action(action):
        del holder[key]
        return
    if not isinstance(action, pikepdf.Dictionary):
        return  # e.g. /OpenAction given as a destination array
    nxt = action.get('/Next')
    if isinstance(nxt, pikepdf.Array):
        kept = [a for a in nxt if not _is_dropped_action(a)]
        if len(kept) != len(nxt):
            action['/Next'] = pikepdf.Array(kept)
        for a in kept:
            if isinstance(a, pikepdf.Dictionary):
                _scrub_action(a, '/Next', depth + 1)
    elif nxt is not None:
        _scrub_action(action, '/Next', depth + 1)


def _strip_js_from(obj, action_key='/A'):
    """Remove a dropped action from obj's action and delete all its /AA triggers."""
    _scrub_action(obj, action_key)
    if '/AA' in obj:
        del obj['/AA']


def _belongs_to(annot, removed) -> bool:
    """True if the annotation, or one of its /Parent fields, was removed."""
    node, depth = annot, 0
    while isinstance(node, pikepdf.Dictionary) and depth < 16:
        if node.objgen in removed:
            return True
        node = node.get('/Parent')
        depth += 1
    return False


def sanitize_pdf(pdf_bytes: bytes, keep_metadata: bool = False,
                 remove_embedded_files: bool = False,
                 flatten_forms: bool = False) -> bytes:
    """
    Strip everything that locks or bloats a PDF:
    - /Perms (UR3 usage-rights / certification lock)
    - /SigFlags and /Sig fields (including their widget annotations on pages)
    - /XFA (LiveCycle dynamic form definition)
    - JavaScript: /Names/JavaScript, /OpenAction, document/page/annotation/field
      /AA triggers, and /A actions of type /JavaScript, /Launch, /SubmitForm or
      /ImportData (also inside /Next chains)
    - /Metadata and /Info (authoring tool fingerprints) — unless keep_metadata=True
    - Pushbutton fields (including their widget annotations on pages)
    - RichText flag (Ff bit 23) on text fields

    remove_embedded_files: also remove /Names/EmbeddedFiles, root /AF and
                           FileAttachment annotations
    flatten_forms:         bake form fields and other annotations into the page
                           content (uses pikepdf's flatten_annotations)
    """
    PUSHBUTTON_FF = 1 << 16
    RICHTEXT_FF   = 1 << 23

    removed = set()  # objgens of dropped fields and all their kids/widgets

    def collect(f):
        if f.objgen != (0, 0):
            removed.add(f.objgen)
        for kid in f.get('/Kids', []):
            collect(_resolve(kid))

    def clean_fields(fields_arr, inh_ft='', inh_ff=0):
        result = []
        for ref in fields_arr:
            f = _resolve(ref)
            # /FT and /Ff are inheritable from parent fields
            ft = str(f.get('/FT', inh_ft))
            ff = int(str(f.get('/Ff', inh_ff)))
            if ft == '/Sig' or (ft == '/Btn' and (ff & PUSHBUTTON_FF)):
                collect(f)
                continue
            if ft == '/Tx' and (ff & RICHTEXT_FF):
                f['/Ff'] = ff & ~RICHTEXT_FF
            _strip_js_from(f)
            if '/Kids' in f:
                f['/Kids'] = clean_fields(f['/Kids'], ft, ff)
            result.append(ref)
        return pikepdf.Array(result)

    with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        root = pdf.Root
        if '/Perms' in root:
            del root['/Perms']

        # Document-level JavaScript: /OpenAction and /AA triggers
        _strip_js_from(root, '/OpenAction')

        if '/AcroForm' in root:
            af = root['/AcroForm']
            # /CO (calculation order) only matters for JavaScript calculations
            for key in ('/XFA', '/SigFlags', '/CO'):
                if key in af:
                    del af[key]
            if '/Fields' in af:
                af['/Fields'] = clean_fields(af['/Fields'])

        # Pages: drop orphaned widgets, page-level /AA, annotation JavaScript
        for page in pdf.pages:
            page_obj = page.obj
            if '/AA' in page_obj:
                del page_obj['/AA']
            annots = page_obj.get('/Annots')
            if annots is None:
                continue
            kept = []
            for ref in annots:
                annot = _resolve(ref)
                if isinstance(annot, pikepdf.Dictionary):
                    if _belongs_to(annot, removed):
                        continue
                    if (remove_embedded_files
                            and annot.get('/Subtype') == pikepdf.Name('/FileAttachment')):
                        continue
                    _strip_js_from(annot)
                kept.append(ref)
            page_obj['/Annots'] = pikepdf.Array(kept)

        if '/Names' in root:
            names = root['/Names']
            if '/JavaScript' in names:
                del names['/JavaScript']
            if remove_embedded_files and '/EmbeddedFiles' in names:
                del names['/EmbeddedFiles']
            if not list(names.keys()):
                del root['/Names']
        if remove_embedded_files and '/AF' in root:
            del root['/AF']

        if not keep_metadata:
            if '/Metadata' in root:
                del root['/Metadata']
            if '/Info' in pdf.trailer:
                del pdf.trailer['/Info']

        if flatten_forms:
            # Without /NeedAppearances, fields that lack an appearance stream
            # are skipped: they would stay on the page and their value would
            # be lost when /AcroForm is removed below.
            if '/AcroForm' in root:
                root['/AcroForm']['/NeedAppearances'] = True
            pdf.generate_appearance_streams()
            pdf.flatten_annotations('all')
            af = root.get('/AcroForm')
            if af is not None and len(af.get('/Fields', [])) == 0:
                del root['/AcroForm']

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
               subsampling: int = 2, grayscale: bool = False,
               linearize: bool = False,
               progress_callback=None) -> bytes:
    """
    Reduce PDF file size by:
    - Recompressing all streams with flate (zlib)
    - Downsampling, chroma subsampling, and JPEG recompression of all images

    dpi:               an image is downsampled only if its resolution at its
                       placed size on the page exceeds this value. If an image
                       is drawn several times, the largest placement counts.
                       Images with no known placement (e.g. only used in
                       patterns) are re-encoded but never resized.
    progress_callback: optional callable(current, total) for progress reporting
    grayscale:         convert all images to grayscale before encoding
    linearize:         save with linearization (optimized for fast web view)
    """
    from PIL import Image

    def _decode_without_masks(image_obj):
        """
        Decode the pixels only. The masks are hidden from pikepdf while
        decoding (it would otherwise try to merge them into an alpha channel,
        which warns for CMYK and is discarded by convert('RGB') anyway) and
        are always restored, so they stay attached to the image.
        """
        d = image_obj.stream_dict
        hidden = {k: d[k] for k in ('/SMask', '/Mask') if k in d}
        for k in hidden:
            del d[k]
        try:
            return pikepdf.PdfImage(image_obj).as_pil_image().convert('RGB')
        finally:
            for k, v in hidden.items():
                d[k] = v

    def _to_pil(image_obj):
        try:
            return _decode_without_masks(image_obj)
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
        target_mode = 'L' if grayscale else 'RGB'
        color_space  = pikepdf.Name('/DeviceGray') if grayscale else pikepdf.Name('/DeviceRGB')
        pil_img = pil_img.convert(target_mode)
        # Grayscale JPEG must use subsampling=0
        sub = 0 if grayscale else subsampling
        with io.BytesIO() as out:
            pil_img.save(out, format='JPEG', quality=image_quality,
                         optimize=True, subsampling=sub)
            jpeg_bytes = out.getvalue()
        # Keep the original if re-encoding doesn't make it smaller. Grayscale
        # is an explicit user choice, so it is applied regardless of size.
        if not grayscale and len(jpeg_bytes) >= _stored_size(image_obj):
            return False
        image_obj.stream_dict['/Filter'] = pikepdf.Name('/DCTDecode')
        image_obj.stream_dict['/ColorSpace'] = color_space
        image_obj.stream_dict['/BitsPerComponent'] = 8
        image_obj.stream_dict['/Width'] = pil_img.width
        image_obj.stream_dict['/Height'] = pil_img.height
        # Keep /SMask and /Mask (transparency). /Decode described the old
        # pixel data and would be wrong for the new JPEG.
        for key in ('/DecodeParms', '/Decode'):
            if key in image_obj.stream_dict:
                del image_obj.stream_dict[key]
        image_obj.write(jpeg_bytes, filter=pikepdf.Name('/DCTDecode'))
        return True

    with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        images = list(_iter_images(pdf))
        placements = _image_placements(pdf)
        smask_ids = _soft_mask_ids(images)
        total  = len(images)
        done   = 0

        for image_obj in images:
            # One bad image must not abort the whole file. The image is left
            # as it was and the skip is reported as a console warning only,
            # because the UI does not display warnings.
            try:
                # Masks and images that depend on masks are left untouched
                pil_img = _to_pil(image_obj) if _reencodable(image_obj, smask_ids) else None
                if pil_img is not None and pil_img.width >= 50 and pil_img.height >= 50:
                    # Downsample only if the image exceeds `dpi` at its placed size.
                    # Images with no known placement are re-encoded but not resized.
                    placed = placements.get(image_obj.objgen)
                    if placed is not None:
                        # floor of 1 pt avoids collapsing images that are drawn tiny
                        target_w = max(placed[0], 1.0) / 72 * dpi
                        target_h = max(placed[1], 1.0) / 72 * dpi
                        scale = min(target_w / pil_img.width, target_h / pil_img.height)
                        if scale < 1:
                            pil_img = pil_img.resize(
                                (max(1, int(pil_img.width * scale)),
                                 max(1, int(pil_img.height * scale))),
                                Image.LANCZOS,
                            )
                    _write_jpeg(image_obj, pil_img)
            except Exception as e:  # noqa: BLE001 — skip this image, keep the rest
                warnings.warn(
                    f'skipped image {image_obj.objgen} that could not be re-encoded: {e}',
                    stacklevel=2)
            done += 1
            if progress_callback and total > 0:
                progress_callback(done, total)

        pdf.save(buf, compress_streams=True, recompress_flate=True,
                 linearize=linearize)
        return buf.getvalue()


def remove_images(pdf_bytes: bytes, linearize: bool = False,
                  progress_callback=None) -> bytes:
    """
    Replace all image XObjects with 1x1 white JPEG placeholders.

    linearize: save with linearization (optimized for fast web view)
    """
    from PIL import Image as _Image

    with io.BytesIO() as blank_buf, _Image.new('RGB', (1, 1), (255, 255, 255)) as blank_img, \
         io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        blank_img.save(blank_buf, format='JPEG')
        blank_jpeg = blank_buf.getvalue()
        images = list(_iter_images(pdf))
        total  = len(images)
        done   = 0
        for image_obj in images:
            for key in ('/DecodeParms', '/Decode', '/SMask', '/Mask'):
                if key in image_obj.stream_dict:
                    del image_obj.stream_dict[key]
            if bool(image_obj.stream_dict.get('/ImageMask', False)):
                # Stencil masks must stay 1-bit with no colour space. One
                # sample of 1 means "do not paint", so the stencil draws nothing.
                for key in ('/Filter', '/ColorSpace'):
                    if key in image_obj.stream_dict:
                        del image_obj.stream_dict[key]
                image_obj.stream_dict['/BitsPerComponent'] = 1
                image_obj.stream_dict['/Width'] = 1
                image_obj.stream_dict['/Height'] = 1
                image_obj.write(b'\x80')
                done += 1
                if progress_callback and total > 0:
                    progress_callback(done, total)
                continue
            image_obj.stream_dict['/Filter'] = pikepdf.Name('/DCTDecode')
            image_obj.stream_dict['/ColorSpace'] = pikepdf.Name('/DeviceRGB')
            image_obj.stream_dict['/BitsPerComponent'] = 8
            image_obj.stream_dict['/Width'] = 1
            image_obj.stream_dict['/Height'] = 1
            image_obj.write(blank_jpeg, filter=pikepdf.Name('/DCTDecode'))
            done += 1
            if progress_callback and total > 0:
                progress_callback(done, total)
        pdf.save(buf, compress_streams=True, recompress_flate=True,
                 linearize=linearize)
        return buf.getvalue()


def linearize_pdf(pdf_bytes: bytes) -> bytes:
    """Save the PDF with linearization (optimized for fast web view)."""
    with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        pdf.save(buf, linearize=True)
        return buf.getvalue()


def needs_password(pdf_bytes: bytes) -> bool:
    """True if the PDF requires a user password before it can be opened."""
    try:
        with io.BytesIO(pdf_bytes) as src, pikepdf.open(src):
            return False
    except pikepdf.PasswordError:
        return True
    except Exception:  # noqa: BLE001 — corrupt files are reported by is_valid_pdf
        return False


def unlock_pdf(pdf_bytes: bytes, password: str) -> bytes:
    """
    Open the PDF with the user's password and return it saved without
    encryption. Raises pikepdf.PasswordError if the password is wrong.
    """
    try:
        with io.BytesIO() as buf, io.BytesIO(pdf_bytes) as src, \
             pikepdf.open(src, password=password) as pdf:
            pdf.save(buf)  # no encryption argument, so the output is unencrypted
            return buf.getvalue()
    except pikepdf.PasswordError as e:
        raise ValueError('Incorrect password') from e


def count_images(pdf_bytes: bytes) -> int:
    """Number of image XObjects (including soft masks) that the image steps will process."""
    with io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        return len(list(_iter_images(pdf)))


def pdf_info(pdf_bytes: bytes) -> dict:
    """Return basic stats about a PDF for display in the UI."""
    with io.BytesIO(pdf_bytes) as src, pikepdf.open(src) as pdf:
        return {
            'pages': len(pdf.pages),
            'size_kb': round(len(pdf_bytes) / 1024, 1),
        }
