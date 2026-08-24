"""Local RAG knowledge base for the Explain assistant.

Builds a chunk index from the static catalogue documentation plus WCMP2
records harvested from a WIS2 Global Discovery Catalogue, embeds chunks
via Ollama, and retrieves relevant excerpts at question time.
"""

import json
import logging
import math
import os
import re
import time
from pathlib import Path

import requests
from lxml import html as lxml_html

from .llm import clip_text, ollama_host

logger = logging.getLogger("WIS2_LLM")

DEFAULT_GDC_API_URL = "https://wis2-gdc.weather.gc.ca"
DEFAULT_EMBED_MODEL = "nomic-embed-text"

RETRIEVE_TOP_K = 6
GDC_PAGE_SIZE = 100
EMBED_BATCH_SIZE = 16
EMBED_TIMEOUT = 120
CHUNK_MAX_CHARS = 1600
GDC_TEXT_LIMIT = 2000

_CATALOGUE_TEMPLATE = (
    Path(__file__).resolve().parent / "templates" / "telemetry" / "catalogue.html"
)

_INDEX_CACHE = {"key": None, "data": None}

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def index_path():
    override = os.environ.get("LLM_KB_PATH")
    if override:
        return Path(override)
    from django.conf import settings

    return Path(settings.BASE_DIR) / "var" / "llm_kb.json"


def embed_model():
    return os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)


def gdc_api_url():
    return os.environ.get("GDC_API_URL", DEFAULT_GDC_API_URL).rstrip("/")


def _slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or "chunk"


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _split_text(text, limit=CHUNK_MAX_CHARS):
    if len(text) <= limit:
        return [text]
    parts = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
            buf = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            parts.append(para[:cut].rstrip())
            para = para[cut:].lstrip("\n")
        buf = para
    if buf:
        parts.append(buf)
    return parts


def _render_block(el):
    tag = el.tag
    if tag == "p":
        return _clean(el.text_content())
    if tag == "pre":
        return el.text_content().strip()
    if tag == "ul":
        items = ["- " + _clean(li.text_content()) for li in el.xpath("./li")]
        return "\n".join(items)
    if tag == "table":
        rows = []
        for tr in el.xpath(".//tr"):
            cells = [_clean(cell.text_content()) for cell in tr.xpath("./th|./td")]
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)
    return ""


def _in_toc(el):
    for ancestor in el.iterancestors():
        classes = (ancestor.get("class") or "").split()
        if "doc-toc" in classes:
            return True
    return False


def doc_chunks(path=None):
    """Split catalogue.html into heading-scoped text chunks."""
    path = Path(path) if path else _CATALOGUE_TEMPLATE
    raw = path.read_text(encoding="utf-8")
    start = raw.find("{% block content %}")
    if start != -1:
        end = raw.rfind("{% endblock %}")
        raw = raw[start:end]
    root = lxml_html.fromstring(raw)

    chunks = []
    state = {"title": "Overview", "anchor": "", "blocks": []}
    current_h2 = ""

    def flush():
        blocks = state["blocks"]
        state["blocks"] = []
        text = "\n\n".join(block for block in blocks if block)
        if not text.strip():
            return
        pieces = _split_text(text)
        base_slug = _slug(state["title"])
        total = len(pieces)
        for i, piece in enumerate(pieces, 1):
            title = state["title"] if total == 1 else f"{state['title']} (part {i}/{total})"
            chunks.append(
                {
                    "id": f"doc:{base_slug}:{i}",
                    "source": "doc",
                    "ref_id": state["anchor"] or base_slug,
                    "title": title,
                    "text": piece,
                }
            )

    for el in root.xpath("//h2|//h3|//p|//ul|//table|//pre"):
        if _in_toc(el):
            continue
        if el.tag in ("h2", "h3"):
            flush()
            heading = _clean(el.text_content())
            if el.tag == "h2":
                current_h2 = heading
                title = heading
            else:
                title = f"{current_h2} - {heading}" if current_h2 else heading
            anchor = ""
            node = el
            while node is not None:
                anchor = node.get("id") or ""
                if anchor:
                    break
                node = node.getparent()
            state["title"] = title
            state["anchor"] = anchor
            continue
        body = _render_block(el)
        if body:
            state["blocks"].append(body)
    flush()
    return chunks


