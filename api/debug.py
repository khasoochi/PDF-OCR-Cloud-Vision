"""
Debug endpoint to test multipart parsing.
"""

import json
import re
from http.server import BaseHTTPRequestHandler


def parse_multipart(body: bytes, content_type: str) -> dict:
    """Parse multipart form data."""
    result = {}
    debug_info = []

    # Extract boundary
    boundary_match = re.search(r'boundary=([^\s;]+)', content_type)
    if not boundary_match:
        return {'error': 'No boundary found', 'content_type': content_type}

    boundary = boundary_match.group(1).strip('"').strip()
    debug_info.append(f"Boundary: {boundary}")

    # Try with and without leading dashes
    for prefix in ['--', '']:
        boundary_bytes = f'{prefix}{boundary}'.encode()
        parts = body.split(boundary_bytes)
        debug_info.append(f"Parts with prefix '{prefix}': {len(parts)}")

        if len(parts) > 2:  # Found valid parts
            for i, part in enumerate(parts[1:], 1):  # Skip first empty part
                part = part.strip()
                if not part or part == b'--' or part.startswith(b'--'):
                    continue

                # Find header/content separator
                for sep in [b'\r\n\r\n', b'\n\n']:
                    if sep in part:
                        headers_section, content = part.split(sep, 1)
                        headers_text = headers_section.decode('utf-8', errors='ignore')

                        # Clean up content
                        content = content.rstrip(b'\r\n').rstrip(b'--').rstrip(b'\r\n')

                        # Extract field name
                        name_match = re.search(r'name="([^"]+)"', headers_text)
                        if name_match:
                            field_name = name_match.group(1)
                            filename_match = re.search(r'filename="([^"]*)"', headers_text)

                            if filename_match:
                                result[field_name] = {
                                    'filename': filename_match.group(1),
                                    'content_length': len(content),
                                    'content_preview': content[:50].hex() if content else 'empty'
                                }
                            else:
                                result[field_name] = content.decode('utf-8', errors='ignore').strip()
                        break
            break

    return {'fields': result, 'debug': debug_info, 'body_length': len(body)}


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else b''

        result = parse_multipart(body, content_type)
        result['headers'] = {
            'Content-Type': content_type,
            'Content-Length': content_length
        }

        response = json.dumps(result, indent=2).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
