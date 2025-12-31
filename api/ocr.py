"""
PDF OCR API endpoint using Google Cloud Vision.

Converts scanned PDFs into searchable PDFs by adding an invisible text layer
with OCR text positioned at exact coordinates.
"""

import base64
import io
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler

import fitz  # PyMuPDF
import requests


def parse_multipart(body: bytes, content_type: str) -> dict:
    """
    Parse multipart form data manually.

    Args:
        body: Request body bytes
        content_type: Content-Type header value

    Returns:
        Dictionary with field names as keys
    """
    result = {}

    # Extract boundary from content-type
    boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
    if not boundary_match:
        raise ValueError('No boundary found in Content-Type')

    boundary = boundary_match.group(1).strip('"')
    boundary_bytes = f'--{boundary}'.encode()

    # Split body by boundary
    parts = body.split(boundary_bytes)

    for part in parts:
        if not part or part == b'--' or part == b'--\r\n':
            continue

        # Skip empty parts
        part = part.strip()
        if not part or part == b'--':
            continue

        # Split headers from content
        try:
            if b'\r\n\r\n' in part:
                headers_section, content = part.split(b'\r\n\r\n', 1)
            elif b'\n\n' in part:
                headers_section, content = part.split(b'\n\n', 1)
            else:
                continue
        except ValueError:
            continue

        # Remove trailing boundary markers
        if content.endswith(b'\r\n'):
            content = content[:-2]
        if content.endswith(b'--'):
            content = content[:-2]
        if content.endswith(b'\r\n'):
            content = content[:-2]

        # Parse headers
        headers_text = headers_section.decode('utf-8', errors='ignore')

        # Extract field name
        name_match = re.search(r'name="([^"]+)"', headers_text)
        if not name_match:
            continue

        field_name = name_match.group(1)

        # Check if it's a file
        filename_match = re.search(r'filename="([^"]*)"', headers_text)

        if filename_match:
            # It's a file field
            result[field_name] = {
                'filename': filename_match.group(1),
                'content': content
            }
        else:
            # It's a regular field
            result[field_name] = content.decode('utf-8', errors='ignore').strip()

    return result


