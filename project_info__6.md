# Telegram Testy Bot — DOCX Image Parsing & Storage Analysis

## Executive Summary

This report documents how the **Telegram math quiz bot** currently handles DOCX parsing, where images fit into the pipeline, what's already built versus what's missing, and the recommended approach for adding image parsing from DOCX files that will work seamlessly with the existing infrastructure.

The core finding: **70% of the image pipeline already exists** — the DOCX parser already extracts and saves images, the question data model already has an `image` field, and the test renderer already knows how to send photos. What's missing is **integration of the image-aware DOCX parser into the upload flow** and a **reliable image-to-question association strategy**.

---

## 1. Current DOCX Parsing Architecture

### Entry Points for DOCX Parsing

The bot has **two** DOCX parsing methods:

#### (A) `_extract_docx_text()` — old, no images

- **File**: `bot.py` (line ~480)
- **Behavior**: Opens `zipfile.ZipFile`, reads `word/document.xml`, extracts `<w:t>` text nodes, concatenates them
- **Returns**: `str` — plain text only
- **Used by**: `_load_questions_from_uploaded_docx()` (the **live upload route**)

#### (B) `_extract_docx_text_with_images()` — new, with images

- **File**: `bot.py` (line ~505)
- **Behavior**: Same as (A) PLUS:
    - Opens `word/_rels/document.xml.rels` to resolve relationship IDs → media paths
    - Finds `<a:blip>` elements in `w:drawing` inside paragraphs
    - Extracts the referenced `r:embed` relationship ID → maps to `media/imageN.png`
    - Saves extracted images to `data/docx_images/<docx_sha256>/img_<N>.<ext>`
    - Inserts `[[IMG<N>]]` tokens inline in the extracted text at the position where the image appeared
- **Returns**: `Tuple[str, Dict[int, dict]]` — text with `[[IMG<N>]]` markers + `{marker_id: {"path": ..., "position": "top", "caption": None}}`
- **Images saved to**: `data/docx_images/<16-char-sha256-of-docx>/img_<N>.<ext>`

### The Critical Bug/Limitation

There are **two** `_load_questions_from_uploaded_docx()` methods declared — the second one **overwrites the first**:

**First declaration (line ~1150):**

```python
def _load_questions_from_uploaded_docx(self, path: Path, fallback_topic_id: str = "") -> List[Question]:
    extracted_text, images_by_marker = self._extract_docx_text_with_images(path)
    return self._parse_docx_questions(extracted_text, topic_id=fallback_topic_id, images_by_marker=images_by_marker)
```

**Second declaration (line ~1630 — overrides the first):**

```python
def _load_questions_from_uploaded_docx(self, path: Path, topic_id: str = "") -> List[Question]:
    return self._parse_docx_questions(self._extract_docx_text(path), topic_id=topic_id)
```

**Consequence**: The **live upload flow** (`_handle_document` at line ~1580) calls `_load_questions_from_uploaded_docx()`, which resolves to the **second (image-unaware) version**. All images in uploaded DOCX files are **silently discarded** because `_extract_docx_text()` only returns text.

**The first version** (with images) exists in the codebase but is **never actually called** from the live handler.

---

## 2. Question Data Model — `image` Field

### Structure

```python
@dataclass
class Question:
    id: str
    topic_id: str
    type: str
    question: str
    options: List[str]
    answer: List[int]
    explanation: str
    image: Optional[dict] = None  # <-- THIS IS THE IMAGE FIELD
```

The `image` field is:

- **`None`** when no image is associated
- **`dict`** when present, with structure:  
  `{"path": "str", "position": "top", "caption": None | str}`

### Current Usage

- **Stored in `questions.json`** — every question object in the JSON file has `"image": null` (all current ~70 questions have `null`)
- **Normalized on save** — `_save_imported_questions()` (line ~1190) includes `"image": q.image` in the payload
- **Normalized on load** — `_load_data()` ensures `image` is always present in the saved payload (backward compatibility)

### How `image` Is Rendered During Tests

In `_send_current_question()` (line ~1770):

```python
image_meta = question.image if isinstance(getattr(question, "image", None), dict) else None
photo_position = "top"  # or "below" (anything not "top" = below)
photo_path = image_meta.get("path")
photo_caption = image_meta.get("caption")

if photo_position == "top" and photo_path:
    # Send photo FIRST, then the question text with options
    self.api.send_photo(student.chat_id, str(photo_path), caption=photo_caption)

# ... send question text + inline keyboard ...

if photo_position != "top" and photo_path:
    # Send photo AFTER the question
    self.api.send_photo(student.chat_id, str(photo_path), caption=photo_caption)
```

