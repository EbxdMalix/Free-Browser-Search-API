import time
import asyncio
import logging
import httpx
from typing import List, Dict, Any, Optional
from pydantic import HttpUrl

from modals.results import SearchResponse, SearchResultItem, SearchMeta
from modals.inputs import SearchQueryParams
from modals.instances import SearxInstanceStats
from states.db import get_cached_query, cache_query, get_alive_instances
from runnables.searchengines import SearXNGEngine
from runnables.ranker import merge_and_rank_results

logger = logging.getLogger(__name__)

def is_news_query(query: str) -> bool:
    """Helper to detect if query targets news or temporal events (smaller cache TTL)."""
    low_query = query.lower()
    news_terms = {"news", "recent", "today", "latest", "yesterday", "current", "update", "2026", "2025"}
    words = set(low_query.split())
    return not words.isdisjoint(news_terms)

async def searxng_query(params: SearchQueryParams, browser: Any, semaphore: asyncio.Semaphore, client: Optional[httpx.AsyncClient] = None) -> SearchResponse:
    """
    Orchestrates search queries across public SearXNG instances:
    1. Checks the cache first with dynamic TTL.
    2. Queries top 3 active instances in parallel.
    3. Offloads deduplication & ranking to a worker thread.
    4. Concurrently crawls article page content with HTTP-first and Playwright fallback.
    5. Caches the result and returns.
    """
    start_time = time.time()
    query_normalized = params.query.strip().lower()

    # 1. Cache Check
    ttl_seconds = 600 if is_news_query(params.query) else 3600  # 10 mins vs 60 mins
    logger.info(f"Checking cache for query: '{query_normalized}' with TTL of {ttl_seconds}s...")
    cached_data = await asyncio.to_thread(get_cached_query, query_normalized, ttl_seconds)
    
    if cached_data is not None:
        logger.info(f"Cache HIT for query: '{query_normalized}'")
        results = [SearchResultItem(**item) for item in cached_data]
        took_ms = int((time.time() - start_time) * 1000)
        return SearchResponse(
            query=params.query,
            results=results,
            meta=SearchMeta(cached=True, took_ms=took_ms)
        )

    logger.info(f"Cache MISS for query: '{query_normalized}'")

    # 2. Retrieve instances from DB
    db_instances = await asyncio.to_thread(get_alive_instances)
    if not db_instances:
        logger.warning("No alive SearXNG instances found in database.")
        took_ms = int((time.time() - start_time) * 1000)
        return SearchResponse(
            query=params.query,
            results=[],
            meta=SearchMeta(cached=False, took_ms=took_ms)
        )

    # Try active instances in batches of 3 for fast failover
    batch_size = 3
    max_instances_to_try = min(len(db_instances), 15)
    merged_raw = []
    engine = None

    for i in range(0, max_instances_to_try, batch_size):
        batch = db_instances[i:i+batch_size]
        logger.info(f"Querying batch {i//batch_size + 1} of active instances: {[inst['url'] for inst in batch]}")
        
        batch_instances = []
        for inst in batch:
            try:
                batch_instances.append(SearxInstanceStats(
                    url=inst["url"],
                    latency_ms=inst["latency_ms"]
                ))
            except Exception as e:
                logger.error(f"Error parsing instance stats for URL {inst['url']}: {e}")
        
        if not batch_instances:
            continue

        # Instantiate engine for the current batch
        engine = SearXNGEngine(params.query, batch_instances, browser, semaphore, client)
        
        tasks = [engine.fetch_results_from_instance(inst) for inst in batch_instances]
        results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        
        batch_results_found = False
        for res in results_lists:
            if isinstance(res, list) and res:
                merged_raw.extend(res)
                batch_results_found = True
            elif isinstance(res, Exception):
                logger.error(f"Parallel instance fetch encountered error: {res}")
        
        if batch_results_found:
            logger.info(f"Successfully retrieved {len(merged_raw)} raw results from batch. Stopping search loop.")
            break
            
        if time.time() - start_time > 10.0:
            logger.warning("Approaching global deadline during search. Stopping search loop.")
            break

    if not merged_raw or not engine:
        logger.warning("No search results gathered across tried instances.")
        took_ms = int((time.time() - start_time) * 1000)
        return SearchResponse(
            query=params.query,
            results=[],
            meta=SearchMeta(cached=False, took_ms=took_ms)
        )

    # 4. Offload deduplication and ranking calculation to worker thread (CPU-isolated)
    ranked_candidates = await asyncio.to_thread(merge_and_rank_results, params.query, merged_raw)
    target_candidates = ranked_candidates[:params.max_results]
    
    logger.info(f"Found {len(ranked_candidates)} unique results. Crawling top {len(target_candidates)}...")

    # 5. Concurrent page crawling with graceful fallback (using snippet if crawl fails)
    async def crawl_candidate(cand) -> SearchResultItem:
        # Wrap crawl call to catch any exception
        context_text = None
        try:
            # Crawl page contents with timeout
            context_text = await asyncio.wait_for(
                engine.crawl_article_content(cand.link),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"Crawling timeout (5.0s exceeded) for page: {cand.link}. Falling back to snippet.")
        except Exception as e:
            logger.warning(f"Crawling failed for page: {cand.link} ({e}). Falling back to snippet.")
            
        final_context = context_text if context_text else cand.snippet
        return SearchResultItem(
            title=cand.title,
            link=cand.link,
            snippet=cand.snippet,
            domain=cand.domain,
            context=final_context,
            score=cand.score
        )

    crawl_tasks = [crawl_candidate(cand) for cand in target_candidates]
    crawled_items = await asyncio.gather(*crawl_tasks, return_exceptions=True)
    
    final_results = []
    cached_list = []
    for item in crawled_items:
        if isinstance(item, SearchResultItem):
            final_results.append(item)
            cached_list.append(item.model_dump())
        elif isinstance(item, Exception):
            logger.error(f"Page crawl task encountered critical error: {item}")

    # 6. Cache output
    if final_results:
        await asyncio.to_thread(cache_query, query_normalized, cached_list)

    took_ms = int((time.time() - start_time) * 1000)
    logger.info(f"Completed query: '{query_normalized}' with {len(final_results)} results in {took_ms}ms")
    
    return SearchResponse(
        query=params.query,
        results=final_results,
        meta=SearchMeta(cached=False, took_ms=took_ms)
    )
