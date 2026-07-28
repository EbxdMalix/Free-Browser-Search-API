from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
import asyncio
import logging
import time
import urllib.parse
import json
import httpx

from modals.inputs import SearchQueryParams
from modals.results import SearchResponse
from states.db import init_db, save_instances, get_alive_instances, update_instance_status
from runnables.builders import SearxInstanceScraper
from workflows.text_query_search import searxng_query
from runnables.searchengines import http_get
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def ping_instance_health(url: str, client: Optional[httpx.AsyncClient] = None) -> None:
    """Ping the home page of a SearXNG instance to check if it is online and responsive."""
    start = time.time()
    try:
        # Override SSL validation with verify=False specifically for the dynamic SearXNG endpoints
        res = await http_get(url, timeout=3.0, client=client, verify=False)
        # If response contains html structure, consider it alive
        if res and ("<html" in res.lower() or "<body" in res.lower() or "<div" in res.lower()):
            latency = int((time.time() - start) * 1000)
            await asyncio.to_thread(update_instance_status, url, True, latency)
            return
    except Exception:
        pass
        
    await asyncio.to_thread(update_instance_status, url, False)

async def health_check_loop(client: httpx.AsyncClient):
    """Lightweight 15-minute HTTP check loop"""
    # Wait a bit on startup to avoid thundering herd during initialization
    await asyncio.sleep(10)
    while True:
        try:
            instances = await asyncio.to_thread(get_alive_instances)
            if instances:
                logger.info(f"Background Health Checker: checking {len(instances)} instances...")
                sem = asyncio.Semaphore(5)
                async def check_worker(inst_url):
                    async with sem:
                        await ping_instance_health(inst_url, client)
                await asyncio.gather(*(check_worker(inst["url"]) for inst in instances), return_exceptions=True)
                logger.info("Background Health Checker: finished health check cycle.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in health check loop: {e}")
        await asyncio.sleep(15 * 60) # 15 minutes

async def instance_refresh_loop():
    """Scrape fresh SearXNG instances every 12 hours"""
    while True:
        try:
            logger.info("Background Instance Refresh: scraping searx.space...")
            scraper = SearxInstanceScraper()
            instances = await asyncio.to_thread(scraper.get_instance_stats)
            if instances:
                inst_dicts = []
                for inst in instances:
                    lat = 1000
                    if inst.google_response_time is not None:
                        lat = int(inst.google_response_time * 1000)
                    elif inst.initial_response_time is not None:
                        lat = int(inst.initial_response_time * 1000)
                    elif inst.search_response_time is not None:
                        lat = int(inst.search_response_time * 1000)
                        
                    inst_dicts.append({
                        "url": str(inst.url),
                        "latency_ms": lat
                    })
                await asyncio.to_thread(save_instances, inst_dicts)
                logger.info(f"Background Instance Refresh: successfully updated {len(inst_dicts)} instances.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in instance refresh loop: {e}")
        await asyncio.sleep(12 * 60 * 60) # 12 hours

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database schema
    logger.info("Initializing database...")
    await asyncio.to_thread(init_db)

    # Reset all instances status to alive on startup to recover from past false-positive dead markers
    def _reset_instances():
        from states.db import _get_connection
        try:
            with _get_connection() as conn:
                conn.execute("UPDATE instances SET status = 'alive', fail_count = 0")
                conn.commit()
            logger.info("Reset all instances status to alive on startup.")
        except Exception as e:
            logger.error(f"Failed to reset instances on startup: {e}")
    await asyncio.to_thread(_reset_instances)

    # Scrape instances on first run if DB is empty
    existing_instances = await asyncio.to_thread(get_alive_instances)
    if not existing_instances:
        logger.info("Database is empty. Populating initial instance list...")
        scraper = SearxInstanceScraper()
        instances = await asyncio.to_thread(scraper.get_instance_stats)
        if instances:
            inst_dicts = []
            for inst in instances:
                lat = 1000
                if inst.google_response_time is not None:
                    lat = int(inst.google_response_time * 1000)
                elif inst.initial_response_time is not None:
                    lat = int(inst.initial_response_time * 1000)
                elif inst.search_response_time is not None:
                    lat = int(inst.search_response_time * 1000)
                inst_dicts.append({
                    "url": str(inst.url),
                    "latency_ms": lat
                })
            await asyncio.to_thread(save_instances, inst_dicts)
            logger.info(f"Successfully populated database with {len(inst_dicts)} instances.")

    # Initialize global httpx AsyncClient with connection pooling
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
    app.state.http_client = httpx.AsyncClient(
        limits=limits,
        verify=True,  # Secure verification by default
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        },
        timeout=httpx.Timeout(3.0)
    )

    # Launch global Playwright browser
    logger.info("Starting Playwright browser...")
    app.state.playwright = await async_playwright().start()
    app.state.browser = await app.state.playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu"
        ]
    )
    app.state.semaphore = asyncio.Semaphore(3)

    # Spawn background scheduler tasks
    app.state.health_task = asyncio.create_task(health_check_loop(app.state.http_client))
    app.state.refresh_task = asyncio.create_task(instance_refresh_loop())

    yield

    # Clean up background tasks
    logger.info("Stopping background tasks...")
    app.state.health_task.cancel()
    app.state.refresh_task.cancel()
    await asyncio.gather(app.state.health_task, app.state.refresh_task, return_exceptions=True)

    # Close Playwright browser
    logger.info("Closing Playwright browser...")
    await app.state.browser.close()
    await app.state.playwright.stop()

    # Close HTTP client
    await app.state.http_client.aclose()