class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler."""

    def log_message(self, format, *args):
        """Override to suppress default logging."""
        pass

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_POST(self):
        """Handle PDF OCR requests."""
        try:
            # Get content type
            content_type = self.headers.get('Content-Type', '')

            if 'multipart/form-data' not in content_type:
                self._send_error(400, 'Content-Type must be multipart/form-data')
                return

            # Read request body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_error(400, 'Empty request body')
                return

            if content_length > 50 * 1024 * 1024:  # 50MB limit
                self._send_error(400, 'File too large (max 50MB)')
                return

            body = self.rfile.read(content_length)

            # Parse multipart data
            try:
                form_data = parse_multipart(body, content_type)
            except Exception as e:
                self._send_error(400, f'Failed to parse form data: {str(e)}')
                return

            # Get API key
            api_key = form_data.get('api_key')
            if not api_key or (isinstance(api_key, dict)):
                self._send_error(400, 'API key is required')
                return

            # Get PDF file
            file_data = form_data.get('file')
            if not file_data or not isinstance(file_data, dict):
                self._send_error(400, 'PDF file is required')
                return

            filename = file_data.get('filename', 'document.pdf')
            pdf_data = file_data.get('content', b'')

            if not pdf_data:
                self._send_error(400, 'Empty PDF file')
                return

            # Process the PDF
            result_pdf = process_pdf_ocr(pdf_data, api_key)

            # Send the result
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition',
                           f'attachment; filename="searchable_{filename}"')
            self.send_header('Content-Length', str(len(result_pdf)))
            self.end_headers()
            self.wfile.write(result_pdf)

        except ValueError as e:
            self._send_error(400, str(e))
        except Exception as e:
            error_msg = f'{str(e)}\n{traceback.format_exc()}'
            print(f"Error processing PDF: {error_msg}")
            self._send_error(500, f'Processing error: {str(e)}')

    def _send_error(self, status_code: int, message: str):
        """Send JSON error response."""
        response = json.dumps({'error': message}).encode()
        self.send_response(status_code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def call_vision_api(image_bytes: bytes, api_key: str) -> dict:
    """
    Call Google Cloud Vision API with an image.

    Args:
        image_bytes: Image data as bytes
        api_key: Google Cloud Vision API key

    Returns:
        API response containing text annotations with bounding boxes
    """
    url = f'https://vision.googleapis.com/v1/images:annotate?key={api_key}'

    # Encode image to base64
    image_b64 = base64.b64encode(image_bytes).decode('utf-8')

    # Build request payload
    payload = {
        'requests': [{
            'image': {
                'content': image_b64
            },
            'features': [{
                'type': 'DOCUMENT_TEXT_DETECTION'
            }]
        }]
    }

    # Make API call with timeout
    try:
        response = requests.post(url, json=payload, timeout=120)
    except requests.exceptions.Timeout:
        raise ValueError('Cloud Vision API request timed out')
    except requests.exceptions.RequestException as e:
        raise ValueError(f'Cloud Vision API request failed: {str(e)}')

    if response.status_code == 403:
        raise ValueError('Invalid API key or API not enabled. Please check your Google Cloud Vision API key.')
    elif response.status_code == 400:
        error_data = response.json()
        error_msg = error_data.get('error', {}).get('message', 'Bad request')
        raise ValueError(f'Cloud Vision API error: {error_msg}')
    elif response.status_code != 200:
        try:
            error_data = response.json()
            error_msg = error_data.get('error', {}).get('message', 'Unknown error')
        except:
            error_msg = f'HTTP {response.status_code}'
        raise ValueError(f'Cloud Vision API error: {error_msg}')

    return response.json()


def extract_text_with_positions(api_response: dict, page_width: float, page_height: float,
                                 image_width: int, image_height: int) -> list:
    """
    Extract text and positions from Cloud Vision API response.

    Args:
        api_response: Response from Cloud Vision API
        page_width: PDF page width in points
        page_height: PDF page height in points
        image_width: Image width in pixels
        image_height: Image height in pixels

    Returns:
        List of text blocks with positions
    """
    text_blocks = []

    responses = api_response.get('responses', [])
    if not responses:
        return text_blocks

    response = responses[0]

    # Check for errors in response
    if 'error' in response:
        error_msg = response['error'].get('message', 'Unknown error')
        raise ValueError(f'Cloud Vision API error: {error_msg}')

    # Get the full text annotation
    full_annotation = response.get('fullTextAnnotation')
    if not full_annotation:
        # No text found in image
        return text_blocks

    # Calculate scale factors
    scale_x = page_width / image_width
    scale_y = page_height / image_height

    # Process each page (usually just one for images)
    for page in full_annotation.get('pages', []):
        for block in page.get('blocks', []):
            for paragraph in block.get('paragraphs', []):
                for word in paragraph.get('words', []):
                    # Extract word text
                    word_text = ''.join(
                        symbol.get('text', '')
                        for symbol in word.get('symbols', [])
                    )

                    if not word_text.strip():
                        continue

                    # Get bounding box
                    bounding_box = word.get('boundingBox', {})
                    vertices = bounding_box.get('vertices', [])

                    # Handle normalized vertices if present
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

                    # Calculate position (convert from image coords to PDF coords)
                    x_coords = [v.get('x', 0) for v in vertices]
                    y_coords = [v.get('y', 0) for v in vertices]

                    min_x = min(x_coords) * scale_x
                    min_y = min(y_coords) * scale_y
                    max_x = max(x_coords) * scale_x
                    max_y = max(y_coords) * scale_y

                    text_blocks.append({
                        'text': word_text,
                        'x': min_x,
                        'y': min_y,
                        'width': max_x - min_x,
                        'height': max_y - min_y,
                        'font_size': max_y - min_y
                    })

    return text_blocks


def add_text_layer_to_page(page, text_blocks: list):
    """
    Add invisible text layer to a PDF page.

    Args:
        page: PyMuPDF page object
        text_blocks: List of text blocks with positions
    """
    for block in text_blocks:
        x = block['x']
        y = block['y']
        text = block['text']
        height = block['height']

        # Calculate font size (approximate from height, with reasonable limits)
        font_size = max(4, min(height * 0.85, 72))

        try:
            # Use TextWriter for better control
            tw = fitz.TextWriter(page.rect)

            # Position is baseline, so add height to y
            tw.append(
                pos=(x, y + height * 0.85),
                text=text,
                fontsize=font_size,
                font=fitz.Font("helv")
            )

            # Write with invisible render mode (3)
            tw.write_text(page, render_mode=3)

        except Exception:
            # Fallback: simple text insertion
            try:
                page.insert_text(
                    point=(x, y + height * 0.85),
                    text=text,
                    fontsize=font_size,
                    fontname="helv",
                    render_mode=3
                )
            except Exception:
                # Skip this text block if all methods fail
                pass


def process_pdf_ocr(pdf_data: bytes, api_key: str) -> bytes:
    """
    Process a PDF file and add OCR text layer.

    Args:
        pdf_data: Input PDF file as bytes
        api_key: Google Cloud Vision API key

    Returns:
        Processed PDF with text layer as bytes
    """
    # Validate PDF data
    if not pdf_data.startswith(b'%PDF'):
        raise ValueError('Invalid PDF file')

    # Open the input PDF
    try:
        input_doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise ValueError(f'Failed to open PDF: {str(e)}')

    if input_doc.page_count == 0:
        input_doc.close()
        raise ValueError('PDF has no pages')

    # Limit pages to prevent timeout (process max 10 pages)
    max_pages = min(input_doc.page_count, 10)
    if input_doc.page_count > 10:
        print(f"Warning: PDF has {input_doc.page_count} pages, processing only first 10")

    # Create output document
    output_doc = fitz.open()

    try:
        # Process each page
        for page_num in range(max_pages):
            page = input_doc[page_num]

            # Render page as image (150 DPI - good balance of speed and quality)
            dpi = 150
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)

            try:
                pixmap = page.get_pixmap(matrix=matrix)
            except Exception as e:
                raise ValueError(f'Failed to render page {page_num + 1}: {str(e)}')

            # Convert to JPEG for faster processing (smaller size)
            image_bytes = pixmap.tobytes("jpeg")
            image_width = pixmap.width
            image_height = pixmap.height

            # Call Cloud Vision API
            api_response = call_vision_api(image_bytes, api_key)

            # Extract text with positions
            text_blocks = extract_text_with_positions(
                api_response,
                page.rect.width,
                page.rect.height,
                image_width,
                image_height
            )

            # Create new page in output document with same size
            new_page = output_doc.new_page(
                width=page.rect.width,
                height=page.rect.height
            )

            # Insert the original page as an image (use same pixmap, already at good resolution)
            new_page.insert_image(
                new_page.rect,
                stream=pixmap.tobytes("jpeg")
            )

            # Add invisible text layer
            add_text_layer_to_page(new_page, text_blocks)

        # Save to bytes with optimization
        output_bytes = output_doc.tobytes(deflate=True, garbage=4)

    finally:
        # Cleanup
        input_doc.close()
        output_doc.close()

    return output_bytes
