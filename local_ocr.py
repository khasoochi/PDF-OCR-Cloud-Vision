#!/usr/bin/env python3
"""
Local PDF OCR processor for large files.
No page limits, with progress tracking, batch processing, and multiple output formats.

Usage:
    python local_ocr.py input.pdf output.pdf --api-key YOUR_API_KEY

Output formats:
    python local_ocr.py input.pdf output.pdf                    # Searchable PDF (default)
    python local_ocr.py input.pdf output.txt --text-only        # Text file only
    python local_ocr.py input.pdf output.json --json            # JSON with coordinates
    python local_ocr.py input.pdf output.pdf --side-by-side     # PDF with text on side

Options:
    --pages 1-10,15,20-30    Process specific pages only
    --resume                  Resume from checkpoint if interrupted
    --workers 8               Parallel API calls (default: 4)
    --dpi 200                 Resolution for OCR (default: 150)
"""

import argparse
import base64
import json
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
import requests


# ============================================================================
# API and OCR Functions
# ============================================================================

def call_vision_api(image_bytes: bytes, api_key: str, page_num: int, lang_hints: list = None) -> tuple:
    """Call Google Cloud Vision API with an image."""
    url = f'https://vision.googleapis.com/v1/images:annotate?key={api_key}'

    image_b64 = base64.b64encode(image_bytes).decode('utf-8')

    feature = {'type': 'DOCUMENT_TEXT_DETECTION'}

    image_context = {}
    if lang_hints:
        image_context['languageHints'] = lang_hints

    payload = {
        'requests': [{
            'image': {'content': image_b64},
            'features': [feature],
            'imageContext': image_context if image_context else None
        }]
    }

    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return (page_num, response.json(), None)
    except Exception as e:
        return (page_num, None, str(e))


def extract_text_with_positions(api_response: dict, page_width: float, page_height: float,
                                 image_width: int, image_height: int) -> dict:
    """Extract text and positions from Cloud Vision API response."""
    result = {
        'text': '',
        'words': [],
        'lines': [],
        'blocks': []
    }

    responses = api_response.get('responses', [])
    if not responses:
        return result

    response = responses[0]
    if 'error' in response:
        return result

    full_annotation = response.get('fullTextAnnotation')
    if not full_annotation:
        return result

    # Get full text
    result['text'] = full_annotation.get('text', '')

    scale_x = page_width / image_width
    scale_y = page_height / image_height

    for page in full_annotation.get('pages', []):
        for block in page.get('blocks', []):
            block_text = []
            block_words = []

            for paragraph in block.get('paragraphs', []):
                para_text = []

                for word in paragraph.get('words', []):
                    word_text = ''.join(
                        symbol.get('text', '')
                        for symbol in word.get('symbols', [])
                    )

                    if not word_text.strip():
                        continue

                    bounding_box = word.get('boundingBox', {})
                    vertices = bounding_box.get('vertices', [])

                    if not vertices:
                        normalized = bounding_box.get('normalizedVertices', [])
                        if normalized:
                            vertices = [
                                {'x': int(v.get('x', 0) * image_width),
                                 'y': int(v.get('y', 0) * image_height)}
                                for v in normalized
                            ]

                    if len(vertices) < 4:
                        continue

                    x_coords = [v.get('x', 0) for v in vertices]
                    y_coords = [v.get('y', 0) for v in vertices]

                    word_data = {
                        'text': word_text,
                        'x': min(x_coords) * scale_x,
                        'y': min(y_coords) * scale_y,
                        'width': (max(x_coords) - min(x_coords)) * scale_x,
                        'height': (max(y_coords) - min(y_coords)) * scale_y
                    }

                    result['words'].append(word_data)
                    block_words.append(word_data)
                    para_text.append(word_text)

                block_text.append(' '.join(para_text))

            if block_words:
                result['blocks'].append({
                    'text': '\n'.join(block_text),
                    'words': block_words
                })

    return result


def add_text_layer_to_page(page, words: list):
    """Add invisible text layer to a PDF page."""
    for word in words:
        font_size = max(4, min(word['height'] * 0.85, 72))
        try:
            tw = fitz.TextWriter(page.rect)
            tw.append(
                pos=(word['x'], word['y'] + word['height'] * 0.85),
                text=word['text'],
                fontsize=font_size,
                font=fitz.Font("helv")
            )
            tw.write_text(page, render_mode=3)
        except Exception:
            pass