app = FastAPI(title="Free Search API Gateway", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    html = get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
    )
    
    # Premium Dark Theme style customization for Swagger UI
    custom_css = """
    <style>
        body {
            background-color: #0b0f19 !important;
            color: #f1f5f9 !important;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
        }
        .swagger-ui {
            background-color: #0b0f19 !important;
            color: #f1f5f9 !important;
        }
        .swagger-ui .topbar {
            background-color: #111827 !important;
            border-bottom: 1px solid #1f2937 !important;
            padding: 12px 0 !important;
        }
        .swagger-ui .topbar .download-url-wrapper input[type=text] {
            border: 1px solid #374151 !important;
            background: #1f2937 !important;
            color: #f1f5f9 !important;
            border-radius: 6px !important;
        }
        .swagger-ui .topbar .download-url-wrapper .download-url-button {
            background: #0ea5e9 !important;
            color: #ffffff !important;
            border-radius: 6px !important;
        }
        .swagger-ui .info .title {
            color: #38bdf8 !important;
            font-size: 32px !important;
            font-weight: 700 !important;
            letter-spacing: -0.025em !important;
        }
        .swagger-ui .info p, .swagger-ui .info li, .swagger-ui .info td, .swagger-ui .info th {
            color: #94a3b8 !important;
            font-size: 14px !important;
            line-height: 1.6 !important;
        }
        .swagger-ui .scheme-container {
            background-color: #111827 !important;
            box-shadow: none !important;
            border: 1px solid #1f2937 !important;
            border-radius: 8px !important;
            padding: 20px !important;
            margin: 20px 0 !important;
        }
        .swagger-ui select {
            background-color: #1f2937 !important;
            color: #f1f5f9 !important;
            border: 1px solid #374151 !important;
            border-radius: 6px !important;
            padding: 6px 10px !important;
        }
        .swagger-ui input[type=text] {
            background-color: #1f2937 !important;
            color: #f1f5f9 !important;
            border: 1px solid #374151 !important;
            border-radius: 6px !important;
            padding: 8px 12px !important;
        }
        .swagger-ui .opblock.opblock-get {
            background: rgba(14, 165, 233, 0.04) !important;
            border-color: #0ea5e9 !important;
            border-radius: 8px !important;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06) !important;
        }
        .swagger-ui .opblock.opblock-get .opblock-summary-method {
            background: #0ea5e9 !important;
            color: #ffffff !important;
            border-radius: 6px !important;
            font-weight: 600 !important;
        }
        .swagger-ui .opblock.opblock-get .opblock-summary {
            border-color: #0ea5e9 !important;
            padding: 10px 16px !important;
        }
        .swagger-ui .opblock-summary-path {
            color: #f1f5f9 !important;
            font-weight: 500 !important;
            font-family: monospace !important;
        }
        .swagger-ui .opblock-summary-description {
            color: #94a3b8 !important;
        }
        .swagger-ui .btn.try-out__btn {
            background-color: #0ea5e9 !important;
            color: #ffffff !important;
            border: none !important;
            border-radius: 6px !important;
            transition: all 0.2s ease !important;
        }
        .swagger-ui .btn.try-out__btn:hover {
            background-color: #0284c7 !important;
        }
        .swagger-ui .btn.execute {
            background-color: #10b981 !important;
            color: #ffffff !important;
            border: none !important;
            border-radius: 6px !important;
        }
        .swagger-ui .btn.execute:hover {
            background-color: #059669 !important;
        }
        .swagger-ui .opblock-description-wrapper p, .swagger-ui .opblock-external-docs-wrapper p, .swagger-ui .opblock-title_normal p {
            color: #cbd5e1 !important;
        }
        .swagger-ui .tabli button {
            color: #cbd5e1 !important;
            font-weight: 600 !important;
        }
        .swagger-ui .response-col_status {
            color: #f1f5f9 !important;
            font-weight: 600 !important;
        }
        .swagger-ui table thead tr td, .swagger-ui table thead tr th {
            color: #94a3b8 !important;
            border-bottom: 1px solid #1f2937 !important;
        }
        .swagger-ui .dialog-ux .modal-ux {
            background-color: #111827 !important;
            border: 1px solid #1f2937 !important;
            border-radius: 12px !important;
        }
        .swagger-ui .dialog-ux .modal-ux-header h3 {
            color: #f1f5f9 !important;
        }
        .swagger-ui .dialog-ux .modal-ux-content p {
            color: #94a3b8 !important;
        }
        .swagger-ui .model-box {
            background-color: #111827 !important;
            border: 1px solid #1f2937 !important;
            border-radius: 6px !important;
            padding: 10px !important;
        }
        .swagger-ui .model {
            color: #cbd5e1 !important;
        }
        .swagger-ui .prop-type {
            color: #38bdf8 !important;
        }
        .swagger-ui .prop-format {
            color: #64748b !important;
        }
        .swagger-ui .servers-title {
            color: #f1f5f9 !important;
        }
        .swagger-ui .servers > label select {
            background-color: #1f2937 !important;
            color: #f1f5f9 !important;
        }
        .swagger-ui pre {
            background-color: #030712 !important;
            color: #a5f3fc !important;
            border: 1px solid #1f2937 !important;
            border-radius: 8px !important;
            padding: 16px !important;
        }
        .swagger-ui code {
            background-color: #030712 !important;
            color: #a5f3fc !important;
        }
        .swagger-ui .opblock-body pre.microlight {
            background-color: #030712 !important;
        }
        .swagger-ui .parameter__name {
            color: #f1f5f9 !important;
            font-weight: 600 !important;
        }
        .swagger-ui .parameter__type {
            color: #38bdf8 !important;
        }
        .swagger-ui .parameter__in {
            color: #64748b !important;
        }
    </style>
    """
    body_content = html.body.decode("utf-8")
    modified_body = body_content.replace("</head>", f"{custom_css}</head>")
    return HTMLResponse(content=modified_body)


