#!/usr/bin/env python3
"""
Local development server for PDF OCR tool.
Serves static files and handles the /api/ocr endpoint.

Usage:
    pip install flask
    python dev_server.py

Then open http://localhost:5000
"""

import os
import sys

# Add the api directory to path so we can import the ocr module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'api'))

from flask import Flask, request, jsonify, send_from_directory, send_file
import io

# Import the processing function from our API module
from ocr import process_pdf_ocr

app = Flask(__name__, static_folder='public')


@app.route('/')
def index():
    """Serve the main page."""
    return send_from_directory('public', 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    """Serve static files from public directory."""
    return send_from_directory('public', filename)


@app.route('/api/ocr', methods=['POST', 'OPTIONS'])
def ocr_endpoint():
    """Handle OCR requests."""
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    try:
        # Get API key
        api_key = request.form.get('api_key')
        if not api_key:
            return jsonify({'error': 'API key is required'}), 400

        # Get file
        if 'file' not in request.files:
            return jsonify({'error': 'PDF file is required'}), 400

        file = request.files['file']
        if not file.filename:
            return jsonify({'error': 'No file selected'}), 400

        # Read PDF data
        pdf_data = file.read()

        # Process OCR
        result_pdf = process_pdf_ocr(pdf_data, api_key)

        # Return the result
        return send_file(
            io.BytesIO(result_pdf),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'searchable_{file.filename}'
        )

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Processing error: {str(e)}'}), 500


if __name__ == '__main__':
    print("Starting PDF OCR development server...")
    print("Open http://localhost:5000 in your browser")
    print("Press Ctrl+C to stop")
    app.run(host='0.0.0.0', port=5000, debug=True)
