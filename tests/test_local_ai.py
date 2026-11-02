import json
import unittest
from unittest.mock import MagicMock, patch

from boom import format_local_ai_result
import boom_local_ai


class LocalOllamaTests(unittest.TestCase):
    def test_model_discovery_keeps_only_valid_local_models_and_sorts_smallest_first(self):
        with patch("boom_local_ai._ollama_request", return_value={"models": [
            {"name": "large:latest", "size": 8_000_000_000, "details": {"parameter_size": "8B"}},
            {"model": "cloud:cloud", "size": 1},
            {"model": "small:latest", "size": 2_000_000_000},
            {"model": "bad name", "size": 4},
        ]}):
            status = boom_local_ai.local_ollama_models()
        self.assertEqual(status["state"], "ready")
        self.assertEqual([item["name"] for item in status["models"]], ["small:latest", "large:latest"])
        self.assertEqual(status["models"][1]["parameter_size"], "8B")

    def test_model_discovery_reports_no_local_models(self):
        with patch("boom_local_ai._ollama_request", return_value={"models": [{"name": "gemma:cloud"}]}):
            status = boom_local_ai.local_ollama_models()
        self.assertEqual(status["state"], "no_models")
        self.assertEqual(status["models"], [])

    def test_ollama_requests_are_loopback_only_and_ignore_proxy_environment(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://127.0.0.1:11434/api/tags"
        response.read.return_value = b'{"models": []}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch("boom_local_ai.urllib.request.build_opener", return_value=opener) as build_opener:
            payload = boom_local_ai._ollama_request("tags")
        self.assertEqual(payload, {"models": []})
        self.assertEqual(opener.open.call_args.args[0].full_url, "http://127.0.0.1:11434/api/tags")
        proxy_handler = next(
            handler for handler in build_opener.call_args.args
            if isinstance(handler, boom_local_ai.urllib.request.ProxyHandler)
        )
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIn(boom_local_ai._RejectRedirects, build_opener.call_args.args)

    def test_ollama_request_rejects_a_redirect_outside_loopback(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://example.invalid/api/tags"
        opener = MagicMock()
        opener.open.return_value = response
        with patch("boom_local_ai.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(boom_local_ai.LocalAIError, "redirecionar"):
                boom_local_ai._ollama_request("tags")

    def test_offline_ollama_state_is_reported_instead_of_a_model_selection_error(self):
        with patch("boom_local_ai.local_ollama_models", return_value={
            "state": "offline", "message": "Ollama local não respondeu.", "models": [],
        }):
            with self.assertRaisesRegex(boom_local_ai.LocalAIError, "não respondeu"):
                boom_local_ai.explain_performance_diagnosis("local-model", {
                    "status": "analyzed", "sample_count": 12, "window_seconds": 55,
                    "coverage_percent": 100, "confidence": "low", "summary": "Sem saturação.",
                    "findings": [],
                })

    def test_result_formatter_keeps_evidence_risk_and_no_changes_note(self):
        rendered = format_local_ai_result({
            "diagnosis": {"findings": [{
                "id": "cpu_signal", "title": "Sinal de CPU", "message": "Uso sustentado.",
                "evidence": {"cpu_median_percent": 92.0},
            }]},
            "explanation": {
                "summary": "Há um sinal para investigar.", "model": "local-model",
                "sample_count": 24, "window_seconds": 115, "coverage_percent": 100,
                "confidence": "moderate", "explanations": [{
                    "finding_id": "cpu_signal", "explanation": "A CPU ficou ocupada.",
                    "recommendation": "Compare durante a lentidão.", "risk": "Não prova a causa.",
                }],
                "limitations": ["Não identifica o processo responsável."],
            },
        })
        self.assertIn("Evidência medida: cpu median percent: 92.0", rendered)
        self.assertIn("Risco ou limite: Não prova a causa.", rendered)
        self.assertIn("A IA não aplicou alterações e a resposta não foi salva.", rendered)

    def test_insufficient_history_is_rejected_before_contacting_ollama(self):
        with patch("boom_local_ai._ollama_request") as request:
            with self.assertRaisesRegex(boom_local_ai.LocalAIError, "12 amostras"):
                boom_local_ai.explain_performance_diagnosis("local-model", {"status": "insufficient_data"})
        request.assert_not_called()

    def test_empty_diagnosis_gets_an_evidence_based_overview_instead_of_an_orphan_id(self):
        context = boom_local_ai._safe_diagnosis_context({
            "status": "analyzed", "sample_count": 24, "window_seconds": 115,
            "coverage_percent": 100, "confidence": "high", "summary": "Nenhum sinal sustentado.",
            "findings": [],
        })
        self.assertEqual(context["findings"][0]["id"], "overview")
        self.assertEqual(context["findings"][0]["evidence"]["sample_count"], 24)
        self.assertIn("Nenhum sinal sustentado", context["findings"][0]["message"])

    def test_explanation_uses_only_local_measured_context_and_validated_findings(self):
        diagnosis = {
            "status": "analyzed",
            "sample_count": 120,
            "window_seconds": 595,
            "coverage_percent": 99.2,
            "confidence": "high",
            "summary": "Um sinal sustentado foi encontrado.",
            "device_serial": "MUST-NOT-BE-SENT",
            "findings": [{
                "id": "possible_cpu_limit",
                "severity": "warning",
                "confidence": "high",
                "title": "Possível limite de CPU",
                "message": "A CPU ficou ocupada enquanto a GPU teve folga.",
                "suggestion": "Compare a carga durante a tarefa.",
                "evidence": {"cpu_median_percent": 92.0, "gpu_median_percent": 30.0},
            }],
        }
        response = {"message": {"content": json.dumps({
            "summary": "Há indício de limite de CPU nesta janela.",
            "explanations": [{
                "finding_id": "possible_cpu_limit",
                "explanation": "A CPU teve mediana alta e a GPU manteve folga.",
                "recommendation": "Repita a leitura durante o aplicativo lento e compare seus processos.",
                "risk": "Não atribui a causa a um processo específico.",
            }],
            "limitations": ["A telemetria não identifica o processo responsável."],
        })}}
        with patch("boom_local_ai.local_ollama_models", return_value={
            "state": "ready", "message": "1 modelo local", "models": [{"name": "local-model"}],
        }), patch("boom_local_ai._ollama_request", return_value=response) as request:
            result = boom_local_ai.explain_performance_diagnosis("local-model", diagnosis)
        sent = json.dumps(request.call_args.args[1], ensure_ascii=False)
        self.assertNotIn("MUST-NOT-BE-SENT", sent)
        self.assertEqual(request.call_args.args[0], "chat")
        self.assertEqual(request.call_args.args[1]["model"], "local-model")
        self.assertFalse(request.call_args.args[1]["stream"])
        self.assertEqual(request.call_args.args[1]["keep_alive"], "0")
        self.assertEqual(request.call_args.args[1]["format"]["properties"]["explanations"]["items"]["properties"]["finding_id"]["enum"], ["possible_cpu_limit"])
        self.assertEqual(result["confidence"], "high")
        self.assertFalse(result["stored"])
        self.assertEqual(result["explanations"][0]["finding_id"], "possible_cpu_limit")

    def test_explanation_rejects_unknown_finding_ids(self):
        response = {"message": {"content": json.dumps({
            "summary": "Resumo.",
            "explanations": [{
                "finding_id": "invented_bottleneck",
                "explanation": "Texto.",
                "recommendation": "Próximo passo.",
                "risk": "Risco.",
            }],
            "limitations": [],
        })}}
        with patch("boom_local_ai.local_ollama_models", return_value={
            "state": "ready", "message": "1 modelo local", "models": [{"name": "local-model"}],
        }), patch("boom_local_ai._ollama_request", return_value=response):
            with self.assertRaisesRegex(boom_local_ai.LocalAIError, "sinal sem evidência"):
                boom_local_ai.explain_performance_diagnosis("local-model", {
                    "status": "analyzed", "sample_count": 12, "window_seconds": 55,
                    "coverage_percent": 100, "confidence": "low", "summary": "Um sinal.",
                    "findings": [{"id": "possible_cpu_limit", "title": "CPU", "evidence": {}}],
                })

    def test_explanation_requires_a_selected_installed_local_model(self):
        with patch("boom_local_ai.local_ollama_models", return_value={
            "state": "ready", "message": "1 modelo local", "models": [{"name": "local-model"}],
        }), patch("boom_local_ai._ollama_request") as request:
            with self.assertRaisesRegex(boom_local_ai.LocalAIError, "modelos de nuvem"):
                boom_local_ai.explain_performance_diagnosis("remote:cloud", {
                    "status": "analyzed", "sample_count": 12, "window_seconds": 55,
                    "coverage_percent": 100, "confidence": "low", "summary": "Sem saturação.",
                    "findings": [],
                })
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