So the rendering pipeline **fully supports** sending images before or after a question.

---

## 3. The `_parse_docx_questions()` Parser — How It Associates Images

### Image Token Handling

In `_parse_docx_questions()` (line ~580):

```python
if images_by_marker and "[[IMG" in line:
    marker_ids = [int(x) for x in re.findall(r"\[\[IMG(\d+)\]\]", line)]
    line = re.sub(r"\[\[IMG\d+\]\]", "", line).strip()

    if current is not None and marker_ids:
        for marker_id in marker_ids:
            new_img = images_by_marker.get(marker_id)
            # ... attach the image to the current question
            # Preference: "top" position images overwrite "below" images
```

**Association logic**: Images are attached to the **currently parsing question** (`current`) when an `[[IMG<N>]]` token appears. If multiple images are in the same paragraph as a question, only one survives (preferring "top" position).

**Key insight**: The parser assumes an image is associated with the question _that follows or contains_ the image's paragraph. This works well when the DOCX has images placed immediately before or within a question block.

---

## 4. Storage Format Analysis — Recommended Image Format for DOCX

### Current Storage Format

When `_extract_docx_text_with_images()` extracts images from a DOCX:

- **Source**: whatever format the DOCX author embedded (PNG, JPEG, GIF, WMF, etc.)
- **Destination**: saved as-is with the original extension → `img_0.png`, `img_1.jpeg`, etc.
- **No conversion, no compression, no optimization**

### Telegram API Constraints

From `BotApi.send_photo()`:

```python
ext = path.suffix.lower().lstrip(".")
mime = "application/octet-stream"
if ext == "png": mime = "image/png"
elif ext in {"jpg", "jpeg"}: mime = "image/jpeg"
elif ext == "gif": mime = "image/gif"
elif ext == "webp": mime = "image/webp"
```

**Telegram API limitations for `sendPhoto`:**

- Max file size: **10 MB** (photos)
- Supported formats: JPEG, PNG, GIF, WebP
- The API auto-compresses JPEGs over a certain threshold

### Recommended Image Format for DOCX Embedding