@app.get("/search", response_model=SearchResponse)
async def search(query: str, max_results: int = 10):
    """
    Endpoint to search for a query and return results with additional context.
    """
    if max_results < 1 or max_results > 20:
        raise HTTPException(status_code=400,
                            detail="max_results must be between 1 and 20")

    try:
        params = SearchQueryParams(query=query, max_results=max_results)
        
        # Enforce global request timeout of 15 seconds
        response = await asyncio.wait_for(
            searxng_query(params, app.state.browser, app.state.semaphore, app.state.http_client),
            timeout=15.0
        )
        return response
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request deadline exceeded (15.0s)")
    except Exception as e:
        logger.error(f"Search endpoint exception: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/update-instances")
async def update_instances_endpoint():
    logger.info("Triggering update instances manually...")
    scraper = SearxInstanceScraper()
    instances = await asyncio.to_thread(scraper.get_instance_stats)
    if instances:
        inst_dicts = []
        for inst in instances:
            lat = 1000
            if inst.google_response_time is not None:
                lat = int(inst.google_response_time * 1000)
            elif inst.initial_response_time is not None:
                lat = int(inst.initial_response_time * 1000)
            elif inst.search_response_time is not None:
                lat = int(inst.search_response_time * 1000)
            inst_dicts.append({
                "url": str(inst.url),
                "latency_ms": lat
            })
        await asyncio.to_thread(save_instances, inst_dicts)
        return {"instances_found": len(inst_dicts)}
    return {"instances_found": 0}


@app.get("/get-instances")
async def get_instances_endpoint():
    instances = await asyncio.to_thread(get_alive_instances)
    return {"available_instances": instances}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=11236)
