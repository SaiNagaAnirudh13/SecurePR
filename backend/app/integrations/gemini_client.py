"""
Gemini integration.

IMPORTANT: the configured GEMINI_MODEL ("gemini-3.8-flash" by default, per
the project brief) may not exist on every account/region at any given time --
model identifiers change over time and availability varies by account. This
client probes the model at startup and transparently falls back to
GEMINI_FALLBACK_MODEL (default: "gemini-2.0-flash") if the configured one is
unavailable, logging a clear warning either way. Nothing here assumes a
specific model is guaranteed to exist.

The AI is used only to produce supplementary rationale and (bounded,
clamped) dimension-score opinions. It never makes the final approve/block
decision -- see services/decision_engine.py.
"""
import json
import logging

from app.core.config import get_settings

logger = logging.getLogger("securepr.gemini")

_SYSTEM_PROMPT = """You are a code-review risk analyst. You will be given a list of changed
files (paths + truncated diffs) from a GitHub Pull Request. Score six risk dimensions from 0-100
(higher = riskier): change_surface, reversibility, data_security, operational, verification,
policy_compliance. Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly
this shape:
{"change_surface": {"score": 0, "rationale": "..."}, "reversibility": {"score": 0, "rationale": "..."},
 "data_security": {"score": 0, "rationale": "..."}, "operational": {"score": 0, "rationale": "..."},
 "verification": {"score": 0, "rationale": "..."}, "policy_compliance": {"score": 0, "rationale": "..."},
 "summary": "one paragraph overall summary"}
"""


class GeminiUnavailable(Exception):
    pass


class GeminiClient:
    def __init__(self):
        self.settings = get_settings()
        self._model_name: str | None = None
        self._configured = False

    def _ensure_configured(self):
        if self._configured:
            return
        if not self.settings.GEMINI_API_KEY:
            raise GeminiUnavailable("GEMINI_API_KEY is not set.")
        import google.generativeai as genai
        genai.configure(api_key=self.settings.GEMINI_API_KEY)
        self._genai = genai
        self._model_name = self._resolve_model_name()
        self._configured = True

    def _resolve_model_name(self) -> str:
        """Probe whether the configured model is listed for this API key; fall back if not."""
        try:
            available = {
                m.name.split("/")[-1]
                for m in self._genai.list_models(
                    request_options={"timeout": self.settings.GEMINI_REQUEST_TIMEOUT_SECONDS}
                )
            }
        except Exception as exc:  # network / auth errors
            logger.warning("Could not list Gemini models (%s); assuming configured model is usable.", exc)
            return self.settings.GEMINI_MODEL

        if self.settings.GEMINI_MODEL in available:
            return self.settings.GEMINI_MODEL

        logger.warning(
            "Configured GEMINI_MODEL '%s' is not available on this account/region. "
            "Falling back to '%s'. Update GEMINI_MODEL in .env once the intended model is available.",
            self.settings.GEMINI_MODEL, self.settings.GEMINI_FALLBACK_MODEL,
        )
        if self.settings.GEMINI_FALLBACK_MODEL in available:
            return self.settings.GEMINI_FALLBACK_MODEL
        if available:
            fallback = sorted(available)[0]
            logger.warning("Fallback model also unavailable; using first listed model '%s'.", fallback)
            return fallback
        raise GeminiUnavailable("No Gemini models are available for this API key.")

    def score_dimensions(self, changed_files: list[dict]) -> dict | None:
        """Returns parsed dict or None on any failure (caller must handle gracefully)."""
        try:
            self._ensure_configured()
        except GeminiUnavailable as exc:
            logger.warning("Gemini unavailable, skipping AI scoring: %s", exc)
            return None

        file_summaries = []
        for f in changed_files[:40]:  # cap payload size
            excerpt = (f.get("patch_excerpt") or "")[:1500]
            file_summaries.append(f"FILE: {f['file_path']} ({f.get('change_type')})\n{excerpt}")
        prompt = _SYSTEM_PROMPT + "\n\n---\n\n" + "\n\n".join(file_summaries)

        try:
            model = self._genai.GenerativeModel(self._model_name)
            response = model.generate_content(
                prompt,
                request_options={"timeout": self.settings.GEMINI_REQUEST_TIMEOUT_SECONDS},
            )
            text = response.text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[-1] if "\n" in text else text
            return json.loads(text)
        except Exception as exc:
            logger.warning("Gemini scoring call failed or returned malformed output: %s", exc)
            return None


gemini_client = GeminiClient()