def gdc_record_chunk(feature):
    """Map one OGC API - Records WCMP2 feature to a knowledge chunk."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    identifier = str(props.get("identifier") or feature.get("id") or "").strip()
    title = _clean(str(props.get("title") or identifier or "Untitled record"))
    lines = [f"Identifier: {identifier}", f"Title: {title}"]

    description = props.get("description")
    if description:
        stripped = re.sub(r"<[^>]+>", " ", str(description))
        lines.append(f"Description: {_clean(stripped)}")

    publisher = props.get("publisher")
    if isinstance(publisher, str) and publisher.strip():
        lines.append(f"Publisher: {_clean(publisher)}")

    contacts = props.get("contacts") or []
    contact_bits = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        name = _clean(str(contact.get("organization") or contact.get("name") or ""))
        emails = ", ".join(
            entry.get("value", "")
            for entry in contact.get("emails", []) or []
            if isinstance(entry, dict) and entry.get("value")
        )
        role = contact.get("role") if isinstance(contact.get("role"), str) else ""
        bits = [part for part in (name, role, emails) if part]
        if bits:
            contact_bits.append(" - ".join(bits))
    if contact_bits:
        lines.append("Contacts: " + "; ".join(contact_bits[:5]))

    keywords = []
    for theme in props.get("themes") or []:
        if not isinstance(theme, dict):
            continue
        for concept in theme.get("concepts", []) or []:
            keyword = concept.get("id") or concept.get("url") if isinstance(concept, dict) else concept
            if keyword:
                keywords.append(str(keyword))
    if keywords:
        lines.append("Themes: " + ", ".join(dict.fromkeys(keywords[:15])))

    temporal = props.get("temporal")
    interval = None
    if isinstance(temporal, dict):
        extent = temporal.get("extent") or {}
        interval = extent.get("interval") if isinstance(extent, dict) else None
    if interval:
        flat = interval[0] if interval and isinstance(interval[0], list) else interval
        lines.append("Temporal coverage: " + ", ".join(str(value) for value in flat))

    links = feature.get("links") or props.get("links") or []
    link_bits = []
    for link in links:
        if isinstance(link, dict) and link.get("href"):
            rel = link.get("rel") or ""
            link_title = _clean(str(link.get("title") or ""))
            link_bits.append(f"{rel} {link_title} {link['href']}".strip())
    if link_bits:
        lines.append("Links: " + "; ".join(link_bits[:8]))

    ref_id = identifier or f"gdc:{_slug(title)}"
    return {
        "id": f"gdc:{ref_id}",
        "source": "gdc",
        "ref_id": ref_id,
        "title": title,
        "text": clip_text("\n".join(lines), GDC_TEXT_LIMIT),
    }


def harvest_gdc(api_url=None, limit=None, delay=0.2):
    """Page through a GDC's wis2-discovery-metadata collection."""
    base = (api_url or gdc_api_url()).rstrip("/")
    url = f"{base}/collections/wis2-discovery-metadata/items"
    out = []
    offset = 0
    while True:
        try:
            response = requests.get(
                url,
                params={"limit": GDC_PAGE_SIZE, "offset": offset},
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("GDC harvest failed at offset %s: %s", offset, exc)
            break
        features = data.get("features") or []
        for feature in features:
            chunk = gdc_record_chunk(feature)
            if chunk:
                out.append(chunk)
        offset += len(features)
        if not features:
            break
        if limit is not None and len(out) >= limit:
            out = out[:limit]
            break
        matched = data.get("numberMatched")
        if isinstance(matched, int) and offset >= matched:
            break
        time.sleep(delay)
    return out


def embed_texts(texts):
    """Embed texts via Ollama /api/embed. Raises RuntimeError on failure."""
    if not texts:
        return []
    host = ollama_host()
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = [text[:8000] for text in texts[start : start + EMBED_BATCH_SIZE]]
        try:
            response = requests.post(
                f"{host}/api/embed",
                json={"model": embed_model(), "input": batch},
                timeout=EMBED_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Embedding request failed: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Ollama embeddings returned HTTP {response.status_code}. "
                f"Is model '{embed_model()}' pulled?"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Invalid embedding response.") from exc
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise RuntimeError("Unexpected embedding response shape.")
        vectors.extend(embeddings)
    return vectors


def _cosine(a, b):
    if not isinstance(a, list) or not isinstance(b, list) or not a or not b:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _keyword_score(query, chunk):
    query_tokens = set(_TOKEN_RE.findall(query.lower()))
    if not query_tokens:
        return 0.0
    chunk_tokens = set(
        _TOKEN_RE.findall((chunk.get("title", "") + " " + chunk.get("text", "")).lower())
    )
    overlap = query_tokens & chunk_tokens
    if not overlap:
        return 0.0
    return len(overlap) / len(query_tokens)


def save_index(chunks, embed_model_name=None):
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "embed_model": embed_model_name or embed_model(),
        "chunks": [
            {
                "id": chunk["id"],
                "source": chunk["source"],
                "ref_id": chunk.get("ref_id", ""),
                "title": chunk["title"],
                "text": chunk["text"],
                "embedding": chunk.get("embedding"),
            }
            for chunk in chunks
        ],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
    _INDEX_CACHE["key"] = None
    _INDEX_CACHE["data"] = None
    return path


def load_index(force=False):
    path = index_path()
    if not path.exists():
        return None
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if not force and _INDEX_CACHE["key"] == key:
        return _INDEX_CACHE["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not load knowledge base index %s: %s", path, exc)
        return None
    _INDEX_CACHE["key"] = key
    _INDEX_CACHE["data"] = data
    return data


def retrieve(query, k=RETRIEVE_TOP_K):
    """Return the top-k knowledge chunks for a query (semantic, keyword fallback)."""
    query = (query or "").strip()
    if not query:
        return []
    index = load_index()
    chunks = (index or {}).get("chunks") or []
    if not chunks:
        return []
    query_vector = None
    try:
        query_vector = embed_texts([query])[0]
    except Exception as exc:
        logger.warning("Query embedding unavailable (%s); using keyword scoring.", exc)
    scored = []
    for chunk in chunks:
        embedding = chunk.get("embedding")
        if query_vector and embedding:
            score = _cosine(query_vector, embedding)
        else:
            score = _keyword_score(query, chunk)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits = []
    for score, chunk in scored[:k]:
        hit = {field: chunk.get(field) for field in ("id", "source", "ref_id", "title", "text")}
        hit["score"] = round(score, 4)
        hits.append(hit)
    return hits


def find_by_ref(ref_id):
    """Exact-match lookup of a chunk by WCMP2 identifier or chunk id."""
    target = str(ref_id or "").strip()
    if not target:
        return None
    index = load_index()
    for chunk in (index or {}).get("chunks") or []:
        if str(chunk.get("ref_id")) == target or str(chunk.get("id")) == target:
            return {field: chunk.get(field) for field in ("id", "source", "ref_id", "title", "text")}
    return None


def format_excerpts(hits):
    blocks = []
    for hit in hits:
        label = "[gdc]" if hit.get("source") == "gdc" else "[doc]"
        head = f"{label} {hit.get('ref_id') or ''} - {hit.get('title') or ''}".strip(" -")
        blocks.append(f"{head}\n{hit.get('text', '')}")
    return "\n\n".join(blocks)


def find_metadata_id(value, _depth=0):
    """Depth-limited search for a metadata_id string inside raw event JSON."""
    if _depth > 6:
        return None
    if isinstance(value, dict):
        for key, val in value.items():
            if key == "metadata_id" and isinstance(val, str) and val.strip():
                return val.strip()
        for val in value.values():
            found = find_metadata_id(val, _depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for val in value:
            found = find_metadata_id(val, _depth + 1)
            if found:
                return found
    return None
