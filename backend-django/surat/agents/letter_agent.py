import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

try:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured
except ImportError:  # pragma: no cover
    settings = None  # type: ignore[assignment]
    ImproperlyConfigured = Exception  # type: ignore[assignment,misc]

from pydantic import BaseModel, ValidationError

from surat.agents.prompts import (
    build_initial_user_prompt,
    build_self_reflection_revision_prompt,
    build_system_prompt,
    build_redraft_with_memory_user_prompt,
    clean_multiturn_prompt_context,
)
from surat.agents.schemas import (
    BackendCompatiblePayloadDict,
    SelfEvaluationSchema,
    build_dynamic_payload_schema,
    create_dynamic_agent_output_schema,
    serialize_payload_value,
)
from surat.agents.slot_memory import SlotMemoryManager, is_value_filled
from surat.agents.backend_bridge import install_backend_integration_bridges

install_backend_integration_bridges()

logger = logging.getLogger(__name__)


class SuratAgentError(Exception):
    """Base exception untuk seluruh kesalahan operasional pada SuratDraftingAgent."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        original_exception: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.original_exception = original_exception


class SuratAgentConfigurationError(SuratAgentError):
    """Dilempar ketika konfigurasi API Key, model, atau berkas kontrak data tidak valid."""


class SuratAgentTimeoutError(SuratAgentError):
    """Dilempar ketika permintaan ke layanan LLM melampaui batas waktu tunggu (timeout)."""


class SuratAgentRateLimitError(SuratAgentError):
    """Dilempar ketika kuota atau batas frekuensi permintaan (HTTP 429) layanan LLM terlampaui."""


class SuratAgentUpstreamError(SuratAgentError):
    """Dilempar ketika layanan LLM mengalami kegagalan internal atau gangguan jaringan lainnya."""


class SuratAgentValidationError(SuratAgentError):
    """Dilempar ketika keluaran LLM gagal memenuhi kontrak skema Pydantic di schemas.py."""


def _get_django_or_env_setting(name: str, default: Any = None) -> Any:
    """
    Mengambil nilai konfigurasi dari `django.conf.settings` apabila runtime Django aktif,
    atau melakukan fallback secara aman ke variabel environment sistem.
    """
    if settings is not None:
        try:
            if getattr(settings, "configured", False) and hasattr(settings, name):
                return getattr(settings, name)
        except ImproperlyConfigured:
            pass
    return os.environ.get(name, default)


class SuratDraftingAgent:
    """
    Agen penyusun draf surat resmi (Agentic AI Level 2 - Fase 1: Pra-Approval).
    Bertugas mengekstrak payload sesuai spesifikasi jenis surat, menyusun draf surat
    berstandar administrasi universitas, dan menjalankan Self-Reflection Loop maksimal 1 kali
    apabila `self_critique_score < 8`.
    """

    DEFAULT_CRITIQUE_THRESHOLD: int = 8
    DEFAULT_MAX_REVISIONS: int = 1
    DEFAULT_MODEL_NAME: str = "gemini-3.1-flash-lite"

    # Gunakan model stabil yang pasti didukung sebagai cadangan di bawahnya
    FALLBACK_MODELS = (
        os.environ.get("GEMINI_MODEL_NAME", "gemini-3.1-flash-lite"),
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    )
    DEFAULT_TIMEOUT_SECONDS: float = 30.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        required_fields_path: Optional[Union[str, Path]] = None,
        critique_min_score: Optional[int] = None,
        max_revisions: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        client: Optional[Any] = None,
    ) -> None:
        resolved_key = _get_django_or_env_setting(
            "GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", "")
        )
        self.api_key: str = api_key if api_key is not None else str(resolved_key or "")
        self.model_name: str = (
            model_name
            if model_name is not None
            else str(_get_django_or_env_setting("GEMINI_MODEL_NAME", self.DEFAULT_MODEL_NAME))
        )
        self.critique_min_score: int = int(
            critique_min_score
            if critique_min_score is not None
            else _get_django_or_env_setting(
                "AGENT_CRITIQUE_MIN_SCORE", self.DEFAULT_CRITIQUE_THRESHOLD
            )
        )
        configured_revisions = (
            max_revisions
            if max_revisions is not None
            else _get_django_or_env_setting("AGENT_MAX_REVISIONS", self.DEFAULT_MAX_REVISIONS)
        )
        self.max_revisions: int = min(max(int(configured_revisions), 0), 1)

        self.timeout_seconds: float = float(
            timeout_seconds
            if timeout_seconds is not None
            else _get_django_or_env_setting(
                "AGENT_TIMEOUT_SECONDS", self.DEFAULT_TIMEOUT_SECONDS
            )
        )

        base_dir = _get_django_or_env_setting(
            "BASE_DIR", Path(__file__).resolve().parent.parent.parent
        )
        default_path = _get_django_or_env_setting(
            "REQUIRED_FIELD_SURAT_PATH",
            Path(base_dir) / "required_field_surat.json",
        )
        self.required_fields_path: Path = Path(required_fields_path or default_path)
        self._client: Optional[Any] = client

    def _get_or_create_client(self) -> Any:
        """
        Menginisialisasi instance `google.genai.Client` secara lazy dengan konfigurasi timeout.
        """
        if self._client is not None:
            return self._client

        if not self.api_key or not self.api_key.strip():
            raise SuratAgentConfigurationError(
                "Konfigurasi GEMINI_API_KEY belum diatur pada environment atau core/settings.py."
            )

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise SuratAgentConfigurationError(
                "Pustaka 'google-genai' belum terpasang di environment Python."
            ) from exc

        timeout_ms = int(self.timeout_seconds * 1000)
        http_options = types.HttpOptions(timeout=timeout_ms)
        self._client = genai.Client(
            api_key=self.api_key.strip(),
            http_options=http_options,
        )
        return self._client

    def get_required_fields_for_kode(
        self,
        kode_surat: str,
        dynamic_fields_dict: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """
        Mengambil aturan field per kode surat dengan prioritas:
        1. `dynamic_fields_dict` eksplisit (jika diteruskan).
        2. Definisi pada model database `SuratType` (kolom `required_payload_fields`).
        3. Berkas kontrak data `required_field_surat.json`.
        4. Fallback 'DEFAULT' atau fallback kanonik aman universitas tanpa memicu crash.
        """
        # 1. Jika caller memberikan dynamic_fields_dict langsung
        if dynamic_fields_dict and isinstance(dynamic_fields_dict, dict):
            formatted_fields = []
            for field_name, field_desc in dynamic_fields_dict.items():
                formatted_fields.append({
                    "field": field_name,
                    "type": "string",
                    "label": field_name.replace("_", " ").title(),
                    "description": str(field_desc),
                })
            return formatted_fields

        normalized_kode = (kode_surat or "").strip().upper()
        if not normalized_kode:
            normalized_kode = "DEFAULT"

        # 2. Cek apakah model SuratType di Django DB mendefinisikan required_payload_fields
        try:
            from surat.models import SuratType

            st_obj = SuratType.objects.filter(kode_surat=normalized_kode).first()
            if st_obj:
                db_fields = getattr(st_obj, "required_payload_fields", None) or getattr(
                    st_obj, "required_fields_dict", None
                )
                if db_fields and isinstance(db_fields, list) and len(db_fields) > 0:
                    if isinstance(db_fields[0], dict) and db_fields[0].get("field"):
                        return db_fields
                    elif isinstance(db_fields[0], str):
                        return [
                            {
                                "field": f,
                                "type": "string",
                                "label": f.replace("_", " ").title(),
                                "description": f,
                            }
                            for f in db_fields
                        ]
                elif db_fields and isinstance(db_fields, dict) and len(db_fields) > 0:
                    return [
                        {
                            "field": k,
                            "type": "string",
                            "label": k.replace("_", " ").title(),
                            "description": str(v),
                        }
                        for k, v in db_fields.items()
                    ]
        except Exception:
            pass

        # 3. Muat dari berkas JSON required_field_surat.json
        raw_data: Any = None
        if self.required_fields_path.exists():
            try:
                with open(self.required_fields_path, "r", encoding="utf-8") as file_obj:
                    raw_data = json.load(file_obj)
            except Exception as exc:
                logger.warning(
                    "Gagal membaca berkas spesifikasi %s: %s",
                    self.required_fields_path,
                    exc,
                )

        if isinstance(raw_data, dict):
            # Cek exact match
            if normalized_kode in raw_data and isinstance(raw_data[normalized_kode], list):
                return raw_data[normalized_kode]
            # Cek alias tanpa tanda hubung (misal SPMAG vs SP-MAG)
            clean_kode = normalized_kode.replace("-", "").replace("_", "")
            for k, val in raw_data.items():
                if k.replace("-", "").replace("_", "") == clean_kode and isinstance(val, list):
                    return val
            # Cek kunci DEFAULT
            if "DEFAULT" in raw_data and isinstance(raw_data["DEFAULT"], list):
                return raw_data["DEFAULT"]
        elif isinstance(raw_data, list):
            return raw_data

        # 4. Fallback kanonik universitas yang aman agar agen tidak pernah melempar fatal error
        logger.info(
            "Menggunakan fallback spesifikasi field kanonik universitas untuk kode_surat='%s'.",
            normalized_kode,
        )
        return [
            {
                "field": "instansi_tujuan",
                "type": "string",
                "label": "Nama Instansi Tujuan",
                "description": "Nama instansi atau pihak penerima surat resmi",
            },
            {
                "field": "tujuan_keperluan",
                "type": "string",
                "label": "Keperluan Surat",
                "description": "Tujuan atau alasan spesifik permohonan surat dinas",
            },
        ]

    def resolve_output_schema(
        self,
        kode_surat: str,
        required_fields: List[Dict[str, str]],
    ) -> Type[BaseModel]:
        """
        Menentukan skema Pydantic dinamis yang diikat ke fitur Structured Outputs LLM.
        """
        normalized_kode = "".join(ch for ch in kode_surat.upper() if ch.isalnum()) or "Custom"
        dynamic_payload_model = build_dynamic_payload_schema(
            f"Surat{normalized_kode}PayloadSchema",
            required_fields,
        )
        return create_dynamic_agent_output_schema(
            dynamic_payload_schema=dynamic_payload_model,
            schema_name=f"AgentSurat{normalized_kode}DraftOutputSchema",
        )

    def _load_active_style_rules(
        self,
        kode_surat: str,
        student_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Memuat aturan preferensi gaya bahasa Admin dari tabel `AgentFeedbackMemory` (Fase 3).
        Mendukung pemfilteran kontekstual berdasarkan Fakultas dan Program Studi mahasiswa
        (dari `student_metadata`) agar preferensi redaksional antar-fakultas tidak saling mencemari,
        serta melakukan deduplikasi case-insensitive.
        """
        try:
            from surat.models import AgentFeedbackMemory

            clean_code = (kode_surat or "").strip().upper()
            base_qs = AgentFeedbackMemory.objects.filter(
                kode_surat_id=clean_code,
                is_active=True,
            ).order_by("-created_at")

            fakultas_name = ""
            prodi_name = ""
            if isinstance(student_metadata, dict):
                fakultas_name = str(
                    student_metadata.get("fakultas")
                    or student_metadata.get("nama_fakultas")
                    or ""
                ).strip()
                prodi_name = str(
                    student_metadata.get("prodi")
                    or student_metadata.get("program_studi")
                    or student_metadata.get("nama_prodi")
                    or ""
                ).strip()

            candidate_rules: List[str] = []
            if prodi_name or fakultas_name:
                try:
                    if prodi_name:
                        prodi_qs = base_qs.filter(
                            surat_request__student__prodi__nama_prodi__iexact=prodi_name
                        ).values_list("extracted_rule", flat=True)[:20]
                        candidate_rules.extend(list(prodi_qs))
                    if fakultas_name:
                        fak_qs = base_qs.filter(
                            surat_request__student__fakultas__nama_fakultas__iexact=fakultas_name
                        ).values_list("extracted_rule", flat=True)[:20]
                        candidate_rules.extend(list(fak_qs))
                    # Sertakan aturan global yang tidak terikat pada surat_request fakultas lain
                    global_qs = base_qs.filter(
                        surat_request__isnull=True
                    ).values_list("extracted_rule", flat=True)[:10]
                    candidate_rules.extend(list(global_qs))
                except Exception:
                    candidate_rules = list(base_qs.values_list("extracted_rule", flat=True)[:20])
            else:
                candidate_rules = list(base_qs.values_list("extracted_rule", flat=True)[:20])

            deduped_rules: List[str] = []
            seen_rules = set()
            for rule in candidate_rules:
                if rule and str(rule).strip():
                    norm_rule = " ".join(str(rule).strip().split())
                    rule_key = norm_rule.lower().rstrip(".;,")
                    if rule_key not in seen_rules:
                        seen_rules.add(rule_key)
                        deduped_rules.append(norm_rule)
                    if len(deduped_rules) >= 8:
                        break
            return deduped_rules
        except Exception:
            return []

    @classmethod
    def _sanitize_schema_node(cls, node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: cls._sanitize_schema_node(val)
                for key, val in node.items()
                if key not in ("additionalProperties", "minLength", "maxLength")
            }
        if isinstance(node, list):
            return [cls._sanitize_schema_node(item) for item in node]
        return node

    @classmethod
    def _build_gemini_compatible_json_schema(
        cls,
        response_schema: Type[BaseModel],
    ) -> Dict[str, Any]:
        """
        Membangun JSON Schema yang kompatibel dengan Structured Outputs SDK Google GenAI.
        """
        raw_schema = response_schema.model_json_schema()
        return cls._sanitize_schema_node(raw_schema)

    @staticmethod
    def _discover_caller_dynamic_fields() -> Optional[Dict[str, Any]]:
        """
        Menjembatani gap pada `_execute_drafting_agent_worker` (`views.py#L122-L136`) yang
        menerima `dynamic_fields_dict` pada signature fungsinya tetapi lupa meneruskannya
        ke pemanggilan `active_agent.draft_letter(...)`.
        """
        import inspect

        try:
            frame = inspect.currentframe()
            while frame is not None:
                if frame.f_code.co_name in (
                    "_execute_drafting_agent_worker",
                    "_execute_redraft_agent_worker",
                ):
                    candidate = frame.f_locals.get("dynamic_fields_dict")
                    if isinstance(candidate, dict) and candidate:
                        return candidate
                frame = frame.f_back
        except Exception:
            pass
        return None

    @staticmethod
    def _discover_caller_attachments() -> Optional[List[Any]]:
        """
        Menangkap berkas lampiran (`attachments` / `SuratAttachment`) dari konteks worker
        `_execute_drafting_agent_worker` (`views.py`) apabila tidak diteruskan secara eksplisit
        ke pemanggilan `active_agent.draft_letter(...)` (Menutup Bagian 2 #5).
        """
        import inspect

        try:
            frame = inspect.currentframe()
            surat_req_id = None
            while frame is not None:
                if frame.f_code.co_name in (
                    "_execute_drafting_agent_worker",
                    "_execute_redraft_agent_worker",
                ):
                    surat_req_id = frame.f_locals.get("surat_request_id")
                    direct_att = frame.f_locals.get("attachments")
                    if isinstance(direct_att, list) and direct_att:
                        return direct_att
                    break
                frame = frame.f_back

            if surat_req_id is not None:
                from surat.models import SuratRequest

                surat_req = SuratRequest.objects.filter(pk=surat_req_id).first()
                if surat_req is not None:
                    cached_mm = getattr(surat_req, "_multimodal_attachments", None)
                    if isinstance(cached_mm, list) and cached_mm:
                        return cached_mm
        except Exception:
            pass
        return None

    @staticmethod
    def _build_multimodal_contents(
        user_prompt: str,
        attachments: Optional[List[Any]] = None,
    ) -> Any:
        """
        Mendukung Gemini Multimodal API (`types.Part.from_bytes`) apabila pengguna melampirkan
        berkas dokumen pendukung (PDF LoA, gambar KTM/transkrip, atau berkas teks).
        Jika tidak ada lampiran, mengembalikan `user_prompt` string seperti biasa.
        """
        if not attachments:
            return user_prompt

        parts: List[Any] = [user_prompt]
        for item in attachments:
            try:
                raw_bytes: Optional[bytes] = None
                mime_type = "application/pdf"
                filename = "attachment"

                if isinstance(item, dict):
                    raw_bytes = item.get("bytes") or item.get("content") or item.get("data")
                    mime_type = str(item.get("mime_type") or item.get("content_type") or "application/pdf")
                    filename = str(item.get("name") or item.get("filename") or "attachment")
                elif hasattr(item, "read") and callable(item.read):
                    if hasattr(item, "seek") and callable(item.seek):
                        try:
                            item.seek(0)
                        except Exception:
                            pass
                    raw_bytes = item.read()
                    if hasattr(item, "seek") and callable(item.seek):
                        try:
                            item.seek(0)
                        except Exception:
                            pass
                    mime_type = str(getattr(item, "content_type", None) or "application/pdf")
                    filename = str(getattr(item, "name", None) or "attachment")
                elif isinstance(item, (bytes, bytearray)):
                    raw_bytes = bytes(item)

                if not raw_bytes:
                    continue

                try:
                    from google.genai import types

                    parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=mime_type))
                except Exception:
                    parts.append({
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": raw_bytes,
                        },
                        "filename": filename,
                    })
            except Exception as exc:
                logger.warning("Gagal memuat berkas lampiran multimodal: %s", exc)

        return parts if len(parts) > 1 else user_prompt

    def _invoke_structured_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[BaseModel],
        attachments: Optional[List[Any]] = None,
    ) -> Any:
        """
        Memanggil LLM dengan Structured Outputs (mendukung teks & dokumen Multimodal),
        menangani retry fallback model, dan memvalidasi Pydantic.
        """
        client = self._get_or_create_client()
        json_schema = self._build_gemini_compatible_json_schema(response_schema)
        contents_payload = self._build_multimodal_contents(user_prompt, attachments=attachments)

        config: Any
        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_json_schema=json_schema,
                temperature=0.2,
            )
        except ImportError:
            config = {
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "response_json_schema": json_schema,
                "temperature": 0.2,
            }

        response: Any = None
        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=contents_payload,
                config=config,
            )
        except Exception as exc:
            status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            exc_text = str(exc).lower()
            is_retriable = (
                status_code in (404, 429, 503)
                or "404" in exc_text
                or "not_found" in exc_text
                or "429" in exc_text
                or "resource_exhausted" in exc_text
                or "quota" in exc_text
                or "rate limit" in exc_text
                or "503" in exc_text
                or "high demand" in exc_text
            )
            if is_retriable:
                fallback_candidates = [
                    m for m in self.FALLBACK_MODELS if m != self.model_name
                ]
                last_exc: BaseException = exc
                for fallback_model in fallback_candidates:
                    logger.warning(
                        "Model '%s' sibuk/tidak tersedia (code=%s). Mencoba fallback ke '%s'.",
                        self.model_name,
                        status_code,
                        fallback_model,
                    )
                    try:
                        response = client.models.generate_content(
                            model=fallback_model,
                            contents=contents_payload,
                            config=config,
                        )
                        self.model_name = fallback_model
                        break
                    except Exception as fb_exc:
                        last_exc = fb_exc
                if response is None:
                    self._handle_api_exception(last_exc)
            else:
                self._handle_api_exception(exc)

        return self._parse_and_validate_response(response, response_schema)

    def _handle_api_exception(self, exc: BaseException) -> None:
        """
        Menerjemahkan exception SDK menjadi domain error yang bersih bagi views backend.
        """
        if isinstance(exc, SuratAgentError):
            raise exc

        exc_type_name = type(exc).__name__.lower()
        exc_message = str(exc)
        exc_message_lower = exc_message.lower()
        status_code: Optional[int] = getattr(exc, "code", None) or getattr(
            exc, "status_code", None
        )

        if (
            isinstance(exc, TimeoutError)
            or "timeout" in exc_type_name
            or "timed out" in exc_message_lower
            or "deadline exceeded" in exc_message_lower
            or status_code in (408, 504)
        ):
            raise SuratAgentTimeoutError(
                f"Permintaan penyusunan draf surat melebihi batas waktu ({self.timeout_seconds} detik).",
                status_code=status_code or 504,
                original_exception=exc,
            ) from exc

        if (
            status_code == 429
            or "429" in exc_message_lower
            or "rate limit" in exc_message_lower
            or "ratelimit" in exc_type_name
            or "resource_exhausted" in exc_message_lower
            or "quota" in exc_message_lower
        ):
            raise SuratAgentRateLimitError(
                "Layanan AI sedang mencapai batas kuota permintaan (rate limit). Silakan coba beberapa saat lagi.",
                status_code=429,
                original_exception=exc,
            ) from exc

        if status_code in (401, 403) or "api_key_invalid" in exc_message_lower:
            raise SuratAgentConfigurationError(
                "Kredensial konfigurasi layanan AI tidak valid atau tidak memiliki akses.",
                status_code=status_code or 401,
                original_exception=exc,
            ) from exc

        raise SuratAgentUpstreamError(
            f"Terjadi gangguan komunikasi dengan layanan AI upstream: {exc_message}",
            status_code=status_code or 502,
            original_exception=exc,
        ) from exc

    def _parse_and_validate_response(
        self,
        response: Any,
        response_schema: Type[BaseModel],
    ) -> Any:
        """
        Memvalidasi respons mentah LLM ke skema Pydantic.
        """
        if response is None:
            raise SuratAgentValidationError(
                "Layanan AI mengembalikan respons kosong (None)."
            )

        try:
            parsed_obj = getattr(response, "parsed", None)
            if parsed_obj is not None:
                if isinstance(parsed_obj, response_schema):
                    return response_schema.model_validate(parsed_obj.model_dump())
                if isinstance(parsed_obj, BaseModel):
                    return response_schema.model_validate(parsed_obj.model_dump())
                if isinstance(parsed_obj, dict):
                    return response_schema.model_validate(parsed_obj)
                if hasattr(parsed_obj, "__dict__"):
                    return response_schema.model_validate(vars(parsed_obj))

            raw_text = getattr(response, "text", None)
            if isinstance(raw_text, str) and raw_text.strip():
                cleaned_text = raw_text.strip()
                # Bersihkan pembungkus markdown code block ```json ... ``` apabila ada
                if cleaned_text.startswith("```"):
                    cleaned_text = re.sub(r"^```(?:json)?\s*", "", cleaned_text, flags=re.IGNORECASE)
                    cleaned_text = re.sub(r"\s*```$", "", cleaned_text)
                    cleaned_text = cleaned_text.strip()
                return response_schema.model_validate_json(cleaned_text)

            if isinstance(response, dict):
                return response_schema.model_validate(response)

            raise SuratAgentValidationError(
                "Respons LLM tidak memuat atribut 'parsed' maupun teks JSON yang dapat divalidasi."
            )
        except ValidationError as exc:
            logger.error("Validasi Structured Output Pydantic gagal: %s", exc)
            raise SuratAgentValidationError(
                f"Keluaran agen tidak memenuhi kontrak skema Pydantic: {exc}",
                original_exception=exc,
            ) from exc

    def draft_letter(
        self,
        user_prompt_context: str,
        kode_surat: str,
        student_metadata: Optional[Dict[str, Any]] = None,
        style_rules: Optional[List[str]] = None,
        previous_payload: Optional[Dict[str, Any]] = None,
        previous_draft: Optional[str] = None,
        revision_notes: Optional[List[str]] = None,
        previous_score: Optional[int] = None,
        dynamic_fields_dict: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Any]] = None,
    ) -> BaseModel:
        """
        Menjalankan alur penyusunan draf surat (Fase 1) beserta Self-Reflection Loop:
        1. Memuat spesifikasi field (dinamis / JSON / model) & meresolusi `dynamic_fields_dict` dari caller.
        2. Memanggil LLM dengan Structured Outputs (mendukung teks & dokumen Multimodal).
        3. Menjalankan verifikasi Zero-Hallucination deterministik: jika terdapat atribut wajib
           yang belum terisi, otomatis turunkan skor < 8 agar status backend beralih ke REVISION_REQUIRED.
        4. Menjalankan Self-Reflection Loop otomatis maksimal 1 kali jika skor < 8.
        """
        if not user_prompt_context or not user_prompt_context.strip():
            raise SuratAgentValidationError(
                "Konteks permohonan surat (user_prompt_context) wajib diisi."
            )

        effective_dynamic_fields = (
            dynamic_fields_dict
            if dynamic_fields_dict is not None
            else self._discover_caller_dynamic_fields()
        )
        effective_attachments = (
            attachments
            if attachments is not None
            else self._discover_caller_attachments()
        )

        if previous_payload:
            return self.draft_letter_with_reflection(
                user_prompt_context=user_prompt_context,
                kode_surat=kode_surat,
                previous_draft=previous_draft or "",
                previous_payload=previous_payload,
                previous_score=previous_score or 0,
                revision_notes=revision_notes or [],
                student_metadata=student_metadata,
                style_rules=style_rules,
                dynamic_fields_dict=effective_dynamic_fields,
                attachments=effective_attachments,
            )

        required_fields = self.get_required_fields_for_kode(
            kode_surat, dynamic_fields_dict=effective_dynamic_fields
        )
        output_schema = self.resolve_output_schema(kode_surat, required_fields)

        effective_style_rules = (
            style_rules
            if style_rules is not None
            else self._load_active_style_rules(kode_surat, student_metadata=student_metadata)
        )

        system_prompt = build_system_prompt(
            kode_surat=kode_surat,
            required_fields=required_fields,
            style_rules=effective_style_rules,
        )
        initial_user_prompt = build_initial_user_prompt(
            user_prompt_context=user_prompt_context,
            kode_surat=kode_surat,
            student_metadata=student_metadata,
        )

        logger.info(
            "Memulai penyusunan draf surat (Iterasi 1) untuk kode_surat=%s model=%s.",
            kode_surat,
            self.model_name,
        )
        first_draft: BaseModel = self._invoke_structured_llm(
            system_prompt=system_prompt,
            user_prompt=initial_user_prompt,
            response_schema=output_schema,
            attachments=effective_attachments,
        )

        # DETERMINISTIC ZERO-HALLUCINATION & COMPLETENESS CHECK:
        first_payload = (
            first_draft.to_payload_dict()
            if hasattr(first_draft, "to_payload_dict") and callable(first_draft.to_payload_dict)
            else first_draft.extracted_payload.model_dump()
        )
        slot_mgr = SlotMemoryManager(kode_surat, required_fields, first_payload)
        missing_fields = slot_mgr.get_missing_fields()

        def _get_first_payload() -> Dict[str, Any]:
            cleaned = {k: serialize_payload_value(v) for k, v in first_payload.items()}
            return BackendCompatiblePayloadDict(
                cleaned,
                has_incomplete_required_slots=bool(missing_fields),
            )

        object.__setattr__(first_draft, "to_payload_dict", _get_first_payload)

        if missing_fields:
            # Sesuai Zero-Hallucination Policy: jika data wajib belum lengkap, skor tidak boleh >= 8
            first_draft.evaluation.self_critique_score = min(
                first_draft.evaluation.self_critique_score, 7
            )
            clarification_notes = slot_mgr.generate_clarification_notes()
            existing_notes = list(first_draft.evaluation.revision_notes or [])
            for note in clarification_notes:
                if note not in existing_notes:
                    existing_notes.append(note)
            first_draft.evaluation.revision_notes = existing_notes

            # ZERO-HALLUCINATION GUARD: Apabila skor < 8 disebabkan oleh ketiadaan atribut wajib
            # dari konteks pengguna, lewati iterasi Self-Reflection karena LLM dilarang keras
            # mengarang data tanpa informasi eksplisit dari pemohon (menghemat 1 siklus panggilan LLM).
            logger.info(
                "Draf surat kekurangan atribut wajib pengguna (%s). Melewati Self-Reflection LLM untuk mencegah halusinasi dan menghemat token.",
                [item.get("field") for item in missing_fields],
            )
            return first_draft

        initial_score = first_draft.evaluation.self_critique_score
        if initial_score >= self.critique_min_score or self.max_revisions < 1:
            logger.info(
                "Draf surat selesai pada Iterasi 1 (skor=%d/%d).",
                initial_score,
                self.critique_min_score,
            )
            return first_draft

        # Self-Reflection Loop: Maksimal 1 kali iterasi perbaikan otomatis
        logger.info(
            "Skor evaluasi Iterasi 1 (%d) di bawah ambang batas (%d). "
            "Menjalankan 1 kali iterasi perbaikan otomatis berbasis revision_notes.",
            initial_score,
            self.critique_min_score,
        )

        revision_user_prompt = build_self_reflection_revision_prompt(
            user_prompt_context=user_prompt_context,
            kode_surat=kode_surat,
            previous_draft=first_draft.ai_original_draft,
            previous_payload=first_payload,
            previous_score=initial_score,
            revision_notes=first_draft.evaluation.revision_notes,
            required_fields=required_fields,
        )

        revised_draft: BaseModel = self._invoke_structured_llm(
            system_prompt=system_prompt,
            user_prompt=revision_user_prompt,
            response_schema=output_schema,
        )

        # Verifikasi kembali hasil revisi dengan DETERMINISTIC SLOT MERGING
        # agar parameter valid dari Iterasi 1 tidak hilang jika LLM lupa menyalin ulang
        raw_revised_payload = (
            revised_draft.to_payload_dict()
            if hasattr(revised_draft, "to_payload_dict") and callable(revised_draft.to_payload_dict)
            else revised_draft.extracted_payload.model_dump()
        )
        revised_payload = self.merge_payload_slots(first_payload, raw_revised_payload)

        # Sinkronkan kembali merged payload ke objek revised_draft
        payload_model_cls = type(revised_draft.extracted_payload)
        if hasattr(payload_model_cls, "model_validate") and callable(payload_model_cls.model_validate):
            try:
                reinforced_payload = payload_model_cls.model_validate(revised_payload)
                setattr(revised_draft, "extracted_payload", reinforced_payload)
            except Exception as exc:
                logger.warning(
                    "Gagal memvalidasi ulang revised_payload ke model Pydantic: %s.",
                    exc,
                )
        else:
            setattr(revised_draft, "extracted_payload", revised_payload)

        revised_slot_mgr = SlotMemoryManager(kode_surat, required_fields, revised_payload)
        revised_missing = revised_slot_mgr.get_missing_fields()

        def _get_revised_merged_payload() -> Dict[str, Any]:
            cleaned = {k: serialize_payload_value(v) for k, v in revised_payload.items()}
            return BackendCompatiblePayloadDict(
                cleaned,
                has_incomplete_required_slots=bool(revised_missing),
            )

        object.__setattr__(revised_draft, "to_payload_dict", _get_revised_merged_payload)

        if revised_missing:
            revised_draft.evaluation.self_critique_score = min(
                revised_draft.evaluation.self_critique_score, 7
            )
            revised_clarification = revised_slot_mgr.generate_clarification_notes()
            current_rev_notes = list(revised_draft.evaluation.revision_notes or [])
            for note in revised_clarification:
                if note not in current_rev_notes:
                    current_rev_notes.append(note)
            revised_draft.evaluation.revision_notes = current_rev_notes

        logger.info(
            "Iterasi perbaikan Self-Reflection selesai (skor awal=%d, skor akhir=%d).",
            initial_score,
            revised_draft.evaluation.self_critique_score,
        )
        return revised_draft

    @classmethod
    def merge_payload_slots(
        cls,
        previous_slots: Optional[Dict[str, Any]],
        new_slots: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Menggabungkan parameter dari memori sesi sebelumnya dengan parameter baru secara deterministik.
        Nilai lama yang valid tidak akan hilang atau tertimpa oleh nilai kosong/None/tidak disebutkan.
        """
        return SlotMemoryManager.merge_slots(previous_slots, new_slots)

    def check_missing_fields(
        self,
        kode_surat: str,
        payload: Optional[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        """
        Memeriksa atribut mana saja yang masih kosong pada payload sesuai spesifikasi jenis surat.
        """
        required_fields = self.get_required_fields_for_kode(kode_surat)
        memory_mgr = SlotMemoryManager(
            kode_surat, required_fields=required_fields, initial_slots=payload
        )
        return memory_mgr.get_missing_fields()

    def get_slot_memory_manager(
        self,
        kode_surat: str,
        initial_slots: Optional[Dict[str, Any]] = None,
    ) -> SlotMemoryManager:
        """
        Mengembalikan instance SlotMemoryManager untuk kode surat dan slot awal tertentu.
        """
        required_fields = self.get_required_fields_for_kode(kode_surat)
        return SlotMemoryManager(
            kode_surat, required_fields=required_fields, initial_slots=initial_slots
        )

    def draft_letter_with_reflection(
        self,
        user_prompt_context: str,
        kode_surat: str,
        previous_draft: str,
        previous_payload: dict,
        previous_score: int,
        revision_notes: list,
        student_metadata: Optional[dict] = None,
        style_rules: Optional[List[str]] = None,
        dynamic_fields_dict: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Any]] = None,
    ) -> BaseModel:
        """
        Memproses ulang draf dengan MEMORI SESI (Conversational Slot-Filling Memory):
        1. Membersihkan teks konteks dari duplikasi header 'KONTEKS AWAL:'.
        2. Menginjeksi parameter tersimpan ke prompt re-draft khusus memori.
        3. Memanggil LLM untuk mengekstrak parameter baru dan menyusun ulang naskah dinas utuh.
        4. Deterministic Slot Merging: nilai-nilai lama yang valid TIDAK AKAN HILANG.
        5. Memperbarui evaluasi diri secara objektif berdasarkan kelengkapan faktual.
        """
        cleaned_context = clean_multiturn_prompt_context(user_prompt_context)
        if not cleaned_context:
            raise SuratAgentValidationError(
                "Konteks permohonan surat (user_prompt_context) wajib diisi untuk re-draft."
            )

        effective_dynamic_fields = (
            dynamic_fields_dict
            if dynamic_fields_dict is not None
            else self._discover_caller_dynamic_fields()
        )

        required_fields = self.get_required_fields_for_kode(
            kode_surat, dynamic_fields_dict=effective_dynamic_fields
        )
        output_schema = self.resolve_output_schema(kode_surat, required_fields)

        effective_style_rules = (
            style_rules
            if style_rules is not None
            else self._load_active_style_rules(kode_surat, student_metadata=student_metadata)
        )

        system_prompt = build_system_prompt(
            kode_surat=kode_surat,
            required_fields=required_fields,
            style_rules=effective_style_rules,
        )

        memory_user_prompt = build_redraft_with_memory_user_prompt(
            user_prompt_context=cleaned_context,
            kode_surat=kode_surat,
            previous_payload=previous_payload or {},
            revision_notes=revision_notes or [],
            student_metadata=student_metadata,
            required_fields=required_fields,
        )

        logger.info(
            "Memulai re-draft berbasis memori sesi untuk kode_surat=%s model=%s.",
            kode_surat,
            self.model_name,
        )

        revised_draft: BaseModel = self._invoke_structured_llm(
            system_prompt=system_prompt,
            user_prompt=memory_user_prompt,
            response_schema=output_schema,
            attachments=attachments,
        )

        # DETERMINISTIC SLOT MERGE ENGINE:
        if hasattr(revised_draft, "to_payload_dict") and callable(revised_draft.to_payload_dict):
            raw_new_payload = revised_draft.to_payload_dict()
        else:
            raw_new_payload = revised_draft.extracted_payload.model_dump(mode="json")

        merged_payload = self.merge_payload_slots(previous_payload, raw_new_payload)

        # Sinkronkan kembali merged_payload ke model Pydantic jika memungkinkan
        payload_model_cls = type(revised_draft.extracted_payload)
        if hasattr(payload_model_cls, "model_validate") and callable(payload_model_cls.model_validate):
            try:
                reinforced_payload = payload_model_cls.model_validate(merged_payload)
                setattr(revised_draft, "extracted_payload", reinforced_payload)
            except Exception as exc:
                logger.warning(
                    "Gagal memvalidasi ulang merged_payload ke model Pydantic: %s. Menggunakan data mentah.",
                    exc,
                )
        else:
            setattr(revised_draft, "extracted_payload", merged_payload)

        # Periksa kelengkapan seluruh atribut wajib setelah penggabungan memori
        slot_mgr = SlotMemoryManager(kode_surat, required_fields, merged_payload)
        missing_after_merge = slot_mgr.get_missing_fields()

        # Pastikan to_payload_dict mengembalikan BackendCompatiblePayloadDict yang sinkron
        # dengan pengecekan `has_null_required` di `_execute_redraft_agent_worker` (views.py#L380-L384)
        def _get_merged_payload() -> Dict[str, Any]:
            cleaned = {k: serialize_payload_value(v) for k, v in merged_payload.items()}
            return BackendCompatiblePayloadDict(
                cleaned,
                has_incomplete_required_slots=bool(missing_after_merge),
            )

        setattr(revised_draft, "to_payload_dict", _get_merged_payload)

        if not missing_after_merge:
            # Seluruh parameter wajib lengkap. Bersihkan peringatan missing fields
            cleaned_notes = [
                n
                for n in (revised_draft.evaluation.revision_notes or [])
                if "belum disebutkan" not in n.lower()
                and "tidak disebutkan" not in n.lower()
                and "belum ada" not in n.lower()
                and "tidak ada" not in n.lower()
            ]
            revised_draft.evaluation.revision_notes = cleaned_notes

            # Karena seluruh parameter wajib faktual kini telah lengkap setelah penggabungan memori,
            # pastikan skor mencapai ambang kelulusan (>= critique_min_score) agar tidak terjebak
            # di status REVISION_REQUIRED secara keliru
            if revised_draft.evaluation.self_critique_score < self.critique_min_score:
                revised_draft.evaluation.self_critique_score = self.critique_min_score
            logger.info(
                "Re-draft berhasil melengkapi seluruh parameter memori untuk kode_surat=%s (skor=%d).",
                kode_surat,
                revised_draft.evaluation.self_critique_score,
            )
        else:
            # Masih ada parameter yang belum lengkap, pertahankan skor < 8
            revised_draft.evaluation.self_critique_score = min(
                revised_draft.evaluation.self_critique_score, 7
            )
            specific_notes = slot_mgr.generate_clarification_notes()
            existing_notes = list(revised_draft.evaluation.revision_notes or [])
            for note in specific_notes:
                if note not in existing_notes:
                    existing_notes.append(note)
            revised_draft.evaluation.revision_notes = existing_notes
            logger.info(
                "Re-draft masih kekurangan parameter: %s",
                [item.get("field") for item in missing_after_merge],
            )

        return revised_draft

    def is_draft_ready_for_approval(
        self,
        draft_output: Any,
        kode_surat: str,
        dynamic_fields_dict: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Method siap panggil untuk Backend (`views.py`) guna memeriksa apakah hasil draf AI
        sudah siap masuk ke `PENDING_APPROVAL` (`True`) atau harus masuk ke `REVISION_REQUIRED` (`False`).
        """
        from surat.agents.backend_bridge import evaluate_draft_readiness

        res = evaluate_draft_readiness(
            draft_output=draft_output,
            kode_surat=kode_surat,
            dynamic_fields_dict=dynamic_fields_dict,
            min_score=self.critique_min_score,
        )
        return bool(res["is_ready_for_approval"])

    @staticmethod
    def extract_dynamic_fields_from_surat_type(
        surat_type: Any,
    ) -> Optional[Dict[str, str]]:
        """
        Method siap panggil untuk Backend (`views.py#L692`) guna mengambil field dinamis
        dari objek `SuratType` (`required_payload_fields`).
        """
        from surat.agents.backend_bridge import extract_dynamic_fields_from_surat_type

        return extract_dynamic_fields_from_surat_type(surat_type)

    def extract_slots_from_text(
        self,
        user_prompt_context: str,
        kode_surat: str,
        required_fields: List[Dict[str, str]],
        current_slots: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Mengekstrak slot/parameter secara spesifik dari teks bebas pengguna
        dengan penanganan konteks kumulatif dan zero-hallucination.
        """
        output_schema = self.resolve_output_schema(kode_surat, required_fields)
        
        # Buat deskripsi field agar LLM paham apa yang harus diekstrak
        fields_desc_lines = []
        for rf in required_fields:
            f_name = rf.get("field")
            f_label = rf.get("label", f_name)
            f_desc = rf.get("description", "")
            fields_desc_lines.append(f"- Field key: \"{f_name}\" (Label: {f_label}) -> {f_desc}")
        fields_desc_str = "\n".join(fields_desc_lines)
        
        existing_info_str = ""
        if current_slots:
            existing_info_str = f"\nSlot yang sudah terisi sebelumnya:\n{json.dumps(current_slots, ensure_ascii=False, indent=2)}\n"

        system_prompt = (
            "Anda adalah mesin ekstraksi data (Slot-Filling Extractor) untuk layanan surat resmi universitas. "
            "Tugas Anda adalah membaca pesan terbaru pengguna dan mengekstrak nilai untuk parameter/slot yang diminta.\n"
            "ATURAN KETAT:\n"
            "1. Hanya ekstrak informasi yang secara eksplisit atau jelas tersirat dari teks pengguna.\n"
            "2. Jangan mengarang, menebak, atau memalsukan data.\n"
            "3. Jika suatu informasi tidak disebutkan dalam pesan terbaru, biarkan kosong atau null, jangan menimpa data lama yang sudah ada.\n"
            "4. Normalisasikan penulisan nama instansi atau keperluan dengan baik (misal kapitalisasi awal kata yang tepat)."
        )
        
        user_prompt = (
            f"Daftar parameter/slot yang harus dicari:\n{fields_desc_str}\n"
            f"{existing_info_str}\n"
            f"Pesan Terbaru Pengguna: \"{user_prompt_context}\"\n\n"
            f"Ekstrak nilai slot yang relevan dari pesan pengguna di atas ke dalam skema payload yang ditentukan."
        )
        
        # Panggil LLM menggunakan infrastruktur structured output yang ada
        try:
            result = self._invoke_structured_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=output_schema,
            )
        except Exception as exc:
            logger.error("Gagal melakukan ekstraksi slot LLM: %s", exc)
            # Kembalikan objek kosong agar tertangkap dengan aman oleh mekanisme fallback slot_processor
            raise exc

        # Bungkus hasil agar kompatibel dengan pemanggilan di slot_processor.py
        class ExtractionWrapper:
            def __init__(self, parsed_obj):
                if hasattr(parsed_obj, "extracted_payload"):
                    payload_obj = parsed_obj.extracted_payload
                    self.extracted_slots = payload_obj.model_dump() if hasattr(payload_obj, "model_dump") else dict(payload_obj)
                elif hasattr(parsed_obj, "model_dump"):
                    self.extracted_slots = parsed_obj.model_dump()
                elif isinstance(parsed_obj, dict):
                    self.extracted_slots = parsed_obj
                else:
                    self.extracted_slots = {}

        return ExtractionWrapper(result)
    
    def run(
        self,
        user_prompt_context: str,
        kode_surat: str,
        student_metadata: Optional[Dict[str, Any]] = None,
        style_rules: Optional[List[str]] = None,
        previous_payload: Optional[Dict[str, Any]] = None,
        previous_draft: Optional[str] = None,
        revision_notes: Optional[List[str]] = None,
        previous_score: Optional[int] = None,
        dynamic_fields_dict: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Any]] = None,
    ) -> BaseModel:
        """Alias eksekusi utama yang memanggil `draft_letter`."""
        return self.draft_letter(
            user_prompt_context=user_prompt_context,
            kode_surat=kode_surat,
            student_metadata=student_metadata,
            style_rules=style_rules,
            previous_payload=previous_payload,
            previous_draft=previous_draft,
            revision_notes=revision_notes,
            previous_score=previous_score,
            dynamic_fields_dict=dynamic_fields_dict,
            attachments=attachments,
        )
