# Free Search API
<img src="https://i.ibb.co/B2Wf3kXW/freesearchlogo.png" width="820" height="312">

Free Search API provides real time search results from best search engines (including google and bing) to enhance internet accessibility for AI agents and LLM applications. It uses SearXNG to source results.

## Public Instance

A publicly accessible demo of this API has been hosted

URL: https://freesearch.replit.app/docs

## Overview

This API provides a search endpoint that:
- Performs a search query.
- Retrieves a specified number of top results.
- Crawls each result page to extract content.
- Returns structured data with source, link, and context for each result

<video src="https://github-production-user-asset-6210df.s3.amazonaws.com/54291721/428418652-0ab860b0-dee3-4297-9077-546c94405af9.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAVCODYLSA53PQK4ZA%2F20250330%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20250330T182054Z&X-Amz-Expires=300&X-Amz-Signature=683fb5ec1a3bf8130dc69469f3d1ac73a9549bb78ec4c882da712d9eed535fb4&X-Amz-SignedHeaders=host" controls></video>

---

## Installation

### Prerequisites
- Python 3.7+
- Playwright
- Beautiful Soup 4
- FastAPI
- Uvicorn
- Markdownify

### Setup

1. **Clone this repository**
2. **Install dependencies:**
   ```sh
   pip install fastapi uvicorn pydantic playwright beautifulsoup4 markdownify
   ```
3. **Install playwright browser:**
   ```sh
   playwright install chromium
   ```

### Usage
```sh
xvfb-run python main.py
```

## API Endpoints

### GET /search

Search for a query and retrieve results with additional context.

| Parameter    | Type     | Required | Default | Description |
|-------------|----------|----------|---------|-------------|
| `query`     | string   | ✅        | -       | The search query string |
| `max_results` | integer | ❌        | `3`     | Number of results to return (range: 1-5) |

#### Example Request

```sh
curl -X 'GET' \
  'http://localhost:11235/search?query=News%20in%20Indianapolis&max_results=3' \
  -H 'accept: application/json'
```

#### Response

```json
[
  {
    "source": "source_name",
    "link": "https://example.com/page",
    "context": "Extracted content from the page..."
  }
]
```

### GET /update-instances

Update internal state about public searxng instances. 

#### Example Request

```sh
curl -X 'GET' \
  'http://localhost:11235/update-instances' \
  -H 'accept: application/json'
```

#### Response

```json
{
  "instances_found:": 75
}
```

### GET /get-instances

Return information about available searxng instances.

#### Example Request

```sh
curl -X 'GET' \
  'http://localhost:11235//get-instances' \
  -H 'accept: application/json'
```

#### Response

```json
[
   {
         "url": "https://searxng.brihx.fr/",
         "search_response_time": 0.923,
         "google_response_time": 0.358,
         "initial_response_time": 0.117,
         "uptime_percent": 100
   },
   {
         "url": "https://searx.tuxcloud.net/",
         "search_response_time": 0.814,
         "google_response_time": 0.375,
         "initial_response_time": 0.156,
         "uptime_percent": 100
   }
]
```

