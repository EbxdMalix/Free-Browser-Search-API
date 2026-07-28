import urllib.request
import urllib.parse
import json
import time
import logging
import asyncio
import ssl
import httpx
from typing import List, Optional, Dict, Any
from bs4 import BeautifulSoup
from playwright.async_api import Browser

from modals.instances import SearxInstanceStats
from markdownify import markdownify as md
from states.db import update_instance_status

logger = logging.getLogger(__name__)

async def http_get(url: str, timeout: float = 3.0, client: Optional[httpx.AsyncClient] = None, verify: bool = True) -> Optional[str]:
    """Asynchronous HTTP GET using httpx.AsyncClient if available, falling back to urllib."""
    if client:
        try:
            response = await client.get(url, timeout=timeout, verify=verify)
            if response.status_code >= 400:
                return None
            return response.text
        except Exception as e:
            logger.debug(f"httpx GET failed for {url}: {e}")
            return None
            
    # Fallback urllib fetch
    def _fetch():
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8'
            }
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
            if response.status >= 400:
                return None
            return response.read().decode('utf-8', errors='ignore')
    
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.debug(f"Urllib HTTP GET failed for {url}: {e}")
        return None

class SearXNGEngine:
    """
    Interface to connect with SearXNG using an HTTP-first cascade approach.
    """

    def __init__(self, query: str, instances: List[SearxInstanceStats], browser: Optional[Browser] = None, 
                 semaphore: Optional[asyncio.Semaphore] = None, client: Optional[httpx.AsyncClient] = None):
        self.query = query
        self.instances = instances
        self.browser = browser
        self.semaphore = semaphore or asyncio.Semaphore(3)
        self.client = client

    def _scrape_page_html(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extract articles from a BeautifulSoup HTML page."""
        articles_found = soup.find_all('article', class_='result')
        results = []

        for article in articles_found:
            a_tag = article.find('a', class_='url_header')
            if not (a_tag and a_tag.has_attr('href')):
                continue
            link = a_tag['href']
            title = a_tag.get_text(strip=True)

            snippet = ""
            content_p = article.find(['p', 'div'], class_='content')
            if content_p:
                snippet = content_p.get_text(strip=True)

            source = ""
            engines_div = article.find('div', class_='engines')
            if engines_div:
                source_span = engines_div.find('span')
                if source_span:
                    source = source_span.get_text(strip=True)

            results.append({
                "title": title,
                "link": link,
                "snippet": snippet,
                "source": source
            })

        return results

    async def fetch_results_from_instance(self, instance: SearxInstanceStats) -> List[Dict[str, Any]]:
        """
        Attempts to fetch search results from a single SearxNG instance
        using JSON-first format, HTML parsing fallback, and emergency Playwright fallback.
        """
        instance_url = str(instance.url)
        escaped_query = urllib.parse.quote(self.query)
        
        # 1. Prefer JSON API (format=json)
        json_url = f"{instance_url}search?q={escaped_query}&format=json"
        start_time = time.time()
        try:
            logger.info(f"Trying SearXNG JSON endpoint: {json_url}")
            response_text = await http_get(json_url, timeout=3.0, client=self.client, verify=False)
            if response_text:
                data = json.loads(response_text)
                raw_results = data.get("results", [])
                if raw_results:
                    latency = int((time.time() - start_time) * 1000)
                    await asyncio.to_thread(update_instance_status, instance_url, True, latency)
                    
                    results = []
                    for item in raw_results:
                        results.append({
                            "title": item.get("title", ""),
                            "link": item.get("url", ""),
                            "snippet": item.get("content", ""),
                            "source": ", ".join(item.get("engines", [])) or item.get("engine", "")
                        })
                    logger.info(f"JSON fetch succeeded for {instance_url}: {len(results)} results.")
                    return results
        except Exception as e:
            logger.debug(f"JSON query failed for {instance_url}: {e}")

        # 2. Fallback: HTTP HTML Scraping
        html_url = f"{instance_url}search?q={escaped_query}"
        try:
            logger.info(f"Fallback to HTTP HTML endpoint: {html_url}")
            response_text = await http_get(html_url, timeout=3.0, client=self.client, verify=False)
            if response_text:
                soup = BeautifulSoup(response_text, 'html.parser')
                results = self._scrape_page_html(soup)
                if results:
                    latency = int((time.time() - start_time) * 1000)
                    await asyncio.to_thread(update_instance_status, instance_url, True, latency)
                    logger.info(f"HTML HTTP fetch succeeded for {instance_url}: {len(results)} results.")
                    return results
        except Exception as e:
            logger.debug(f"HTML HTTP query failed for {instance_url}: {e}")

        # 3. Last Resort Fallback: Playwright Search
        if self.browser:
            logger.info(f"Last resort: Playwright search for {instance_url}")
            try:
                # Limit Playwright usage with semaphore to prevent bottlenecks
                async with self.semaphore:
                    context = await self.browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        viewport={"width": 1280, "height": 720},
                        device_scale_factor=1,
                        locale="en-US",
                        timezone_id="America/New_York"
                    )
                    page = await context.new_page()
                    try:
                        await page.goto(html_url, timeout=5000, wait_until="domcontentloaded")
                        html_content = await page.content()
                        soup = BeautifulSoup(html_content, 'html.parser')
                        results = self._scrape_page_html(soup)
                        if results:
                            latency = int((time.time() - start_time) * 1000)
                            await asyncio.to_thread(update_instance_status, instance_url, True, latency)
                            logger.info(f"Playwright search succeeded for {instance_url}: {len(results)} results.")
                            return results
                    finally:
                        await page.close()
                        await context.close()
            except Exception as e:
                logger.error(f"Playwright fallback search failed for {instance_url}: {e}")

        # Mark instance as failed
        await asyncio.to_thread(update_instance_status, instance_url, False)
        return []

    async def crawl_article_content(self, link: str) -> Optional[str]:
        """
        Crawls the link target using HTTP-first, falling back to Playwright if needed.
        Uses verify=True globally for crawler requests to avoid MITM vulnerability.
        """
        # A. HTTP GET crawl first
        try:
            content = await http_get(link, timeout=5.0, client=self.client, verify=True)
            if content:
                soup = BeautifulSoup(content, 'html.parser')
                for tag in soup(["script", "style", "meta", "link", "noscript"]):
                    tag.decompose()
                body = soup.find('body')
                if body:
                    markdown_text = md(body.get_text(separator=' ', strip=True)).strip()
                    if markdown_text:
                        return markdown_text
        except Exception as e:
            logger.debug(f"HTTP crawl failed for {link}: {e}")

        # B. Fallback to Playwright (rare fallback)
        if self.browser:
            logger.info(f"Fallback to Playwright crawling for: {link}")
            async with self.semaphore:
                context = None
                page = None
                try:
                    context = await self.browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        viewport={"width": 1280, "height": 720},
                        device_scale_factor=1,
                        locale="en-US",
                        timezone_id="America/New_York"
                    )
                    page = await context.new_page()
                    # Wait up to 5 seconds
                    response = await page.goto(link, wait_until="domcontentloaded", timeout=5000)
                    if response and response.status < 400:
                        await page.wait_for_timeout(2000)  # Wait for JS loading
                        html_content = await page.content()
                        soup = BeautifulSoup(html_content, 'html.parser')
                        for tag in soup(["script", "style", "meta", "link", "noscript"]):
                            tag.decompose()
                        body = soup.find('body')
                        if body:
                            markdown_text = md(body.get_text(separator=' ', strip=True)).strip()
                            if markdown_text:
                                return markdown_text
                except Exception as e:
                    logger.debug(f"Playwright crawl failed for {link}: {e}")
                finally:
                    if page:
                        await page.close()
                    if context:
                        await context.close()
        return None
