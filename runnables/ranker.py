import os
from urllib.parse import urlparse, parse_qs, urlencode
from pydantic import BaseModel
from typing import List, Dict, Any

class NormalizedResult(BaseModel):
    title: str
    link: str
    snippet: str
    domain: str
    score: float = 0.0

RANKING_WEIGHTS = {
    "keyword_match": 5.0,
    "domain_trust": 3.0,
    "position_boost": 1.5,
    "domain_density_penalty": 1.0
}

TRUSTED_DOMAINS = {
    "wikipedia.org",
    "github.com",
    "stackoverflow.com",
    "arxiv.org",
    "bbc.co.uk",
    "bbc.com",
    "reuters.com",
    "cnn.com",
    "nytimes.com",
    "theguardian.com",
    "bloomberg.com",
    "techcrunch.com",
    "wired.com",
    "medium.com",
    "sciencedirect.com",
    "nature.com",
    "nasa.gov"
}

def extract_domain(url: str) -> str:
    """Extract clean domain (stripping www.)."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""

def normalize_url(url: str) -> str:
    """Normalize URL by stripping protocols, trailing slashes, and tracking query params."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
            
        path = parsed.path.lower()
        if path.endswith("/"):
            path = path[:-1]
            
        query_params = parse_qs(parsed.query)
        clean_params = {}
        for k, v in query_params.items():
            k_low = k.lower()
            # Ignore tracking parameters
            if k_low.startswith("utm_") or k_low in ("ref", "fbclid", "gclid", "referrer", "source"):
                continue
            clean_params[k] = v
            
        query_str = ""
        if clean_params:
            sorted_params = sorted(clean_params.items())
            query_str = urlencode(sorted_params, doseq=True)
            
        normalized = f"{netloc}{path}"
        if query_str:
            normalized += f"?{query_str}"
        return normalized
    except Exception:
        # Fallback to simple stripping
        url_low = url.lower()
        for prefix in ("https://", "http://", "www."):
            if url_low.startswith(prefix):
                url_low = url_low[len(prefix):]
        if url_low.endswith("/"):
            url_low = url_low[:-1]
        return url_low

def count_keyword_matches(query: str, title: str, snippet: str) -> float:
    """Count number of query terms found in title and snippet."""
    query_words = set(w.lower() for w in query.split() if len(w) > 2)
    if not query_words:
        return 0.0
    combined = (title + " " + snippet).lower()
    matches = sum(1 for w in query_words if w in combined)
    return float(matches)

def merge_and_rank_results(query: str, raw_results: List[Dict[str, Any]]) -> List[NormalizedResult]:
    """
    Normalizes, scores, and deduplicates raw search results.
    1. Apply heuristic rule-based scoring first.
    2. Deduplicate results based on base domain and link path.
    3. Return sorted results.
    """
    if not raw_results:
        return []

    scored_candidates: List[NormalizedResult] = []
    query_clean = query.strip().lower()
    
    # 1. Heuristic Rule-Based Scoring
    for idx, item in enumerate(raw_results):
        title = item.get("title", "")
        link = item.get("link", "")
        snippet = item.get("snippet", "")
        domain = extract_domain(link)
        
        score = 0.0
        
        # A. Keyword matches (weight 5.0)
        matches = count_keyword_matches(query, title, snippet)
        score += matches * 5.0
        
        # B. Exact phrase match boost
        if query_clean:
            if query_clean in title.lower():
                score += 3.0
            elif query_clean in snippet.lower():
                score += 1.5
                
        # C. Domain trust boost
        if any(t in domain for t in TRUSTED_DOMAINS):
            score += 3.0
            
        # D. Position boost
        # idx is the 0-based index of original occurrence
        if idx < 10:
            score += (10 - idx) * 0.2
            
        # E. Snippet length density boost
        score += min(len(snippet) / 200, 1.0)
        
        scored_candidates.append(NormalizedResult(
            title=title,
            link=link,
            snippet=snippet,
            domain=domain,
            score=round(score, 2)
        ))

    # 2. Deduplication (by domain + link path)
    seen_keys = set()
    deduped_results: List[NormalizedResult] = []
    
    # Sort first by score descending so that the highest scoring page in a cluster is kept
    scored_candidates.sort(key=lambda x: x.score, reverse=True)
    
    for cand in scored_candidates:
        # Key: domain + path (link split by ?)
        base_link = cand.link.split("?")[0]
        if base_link.endswith("/"):
            base_link = base_link[:-1]
        key = f"{cand.domain}:{base_link}"
        
        if key not in seen_keys:
            seen_keys.add(key)
            deduped_results.append(cand)
            
    return deduped_results
