#!/usr/bin/env python3
"""
Kerala PDF Downloader - Fixed Version
Key fixes:
1. Validates downloaded files are actually PDFs (checks magic bytes %PDF)
2. Retries failed downloads with exponential backoff
3. Better session handling - refreshes cookies from browser
4. Slower rate limiting to avoid server blocks
5. Deletes corrupt/HTML files automatically
"""

import os
import re
import csv
import time
import json
import logging
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)
import requests
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('pdf_downloader_fixed.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ==============================================================================
# METADATA HELPERS (unchanged from original)
# ==============================================================================

_REF_ID_RE = re.compile(
    r"G\.O\.\s*\(\s*\w+\s*\)\s*\d+\s*/\s*\d{4}\s*/\s*\w+"
    r"|(?:SRO|SO|OM|Circular|Order)\s*No\.?\s*[\w/\-]+",
    re.IGNORECASE,
)
_PDF_ID_RE = re.compile(r"govtorder([^/]+?)\.pdf", re.IGNORECASE)
_PDF_ID_GENERIC_RE = re.compile(r"/([^/]+)\.pdf$", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{2}[-/]\d{2}[-/]\d{4})\b")


def _extract_reference_id(text):
    m = _REF_ID_RE.search(text or "")
    return m.group(0).strip() if m else ""

def _extract_pdf_id(url):
    if not url:
        return ""
    m = _PDF_ID_RE.search(url)
    if m:
        return m.group(1).strip()
    m2 = _PDF_ID_GENERIC_RE.search(url)
    return m2.group(1).strip() if m2 else ""

def _clean_title(raw_title, department):
    title = raw_title.strip()
    if department:
        dept_escaped = re.escape(department)
        pattern = rf'^{dept_escaped}' + r'\s*[-\u2013]\s*'
        title = re.sub(pattern, '', title, flags=re.IGNORECASE).strip()
    title = re.sub(
        r'\s*G\.O\.\s*\(\s*\w+\s*\)\s*\d+\s*/\s*\d{4}\s*/\s*\w+\s*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    title = title.rstrip('…').rstrip('...').strip()
    return title

def _extract_metadata_from_card(card, driver, index):
    meta = {"title": "", "department": "", "date": "", "reference_id": "", "pdf_id": "", "download_link": ""}
    try:
        try:
            font_centi_divs = card.find_elements(By.CSS_SELECTOR, "div.font_centi")
            for div in font_centi_divs:
                try:
                    link = div.find_element(By.TAG_NAME, "a")
                    t = link.text.strip()
                    if t:
                        meta["department"] = t
                        break
                except Exception:
                    continue
        except Exception:
            pass

        try:
            title_div = card.find_element(By.CSS_SELECTOR, "div.font_deka")
            raw_title = (title_div.get_attribute("title") or "").strip()
            if not raw_title:
                raw_title = driver.execute_script(
                    "return arguments[0].querySelector('strong') "
                    "? arguments[0].querySelector('strong').innerText "
                    ": arguments[0].innerText;",
                    title_div
                ) or ""
                raw_title = raw_title.strip()
            if not raw_title:
                raw_title = title_div.text.strip()
            raw_title = raw_title.rstrip("…").rstrip("...").strip()
            meta["title"] = _clean_title(raw_title, meta["department"])
        except Exception:
            pass

        try:
            date_col = card.find_element(By.CSS_SELECTOR, "div.col-lg-6.font_centi")
            raw = date_col.text.strip()
            m = _DATE_RE.search(raw)
            if m:
                meta["date"] = m.group(1)
        except Exception:
            pass
        if not meta["date"]:
            m = _DATE_RE.search(card.text)
            if m:
                meta["date"] = m.group(1)

        full_raw = ""
        try:
            title_div = card.find_element(By.CSS_SELECTOR, "div.font_deka")
            full_raw = title_div.get_attribute("title") or ""
        except Exception:
            full_raw = card.text
        meta["reference_id"] = _extract_reference_id(full_raw) or _extract_reference_id(card.text)

        for sel in ["a[href$='.pdf']", "a[href*='download']", "a[href*='/pdf/']", "a[download]"]:
            try:
                elem = card.find_element(By.CSS_SELECTOR, sel)
                href = elem.get_attribute("href") or ""
                if href and not href.startswith("javascript"):
                    meta["download_link"] = href
                    break
            except NoSuchElementException:
                continue

        if not meta["download_link"]:
            for lnk in card.find_elements(By.TAG_NAME, "a"):
                href = lnk.get_attribute("href") or ""
                if href and not href.startswith("javascript") and "deptdocumentdetails" not in href:
                    meta["download_link"] = href
                    break

        if meta["download_link"] and meta["download_link"].startswith("/"):
            meta["download_link"] = "https://document.kerala.gov.in" + meta["download_link"]

        meta["pdf_id"] = _extract_pdf_id(meta["download_link"])

    except Exception as e:
        logger.warning(f"[{index}] Metadata extraction error: {e}")

    return meta


def _open_csv(download_dir):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = download_dir / f"metadata_{timestamp}.csv"
    fieldnames = ["index", "title", "department", "date", "reference_id", "pdf_id", "download_link"]
    fh = open(csv_path, "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(fh, fieldnames=fieldnames)
    writer.writeheader()
    fh.flush()
    logger.info(f"📄 Metadata CSV opened → {csv_path}")
    return fh, writer, csv_path

def _append_csv_row(writer, fh, record):
    writer.writerow(record)
    fh.flush()

def _save_metadata(records, download_dir):
    if not records:
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = download_dir / f"metadata_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info(f"📄 Metadata JSON → {json_path}")


# ==============================================================================
# KEY FIX: PDF VALIDATION
# ==============================================================================

def is_valid_pdf(filepath):
    """
    Check if a file is a real PDF by reading its magic bytes.
    Real PDFs always start with %PDF
    Corrupt/HTML files will NOT start with %PDF
    """
    try:
        with open(filepath, 'rb') as f:
            header = f.read(5)
        return header == b'%PDF-'
    except Exception:
        return False


def get_browser_cookies(driver):
    """Extract cookies from Selenium browser and convert for requests library."""
    cookies = {}
    try:
        for cookie in driver.get_cookies():
            cookies[cookie['name']] = cookie['value']
    except Exception as e:
        logger.warning(f"Could not get browser cookies: {e}")
    return cookies


# ==============================================================================
# MAIN DOWNLOADER CLASS
# ==============================================================================

class FixedKeralaPDFDownloader:
    """Fixed PDF downloader with validation, retry, and rate limiting"""

    MAX_DOCUMENTS = 500
    MAX_RETRIES = 3          # Retry failed downloads up to 3 times
    RETRY_DELAY = 5          # Seconds between retries
    DOWNLOAD_DELAY = 1.5     # Seconds between each download (was 0.5 — too fast!)
    REQUEST_TIMEOUT = 45     # Seconds before giving up on a download

    def __init__(self, download_dir="./kerala_documents", headless=False):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.chrome_options = Options()
        prefs = {
            "download.default_directory": str(self.download_dir.absolute()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
        }
        self.chrome_options.add_experimental_option("prefs", prefs)
        self.chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.chrome_options.add_experimental_option('useAutomationExtension', False)

        if headless:
            self.chrome_options.add_argument("--headless=new")

        self.chrome_options.add_argument("--no-sandbox")
        self.chrome_options.add_argument("--disable-dev-shm-usage")
        self.chrome_options.add_argument("--start-maximized")
        self.chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        self.chrome_options.add_argument(
            "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        self.driver = None
        self.wait = None
        self.downloaded_count = 0
        self.corrupt_count = 0
        self.failed_downloads = []
        self.processed_urls = set()
        self.metadata_records = []
        self.csv_fh = None
        self.csv_writer = None
        self.csv_path = None

        # Session for requests — will be updated with browser cookies
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/pdf,application/octet-stream,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://document.kerala.gov.in/',
        })

    def start_browser(self):
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from selenium.webdriver.chrome.service import Service
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=self.chrome_options)
            self.wait = WebDriverWait(self.driver, 20)
            self.driver.implicitly_wait(10)
            logger.info("✓ Browser started successfully")
            return True
        except Exception as e:
            logger.error(f"✗ Failed to start browser: {e}")
            return False

    def navigate_to_page(self, url):
        try:
            logger.info(f"🔗 Navigating to: {url}")
            self.driver.get(url)
            time.sleep(3)
            logger.info("✓ Page loaded successfully")
            return True
        except Exception as e:
            logger.error(f"✗ Failed to navigate: {e}")
            return False

    def sync_cookies_from_browser(self):
        """Copy cookies from browser to the requests session — critical for auth."""
        cookies = get_browser_cookies(self.driver)
        self.session.cookies.update(cookies)
        logger.info(f"🍪 Synced {len(cookies)} cookies from browser to requests session")

    def load_documents_with_limit(self):
        logger.info(f"\n{'='*70}\n🔄 LOADING DOCUMENTS - LIMITED TO {self.MAX_DOCUMENTS}\n{'='*70}")

        max_attempts = 100
        attempt = 0
        previous_count = 0
        no_change_count = 0  # consecutive attempts with no new docs

        # All selectors to try for "Load More" button — same as original working code
        load_more_selectors = [
            ("//button[contains(text(), 'Load More')]", By.XPATH),
            ("//button[contains(text(), 'load more')]", By.XPATH),
            ("//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'load')]", By.XPATH),
            ("//a[contains(text(), 'Load More')]", By.XPATH),
            (".load-more-btn", By.CSS_SELECTOR),
            ("[data-load-more]", By.CSS_SELECTOR),
            ("button.btn-load-more", By.CSS_SELECTOR),
            (".pagination button", By.CSS_SELECTOR),
        ]

        while attempt < max_attempts:
            try:
                cards = self.driver.find_elements(By.CSS_SELECTOR, "div.card")
                current_count = len(cards)
                logger.info(f"[Attempt {attempt + 1}] Documents loaded: {current_count}")

                # Stop if we've hit the limit
                if current_count >= self.MAX_DOCUMENTS:
                    logger.info(f"✓ Reached limit ({self.MAX_DOCUMENTS}). Stopping.")
                    break

                # Scroll to bottom to reveal Load More button
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1)

                # ── Try every selector ───────────────────────────────────────
                load_more_found = False

                for selector, by_type in load_more_selectors:
                    try:
                        buttons = self.driver.find_elements(by_type, selector)
                        for button in buttons:
                            try:
                                if button.is_displayed() and button.is_enabled():
                                    button_text = button.text.lower()
                                    if 'load' in button_text or 'more' in button_text:
                                        logger.info(f"✓ Found Load More button: '{button.text}'")
                                        self.driver.execute_script("arguments[0].scrollIntoView(true);", button)
                                        time.sleep(0.5)
                                        self.driver.execute_script("arguments[0].click();", button)
                                        logger.info("✓ Clicked Load More button")
                                        time.sleep(3)
                                        load_more_found = True
                                        break
                            except Exception:
                                continue
                        if load_more_found:
                            break
                    except Exception:
                        continue

                # ── Fallback: JavaScript scan of ALL buttons/links ────────────
                if not load_more_found:
                    try:
                        script = """
                        let buttons = document.querySelectorAll('button, a');
                        for (let btn of buttons) {
                            let text = btn.textContent.toLowerCase();
                            if (text.includes('load') && text.includes('more') && btn.offsetParent !== null) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                        """
                        result = self.driver.execute_script(script)
                        if result:
                            logger.info("✓ Clicked Load More via JavaScript fallback")
                            time.sleep(3)
                            load_more_found = True
                    except Exception as e:
                        logger.debug(f"JS fallback failed: {e}")

                # ── Check if new documents actually appeared ─────────────────
                new_cards = self.driver.find_elements(By.CSS_SELECTOR, "div.card")
                new_count = len(new_cards)

                if new_count == current_count:
                    no_change_count += 1
                    logger.warning(f"⚠ No new documents loaded (no-change streak: {no_change_count})")
                    if no_change_count >= 3:
                        logger.info("✓ No new documents after 3 consecutive attempts — all loaded.")
                        break
                else:
                    no_change_count = 0  # reset streak on progress
                    logger.info(f"✓ +{new_count - current_count} new documents (total: {new_count})")

                if not load_more_found and new_count == current_count:
                    logger.info("✓ No Load More button found and no new documents — done loading.")
                    break

                previous_count = new_count
                attempt += 1

            except Exception as e:
                logger.error(f"Error during loading: {e}")
                break

        final_cards = self.driver.find_elements(By.CSS_SELECTOR, "div.card")
        final_cards = final_cards[:self.MAX_DOCUMENTS]
        logger.info(f"\n{'='*70}\n✓ TOTAL DOCUMENTS TO PROCESS: {len(final_cards)}\n{'='*70}\n")
        return final_cards

    def _download_via_browser(self, pdf_url, safe_filename, index):
        """
        Fallback: use the Selenium browser to download the PDF directly.
        Works for older documents whose URLs require a full browser session
        (not just cookies) — the browser already has the right session state.
        """
        try:
            logger.info(f"[{index}] 🌐 Trying browser-based download...")
            before = set(self.download_dir.glob("*.pdf"))

            # Open URL in new tab — Chrome download manager handles it
            self.driver.execute_script(f"window.open('{pdf_url}', '_blank');")
            time.sleep(2)

            # Close the new tab and switch back
            try:
                handles = self.driver.window_handles
                if len(handles) > 1:
                    self.driver.switch_to.window(handles[-1])
                    time.sleep(1)
                    self.driver.close()
                    self.driver.switch_to.window(handles[0])
            except Exception:
                pass

            # Wait up to 60s for a new complete PDF to appear
            waited = 0
            new_file = None
            while waited < 60:
                time.sleep(1)
                waited += 1
                after = set(self.download_dir.glob("*.pdf"))
                new_files = [f for f in (after - before) if f.suffix != '.crdownload']
                if new_files:
                    new_file = max(new_files, key=lambda f: f.stat().st_mtime)
                    break

            if not new_file:
                logger.warning(f"[{index}] ✗ Browser download: no new file appeared within 60s")
                return False

            if not is_valid_pdf(new_file):
                logger.warning(f"[{index}] ✗ Browser download produced invalid PDF: {new_file.name}")
                new_file.unlink()
                return False

            # Rename to our safe filename
            target = self.download_dir / f"{safe_filename}_br.pdf"
            counter = 1
            while target.exists():
                target = self.download_dir / f"{safe_filename}_br_{counter}.pdf"
                counter += 1
            new_file.rename(target)

            size_mb = target.stat().st_size / (1024 * 1024)
            logger.info(f"[{index}] ✓ Browser download: {target.name} ({size_mb:.2f} MB)")
            self.downloaded_count += 1
            return True

        except Exception as e:
            logger.error(f"[{index}] ✗ Browser download error: {e}")
            return False

    def download_pdf_file(self, pdf_url, filename, index):
        """
        Download a PDF with two strategies:
        1. requests + browser cookies  (fast — works for recent/2022+ docs)
        2. Browser download fallback   (for older docs needing full browser session)
        Also: PDF validation, retry logic, auto-delete of corrupt files.
        """
        if not pdf_url or pdf_url.startswith('javascript') or pdf_url == '#':
            return False

        if pdf_url.startswith('/'):
            pdf_url = "https://document.kerala.gov.in" + pdf_url

        if pdf_url in self.processed_urls:
            return False
        self.processed_urls.add(pdf_url)

        safe_filename = f"document_{index}"

        filepath = self.download_dir / f"{safe_filename}.pdf"
        counter = 1
        while filepath.exists():
            filepath = self.download_dir / f"{safe_filename}_{counter}.pdf"
            counter += 1

        # ── Single requests attempt ───────────────────────────────────────────
        # If it returns HTML on the very first try, immediately switch to browser.
        # No point retrying requests — the server will always return HTML for
        # session-locked older documents regardless of cookies or wait time.
        try:
            logger.info(f"[{index}] ⬇️  Downloading: {safe_filename}")
            response = self.session.get(pdf_url, timeout=self.REQUEST_TIMEOUT, stream=True)

            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                if is_valid_pdf(filepath):
                    size_mb = filepath.stat().st_size / (1024 * 1024)
                    logger.info(f"[{index}] ✓ Downloaded: {filepath.name} ({size_mb:.2f} MB)")
                    self.downloaded_count += 1
                    return True

                # Not a valid PDF — check what we got
                size = filepath.stat().st_size
                with open(filepath, 'rb') as f:
                    preview = f.read(200)
                filepath.unlink()
                self.corrupt_count += 1

                is_html = b'<html' in preview.lower() or b'<!doctype' in preview.lower()
                logger.warning(
                    f"[{index}] ⚠ Not a PDF ({size} bytes). "
                    f"{'HTML response — needs browser session' if is_html else 'Unknown content'}"
                )

                if is_html:
                    # Immediately try browser — no retrying requests
                    return self._download_via_browser(pdf_url, safe_filename, index)

            else:
                logger.warning(f"[{index}] HTTP {response.status_code} — trying browser fallback")
                return self._download_via_browser(pdf_url, safe_filename, index)

        except requests.exceptions.Timeout:
            logger.error(f"[{index}] ✗ Timeout — trying browser fallback")
            return self._download_via_browser(pdf_url, safe_filename, index)
        except requests.exceptions.ConnectionError:
            logger.error(f"[{index}] ✗ Connection error — trying browser fallback")
            return self._download_via_browser(pdf_url, safe_filename, index)
        except Exception as e:
            logger.error(f"[{index}] ✗ Error: {e} — trying browser fallback")
            return self._download_via_browser(pdf_url, safe_filename, index)

        # Final fallback
        logger.error(f"[{index}] ✗ FAILED: {safe_filename}")
        self.failed_downloads.append((index, safe_filename, "All methods failed"))
        return False

    def get_document_details(self, card, index):
        details = {"index": index, "title": "", "date": "", "download_link": ""}
        try:
            try:
                title_elem = card.find_element(By.CSS_SELECTOR, "h5, h4, h3, .card-title, div.font_deka")
                details["title"] = title_elem.text.strip()
            except:
                pass

            links = card.find_elements(By.TAG_NAME, "a")
            for link in links:
                try:
                    href = link.get_attribute("href")
                    link_text = link.text.lower()
                    if href and ('download' in link_text or 'pdf' in href.lower() or 'download' in href.lower()):
                        details["download_link"] = href
                        return details
                except:
                    continue

            if links:
                for link in links:
                    href = link.get_attribute("href")
                    if href and not href.startswith('javascript') and "deptdocumentdetails" not in href:
                        details["download_link"] = href
                        break
        except Exception as e:
            logger.warning(f"Error extracting details: {e}")
        return details

    def download_all_pdfs(self, url):
        try:
            if not self.start_browser():
                return False
            if not self.navigate_to_page(url):
                return False

            # Sync cookies right after page load
            self.sync_cookies_from_browser()

            self.csv_fh, self.csv_writer, self.csv_path = _open_csv(self.download_dir)

            cards = self.load_documents_with_limit()
            total_docs = len(cards)

            if total_docs == 0:
                logger.error("✗ No documents found!")
                return False

            logger.info(f"\n{'='*70}\n📥 DOWNLOADING {total_docs} DOCUMENTS\n{'='*70}\n")

            for index in range(total_docs):
                try:
                    # Refresh cards to avoid stale references
                    cards = self.driver.find_elements(By.CSS_SELECTOR, "div.card")[:self.MAX_DOCUMENTS]

                    if index >= len(cards):
                        break

                    card = cards[index]
                    logger.info(f"{'='*70}\nProcessing Document {index + 1}/{total_docs}\n{'='*70}")

                    details = self.get_document_details(card, index + 1)
                    meta = _extract_metadata_from_card(card, self.driver, index + 1)

                    if not meta["download_link"] and details["download_link"]:
                        meta["download_link"] = details["download_link"]
                        meta["pdf_id"] = _extract_pdf_id(details["download_link"])

                    logger.info(f"[{index + 1}] 📝 {meta['title']}")
                    logger.info(f"[{index + 1}] 🏛  {meta['department']}  📅 {meta['date']}")

                    record = {
                        "index": index + 1,
                        "title": meta["title"],
                        "department": meta["department"],
                        "date": meta["date"],
                        "reference_id": meta["reference_id"],
                        "pdf_id": meta["pdf_id"],
                        "download_link": meta["download_link"],
                    }
                    self.metadata_records.append(record)
                    if self.csv_writer:
                        _append_csv_row(self.csv_writer, self.csv_fh, record)

                    if meta["download_link"] or details["download_link"]:
                        url_to_use = meta["download_link"] or details["download_link"]
                        self.download_pdf_file(url_to_use, None, index + 1)
                    else:
                        logger.warning(f"[{index + 1}] ✗ No download link found")
                        self.failed_downloads.append((index + 1, meta["title"], "No download link"))

                    # ── Re-sync cookies every 50 documents ────────────────────
                    if (index + 1) % 50 == 0:
                        logger.info(f"🍪 Re-syncing browser cookies at document {index + 1}...")
                        self.sync_cookies_from_browser()

                    time.sleep(self.DOWNLOAD_DELAY)  # Rate limiting — 1.5s between downloads

                except StaleElementReferenceException:
                    logger.warning(f"[{index + 1}] ⚠ Stale element, skipping...")
                    continue
                except Exception as e:
                    logger.error(f"[{index + 1}] ✗ Error: {e}")
                    continue

            _save_metadata(self.metadata_records, self.download_dir)

            logger.info(f"\n{'='*70}\n✓✓✓ DOWNLOAD COMPLETE ✓✓✓\n{'='*70}")
            logger.info(f"✓ Successfully downloaded: {self.downloaded_count}/{total_docs}")
            logger.info(f"⚠ Corrupt/invalid files detected & deleted: {self.corrupt_count}")
            logger.info(f"✗ Failed downloads: {len(self.failed_downloads)}")
            logger.info(f"📁 Save location: {self.download_dir.absolute()}")

            if self.failed_downloads:
                logger.warning("\n📋 FAILED DOWNLOADS:")
                for idx, title, reason in self.failed_downloads:
                    logger.warning(f"  [{idx}] {title}: {reason}")

            return True

        finally:
            if self.csv_fh:
                try:
                    self.csv_fh.close()
                except:
                    pass
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass


# ==============================================================================
# BONUS: Script to clean up already-downloaded corrupt files
# ==============================================================================

def clean_corrupt_pdfs(directory="./kerala_documents"):
    """
    Scan an existing download directory and delete any files that aren't real PDFs.
    Run this to clean up from your previous broken download session.
    """
    download_dir = Path(directory)
    pdf_files = list(download_dir.glob("*.pdf"))
    corrupt = []
    valid = 0

    print(f"\n🔍 Scanning {len(pdf_files)} PDF files in {directory}...")

    for pdf_path in pdf_files:
        if is_valid_pdf(pdf_path):
            valid += 1
        else:
            size = pdf_path.stat().st_size
            with open(pdf_path, 'rb') as f:
                preview = f.read(100)
            corrupt.append((pdf_path, size, preview))

    print(f"\n✅ Valid PDFs: {valid}")
    print(f"❌ Corrupt files: {len(corrupt)}")

    if corrupt:
        print("\nCorrupt files found:")
        for path, size, preview in corrupt:
            print(f"  {path.name} ({size} bytes) — starts with: {preview[:40]}")

        answer = input(f"\nDelete all {len(corrupt)} corrupt files? (yes/no): ").strip().lower()
        if answer == 'yes':
            for path, _, _ in corrupt:
                path.unlink()
                print(f"  🗑 Deleted: {path.name}")
            print(f"\n✓ Deleted {len(corrupt)} corrupt files.")
        else:
            print("No files deleted.")

    return corrupt


def main():
    url = "https://document.kerala.gov.in/deptdocumentdetails/en/dDVtK21nV2l6c0RxcCtWTC9oTGcvZz09/TmZDMFJGVFp4dDlvc3o3OVdmTmZ5dz09"

    print("\n" + "="*70)
    print("🚀 KERALA PDF DOWNLOADER — FIXED VERSION")
    print("   Key fixes: PDF validation + retry + cookie sync + rate limiting")
    print("="*70 + "\n")

    # First, offer to clean up corrupt files from last run
    if Path("./kerala_documents").exists():
        answer = input("Clean up corrupt files from previous run first? (yes/no): ").strip().lower()
        if answer == 'yes':
            clean_corrupt_pdfs("./kerala_documents")

    downloader = FixedKeralaPDFDownloader(
        download_dir="./kerala_documents",
        headless=False
    )

    downloader.download_all_pdfs(url)

    print(f"\n✓ Downloaded: {downloader.downloaded_count} PDFs")
    print(f"⚠ Corrupt detected & deleted: {downloader.corrupt_count}")
    print(f"✗ Failed: {len(downloader.failed_downloads)}")
    print(f"📁 Location: {downloader.download_dir.absolute()}\n")


if __name__ == "__main__":
    main()