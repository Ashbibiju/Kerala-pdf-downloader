# Kerala Government Document PDF Downloader

An automated Python tool for bulk-downloading government order (G.O.) documents and associated metadata from the Kerala Government Document Portal (document.kerala.gov.in). The script combines Selenium browser automation with a synchronized `requests` session to reliably retrieve PDF files, validate their integrity, and export structured metadata.

## Features

- Automated browsing and pagination handling via Selenium, including "Load More" detection across multiple UI patterns
- Dual-path retrieval: fast HTTP downloads using session cookies synchronized from the live browser session
- PDF integrity validation by checking file magic bytes (`%PDF-`), with automatic deletion of corrupt or HTML-error responses
- Retry logic with exponential backoff for failed downloads
- Rate-limited requests to reduce the risk of server-side blocking
- Metadata extraction per document, including title, department, date, reference ID, and PDF identifier
- CSV and JSON metadata export, written incrementally during the run
- Utility routine to scan a previous download directory and remove corrupt files from earlier runs
- Persistent logging to a local log file for auditing and debugging

## Requirements

- Python 3.8 or higher
- Google Chrome installed locally
- Internet access to document.kerala.gov.in

## Installation

Clone the repository:

```bash
git clone https://github.com/Ashbibiju/kerala-pdf-downloader.git
cd kerala-pdf-downloader
```

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run the script directly:

```bash
python test.py
```

On startup, the script will:

1. Check whether a previous download directory (`./kerala_documents`) exists and, if so, prompt whether to clean up corrupt files from an earlier run.
2. Launch a visible Chrome browser session (headless mode is not enabled by default) and navigate to the configured document listing URL.
3. Load all available document cards, following pagination or "Load More" controls up to a configured maximum.
4. Extract metadata and download links for each document.
5. Download each PDF, validating its content and retrying on failure.
6. Write metadata to CSV and JSON files, and log a summary of successful, corrupt, and failed downloads.

The script runs interactively and requires terminal input at the prompts described above. It is intended for local, manual execution rather than unattended or scheduled runs in its current form.

### Configuration

The target URL and output directory are currently set directly in the `main()` function of the script. To download a different document listing, edit the `url` variable before running.

Key internal parameters (also defined in the script):

| Parameter | Default | Description |
|---|---|---|
| `MAX_DOCUMENTS` | 500 | Maximum number of document cards to load |
| `MAX_RETRIES` | 3 | Retry attempts per failed download |
| `RETRY_DELAY` | 5 seconds | Delay between retry attempts |
| `DOWNLOAD_DELAY` | 1.5 seconds | Delay between individual downloads |
| `REQUEST_TIMEOUT` | 45 seconds | Timeout for a single download request |

## Output

Downloaded files and metadata are written to `./kerala_documents/` by default:

```
kerala_documents/
├── metadata_YYYYMMDD_HHMMSS.csv
├── metadata_YYYYMMDD_HHMMSS.json
└── <downloaded PDF files>
```

### Metadata Fields

- `index` — Sequence number within the run
- `title` — Document title
- `department` — Issuing department
- `date` — Document date
- `reference_id` — Extracted reference or order number
- `pdf_id` — Identifier parsed from the PDF URL
- `download_link` — Direct URL to the source PDF

## Logging

Runtime activity, warnings, and errors are logged to `pdf_downloader_fixed.log` in the working directory, in addition to console output.

## Known Limitations

- The script runs in the foreground with a visible browser window by default; headless mode must be enabled explicitly by passing `headless=True` when constructing the downloader.
- The target URL is hardcoded in `main()` rather than accepted as a command-line argument.
- The interactive prompts assume a terminal environment and are not suited to automated or scheduled execution without modification.
- Depends on the current HTML structure of the Kerala Government Document Portal; UI changes on the source site may require selector updates.

## Disclaimer

This tool is intended for research and archival purposes. Users are responsible for ensuring their use complies with the terms of service of document.kerala.gov.in and for using reasonable rate limits when running bulk downloads.
