"""
AENIDA GitHub Explorer
Searches GitHub for improvement ideas using free REST API.
Mandatory sandbox (BUG-21): sanitize + Worker analysis only.
Rate limit: 5000 req/hr with free token.
"""
import json          # FIX NF-1: was missing — json.loads() used in extract_patterns()
import logging
import os
import re            # FIX NF-1: was missing — re.sub() used in extract_patterns()
import time
from typing import Any, Dict, List, Optional

try:
    import requests
    REQ_OK = True
except ImportError:
    REQ_OK = False

_remaining = 5000
_reset_at = 0.0
SEARCH_TOPICS = {
    "cognition_router": ["async task router python cosine similarity"],
    "task_executor":    ["write-ahead log asyncio python"],
    "performance_learner": ["rolling average numpy time series"],
    "memory_layer":     ["semantic search local file python"],
    "trading_agent":    ["trading chart AI analysis python"],
    "bridge":           ["grpc client reconnect unstable network"],
    "news_watcher":     ["crypto news sentiment python free api"],
    "technical_indicators": ["technical analysis indicators python free"],
}

def _headers() -> Dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN","")
    h = {"Accept":"application/vnd.github+json",
         "X-GitHub-Api-Version":"2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def _check_rate() -> bool:
    global _remaining, _reset_at
    if _remaining < 10:
        wait = max(0, _reset_at - time.time())
        if wait > 0:
            logging.warning(f"[GITHUB] Rate limit — waiting {wait:.0f}s")
            return False
    return True

def _update_rate(resp) -> None:
    global _remaining, _reset_at
    _remaining = int(resp.headers.get("X-RateLimit-Remaining", _remaining))
    _reset_at = float(resp.headers.get("X-RateLimit-Reset", _reset_at))

def search_repos(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    if not REQ_OK or not _check_rate():
        return []
    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": f"{query} language:python", "sort":"stars",
                    "order":"desc", "per_page": limit},
            headers=_headers(), timeout=10)
        _update_rate(resp)
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            return [{"name":i["full_name"],"stars":i["stargazers_count"],
                     "description":i.get("description",""),
                     "url":i["html_url"]} for i in items]
    except Exception as e:
        logging.warning(f"[GITHUB] search_repos failed: {e}")
    return []

def read_readme(repo_full_name: str) -> str:
    if not REQ_OK or not _check_rate():
        return ""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo_full_name}/readme",
            headers={**_headers(),"Accept":"application/vnd.github.raw+json"},
            timeout=10)
        _update_rate(resp)
        if resp.status_code == 200:
            return resp.text[:8000]
    except Exception as e:
        logging.warning(f"[GITHUB] read_readme failed: {e}")
    return ""

def fetch_python_files(repo_full_name: str,
                        max_files: int = 3,
                        max_bytes: int = 6000) -> List[Dict[str, Any]]:
    """
    BUG 2 FIX — Fetch actual Python source files from a GitHub repo.

    Old code only read READMEs (documentation prose). That means patterns
    were extracted from English text, not real code. This function fetches
    the repo file tree, picks the most relevant .py files (by size and name),
    and returns their content so extract_patterns() can work on real code.

    Returns list of {filename, content, path} dicts.
    """
    if not REQ_OK or not _check_rate():
        return []
    files: List[Dict[str, Any]] = []
    try:
        # Step 1: get the default branch tree (recursive)
        resp = requests.get(
            f"https://api.github.com/repos/{repo_full_name}/git/trees/HEAD",
            params={"recursive": "1"},
            headers=_headers(), timeout=10)
        _update_rate(resp)
        if resp.status_code != 200:
            return []
        blobs = resp.json().get("tree", [])

        # Step 2: filter .py files, skip tests/migrations/setup
        skip_prefixes = ("test_", "setup", "conftest", "migration", "__")
        candidates = [
            b for b in blobs
            if b.get("type") == "blob"
            and b["path"].endswith(".py")
            and not any(os.path.basename(b["path"]).startswith(s)
                        for s in skip_prefixes)
            # Prefer files in root or src/ — these are typically the core logic
            and b["path"].count("/") <= 2
        ]
        # Sort by size descending — bigger files usually have more logic
        candidates.sort(key=lambda b: b.get("size", 0), reverse=True)

        # Step 3: fetch top N file contents
        for blob in candidates[:max_files]:
            fpath = blob["path"]
            file_url = (f"https://api.github.com/repos/"
                        f"{repo_full_name}/contents/{fpath}")
            fr = requests.get(
                file_url,
                headers={**_headers(),
                         "Accept": "application/vnd.github.raw+json"},
                timeout=10)
            _update_rate(fr)
            if fr.status_code == 200:
                content = fr.text[:max_bytes]
                files.append({
                    "filename": os.path.basename(fpath),
                    "path":     fpath,
                    "content":  content,
                })
            time.sleep(0.3)   # polite per-file delay

    except Exception as e:
        logging.warning(f"[GITHUB] fetch_python_files failed for "
                        f"{repo_full_name}: {e}")
    return files