def add_visible_text_to_side(page, words: list, original_width: float, text_color=(0, 0, 0)):
    """Add visible text to the right side of an extended page."""
    margin = 20
    x_start = original_width + margin
    y_pos = margin

    # Group words into lines based on y-coordinate
    if not words:
        return

    # Sort by y, then x
    sorted_words = sorted(words, key=lambda w: (w['y'], w['x']))

    # Group into lines (words with similar y)
    lines = []
    current_line = []
    current_y = None
    threshold = 10  # pixels

    for word in sorted_words:
        if current_y is None or abs(word['y'] - current_y) < threshold:
            current_line.append(word)
            current_y = word['y'] if current_y is None else current_y
        else:
            if current_line:
                lines.append(current_line)
            current_line = [word]
            current_y = word['y']

    if current_line:
        lines.append(current_line)

    # Write lines to side panel
    font_size = 10
    line_height = font_size * 1.4

    for line in lines:
        line_text = ' '.join(w['text'] for w in line)
        try:
            page.insert_text(
                point=(x_start, y_pos + font_size),
                text=line_text,
                fontsize=font_size,
                fontname="helv",
                color=text_color
            )
        except Exception:
            pass
        y_pos += line_height

        # Check if we need to wrap
        if y_pos > page.rect.height - margin:
            break


# ============================================================================
# Page Range Parsing
# ============================================================================

def parse_page_range(page_spec: str, total_pages: int) -> list:
    """
    Parse page range specification.

    Examples:
        "1-10" -> [0, 1, 2, ..., 9]
        "1,3,5" -> [0, 2, 4]
        "1-5,10,15-20" -> [0,1,2,3,4,9,14,15,16,17,18,19]

    Returns 0-indexed page numbers.
    """
    if not page_spec:
        return list(range(total_pages))

    pages = set()
    parts = page_spec.replace(' ', '').split(',')

    for part in parts:
        if '-' in part:
            start, end = part.split('-', 1)
            start = int(start) - 1  # Convert to 0-indexed
            end = int(end) - 1
            pages.update(range(max(0, start), min(end + 1, total_pages)))
        else:
            page = int(part) - 1
            if 0 <= page < total_pages:
                pages.add(page)

    return sorted(pages)


# ============================================================================
# Checkpoint/Resume Support
# ============================================================================

def get_checkpoint_path(input_path: str) -> Path:
    """Get checkpoint file path for a given input file."""
    return Path(input_path).with_suffix('.ocr_checkpoint')


def save_checkpoint(checkpoint_path: Path, data: dict):
    """Save checkpoint data."""
    with open(checkpoint_path, 'wb') as f:
        pickle.dump(data, f)


def load_checkpoint(checkpoint_path: Path) -> dict:
    """Load checkpoint data if exists."""
    if checkpoint_path.exists():
        try:
            with open(checkpoint_path, 'rb') as f:
                return pickle.load(f)
        except Exception:
            pass
    return None


def clear_checkpoint(checkpoint_path: Path):
    """Remove checkpoint file."""
    if checkpoint_path.exists():
        checkpoint_path.unlink()


# ============================================================================
# Main Processing Functions
# ============================================================================

