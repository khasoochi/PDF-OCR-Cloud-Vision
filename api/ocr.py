"""
PDF OCR API endpoint using Google Cloud Vision.

Converts scanned PDFs into searchable PDFs by adding an invisible text layer
with OCR text positioned at exact coordinates.
"""

import base64
import email
import email.policy
import json
import io
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler

# Lazy imports to catch errors
fitz = None
requests = None


def load_dependencies():
    """Load dependencies lazily to catch import errors."""
    global fitz, requests
    if fitz is None:
        import fitz as _fitz
        fitz = _fitz
    if requests is None:
        import requests as _requests
        requests = _requests


def parse_multipart_form(body: bytes, content_type: str) -> dict:
    """
    Parse multipart form data using email module.

    Returns dict with field names as keys.
    For files: {'filename': str, 'content': bytes}
    For regular fields: str value
    """
    result = {}

    # Create a proper MIME message
    # Add required headers for email parser
    full_message = b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body

    # Parse using email module
    msg = email.message_from_bytes(full_message, policy=email.policy.HTTP)

    if msg.is_multipart():
        for part in msg.iter_parts():
            # Get Content-Disposition header
            content_disposition = part.get('Content-Disposition', '')

            # Extract field name
            name_match = re.search(r'name="([^"]+)"', content_disposition)
            if not name_match:
                continue

            field_name = name_match.group(1)

            # Check if it's a file
            filename_match = re.search(r'filename="([^"]*)"', content_disposition)

            if filename_match:
                # It's a file
                content = part.get_payload(decode=True)
                result[field_name] = {
                    'filename': filename_match.group(1),
                    'content': content if content else b''
                }
            else:
                # Regular field
                payload = part.get_payload(decode=True)
                if payload:
                    result[field_name] = payload.decode('utf-8', errors='ignore').strip()
                else:
                    # Try getting as string
                    result[field_name] = str(part.get_payload()).strip()

    return result


class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler."""

    def log_message(self, format, *args):
        """Override to suppress default logging."""
        pass

    def do_GET(self):
        """Health check endpoint."""
        try:
            load_dependencies()
            status = {
                'status': 'ok',
                'python_version': sys.version,
                'fitz_version': fitz.version if fitz else 'not loaded'
            }
            self._send_json(200, status)
        except Exception as e:
            self._send_json(500, {
                'status': 'error',
                'error': str(e),
                'traceback': traceback.format_exc()
            })

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_POST(self):
        """Handle PDF OCR requests."""
        try:
            # Load dependencies first
            try:
                load_dependencies()
            except ImportError as e:
                self._send_error(500, f'Failed to load dependencies: {str(e)}')
                return

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

            if content_length > 10 * 1024 * 1024:  # 10MB limit for Hobby plan
                self._send_error(400, 'File too large (max 10MB)')
                return

            body = self.rfile.read(content_length)

            # Parse multipart data
            try:
                form_data = parse_multipart_form(body, content_type)
            except Exception as e:
                self._send_error(400, f'Failed to parse form data: {str(e)}')
                return

            # Get API key - check multiple possible field names
            api_key = form_data.get('api_key') or form_data.get('google_api_key') or form_data.get('apiKey')
            if not api_key or isinstance(api_key, dict):
                # Return debug info
                self._send_error(400, f'API key is required. Received fields: {list(form_data.keys())}')
                return

            # Get PDF file
            file_data = form_data.get('file')
            if not file_data or not isinstance(file_data, dict):
                self._send_error(400, f'PDF file is required. Received fields: {list(form_data.keys())}')
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

    def _send_json(self, status_code: int, data: dict):
        """Send JSON response."""
        response = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _send_error(self, status_code: int, message: str):
        """Send JSON error response."""
        self._send_json(status_code, {'error': message})


def call_vision_api(image_bytes: bytes, api_key: str) -> dict:
    """
    Call Google Cloud Vision API with an image.
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
        response = requests.post(url, json=payload, timeout=55)
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
        except Exception:
            error_msg = f'HTTP {response.status_code}'
        raise ValueError(f'Cloud Vision API error: {error_msg}')

    return response.json()


def extract_text_with_positions(api_response: dict, page_width: float, page_height: float,
                                 image_width: int, image_height: int) -> list:
    """Extract text and positions from Cloud Vision API response."""
    text_blocks = []

    responses = api_response.get('responses', [])
    if not responses:
        return text_blocks

    response = responses[0]

    if 'error' in response:
        error_msg = response['error'].get('message', 'Unknown error')
        raise ValueError(f'Cloud Vision API error: {error_msg}')

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
    """Add invisible text layer to a PDF page."""
    for block in text_blocks:
        x = block['x']
        y = block['y']
        text = block['text']
        height = block['height']
        font_size = max(4, min(height * 0.85, 72))

        try:
            tw = fitz.TextWriter(page.rect)
            tw.append(
                pos=(x, y + height * 0.85),
                text=text,
                fontsize=font_size,
                font=fitz.Font("helv")
            )
            tw.write_text(page, render_mode=3)
        except Exception:
            try:
                page.insert_text(
                    point=(x, y + height * 0.85),
                    text=text,
                    fontsize=font_size,
                    fontname="helv",
                    render_mode=3
                )
            except Exception:
                pass


def process_pdf_ocr(pdf_data: bytes, api_key: str) -> bytes:
    """Process a PDF file and add OCR text layer."""
    if not pdf_data.startswith(b'%PDF'):
        raise ValueError('Invalid PDF file')

    try:
        input_doc = fitz.open(stream=pdf_data, filetype="pdf")
    except Exception as e:
        raise ValueError(f'Failed to open PDF: {str(e)}')

    if input_doc.page_count == 0:
        input_doc.close()
        raise ValueError('PDF has no pages')

    max_pages = min(input_doc.page_count, 5)

    output_doc = fitz.open()

    try:
        for page_num in range(max_pages):
            page = input_doc[page_num]

            dpi = 150
            zoom = dpi / 72
            matrix = fitz.Matrix(zoom, zoom)

            try:
                pixmap = page.get_pixmap(matrix=matrix)
            except Exception as e:
                raise ValueError(f'Failed to render page {page_num + 1}: {str(e)}')

            image_bytes = pixmap.tobytes("jpeg")
            image_width = pixmap.width
            image_height = pixmap.height

            api_response = call_vision_api(image_bytes, api_key)

            text_blocks = extract_text_with_positions(
                api_response,
                page.rect.width,
                page.rect.height,
                image_width,
                image_height
            )

            new_page = output_doc.new_page(
                width=page.rect.width,
                height=page.rect.height
            )

            new_page.insert_image(
                new_page.rect,
                stream=pixmap.tobytes("jpeg")
            )

            add_text_layer_to_page(new_page, text_blocks)

        output_bytes = output_doc.tobytes(deflate=True, garbage=4)

    finally:
        input_doc.close()
        output_doc.close()

    return output_bytes
