"""LLM clients.

GeminiClient calls the Gemini REST API directly (no SDK dependency) and asks
for JSON constrained by a response schema. MockClient returns deterministic
output so the whole pipeline can run offline in tests and CI.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Protocol

import requests

from .settings import get_settings

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def generate_json(self, *, model: str, system: str, prompt: str, schema: dict,
                      temperature: float = 0.4, task: str = "") -> dict: ...


class GeminiClient:
    def __init__(self, api_key: str, timeout: float = 120.0, max_retries: int = 4):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set (or use --mock).")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()

    def generate_json(self, *, model: str, system: str, prompt: str, schema: dict,
                      temperature: float = 0.4, task: str = "") -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        url = GEMINI_URL.format(model=model)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(
                    url, json=body, timeout=self.timeout,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"retryable HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                data = resp.json()
                cand = (data.get("candidates") or [{}])[0]
                if cand.get("finishReason") not in (None, "STOP", "MAX_TOKENS"):
                    raise LLMError(f"generation stopped: {cand.get('finishReason')}")
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
                return _loads(text)
            except (requests.RequestException, LLMError, ValueError) as exc:
                last_err = exc
                retryable = not (isinstance(exc, LLMError) and str(exc).startswith("HTTP 4"))
                if attempt >= self.max_retries or not retryable:
                    break
                delay = min(60, 2 ** attempt + random.random())
                log.warning("gemini %s attempt %d failed (%s); retrying in %.1fs", task, attempt + 1, exc, delay)
                time.sleep(delay)
        raise LLMError(f"Gemini call failed for {task or model}: {last_err}")


def _gemini_research(self, *, model: str, system: str, prompt: str, temperature: float = 0.3) -> dict:
    """Free-text research grounded with Google Search. Returns {text, sources[{title,url}], queries}."""
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": temperature},
    }
    data = _post_with_retries(self, GEMINI_URL.format(model=model), body, "research")
    cand = (data.get("candidates") or [{}])[0]
    if cand.get("finishReason") not in (None, "STOP", "MAX_TOKENS"):
        raise LLMError(f"research stopped: {cand.get('finishReason')}")
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought")).strip()
    meta = cand.get("groundingMetadata") or {}
    sources, seen = [], set()
    for ch in meta.get("groundingChunks") or []:
        web = ch.get("web") or {}
        if web.get("uri") and web["uri"] not in seen:
            seen.add(web["uri"])
            sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    if not text:
        raise LLMError("research returned no text")
    return {"text": text, "sources": sources, "queries": meta.get("webSearchQueries") or []}


GeminiClient.research = _gemini_research


def _post_with_retries(client: "GeminiClient", url: str, body: dict, task: str) -> dict:
    last_err: Exception | None = None
    for attempt in range(client.max_retries + 1):
        try:
            resp = client.session.post(url, json=body, timeout=client.timeout,
                                       headers={"x-goog-api-key": client.api_key, "Content-Type": "application/json"})
            if resp.status_code in (429, 500, 502, 503, 504):
                raise LLMError(f"retryable HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        except (requests.RequestException, LLMError, ValueError) as exc:
            last_err = exc
            if attempt >= client.max_retries or (isinstance(exc, LLMError) and str(exc).startswith("HTTP 4")):
                break
            delay = min(60, 2 ** attempt + random.random())
            log.warning("gemini %s attempt %d failed (%s); retrying in %.1fs", task, attempt + 1, exc, delay)
            time.sleep(delay)
    raise LLMError(f"Gemini call failed for {task}: {last_err}")


def _loads(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


# --------------------------------------------------------------------------- mock
class MockClient:
    """Deterministic stand-in for tests. Builds output from the prompt itself."""

    def generate_json(self, *, model: str, system: str, prompt: str, schema: dict,
                      temperature: float = 0.4, task: str = "") -> dict:
        if task == "evaluate":
            return self._evaluate(prompt)
        if task == "write":
            return self._write(prompt)
        raise LLMError(f"MockClient has no handler for task {task!r}")

    def research(self, *, model: str, system: str, prompt: str, temperature: float = 0.3) -> dict:
        m = re.search(r"TOPIC: (.*)", prompt)
        topic = m.group(1).strip() if m else "the topic"
        text = (f"Research brief on {topic}.\n"
                "- A widely used benchmark allocation is 60% equities and 40% bonds.\n"
                "- Over the period reviewed, bond yields were about 4.1% while equity valuations stayed above their long-run average.\n"
                "- Advisers point out that the right mix depends on age, goals and how soon the money is needed.\n"
                "- Counterpoint: some analysts argue a higher equity share suits younger savers with long horizons.")
        return {"text": text, "queries": [topic],
                "sources": [{"title": "example-research.org", "url": "https://example-research.org/allocation"},
                            {"title": "example-data.gov", "url": "https://example-data.gov/yields"}]}

    @staticmethod
    def _evaluate(prompt: str) -> dict:
        p = prompt.lower()
        hot = any(k in p for k in ("rate", "cpi", "inflation", "tax", "mortgage", "fee", "refund", "penalt"))
        promo = any(k in p for k in ("perks", "upgrade", "amenities", "warranty"))
        if promo:
            scores = dict(wallet_impact=2, breadth=1, actionability=2, urgency=1, novelty=2)
            flags = ["promotional"]
        elif hot:
            scores = dict(wallet_impact=5, breadth=4, actionability=4, urgency=4, novelty=4)
            flags = []
        else:
            scores = dict(wallet_impact=2, breadth=2, actionability=1, urgency=1, novelty=3)
            flags = ["corporate_admin"]
        return {
            **scores,
            "flags": flags,
            "angle": "What this means for household budgets, borrowers and savers",
            "audience": ["borrowers", "savers"],
            "rationale": "mock evaluation",
        }

    @staticmethod
    def _write(prompt: str) -> dict:
        m = re.search(r"SOURCE MATERIAL\n(.*?)\nEND SOURCE MATERIAL", prompt, re.S)
        src = m.group(1) if m else ""
        t = re.search(r"title: (.*)", src)
        title = t.group(1).strip() if t else "Policy update"
        nums = re.findall(r"\d+(?:\.\d+)?%", src)[:2]
        fact = f" The headline figure was {nums[0]}." if nums else ""
        lines = [
            "For most households the effect arrives gradually, through the rates lenders set on new borrowing and the returns banks pay on deposits.",
            "Variable-rate borrowers usually see changes first, because their repayments are linked to a benchmark that moves with policy.",
            "Fixed-rate borrowers are insulated for now, but the decision shapes the deals on offer when their current term ends.",
            "Savers are the mirror image: easy-access rates tend to follow the benchmark, often with a lag that favours the bank.",
            "Prices matter too, since the cost of food, energy and rent decides how far each pay packet stretches.",
            "Card balances are the most expensive debt most people carry, so even small rate moves compound quickly there.",
            "Renters feel it indirectly, as landlords with mortgages pass on higher costs at renewal time.",
            "People close to retirement should watch annuity and bond yields, which respond to the same expectations.",
            "Self-employed people who pay tax in instalments may want to revisit their cash-flow plans for the months ahead.",
            "Anyone shopping for a loan should compare the total cost of credit, not only the headline rate.",
            "Banks are not obliged to pass on every change, which is why checking your own account terms matters.",
            "Expectations for the next decision often move markets before the decision itself, so quoted deals can change quickly.",
            "Households with emergency savings are better placed to absorb surprises in their monthly bills.",
            "The broad lesson is that timing matters less than knowing which of your products are exposed.",
            "Lenders may also tighten or loosen affordability checks, which changes how much people can borrow.",
            "Comparison sites update daily, so a quick check every few weeks is usually enough for most people.",
            "Over a full year, small differences in rates add up to noticeable sums for typical balances.",
            "None of this changes overnight, which leaves time to review the numbers calmly.",
        ]
        return {
            "headline": f"What the latest update means for your monthly budget",
            "dek": "Borrowers, savers and renters will feel this in different ways over the coming months.",
            "what_happened": f"Officials published new information this week under the title “{title[:60]}”.{fact} "
                             "The announcement sets the backdrop for household borrowing and saving costs.",
            "money_impact": "\n\n".join(" ".join(lines[i:i + 3]) for i in range(0, len(lines), 3)),
            "who_is_affected": "People with variable-rate debt feel changes first. Savers with easy-access accounts "
                               "come next, while those on long fixed deals are insulated until renewal. " * 2,
            "illustrative_example": "Illustrative example: on a 200,000 balance, every 0.25 percentage point change "
                                    "moves annual interest by about 500, or roughly 42 a month.",
            "action_steps": [
                "Check whether your loans are fixed or variable and note any renewal dates.",
                "Compare your savings rate with the best easy-access accounts available.",
                "Review your monthly budget for any interest-sensitive bills.",
                "Don't rush into long fixed deals on headlines alone; compare total costs.",
            ],
            "takeaways": [
                "Variable-rate borrowers feel the change first.",
                "Savers should check whether their rate still competes.",
                "Fixed-rate holders are protected until their deal ends.",
            ],
            "meta_description": "What the latest announcement means for borrowers, savers and household budgets.",
            "topics": ["interest_rates"],
        }


def get_client(force_mock: bool = False) -> LLMClient:
    s = get_settings()
    if force_mock or s.llm_provider == "mock":
        return MockClient()
    if s.llm_provider == "gemini":
        return GeminiClient(s.gemini_api_key or "")
    raise LLMError(f"Unknown LLM_PROVIDER {s.llm_provider!r}")


def model_name(stage: str, client: Any) -> str:
    s = get_settings()
    if isinstance(client, MockClient):
        return "mock"
    return s.desk_model if stage == "desk" else s.writer_model
