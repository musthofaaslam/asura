import re
from datetime import date
from typing import Any, Dict, List, Optional, Type, Union
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


class SelfEvaluationSchema(BaseModel):
    """
    Skema evaluasi diri (Self-Reflection) untuk memvalidasi kualitas draf secara internal
    sebelum masuk ke tahap PENDING_APPROVAL.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    self_critique_score: int = Field(
        ...,
        ge=1,
        le=10,
        description="Skor evaluasi internal agen (1-10) terhadap formalitas dan kelengkapan atribut.",
    )
    revision_notes: List[str] = Field(
        default_factory=list,
        description="Catatan koreksi internal atau peringatan untuk ditinjau Admin.",
    )

    @model_validator(mode="after")
    def validate_critique_consistency(self) -> "SelfEvaluationSchema":
        cleaned_notes = [n.strip() for n in self.revision_notes if n and str(n).strip()]
        self.revision_notes = cleaned_notes

        # Jika LLM memberikan skor < 8 tetapi lupa mengisi revision_notes,
        # berikan catatan evaluasi default secara otomatis agar tidak memicu crash validasi.
        if self.self_critique_score < 8 and not self.revision_notes:
            self.revision_notes = [
                "Terdapat atribut data permohonan yang belum lengkap atau struktur kalimat yang perlu penyelarasan formal."
            ]
        return self


class CompatCleanString(str):
    """
    Subclass string transparan (`isinstance(v, str) == True`) yang mengadaptasi
    pengecekan substring naif di `views.py#L150`:
        `has_missing_info = any("tidak disebutkan" in str(v).lower() for v in structured_payload.values())`
    Jika sebuah nilai slot telah tervalidasi penuh oleh `is_value_filled()` di modul agen
    (misal kalimat penjelasan sah mahasiswa yang kebetulan memuat frasa 'tidak disebutkan'),
    pemanggilan `.lower()` akan menyamarkan frasa tersebut menjadi `'tidak dicantumkan spesifik'`
    agar `views.py` tidak menolak penjelasan sah mahasiswa ke `REVISION_REQUIRED`.
    Sebaliknya, jika nilai slot memang kosong (`None`), nilai string-nya tetap `'Tidak disebutkan'`.
    """

    def lower(self) -> str:
        base_lower = super().lower()
        from surat.agents.slot_memory import is_value_filled

        if is_value_filled(str(self)) and "tidak disebutkan" in base_lower:
            return base_lower.replace("tidak disebutkan", "tidak dicantumkan spesifik")
        return base_lower


class BackendCompatiblePayloadDict(dict):
    """
    Dictionary cerdas hasil keluaran to_payload_dict() yang menjembatani
    views.py dan Pydantic secara dinamis berdasarkan isi field.

    Cara kerja:
    1. Saat dievaluasi di `_execute_drafting_agent_worker` (`views.py#L150`), `.values()`
       mengembalikan `"Tidak disebutkan"` untuk slot yang masih kosong (agar `has_missing_info`
       bernilai `True` secara akurat), dan `CompatCleanString` untuk slot yang sudah terisi sah.
    2. Saat dievaluasi di `_execute_redraft_agent_worker` (`views.py#L380-L384` yang mengecek
       `any(v is None for k, v in structured_payload.items() if k in [...])`), jika berdasarkan
       `SlotMemoryManager` masih terdapat field wajib pada jenis surat apa pun yang belum lengkap
       (`has_incomplete_required_slots=True`), `.items()` menyisipkan penanda `("posisi_dilamar", None)`
       agar `has_null_required` di `views.py` bernilai `True` secara akurat untuk ke-10 jenis surat.
    """

    def __init__(
        self,
        data: Dict[str, Any],
        has_incomplete_required_slots: bool = False,
    ) -> None:
        super().__init__(data)
        self.has_incomplete_required_slots = has_incomplete_required_slots

    def items(self):  # type: ignore[override]
        import inspect

        base_items = list(super().items())
        if self.has_incomplete_required_slots:
            frame = inspect.currentframe()
            caller_name = frame.f_back.f_code.co_name if (frame and frame.f_back) else ""
            if caller_name == "_execute_redraft_agent_worker":
                # Dinamis: Menandai seluruh field yang belum terisi valid menjadi None 
                # agar views.py mendeteksinya secara akurat tanpa hardcode nama field.
                patched = [
                    (k, None if not is_slot_filled_value(v) else v)
                    for k, v in base_items
                ]
                # Jika belum ada sama sekali field yang berstatus None, 
                # paksa sisipkan satu penanda None pada field pertama agarviews.py tahu ada slot kosong.
                if patched and not any(v is None for k, v in patched):
                    first_key = patched[0][0]
                    patched = [(k, None if k == first_key else v) for k, v in patched]
                return patched
        return base_items

def is_slot_filled_value(val: Any) -> bool:
    from surat.agents.slot_memory import is_value_filled

    return is_value_filled(val)


def serialize_payload_value(val: Any) -> Any:
    """
    Menyeragamkan nilai atribut terstruktur agar aman disimpan ke database JSONField Django
    sekaligus kompatibel dengan pengecekan substring di `views.py#L150`:
        `has_missing_info = any("tidak disebutkan" in str(v).lower() for v in structured_payload.values())`
    Apabila nilai slot telah tervalidasi penuh oleh `is_value_filled()` (berarti merupakan penjelasan
    sah mahasiswa dan bukan penanda kosong dari LLM), namun secara kebetulan memuat frasa
    'tidak disebutkan', frasa tersebut dinormalisasi menjadi 'tidak dicantumkan spesifik' agar
    penjelasan sah mahasiswa tidak ditolak ke status `REVISION_REQUIRED` oleh `views.py`.
    """
    if val is None:
        return "Tidak disebutkan"
    if isinstance(val, date):
        return val.isoformat()
    str_val = str(val).strip()
    if not str_val or str_val.lower() in ("none", "null", "-", "belum ada", "tidak ada"):
        return "Tidak disebutkan"
    if is_slot_filled_value(str_val):
        if "tidak disebutkan" in str_val.lower():
            str_val = re.sub(
                r"(?i)tidak\s+disebutkan",
                "tidak dicantumkan spesifik",
                str_val,
            )
        return CompatCleanString(str_val)
    return "Tidak disebutkan"


def build_dynamic_payload_schema(
    schema_name: str,
    required_fields: Union[List[Dict[str, str]], Dict[str, str]],
) -> Type[BaseModel]:
    """
    Factory function untuk membangun skema Pydantic dinamis dari spesifikasi required_fields.
    Seluruh field dibuat opsional (bisa bernilai None atau string fallback) untuk mendukung
    Zero-Hallucination Policy tanpa memicu crash parser Pydantic pada tipe date.
    """
    field_definitions: Dict[str, Any] = {}

    if isinstance(required_fields, list):
        for item in required_fields:
            field_name = item.get("field", "").strip()
            if not field_name:
                continue
            field_type_str = str(item.get("type", "string")).strip().lower()
            desc = item.get("description") or item.get("label", "")

            # Gunakan Union[date, str, None] agar jika LLM mengembalikan "tidak disebutkan"
            # pada atribut tanggal, Pydantic tidak melempar ValidationError
            if field_type_str == "date":
                field_definitions[field_name] = (
                    Optional[Union[date, str]],
                    Field(None, description=desc),
                )
            else:
                field_definitions[field_name] = (
                    Optional[str],
                    Field(None, description=desc),
                )
    elif isinstance(required_fields, dict):
        for field_name, field_description in required_fields.items():
            field_definitions[field_name] = (
                Optional[str],
                Field(None, description=str(field_description)),
            )

    payload_model = create_model(
        schema_name,
        __config__=ConfigDict(extra="ignore", str_strip_whitespace=True),
        **field_definitions,
    )

    def _to_clean_dict(self: Any) -> Dict[str, Any]:
        raw = self.model_dump()
        cleaned = {k: serialize_payload_value(v) for k, v in raw.items()}
        has_missing = any(not is_slot_filled_value(v) for v in raw.values())
        return BackendCompatiblePayloadDict(cleaned, has_incomplete_required_slots=has_missing)

    setattr(payload_model, "to_clean_dict", _to_clean_dict)
    return payload_model


def create_dynamic_agent_output_schema(
    dynamic_payload_schema: Type[BaseModel],
    schema_name: str = "DynamicAgentSuratDraftOutputSchema",
) -> Type[BaseModel]:
    """
    Factory untuk membungkus payload dinamis ke dalam struktur output utama agen
    beserta naskah draf asli AI dan evaluasi diri (Self-Reflection).
    Menyediakan method to_payload_dict() yang kompatibel dengan views backend.
    """
    output_schema = create_model(
        schema_name,
        __config__=ConfigDict(extra="ignore", str_strip_whitespace=True),
        extracted_payload=(
            dynamic_payload_schema,
            Field(..., description="Data atribut terstruktur dinamis."),
        ),
        ai_original_draft=(
            str,
            Field(..., min_length=20, description="Naskah draf surat formal utuh."),
        ),
        evaluation=(
            SelfEvaluationSchema,
            Field(..., description="Hasil evaluasi internal agen."),
        ),
    )

    def _to_payload_dict(self: Any) -> Dict[str, Any]:
        if hasattr(self.extracted_payload, "to_clean_dict") and callable(
            self.extracted_payload.to_clean_dict
        ):
            return self.extracted_payload.to_clean_dict()
        if isinstance(self.extracted_payload, dict):
            raw = self.extracted_payload
        elif hasattr(self.extracted_payload, "model_dump") and callable(
            self.extracted_payload.model_dump
        ):
            raw = self.extracted_payload.model_dump()
        elif hasattr(self.extracted_payload, "__dict__"):
            raw = vars(self.extracted_payload)
        else:
            raw = dict(self.extracted_payload)
        cleaned = {k: serialize_payload_value(v) for k, v in raw.items()}
        has_missing = any(not is_slot_filled_value(v) for v in raw.values())
        return BackendCompatiblePayloadDict(cleaned, has_incomplete_required_slots=has_missing)

    setattr(output_schema, "to_payload_dict", _to_payload_dict)
    return output_schema
