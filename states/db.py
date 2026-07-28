import os
import sqlite3
import json
import time
import calendar
from typing import List, Optional, Dict, Any

# Resolve absolute path for database file in the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "free_search.db")

def _get_connection():
    conn = sqlite3.connect(DB_PATH)
    # Enable WAL mode, busy timeout, and normal sync for concurrency
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_connection() as conn:
        # Create instances table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS instances (
                url TEXT PRIMARY KEY,
                status TEXT,
                fail_count INTEGER,
                latency_ms INTEGER,
                last_checked TEXT
            )
        """)
        # Create search query cache table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                query TEXT PRIMARY KEY,
                results TEXT,
                timestamp INTEGER
            )
        """)
        conn.commit()

def save_instances(instances_list: List[Dict[str, Any]]):
    """
    Saves or updates instances scraped from searx.space.
    """
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with _get_connection() as conn:
        for inst in instances_list:
            url = inst["url"]
            cursor = conn.cursor()
            cursor.execute("SELECT status, fail_count, latency_ms FROM instances WHERE url = ?", (url,))
            row = cursor.fetchone()
            
            status = "alive"
            # Get latency from stats if available, default to 1000 if not provided
            latency_ms = inst.get("latency_ms", 1000)
            
            if row:
                # Existing instance: update latency and check time, preserve status/fail_count if alive
                conn.execute("""
                    UPDATE instances 
                    SET last_checked = ?, latency_ms = ?
                    WHERE url = ?
                """, (now, latency_ms, url))
            else:
                conn.execute("""
                    INSERT INTO instances (url, status, fail_count, latency_ms, last_checked)
                    VALUES (?, ?, 0, ?, ?)
                """, (url, status, latency_ms, now))
        conn.commit()

def get_alive_instances() -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT url, status, fail_count, latency_ms, last_checked 
            FROM instances
        """)
        rows = [dict(row) for row in cursor.fetchall()]
        
    now_epoch = time.time()
    alive_list = []
    
    for row in rows:
        status = row["status"]
        fail_count = row["fail_count"]
        last_checked_str = row["last_checked"]
        
        # Check if alive (and not exceeded fail limit of 5)
        if status == "alive" and fail_count < 5:
            alive_list.append(row)
        else:
            # Check if it qualifies for half-open trial:
            # If status is dead and last checked was > 30 minutes (1800s) ago
            is_half_open = False
            if last_checked_str:
                try:
                    cleaned_str = last_checked_str.replace("Z", "")
                    t_struct = time.strptime(cleaned_str, "%Y-%m-%dT%H:%M:%S")
                    epoch_time = calendar.timegm(t_struct)
                    if now_epoch - epoch_time > 1800:
                        is_half_open = True
                except Exception:
                    is_half_open = True
            else:
                is_half_open = True
                
            if is_half_open:
                row["status"] = "half-open"
                alive_list.append(row)
                
    # Sort alive_list:
    # 1. status ('alive' first, 'half-open' last to prefer healthy ones)
    # 2. fail_count ASC
    # 3. latency_ms ASC
    # 4. url ASC
    alive_list.sort(key=lambda r: (
        0 if r["status"] == "alive" else 1,
        r["fail_count"],
        r["latency_ms"],
        r["url"]
    ))
    return alive_list

def update_instance_status(url: str, success: bool, latency_ms: Optional[int] = None):
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT fail_count, latency_ms FROM instances WHERE url = ?", (url,))
        row = cursor.fetchone()
        if not row:
            status = "alive" if success else "dead"
            fail_count = 0 if success else 1
            lat = latency_ms if latency_ms is not None else (1000 if success else 9999)
            conn.execute("""
                INSERT INTO instances (url, status, fail_count, latency_ms, last_checked)
                VALUES (?, ?, ?, ?, ?)
            """, (url, status, fail_count, lat, now))
        else:
            current_fail_count = row["fail_count"]
            if success:
                new_fail_count = 0
                status = "alive"
                new_latency = latency_ms if latency_ms is not None else row["latency_ms"]
            else:
                new_fail_count = current_fail_count + 1
                status = "dead" if new_fail_count >= 5 else "alive"
                new_latency = 9999  # Penalty latency on failure to immediately deprioritize
            
            conn.execute("""
                UPDATE instances 
                SET status = ?, fail_count = ?, latency_ms = ?, last_checked = ?
                WHERE url = ?
            """, (status, new_fail_count, new_latency, now, url))
        conn.commit()

def get_cached_query(query: str, ttl_seconds: int) -> Optional[List[Dict[str, Any]]]:
    now = int(time.time())
    min_time = now - ttl_seconds
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT results FROM search_cache 
            WHERE query = ? AND timestamp >= ?
        """, (query, min_time))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row["results"])
            except Exception:
                return None
        return None

def cache_query(query: str, results: List[Dict[str, Any]]):
    now = int(time.time())
    results_str = json.dumps(results)
    with _get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO search_cache (query, results, timestamp)
            VALUES (?, ?, ?)
        """, (query, results_str, now))
        conn.commit()

def clear_old_cache(max_age_seconds: int = 86400):
    now = int(time.time())
    cutoff = now - max_age_seconds
    with _get_connection() as conn:
        conn.execute("DELETE FROM search_cache WHERE timestamp < ?", (cutoff,))
        conn.commit()
