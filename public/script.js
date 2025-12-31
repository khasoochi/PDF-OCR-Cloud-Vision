/**
 * PDF OCR Frontend Application
 * Handles file upload, API communication, and result display
 */

(function() {
    'use strict';

    // DOM Elements
    const form = document.getElementById('ocr-form');
    const apiKeyInput = document.getElementById('api-key');
    const toggleKeyBtn = document.getElementById('toggle-key');
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('pdf-file');
    const dropZoneContent = dropZone.querySelector('.drop-zone-content');
    const fileInfo = dropZone.querySelector('.file-info');
    const fileName = dropZone.querySelector('.file-name');
    const removeFileBtn = dropZone.querySelector('.remove-file');
    const submitBtn = document.getElementById('submit-btn');
    const btnText = submitBtn.querySelector('.btn-text');
    const btnLoading = submitBtn.querySelector('.btn-loading');
    const progressSection = document.getElementById('progress-section');
    const progressFill = document.getElementById('progress-fill');
    const progressText = document.getElementById('progress-text');
    const resultSection = document.getElementById('result-section');
    const downloadLink = document.getElementById('download-link');
    const processAnotherBtn = document.getElementById('process-another');
    const errorSection = document.getElementById('error-section');
    const errorText = document.getElementById('error-text');
    const tryAgainBtn = document.getElementById('try-again');

    // State
    let selectedFile = null;
    let resultBlob = null;

    // Initialize
    function init() {
        // Load saved API key from localStorage
        const savedKey = localStorage.getItem('cloudVisionApiKey');
        if (savedKey) {
            apiKeyInput.value = savedKey;
        }

        // Event listeners
        toggleKeyBtn.addEventListener('click', toggleApiKeyVisibility);
        apiKeyInput.addEventListener('change', saveApiKey);

        dropZone.addEventListener('click', () => fileInput.click());
        dropZone.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fileInput.click();
            }
        });

        dropZone.addEventListener('dragover', handleDragOver);
        dropZone.addEventListener('dragleave', handleDragLeave);
        dropZone.addEventListener('drop', handleDrop);

        fileInput.addEventListener('change', handleFileSelect);
        removeFileBtn.addEventListener('click', handleRemoveFile);

        form.addEventListener('submit', handleSubmit);

        processAnotherBtn.addEventListener('click', resetForm);
        tryAgainBtn.addEventListener('click', resetForm);
    }

    // Toggle API key visibility
    function toggleApiKeyVisibility() {
        const type = apiKeyInput.type === 'password' ? 'text' : 'password';
        apiKeyInput.type = type;
    }

    // Save API key to localStorage
    function saveApiKey() {
        const key = apiKeyInput.value.trim();
        if (key) {
            localStorage.setItem('cloudVisionApiKey', key);
        } else {
            localStorage.removeItem('cloudVisionApiKey');
        }
    }

    // Drag and drop handlers
    function handleDragOver(e) {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
    }

    function handleDragLeave(e) {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
    }

    function handleDrop(e) {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');

        const files = e.dataTransfer.files;
        if (files.length > 0) {
            const file = files[0];
            if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) {
                setFile(file);
            } else {
                showError('Please upload a PDF file.');
            }
        }
    }

    // File selection handlers
    function handleFileSelect(e) {
        const file = e.target.files[0];
        if (file) {
            setFile(file);
        }
    }

    function setFile(file) {
        selectedFile = file;
        fileName.textContent = file.name;
        dropZoneContent.hidden = true;
        fileInfo.hidden = false;
    }

    function handleRemoveFile(e) {
        e.stopPropagation();
        selectedFile = null;
        fileInput.value = '';
        dropZoneContent.hidden = false;
        fileInfo.hidden = true;
    }

    // Form submission
    async function handleSubmit(e) {
        e.preventDefault();

        const apiKey = apiKeyInput.value.trim();
        if (!apiKey) {
            showError('Please enter your Google Cloud Vision API key.');
            return;
        }

        if (!selectedFile) {
            showError('Please select a PDF file.');
            return;
        }

        // Save API key
        saveApiKey();

        // Show loading state
        setLoading(true);
        showProgress();

        try {
            const result = await processOCR(selectedFile, apiKey);
            showResult(result);
        } catch (error) {
            showError(error.message || 'An unexpected error occurred.');
        } finally {
            setLoading(false);
        }
    }

    // Process OCR
    async function processOCR(file, apiKey) {
        updateProgress(10, 'Uploading PDF...');

        const formData = new FormData();
        formData.append('api_key', apiKey);
        formData.append('file', file);

        updateProgress(20, 'Processing with OCR...');

        const response = await fetch('/api/ocr', {
            method: 'POST',
            body: formData
        });

        updateProgress(80, 'Generating searchable PDF...');

        if (!response.ok) {
            let errorMessage = 'Processing failed';
            try {
                const errorData = await response.json();
                errorMessage = errorData.error || errorMessage;
            } catch {
                // Response wasn't JSON
                errorMessage = `Server error: ${response.status}`;
            }
            throw new Error(errorMessage);
        }

        const blob = await response.blob();
        updateProgress(100, 'Complete!');

        return blob;
    }

    // UI State functions
    function setLoading(loading) {
        submitBtn.disabled = loading;
        btnText.hidden = loading;
        btnLoading.hidden = !loading;
    }

    function showProgress() {
        form.hidden = true;
        progressSection.hidden = false;
        resultSection.hidden = true;
        errorSection.hidden = true;
    }

    function updateProgress(percent, text) {
        progressFill.style.width = `${percent}%`;
        progressText.textContent = text;
    }

    function showResult(blob) {
        resultBlob = blob;

        // Create download URL
        const url = URL.createObjectURL(blob);
        downloadLink.href = url;
        downloadLink.download = `searchable_${selectedFile.name}`;

        progressSection.hidden = true;
        resultSection.hidden = false;
    }

    function showError(message) {
        errorText.textContent = message;
        form.hidden = true;
        progressSection.hidden = true;
        resultSection.hidden = true;
        errorSection.hidden = false;
    }

    function resetForm() {
        // Clean up blob URL
        if (resultBlob) {
            URL.revokeObjectURL(downloadLink.href);
            resultBlob = null;
        }

        // Reset file selection
        selectedFile = null;
        fileInput.value = '';
        dropZoneContent.hidden = false;
        fileInfo.hidden = true;

        // Reset progress
        updateProgress(0, 'Preparing...');

        // Show form
        form.hidden = false;
        progressSection.hidden = true;
        resultSection.hidden = true;
        errorSection.hidden = true;
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