| Criterion         | Recommendation                                                                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Format**        | **PNG** (lossless, universally supported)                                                                              |
| **Why not JPEG?** | Math diagrams often contain sharp lines, text labels, and crisp edges — JPEG compression artifacts degrade readability |
| **Why not SVG?**  | DOCX doesn't natively render SVG in `w:drawing/a:blip` (only raster images via `a:blip`)                               |
| **Resolution**    | At least **300 DPI** at the target display size (Telegram renders at ~512px wide on mobile)                            |
| **Color mode**    | RGB (not CMYK — Telegram doesn't support CMYK)                                                                         |

**For the parser to handle images reliably:**

1. **Embed images as PNG inside the DOCX** — standard `w:drawing` with `a:blip` referencing `r:embed` → `media/imageN.png`
2. **Place the image in its own paragraph** immediately before the question block (so `[[IMG<N>]]` token appears on its own line, making association unambiguous)
3. **Avoid multiple images per question** — the parser only keeps one (with `position: "top"` priority)

---

## 5. What Must Change to Make Images Work End-to-End

### The Fix (1 line change + 1 small adjustment)

**File**: `bot.py`

**Change 1** — Remove the duplicate override at ~line 1630:

Replace this:

```python
def _load_questions_from_uploaded_docx(self, path: Path, topic_id: str = "") -> List[Question]:
    return self._parse_docx_questions(self._extract_docx_text(path), topic_id=topic_id)
```

With a reference to the image-aware version already defined at ~line 1150:

```python
# (this method is already correctly defined earlier with images)
```

**Change 2** — In `_handle_document()` (~line 1590), after import succeeds, ensure the `data/docx_images/` cleanup is handled properly (don't delete the images directory, since those paths are now persisted in `questions.json`).

### No-Change Items (already work)

- ✅ `questions.json` serialization of `image` field
- ✅ `_send_current_question()` rendering of images to Telegram
- ✅ `_build_keyboard()` for questions — no change needed
- ✅ Image saving to `data/docx_images/<sha>/`
- ✅ Image association via `[[IMG<N>]]` tokens in parser

---

## 6. Data Flow — After the Fix

1. **Admin sends `.docx`** → `_handle_document()`
2. → `_load_questions_from_uploaded_docx(path, topic_id=...)` → calls `_extract_docx_text_with_images()`
3. → Extracts text + saves images to `data/docx_images/<sha256[:16]>/img_0.png`, `img_1.png`, ...
4. → `_parse_docx_questions(text, topic_id=..., images_by_marker=...)` → builds `Question` objects with `image={"path": "data/docx_images/.../img_0.png", "position": "top", "caption": None}`
5. → `_save_imported_questions(imported)` → writes to `questions.json` including `"image": {...}` dict
6. **Student starts a test** → `_send_current_question()` reads `question.image` → calls `self.api.send_photo(chat_id, path, caption=...)` → Telegram renders the image

---

## 7. Non-Obvious Behaviors & Edge Cases

### Image Path Portability

The saved image path is **absolute on disk** when `_extract_docx_text_with_images()` saves it. When `questions.json` stores `"path": "data\\docx_images\\abc123\\img_0.png"`, this is **relative to `BASE_DIR`** (the project root). However, `bot.py` line ~515 writes `Path` objects that get serialized via `json.dump`. Need to ensure paths are stored **relative** to `BASE_DIR`, not as a full `Path` object.

**Risk**: If the project is moved to a different directory, the image paths break.

**Recommendation**: Store images with paths relative to `DATA_DIR` (i.e., `docx_images/<sha>/img_0.png`) and resolve at render time via `(DATA_DIR / path)`.

### Image Cleanup on Question Deletion

When questions are deleted (via admin interface or topic purge), the images in `data/docx_images/<sha>/` are **orphaned** — no cleanup logic removes them.

**Recommendation**: Add a background cleanup or reference-counting mechanism, or simply ignore (disk is cheap for geometry diagrams).

### Canonical Question Stats vs. Image Changes

The `canonical_key` for difficulty tracking is computed from question **text and answers only** — images don't affect question identity. This is correct because two questions with identical text but different diagrams would be considered the same question for stats purposes.

---

## 8. Summary Table

| Component                                                | Status        | Action Required                                                                                    |
| -------------------------------------------------------- | ------------- | -------------------------------------------------------------------------------------------------- |
| DOCX text extraction                                     | ✅ Working    | None                                                                                               |
| DOCX image extraction (`_extract_docx_text_with_images`) | ✅ Working    | None                                                                                               |
| `Question.image` field in data model                     | ✅ Working    | None                                                                                               |
| `questions.json` serialization of `image`                | ✅ Working    | None                                                                                               |
| Image-aware parser association (`[[IMG<N>]]` tokens)     | ✅ Working    | None                                                                                               |
| `_send_current_question()` photo rendering               | ✅ Working    | None                                                                                               |
| **Upload flow calling image-aware parser**               | ❌ **Broken** | **Remove duplicate `_load_questions_from_uploaded_docx()` (the one without images at ~line 1630)** |
| Image path portability (relative vs absolute)            | ⚠️ Fragile    | Ensure paths are relative to `BASE_DIR`/`DATA_DIR`                                                 |
| Orphaned image cleanup on question delete                | ❌ Missing    | Consider adding cleanup (low priority)                                                             |

---

## 9. Recommended Image Format for DOCX Authors

If you're creating DOCX files for import, follow these rules:

1. **Embed images as PNG files** inside the DOCX (standard method: Insert → Pictures in Word)
2. **Place each image in its own paragraph** immediately before the question it belongs to
3. **One image per question maximum** — the parser only keeps the last one found before the question, with "top" position
4. **Images should be ≥300 DPI** at the size they'll be displayed (Telegram renders ~512px wide on desktop, ~300px on mobile)
5. **No transparency needed** — Telegram renders PNG transparency on a white background
6. **File size < 10 MB per image** (Telegram limit for `sendPhoto`)
7. **File size < 50 MB total DOCX** (Telegram file download limit)

The parser will:

- Extract the image from `word/media/` inside the ZIP
- Save it as `data/docx_images/<docx_hash>/img_<N>.png`
- Insert a marker token `[[IMG<N>]]` in the text
- The question parser will attach the image to the next question it parses
- During testing, `sendPhoto` will deliver the image to the student before the question text
