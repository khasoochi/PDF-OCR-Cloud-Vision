#!/usr/bin/env python3
"""
Local PDF OCR processor for large files.
No page limits, with progress tracking and batch processing.

Usage:
    python local_ocr.py input.pdf output.pdf --api-key YOUR_API_KEY

Or with environment variable:
    export GOOGLE_CLOUD_VISION_API_KEY=your_key
    python local_ocr.py input.pdf output.pdf
"""

import argparse
import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz  # PyMuPDF
import requests


def call_vision_api(image_bytes: bytes, api_key: str, page_num: int) -> tuple:
    """Call Google Cloud Vision API with an image."""
    url = f'https://vision.googleapis.com/v1/images:annotate?key={api_key}'

    image_b64 = base64.b64encode(image_bytes).decode('utf-8')

    payload = {
        'requests': [{
            'image': {'content': image_b64},
            'features': [{'type': 'DOCUMENT_TEXT_DETECTION'}]
        }]
    }

    try:
        response = requests.post(url, json=payload, timeout=120)
        response.raise_for_status()
        return (page_num, response.json(), None)
    except Exception as e:
        return (page_num, None, str(e))


def extract_text_with_positions(api_response: dict, page_width: float, page_height: float,
                                 image_width: int, image_height: int) -> list:
    """Extract text and positions from Cloud Vision API response."""
    text_blocks = []

    responses = api_response.get('responses', [])
    if not responses:
        return text_blocks

    response = responses[0]
    if 'error' in response:
        return text_blocks

    full_annotation = response.get('fullTextAnnotation')
    if not full_annotation:
        return text_blocks

    scale_x = page_width / image_width
    scale_y = page_height / image_height

    for page in full_annotation.get('pages', []):
        for block in page.get('blocks', []):
            for paragraph in block.get('paragraphs', []):
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

                    text_blocks.append({
                        'text': word_text,
                        'x': min(x_coords) * scale_x,
                        'y': min(y_coords) * scale_y,
                        'height': (max(y_coords) - min(y_coords)) * scale_y
                    })

    return text_blocks


def add_text_layer_to_page(page, text_blocks: list):
    """Add invisible text layer to a PDF page."""
    for block in text_blocks:
        font_size = max(4, min(block['height'] * 0.85, 72))
        try:
            tw = fitz.TextWriter(page.rect)
            tw.append(
                pos=(block['x'], block['y'] + block['height'] * 0.85),
                text=block['text'],
                fontsize=font_size,
                font=fitz.Font("helv")
            )
            tw.write_text(page, render_mode=3)
        except Exception:
            pass


def process_pdf(input_path: str, output_path: str, api_key: str,
                dpi: int = 150, max_workers: int = 4, batch_size: int = 10):
    """
    Process a PDF file and add OCR text layer.

    Args:
        input_path: Path to input PDF
        output_path: Path to output PDF
        api_key: Google Cloud Vision API key
        dpi: Resolution for OCR (higher = better but slower)
        max_workers: Number of parallel API calls
        batch_size: Pages to process before saving progress
    """
    print(f"Opening {input_path}...")
    input_doc = fitz.open(input_path)
    total_pages = input_doc.page_count
    print(f"Total pages: {total_pages}")

    output_doc = fitz.open()

    # Prepare all pages for OCR
    print(f"Rendering pages at {dpi} DPI...")
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    page_data = []
    for page_num in range(total_pages):
        page = input_doc[page_num]
        pixmap = page.get_pixmap(matrix=matrix)
        page_data.append({
            'page_num': page_num,
            'page': page,
            'pixmap': pixmap,
            'image_bytes': pixmap.tobytes("jpeg"),
            'image_width': pixmap.width,
            'image_height': pixmap.height
        })
        if (page_num + 1) % 50 == 0:
            print(f"  Rendered {page_num + 1}/{total_pages} pages...")

    print(f"\nProcessing OCR with {max_workers} parallel workers...")
    start_time = time.time()

    # Process in batches with parallel API calls
    ocr_results = {}

    for batch_start in range(0, total_pages, batch_size):
        batch_end = min(batch_start + batch_size, total_pages)
        batch = page_data[batch_start:batch_end]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(call_vision_api, p['image_bytes'], api_key, p['page_num']): p
                for p in batch
            }

            for future in as_completed(futures):
                page_num, response, error = future.result()
                if error:
                    print(f"  Page {page_num + 1}: Error - {error}")
                    ocr_results[page_num] = []
                else:
                    p = page_data[page_num]
                    text_blocks = extract_text_with_positions(
                        response,
                        p['page'].rect.width,
                        p['page'].rect.height,
                        p['image_width'],
                        p['image_height']
                    )
                    ocr_results[page_num] = text_blocks
                    word_count = len(text_blocks)
                    print(f"  Page {page_num + 1}/{total_pages}: {word_count} words")

        # Progress update
        elapsed = time.time() - start_time
        pages_done = batch_end
        pages_per_sec = pages_done / elapsed if elapsed > 0 else 0
        eta = (total_pages - pages_done) / pages_per_sec if pages_per_sec > 0 else 0
        print(f"  Batch complete. {pages_done}/{total_pages} pages. "
              f"Speed: {pages_per_sec:.1f} pages/sec. ETA: {eta/60:.1f} min")

    # Build output PDF
    print("\nBuilding output PDF...")
    for page_num in range(total_pages):
        p = page_data[page_num]

        # Create new page
        new_page = output_doc.new_page(
            width=p['page'].rect.width,
            height=p['page'].rect.height
        )

        # Insert original image
        new_page.insert_image(new_page.rect, stream=p['pixmap'].tobytes("jpeg"))

        # Add text layer
        if page_num in ocr_results:
            add_text_layer_to_page(new_page, ocr_results[page_num])

        if (page_num + 1) % 50 == 0:
            print(f"  Built {page_num + 1}/{total_pages} pages...")

    # Save output
    print(f"\nSaving to {output_path}...")
    output_doc.save(output_path, deflate=True, garbage=4)

    # Cleanup
    input_doc.close()
    output_doc.close()

    elapsed = time.time() - start_time
    print(f"\nDone! Processed {total_pages} pages in {elapsed/60:.1f} minutes.")
    print(f"Output saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Convert scanned PDF to searchable PDF using Google Cloud Vision OCR')
    parser.add_argument('input', help='Input PDF file')
    parser.add_argument('output', help='Output PDF file')
    parser.add_argument('--api-key', help='Google Cloud Vision API key (or set GOOGLE_CLOUD_VISION_API_KEY env var)')
    parser.add_argument('--dpi', type=int, default=150, help='DPI for rendering (default: 150)')
    parser.add_argument('--workers', type=int, default=4, help='Parallel API workers (default: 4)')
    parser.add_argument('--batch-size', type=int, default=10, help='Batch size for progress updates (default: 10)')

    args = parser.parse_args()

    # Get API key
    api_key = args.api_key or os.environ.get('GOOGLE_CLOUD_VISION_API_KEY')
    if not api_key:
        print("Error: API key required. Use --api-key or set GOOGLE_CLOUD_VISION_API_KEY environment variable.")
        sys.exit(1)

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    process_pdf(
        input_path=args.input,
        output_path=args.output,
        api_key=api_key,
        dpi=args.dpi,
        max_workers=args.workers,
        batch_size=args.batch_size
    )


if __name__ == '__main__':
    main()
