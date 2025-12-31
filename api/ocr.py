"""
PDF OCR API endpoint using Google Cloud Vision.

Converts scanned PDFs into searchable PDFs by adding an invisible text layer
with OCR text positioned at exact coordinates.
"""

import base64
import cgi
import io
import json
from http.server import BaseHTTPRequestHandler

import fitz  # PyMuPDF
import requests


class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler."""

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
            # Parse multipart form data
            content_type = self.headers.get('Content-Type', '')

            if 'multipart/form-data' not in content_type:
                self._send_error(400, 'Content-Type must be multipart/form-data')
                return

            # Parse the multipart data
            form_data = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    'REQUEST_METHOD': 'POST',
                    'CONTENT_TYPE': content_type
                }
            )

            # Get API key
            api_key = form_data.getvalue('api_key')
            if not api_key:
                self._send_error(400, 'API key is required')
                return

            # Get PDF file
            if 'file' not in form_data:
                self._send_error(400, 'PDF file is required')
                return

            file_item = form_data['file']
            if not file_item.filename:
                self._send_error(400, 'No file uploaded')
                return

            pdf_data = file_item.file.read()

            # Process the PDF
            result_pdf = process_pdf_ocr(pdf_data, api_key)

            # Send the result
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition',
                           f'attachment; filename="searchable_{file_item.filename}"')
            self.send_header('Content-Length', len(result_pdf))
            self.end_headers()
            self.wfile.write(result_pdf)

        except ValueError as e:
            self._send_error(400, str(e))
        except Exception as e:
            self._send_error(500, f'Processing error: {str(e)}')

    def _send_error(self, status_code: int, message: str):
        """Send JSON error response."""
        self.send_response(status_code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}).encode())


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
                'type': 'DOCUMENT_TEXT_DETECTION',
                'maxResults': 50
            }]
        }]
    }

    # Make API call
    response = requests.post(url, json=payload, timeout=60)

    if response.status_code != 200:
        error_msg = response.json().get('error', {}).get('message', 'Unknown error')
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

    # Get the full text annotation
    full_annotation = responses[0].get('fullTextAnnotation')
    if not full_annotation:
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
                    vertices = word.get('boundingBox', {}).get('vertices', [])
                    if len(vertices) < 4:
                        continue

                    # Calculate position (convert from image coords to PDF coords)
                    # Vertices are in order: top-left, top-right, bottom-right, bottom-left
                    x_coords = [v.get('x', 0) for v in vertices]
                    y_coords = [v.get('y', 0) for v in vertices]

                    min_x = min(x_coords) * scale_x
                    min_y = min(y_coords) * scale_y
                    max_x = max(x_coords) * scale_x
                    max_y = max(y_coords) * scale_y

                    # PDF coordinates are from bottom-left, but we're working with
                    # image coordinates from top-left. PyMuPDF handles this.
                    text_blocks.append({
                        'text': word_text,
                        'x': min_x,
                        'y': min_y,  # Top of text box
                        'width': max_x - min_x,
                        'height': max_y - min_y,
                        'font_size': max_y - min_y  # Approximate font size from height
                    })

    return text_blocks


def add_text_layer_to_page(page: fitz.Page, text_blocks: list):
    """
    Add invisible text layer to a PDF page.

    Args:
        page: PyMuPDF page object
        text_blocks: List of text blocks with positions
    """
    for block in text_blocks:
        # Create a text insertion point
        x = block['x']
        y = block['y']
        text = block['text']
        font_size = max(6, min(block['font_size'] * 0.8, 72))  # Clamp font size

        # Create invisible text (render mode 3 = invisible)
        # We use insert_text with a transparent color
        try:
            # Insert text at position with invisible rendering
            text_writer = fitz.TextWriter(page.rect)

            # Add text to writer
            text_writer.append(
                pos=(x, y + block['height']),  # baseline position
                text=text,
                fontsize=font_size,
                font=fitz.Font("helv")  # Standard font
            )

            # Write to page with invisible ink (alpha = 0)
            text_writer.write_text(page, color=(0, 0, 0), render_mode=3)

        except Exception:
            # Fallback: try simple text insertion
            try:
                page.insert_text(
                    point=(x, y + block['height']),
                    text=text,
                    fontsize=font_size,
                    fontname="helv",
                    render_mode=3,  # Invisible
                    color=(0, 0, 0)
                )
            except Exception:
                # Skip this text block if insertion fails
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
    # Open the input PDF
    input_doc = fitz.open(stream=pdf_data, filetype="pdf")

    # Create output document
    output_doc = fitz.open()

    # Process each page
    for page_num in range(len(input_doc)):
        page = input_doc[page_num]

        # Render page as image (300 DPI for good OCR quality)
        # Using a matrix to scale: 300/72 = 4.166... for 300 DPI
        dpi = 300
        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix)

        # Convert to PNG bytes
        image_bytes = pixmap.tobytes("png")
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

        # Insert the original page as an image
        # First render at original resolution
        orig_pixmap = page.get_pixmap()
        new_page.insert_image(
            new_page.rect,
            stream=orig_pixmap.tobytes("png")
        )

        # Add invisible text layer
        add_text_layer_to_page(new_page, text_blocks)

    # Save to bytes
    output_bytes = output_doc.tobytes()

    # Cleanup
    input_doc.close()
    output_doc.close()

    return output_bytes