def process_pdf(input_path: str, output_path: str, api_key: str,
                dpi: int = 150, max_workers: int = 4, batch_size: int = 10,
                pages: str = None, text_only: bool = False, json_output: bool = False,
                side_by_side: bool = False, resume: bool = False,
                lang_hints: list = None):
    """
    Process a PDF file with various output options.

    Args:
        input_path: Path to input PDF
        output_path: Path to output file
        api_key: Google Cloud Vision API key
        dpi: Resolution for OCR
        max_workers: Number of parallel API calls
        batch_size: Pages to process before checkpoint
        pages: Page range specification (e.g., "1-10,15")
        text_only: Output text file only
        json_output: Output JSON with coordinates
        side_by_side: Create PDF with text on side
        resume: Resume from checkpoint
        lang_hints: Language hints for OCR
    """
    print(f"Opening {input_path}...")
    input_doc = fitz.open(input_path)
    total_pages = input_doc.page_count
    print(f"Total pages in document: {total_pages}")

    # Parse page range
    page_numbers = parse_page_range(pages, total_pages)
    print(f"Pages to process: {len(page_numbers)}")

    # Check for checkpoint
    checkpoint_path = get_checkpoint_path(input_path)
    ocr_results = {}
    start_index = 0

    if resume:
        checkpoint = load_checkpoint(checkpoint_path)
        if checkpoint:
            ocr_results = checkpoint.get('results', {})
            processed_pages = set(ocr_results.keys())
            # Find pages still needing processing
            remaining = [p for p in page_numbers if p not in processed_pages]
            if remaining:
                print(f"Resuming from checkpoint. Already processed: {len(processed_pages)} pages")
                page_numbers = remaining
            else:
                print("All pages already processed from checkpoint!")
                page_numbers = []

    if not page_numbers and not ocr_results:
        print("No pages to process!")
        input_doc.close()
        return

    # Render pages
    print(f"\nRendering pages at {dpi} DPI...")
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    page_data = {}
    for i, page_num in enumerate(page_numbers):
        page = input_doc[page_num]
        pixmap = page.get_pixmap(matrix=matrix)
        page_data[page_num] = {
            'page': page,
            'pixmap': pixmap,
            'image_bytes': pixmap.tobytes("jpeg"),
            'image_width': pixmap.width,
            'image_height': pixmap.height,
            'rect': page.rect
        }
        if (i + 1) % 50 == 0:
            print(f"  Rendered {i + 1}/{len(page_numbers)} pages...")

    # Process OCR
    if page_numbers:
        print(f"\nProcessing OCR with {max_workers} parallel workers...")
        start_time = time.time()

        pages_to_process = list(page_numbers)

        for batch_start in range(0, len(pages_to_process), batch_size):
            batch_end = min(batch_start + batch_size, len(pages_to_process))
            batch_pages = pages_to_process[batch_start:batch_end]

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        call_vision_api,
                        page_data[p]['image_bytes'],
                        api_key,
                        p,
                        lang_hints
                    ): p for p in batch_pages
                }

                for future in as_completed(futures):
                    page_num, response, error = future.result()
                    if error:
                        print(f"  Page {page_num + 1}: Error - {error}")
                        ocr_results[page_num] = {'text': '', 'words': [], 'blocks': []}
                    else:
                        p = page_data[page_num]
                        result = extract_text_with_positions(
                            response,
                            p['rect'].width,
                            p['rect'].height,
                            p['image_width'],
                            p['image_height']
                        )
                        ocr_results[page_num] = result
                        word_count = len(result['words'])
                        print(f"  Page {page_num + 1}: {word_count} words")

            # Save checkpoint after each batch
            if resume:
                save_checkpoint(checkpoint_path, {'results': ocr_results})

            # Progress update
            elapsed = time.time() - start_time
            pages_done = batch_end
            pages_per_sec = pages_done / elapsed if elapsed > 0 else 0
            remaining = len(pages_to_process) - pages_done
            eta = remaining / pages_per_sec if pages_per_sec > 0 else 0
            print(f"  Progress: {pages_done}/{len(pages_to_process)} pages. "
                  f"Speed: {pages_per_sec:.1f} p/s. ETA: {eta/60:.1f} min")

    # Generate output
    all_page_nums = parse_page_range(None, total_pages)  # All pages for reference

    if text_only:
        # Text-only output
        print(f"\nGenerating text file: {output_path}")
        with open(output_path, 'w', encoding='utf-8') as f:
            for page_num in sorted(ocr_results.keys()):
                result = ocr_results[page_num]
                f.write(f"--- Page {page_num + 1} ---\n")
                f.write(result.get('text', ''))
                f.write("\n\n")
        print(f"Text saved to: {output_path}")

    elif json_output:
        # JSON output with coordinates
        print(f"\nGenerating JSON file: {output_path}")
        output_data = {
            'input_file': input_path,
            'total_pages': total_pages,
            'processed_pages': len(ocr_results),
            'pages': {}
        }
        for page_num in sorted(ocr_results.keys()):
            result = ocr_results[page_num]
            output_data['pages'][str(page_num + 1)] = {
                'text': result.get('text', ''),
                'words': result.get('words', []),
                'blocks': result.get('blocks', [])
            }
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"JSON saved to: {output_path}")

    elif side_by_side:
        # Side-by-side PDF
        print(f"\nGenerating side-by-side PDF: {output_path}")
        output_doc = fitz.open()

        for page_num in range(total_pages):
            page = input_doc[page_num]
            original_width = page.rect.width
            original_height = page.rect.height

            # Create wider page
            new_width = original_width * 2
            new_page = output_doc.new_page(width=new_width, height=original_height)

            # Insert original page image on left
            if page_num in page_data:
                left_rect = fitz.Rect(0, 0, original_width, original_height)
                new_page.insert_image(left_rect, stream=page_data[page_num]['pixmap'].tobytes("jpeg"))
            else:
                # Page wasn't processed, render it now
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                left_rect = fitz.Rect(0, 0, original_width, original_height)
                new_page.insert_image(left_rect, stream=pixmap.tobytes("jpeg"))

            # Add text on right side
            if page_num in ocr_results:
                add_visible_text_to_side(new_page, ocr_results[page_num]['words'], original_width)

            # Also add invisible text layer for searchability
            if page_num in ocr_results:
                # Adjust word positions for left side
                add_text_layer_to_page(new_page, ocr_results[page_num]['words'])

            if (page_num + 1) % 50 == 0:
                print(f"  Built {page_num + 1}/{total_pages} pages...")

        output_doc.save(output_path, deflate=True, garbage=4)
        output_doc.close()
        print(f"Side-by-side PDF saved to: {output_path}")

    else:
        # Standard searchable PDF
        print(f"\nGenerating searchable PDF: {output_path}")
        output_doc = fitz.open()

        for page_num in range(total_pages):
            page = input_doc[page_num]

            new_page = output_doc.new_page(
                width=page.rect.width,
                height=page.rect.height
            )

            # Insert page image
            if page_num in page_data:
                new_page.insert_image(new_page.rect, stream=page_data[page_num]['pixmap'].tobytes("jpeg"))
            else:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                new_page.insert_image(new_page.rect, stream=pixmap.tobytes("jpeg"))

            # Add text layer
            if page_num in ocr_results:
                add_text_layer_to_page(new_page, ocr_results[page_num]['words'])

            if (page_num + 1) % 50 == 0:
                print(f"  Built {page_num + 1}/{total_pages} pages...")

        output_doc.save(output_path, deflate=True, garbage=4)
        output_doc.close()
        print(f"Searchable PDF saved to: {output_path}")

    # Cleanup
    input_doc.close()
    if resume:
        clear_checkpoint(checkpoint_path)

    print("\nDone!")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Convert scanned PDF to searchable PDF using Google Cloud Vision OCR',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic searchable PDF
  python local_ocr.py input.pdf output.pdf --api-key YOUR_KEY

  # Text file only
  python local_ocr.py input.pdf output.txt --api-key YOUR_KEY --text-only

  # JSON with word coordinates
  python local_ocr.py input.pdf output.json --api-key YOUR_KEY --json

  # Side-by-side (original + text)
  python local_ocr.py input.pdf output.pdf --api-key YOUR_KEY --side-by-side

  # Process specific pages
  python local_ocr.py input.pdf output.pdf --api-key YOUR_KEY --pages 1-10,15,20-30

  # Resume interrupted processing
  python local_ocr.py input.pdf output.pdf --api-key YOUR_KEY --resume

  # Use environment variable for API key
  export GOOGLE_CLOUD_VISION_API_KEY=your_key
  python local_ocr.py input.pdf output.pdf
        """
    )

    parser.add_argument('input', help='Input PDF file')
    parser.add_argument('output', help='Output file (PDF, TXT, or JSON based on options)')
    parser.add_argument('--api-key', help='Google Cloud Vision API key (or set GOOGLE_CLOUD_VISION_API_KEY)')

    # Output format options
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument('--text-only', action='store_true',
                              help='Output text file only (no PDF)')
    format_group.add_argument('--json', action='store_true',
                              help='Output JSON with word coordinates')
    format_group.add_argument('--side-by-side', action='store_true',
                              help='Create PDF with original on left, text on right')

    # Processing options
    parser.add_argument('--pages', type=str, default=None,
                        help='Page range to process (e.g., "1-10,15,20-30")')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from checkpoint if interrupted')
    parser.add_argument('--dpi', type=int, default=150,
                        help='DPI for rendering (default: 150)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Parallel API workers (default: 4)')
    parser.add_argument('--batch-size', type=int, default=10,
                        help='Batch size for checkpoints (default: 10)')
    parser.add_argument('--lang', type=str, default=None,
                        help='Language hints, comma-separated (e.g., "hi,sa,en")')

    args = parser.parse_args()

    # Get API key
    api_key = args.api_key or os.environ.get('GOOGLE_CLOUD_VISION_API_KEY')
    if not api_key:
        print("Error: API key required. Use --api-key or set GOOGLE_CLOUD_VISION_API_KEY")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    # Parse language hints
    lang_hints = None
    if args.lang:
        lang_hints = [l.strip() for l in args.lang.split(',')]

    process_pdf(
        input_path=args.input,
        output_path=args.output,
        api_key=api_key,
        dpi=args.dpi,
        max_workers=args.workers,
        batch_size=args.batch_size,
        pages=args.pages,
        text_only=args.text_only,
        json_output=args.json,
        side_by_side=args.side_by_side,
        resume=args.resume,
        lang_hints=lang_hints
    )


if __name__ == '__main__':
    main()