def extract_patterns(content: str,
                      source_type: str = "readme") -> List[Dict[str, Any]]:
    """
    BUG 2 FIX: Extract architectural patterns from README or Python source.
    source_type: "readme" | "python_code"
    """
    # First sanitize
    try:
        from sanitizer_shield import scrub
        content = scrub(content)
    except Exception:
        pass

    if source_type == "python_code":
        prompt = (
            "Analyze this Python source code and extract reusable architectural "
            "patterns. For each pattern return a JSON object with these exact keys: "
            "{\"pattern\": str, \"description\": str, "
            "\"applicability_to_aenida\": str, \"confidence\": 0.0-1.0}. "
            "Focus on: async patterns, error handling strategies, caching, "
            "retry logic, queue design, data structures. "
            "Return ONLY a JSON array. No markdown.\n\n"
            f"{content[:3000]}"
        )
    else:
        prompt = (
            "Extract architectural patterns from this README. "
            "For each return a JSON object: "
            "{\"pattern\": str, \"description\": str, "
            "\"applicability_to_aenida\": str, \"confidence\": 0.0-1.0}. "
            "Return ONLY a JSON array. No markdown.\n\n"
            f"{content[:3000]}"
        )

    try:
        from model_shell import call
        result = call(prompt)
        import re
        text = re.sub(r"```json\n?|```\n?", "", result.get("result", "[]")).strip()
        patterns = json.loads(text)
        if not isinstance(patterns, list):
            return []
        # Validate and flag all as analyzed
        clean = []
        for p in patterns:
            if not isinstance(p, dict):
                continue
            p["GITHUB_CONTENT_ANALYZED"] = True
            p["source_type"] = source_type
            # Ensure confidence is a float in range
            try:
                p["confidence"] = max(0.0, min(1.0, float(p.get("confidence", 0.65))))
            except (TypeError, ValueError):
                p["confidence"] = 0.65
            clean.append(p)
        return clean
    except Exception as e:
        logging.warning(f"[GITHUB] extract_patterns failed: {e}")
    return []

def run_nightly_research() -> Dict[str, Any]:
    """Search GitHub for each AENIDA module and collect ideas."""
    logging.info("[GITHUB] Starting nightly research...")
    report: Dict[str, Any] = {"modules_searched":0,"patterns_found":0,"repos_read":0}
    all_patterns = []
    for module, queries in SEARCH_TOPICS.items():
        for q in queries:
            repos = search_repos(q, limit=3)
            report["modules_searched"] += 1
            for repo in repos[:2]:
                # ── BUG 2 FIX: fetch README + actual Python source files ──
                # Old: only read README (documentation prose)
                # New: also fetch top .py files so patterns come from real code

                # 1. README patterns
                readme = read_readme(repo["name"])
                if readme:
                    report["repos_read"] += 1
                    patterns = extract_patterns(readme, source_type="readme")
                    report["patterns_found"] += len(patterns)
                    all_patterns.extend(patterns)

                # 2. Python source file patterns
                py_files = fetch_python_files(repo["name"], max_files=2)
                for pyf in py_files:
                    code_patterns = extract_patterns(
                        pyf["content"], source_type="python_code")
                    # Annotate with source filename so suggestions are traceable
                    for cp in code_patterns:
                        cp["source_file"] = pyf["filename"]
                        cp["repo"] = repo["name"]
                    report["patterns_found"] += len(code_patterns)
                    all_patterns.extend(code_patterns)

                time.sleep(0.5)  # polite per-repo delay
    # Save patterns to knowledge_repo
    try:
        from knowledge_repo import record_pattern
        for p in all_patterns:
            record_pattern("success", p.get("pattern","unknown"),
                           p.get("description",""), p)
    except Exception:
        pass
    # ── BUG 1 FIX: call route_suggestion() per pattern, not private _append_to_md ──
    # Old code: called private _append_to_md() with a single summary entry,
    # skipping the DB, skipping dedup, and using type="new_module" which the
    # old gate could never route to module creation.
    # Fix: each pattern becomes an individual suggestion routed through the
    # proper pipeline so the DB records it, dedup works, and confidence routing
    # can trigger module_forge if confidence is high enough.
    if all_patterns:
        try:
            from update_engine import route_suggestion
            # Deduplicate by pattern name before routing
            seen: set = set()
            for p in all_patterns:
                key = str(p.get("pattern", ""))[:80]
                if key in seen:
                    continue
                seen.add(key)
                # Map each pattern to a well-formed suggestion dict
                route_suggestion({
                    "title":         f"[GitHub] {p.get('pattern', 'Pattern')}",
                    "problem":       p.get("description", ""),
                    "solution":      p.get("applicability_to_aenida",
                                          p.get("description", "")),
                    # BUG 1 FIX: use "new_module" — update_engine type gate
                    # is fixed separately (BUG 4) to accept this
                    "type":          "new_module",
                    "affected_file": None,
                    "confidence":    float(p.get("confidence", 0.70)),
                    "priority":      "medium",
                    "effort":        "hours",
                    "evidence":      f"GitHub nightly research — {len(all_patterns)} patterns",
                    "source":        "github_explorer",
                })
        except Exception as e:
            logging.warning(f"[GITHUB] route_suggestion failed: {e}")
    logging.info(f"[GITHUB] Done — {report}")
    return report

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json; print(json.dumps(run_nightly_research(), indent=2))
