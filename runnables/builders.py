import re
import logging
import math
from typing import List, Optional
from playwright.sync_api import sync_playwright, Error as PlaywrightError
from bs4 import BeautifulSoup
from pydantic import ValidationError  # if using Pydantic for model validation
from modals.instances import SearxInstanceStats

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


class SearxInstanceScraper:

    def __init__(self, url: str = "https://searx.space/"):
        """
        Initializes the scraper with the target URL.
        """
        self.url = url

    @staticmethod
    def safe_extract_float(tag, css_selector) -> Optional[float]:
        """
        Safely extracts text from a CSS selector within a tag and converts it to a float.
        """
        if not tag:
            return None
        element = tag.select_one(css_selector)
        if element and element.text:
            text_content = element.text.strip()
            cleaned_text = re.sub(r'[^\d.-]+', '', text_content)
            if cleaned_text and cleaned_text not in ('-', '.'):
                try:
                    return float(cleaned_text)
                except ValueError:
                    return None
        return None

    def fetch_html(self) -> Optional[str]:
        """
        Uses Playwright to launch a headless browser, navigate to the URL, and retrieve the HTML content.
        """
        html_content = None
        logging.info("Launching Playwright browser...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_default_timeout(60000)  # 60 seconds timeout
                    logging.info(f"Navigating to {self.url}")
                    page.goto(self.url, wait_until='networkidle')
                    logging.info("Page loaded. Extracting content...")
                    html_content = page.content()
                except Exception as page_error:
                    logging.error(
                        f"Error during page navigation or content extraction: {page_error}"
                    )
                    return None
                finally:
                    logging.info("Closing browser.")
                    browser.close()
        except PlaywrightError as pw_error:
            logging.error(f"Error initializing Playwright: {pw_error}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error during Playwright session: {e}")
            return None

        return html_content

    def parse_html(self, html_content: str) -> List[SearxInstanceStats]:
        """
        Parses the provided HTML content with BeautifulSoup to extract instance statistics.
        """
        instances_data = []
        if not html_content:
            logging.warning("No HTML content provided for parsing.")
            return instances_data

        logging.info("Parsing HTML content with BeautifulSoup...")
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            # Find the table body containing the instance data
            table_body = soup.select_one(
                'div.table-responsive > table > tbody')
            if not table_body:
                logging.error(
                    "Could not find the table body (tbody) in the HTML.")
                return instances_data

            rows = table_body.find_all('tr', recursive=False)
            logging.info(f"Found {len(rows)} rows in the table body.")

            for i, row in enumerate(rows):
                cols = row.find_all('td', recursive=False)
                if len(cols) < 1:
                    logging.warning(
                        f"Skipping row {i+1}: Found only {len(cols)} columns, expected at least 1."
                    )
                    continue

                # Extract URL (Column 0)
                url_tag = cols[0].select_one('a[href]')
                url = url_tag['href'].strip(
                ) if url_tag and url_tag.has_attr('href') else None
                if not url or not url.startswith('http'):
                    logging.warning(
                        f"Skipping row {i+1}: Invalid or missing URL '{url}'.")
                    continue

                # Extract stats using helper function (using column indices based on table header)
                search_rt = self.safe_extract_float(
                    cols[9],
                    'span.value-responsetime') if len(cols) > 9 else None
                google_rt = self.safe_extract_float(
                    cols[10],
                    'span.value-responsetime') if len(cols) > 10 else None
                initial_rt = self.safe_extract_float(
                    cols[11],
                    'span.value-responsetime') if len(cols) > 11 else None
                uptime = self.safe_extract_float(
                    cols[12], 'a.value-uptime') if len(cols) > 12 else None

                # Create the SearxInstanceStats model instance
                try:
                    instance_stat = SearxInstanceStats(
                        url=url,
                        search_response_time=search_rt,
                        google_response_time=google_rt,
                        initial_response_time=initial_rt,
                        uptime_percent=uptime)
                    instances_data.append(instance_stat)
                except ValidationError as e:
                    logging.error(
                        f"Pydantic Validation Error for URL {url}: {e}")
                except Exception as model_error:
                    logging.error(
                        f"Error creating Pydantic model for URL {url}: {model_error}"
                    )
        except Exception as parse_error:
            logging.error(
                f"Error processing HTML content with BeautifulSoup: {parse_error}"
            )
            return instances_data

        logging.info(
            f"Successfully processed stats for {len(instances_data)} instances."
        )
        return instances_data

    def get_instance_stats(self) -> List[SearxInstanceStats]:
        """
        Fetches HTML content from the URL and returns a list of SearxInstanceStats objects after parsing.
        """
        html_content = self.fetch_html()
        if not html_content:
            return []
        return self.parse_html(html_content)

    @staticmethod
    def sort_instances_by_stats(
            instances: List[SearxInstanceStats]) -> List[SearxInstanceStats]:
        """
        Sorts a list of SearxInstanceStats objects based on performance criteria.

        Sorting Priority (Lower/Higher is better):
        1. Google Response Time (Ascending)
        2. Initial Response Time (Ascending)
        3. Search Response Time (Ascending)
        4. Uptime Percentage (Descending)
        """
        if not instances:
            return []

        def sort_key(instance: SearxInstanceStats):
            # For ascending sorts, substitute None with infinity
            google_rt = instance.google_response_time if instance.google_response_time is not None else math.inf
            initial_rt = instance.initial_response_time if instance.initial_response_time is not None else math.inf
            search_rt = instance.search_response_time if instance.search_response_time is not None else math.inf
            # For descending sort (uptime), substitute None with negative infinity and negate the value
            uptime = instance.uptime_percent if instance.uptime_percent is not None else -math.inf
            uptime_sort_val = -uptime
            return (google_rt, initial_rt, search_rt, uptime_sort_val)

        return sorted(instances, key=sort_key)
