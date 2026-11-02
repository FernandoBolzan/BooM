"""Optional, loopback-only Ollama explanations for BooM's measured findings."""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from typing import Any


OLLAMA_API_BASE = "http://127.0.0.1:11434/api"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/linux"
OLLAMA_REQUEST_TIMEOUT_SECONDS = 180.0
OLLAMA_MAX_RESPONSE_BYTES = 1_000_000
_MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_FINDING_ID_PATTERN = re.compile(r"[a-z0-9_]{1,64}\Z")


class LocalAIError(RuntimeError):
    """A local model could not safely explain BooM's measured findings."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _ollama_request(endpoint: str, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    if endpoint not in {"tags", "chat"}:
        raise LocalAIError("Endpoint local não permitido.")
    url = f"{OLLAMA_API_BASE}/{endpoint}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise LocalAIError("O serviço local tentou redirecionar a consulta; a conexão foi bloqueada.")
            raw = response.read(OLLAMA_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise LocalAIError("O serviço Ollama local tentou um redirecionamento, bloqueado por privacidade.") from None
        raise LocalAIError(f"Ollama local respondeu com erro HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LocalAIError("Ollama local não respondeu em 127.0.0.1:11434.") from None
    if len(raw) > OLLAMA_MAX_RESPONSE_BYTES:
        raise LocalAIError("A resposta do Ollama excedeu o limite de 1 MB.")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LocalAIError("Ollama local retornou JSON inválido.") from None
    if not isinstance(decoded, dict):
        raise LocalAIError("Ollama local retornou um formato inesperado.")
    return decoded


def local_ollama_models() -> dict[str, Any]:
    """List installed local models; cloud models are intentionally excluded."""
    try:
        response = _ollama_request("tags", timeout=5.0)
    except LocalAIError as exc:
        return {"state": "offline", "message": str(exc), "models": []}
    raw_models = response.get("models", [])
    if not isinstance(raw_models, list):
        return {"state": "error", "message": "A lista de modelos locais tem formato inválido.", "models": []}
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        name = item.get("model") or item.get("name")
        if not isinstance(name, str) or not _MODEL_NAME_PATTERN.fullmatch(name) or name.casefold().endswith(":cloud"):
            continue
        if name in seen:
            continue
        seen.add(name)
        size = item.get("size")
        size_bytes = int(size) if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        parameter_size = details.get("parameter_size")
        models.append({
            "name": name,
            "size_bytes": size_bytes,
            "parameter_size": parameter_size if isinstance(parameter_size, str) else None,
        })
    models.sort(key=lambda item: (item["size_bytes"] or math.inf, item["name"].casefold()))
    if not models:
        return {
            "state": "no_models",
            "message": "O Ollama local está ativo, mas não há modelo local instalado.",
            "models": [],
        }
    return {"state": "ready", "message": f"{len(models)} modelo(s) local(is) disponível(is).", "models": models}


def _safe_diagnosis_context(diagnosis: dict[str, Any]) -> dict[str, Any]:
    if diagnosis.get("status") != "analyzed":
        raise LocalAIError("Ative o histórico local e aguarde 12 amostras distribuídas por pelo menos 55 segundos.")
    findings = diagnosis.get("findings", [])
    if not isinstance(findings, list):
        raise LocalAIError("O diagnóstico determinístico tem formato inválido.")
    safe_findings: list[dict[str, Any]] = []
    for finding in findings[:12]:
        if not isinstance(finding, dict):
            continue
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not _FINDING_ID_PATTERN.fullmatch(finding_id):
            continue
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        safe_evidence = {
            key: value
            for key, value in evidence.items()
            if isinstance(key, str)
            and re.fullmatch(r"[a-z0-9_]{1,64}", key)
            and isinstance(value, (str, int, float, bool))
            and (not isinstance(value, float) or math.isfinite(value))
        }
        safe_findings.append({
            "id": finding_id,
            "severity": finding.get("severity") if finding.get("severity") in {"info", "warning", "critical"} else "info",
            "confidence": finding.get("confidence") if finding.get("confidence") in {"none", "low", "moderate", "high"} else "none",
            "title": str(finding.get("title", ""))[:240],
            "message": str(finding.get("message", ""))[:800],
            "suggestion": str(finding.get("suggestion", ""))[:800],
            "evidence": safe_evidence,
        })
    sample_count = int(diagnosis.get("sample_count", 0))
    window_seconds = round(float(diagnosis.get("window_seconds", 0)), 1)
    coverage_percent = round(float(diagnosis.get("coverage_percent", 0)), 1)
    summary = str(diagnosis.get("summary", ""))[:1000]
    confidence = diagnosis.get("confidence") if diagnosis.get("confidence") in {"none", "low", "moderate", "high"} else "none"
    if not safe_findings:
        safe_findings.append({
            "id": "overview",
            "severity": "info",
            "confidence": confidence,
            "title": "Visão geral da janela",
            "message": summary,
            "suggestion": "Se a lentidão ocorrer em momentos específicos, repita a coleta durante essa atividade.",
            "evidence": {
                "sample_count": sample_count,
                "window_seconds": window_seconds,
                "coverage_percent": coverage_percent,
            },
        })
    return {
        "sample_count": sample_count,
        "window_seconds": window_seconds,
        "coverage_percent": coverage_percent,
        "confidence": confidence,
        "summary": summary,
        "findings": safe_findings,
    }


def _explanation_schema(finding_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "explanations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_id": {"type": "string", "enum": finding_ids},
                        "explanation": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "risk": {"type": "string"},
                    },
                    "required": ["finding_id", "explanation", "recommendation", "risk"],
                    "additionalProperties": False,
                },
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "explanations", "limitations"],
        "additionalProperties": False,
    }


def _validate_explanation(payload: Any, finding_ids: list[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LocalAIError("O modelo não retornou um objeto de análise válido.")
    if set(payload) != {"summary", "explanations", "limitations"}:
        raise LocalAIError("A resposta da IA local contém campos fora do formato permitido.")
    summary = payload.get("summary")
    explanations = payload.get("explanations")
    limitations = payload.get("limitations")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
        raise LocalAIError("O resumo da IA local está vazio ou excedeu o limite permitido.")
    if not isinstance(explanations, list) or len(explanations) != len(finding_ids):
        raise LocalAIError("A IA local não explicou todos os sinais medidos.")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in explanations:
        if not isinstance(item, dict):
            raise LocalAIError("Uma explicação da IA local tem formato inválido.")
        if set(item) != {"finding_id", "explanation", "recommendation", "risk"}:
            raise LocalAIError("Uma explicação da IA local contém campos fora do formato permitido.")
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in finding_ids or finding_id in seen:
            raise LocalAIError("A IA local citou um sinal sem evidência correspondente.")
        strings: dict[str, str] = {}
        for key, limit in (("explanation", 1400), ("recommendation", 1000), ("risk", 800)):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise LocalAIError("A IA local retornou um campo vazio ou longo demais.")
            strings[key] = value.strip()
        seen.add(finding_id)
        normalized.append({"finding_id": finding_id, **strings})
    if not isinstance(limitations, list) or len(limitations) > 6 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in limitations
    ):
        raise LocalAIError("As limitações retornadas pela IA local têm formato inválido.")
    return {
        "summary": summary.strip(),
        "explanations": normalized,
        "limitations": [item.strip() for item in limitations],
    }


def explain_performance_diagnosis(model: str, diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Explain verified BooM findings with a selected local-only Ollama model."""
    context = _safe_diagnosis_context(diagnosis)
    models = local_ollama_models()
    if models.get("state") != "ready":
        raise LocalAIError(str(models.get("message") or "Ollama local não está pronto."))
    available = {item["name"] for item in models["models"]}
    if model not in available:
        raise LocalAIError("Escolha um modelo local instalado no Ollama; modelos de nuvem não são aceitos.")
    findings = context["findings"]
    finding_ids = [item["id"] for item in findings] or ["overview"]
    system_prompt = (
        "Você explica medições de desempenho Linux para o usuário do BooM. "
        "Use somente os dados e hipóteses recebidos; não invente valores, causas, processos ou medições. "
        "Diferencie correlação de causa, trate os achados como hipóteses e explique quando não há saturação sustentada. "
        "Recomende somente verificações ou ajustes graduais e reversíveis. Não sugira desativar segurança, "
        "atualizações, proteções térmicas, nem executar comandos privilegiados. Não aplique mudanças. "
        "Responda em português e siga estritamente o JSON Schema recebido."
    )
    user_prompt = "Explique estes dados agregados e locais do BooM: " + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": _explanation_schema(finding_ids),
        "stream": False,
        "keep_alive": "0",
        "options": {"temperature": 0.2, "num_predict": 512},
    }
    response = _ollama_request("chat", request_payload, timeout=OLLAMA_REQUEST_TIMEOUT_SECONDS)
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise LocalAIError("Ollama local não retornou o texto da explicação.")
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        raise LocalAIError("A resposta do modelo não respeitou o formato JSON solicitado.") from None
    explanation = _validate_explanation(decoded, finding_ids)
    return {
        **explanation,
        "model": model,
        "confidence": context["confidence"],
        "sample_count": context["sample_count"],
        "window_seconds": context["window_seconds"],
        "coverage_percent": context["coverage_percent"],
        "findings": findings,
        "source": "histórico local opt-in do BooM + modelo Ollama em 127.0.0.1",
        "stored": False,
    }
