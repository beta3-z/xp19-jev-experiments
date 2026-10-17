from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
PRICE_PER_MTOKEN = 0.042
RETRY_STATUSES = (408, 429, 500, 502, 503, 504)


class TypeSafeError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        self.status = status
        super().__init__(f"TypeSafe: {message[:600]}")


@dataclass(frozen=True)
class Answer:
    answers: dict[str, Any]
    latency_ms: float
    input_tokens: int
    output_tokens: int
    model: str
    attempts: int

    def noul(self, key: str) -> float:
        return float(self.answers[key]["noul"])

    def choice(self, key: str) -> tuple[dict[str, float], float]:
        answer = self.answers[key]
        probabilities = {str(name): float(value) for name, value in answer["probabilities"].items()}
        return probabilities, float(answer["confidence"])


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set. Put it in .env, export it, or pass --env-file."
            )
        self.model = model
        self.base_url = base_url or os.environ.get("TYPESAFE_BASE_URL", DEFAULT_URL)
        self.timeout = timeout
        self.retries = max(1, retries)

    def ask(self, state: Any, questions: dict[str, Any]) -> Answer:
        body = {"model": self.model, "state": state, "questions": questions}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error: TypeSafeError | None = None

        for attempt in range(1, self.retries + 1):
            started = time.perf_counter()
            try:
                data = self._post(payload)
                latency_ms = (time.perf_counter() - started) * 1000
                return _parse(data, questions, latency_ms, attempt)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last_error = TypeSafeError(f"HTTP {exc.code}: {detail}", exc.code)
                if exc.code not in RETRY_STATUSES:
                    raise last_error from None
            except urllib.error.URLError as exc:
                last_error = TypeSafeError(
                    f"{exc.reason}. Is outbound HTTPS to api.typesafe.ai reachable?"
                )
            except TimeoutError:
                last_error = TypeSafeError(f"request timed out after {self.timeout}s")
            except ConnectionError as exc:
                last_error = TypeSafeError(f"connection dropped: {exc}")
            except json.JSONDecodeError:
                raise TypeSafeError("reply is not JSON") from None
            if attempt < self.retries:
                time.sleep(0.4 * (2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error

    def _post(self, payload: bytes) -> Any:
        request = urllib.request.Request(
            self.base_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "dsh-command-classification/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def _parse(data: Any, questions: dict[str, Any], latency_ms: float, attempts: int) -> Answer:
    if not isinstance(data, dict):
        raise TypeSafeError("reply is not a JSON object")
    answers = data.get("answers")
    check_answers(answers, questions)
    usage = data.get("usage") or {}
    return Answer(
        answers=answers,
        latency_ms=latency_ms,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        model=str(data.get("model") or ""),
        attempts=attempts,
    )


def check_answers(answers: Any, questions: dict[str, Any]) -> None:
    if not isinstance(answers, dict):
        raise TypeSafeError("reply has no 'answers' object")
    for name, question in questions.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise TypeSafeError(f"reply has no answer for '{name}'")
        if question["type"] == "noul":
            _check_unit(f"{name}.noul", answer.get("noul"))
        if question["type"] == "choice":
            _check_choice(name, answer, question["criteria"])


def _check_choice(name: str, answer: dict[str, Any], criteria: dict[str, str]) -> None:
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        raise TypeSafeError(f"reply has no probabilities for '{name}'")
    for option, value in probabilities.items():
        if option not in criteria:
            raise TypeSafeError(f"reply has unknown option '{option}' for '{name}'")
        _check_unit(f"{name}.probabilities.{option}", value)
    _check_unit(f"{name}.confidence", answer.get("confidence"))


def _check_unit(field: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise TypeSafeError(f"reply field '{field}' is not a number in 0..1: {value!r}")


def mask_key(key: str) -> str:
    if not key:
        return "(missing)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-2:]} ({len(key)} chars)"
