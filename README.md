# PDF OCR with Google Cloud Vision

A web-based tool that converts scanned PDFs into searchable PDFs by overlaying OCR text from Google Cloud Vision API.

## Features

- **Scanned PDF to Searchable PDF**: Converts image-based PDFs into text-searchable documents
- **Invisible Text Layer**: Adds OCR text at exact coordinates matching the source image
- **Google Cloud Vision API**: Uses Document Text Detection for accurate OCR
- **Bring Your Own API Key**: Users provide their own Google Cloud Vision API key
- **Simple Web Interface**: Drag-and-drop file upload with progress indicator

## How It Works

1. Upload a scanned PDF file
2. Enter your Google Cloud Vision API key
3. The tool extracts each page as an image
4. Each page is sent to Cloud Vision API for OCR
5. Text with bounding box coordinates is received
6. A new PDF is created with:
   - Original page image as background
   - Invisible text layer positioned at exact coordinates
7. Download the searchable PDF

## Local Development

### Prerequisites

- Python 3.9+
- Google Cloud Vision API key

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/khasoochi/PDF-OCR-Cloud-Vision.git
   cd PDF-OCR-Cloud-Vision
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the development server:
   ```bash
   python dev_server.py
   ```

5. Open http://localhost:5000 in your browser

### Getting a Google Cloud Vision API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Cloud Vision API
4. Go to APIs & Services > Credentials
5. Create an API key
6. (Optional) Restrict the API key to Cloud Vision API only

## Deployment

### Vercel

1. Install Vercel CLI:
   ```bash
   npm i -g vercel
   ```

2. Deploy:
   ```bash
   vercel
   ```

## Project Structure

```
PDF-OCR-Cloud-Vision/
├── api/
│   └── ocr.py              # Serverless function for PDF OCR
├── public/
│   ├── index.html          # Frontend interface
│   ├── style.css           # Styles
│   └── script.js           # Frontend logic
├── dev_server.py           # Local development server
├── requirements.txt        # Python dependencies
├── vercel.json            # Vercel configuration
└── README.md
```

## API Usage

### POST /api/ocr

Converts a scanned PDF to a searchable PDF.

**Request:**
- `Content-Type: multipart/form-data`
- `file`: PDF file to process
- `api_key`: Google Cloud Vision API key

**Response:**
- Success: PDF file download
- Error: JSON with error message

## License

MIT License
