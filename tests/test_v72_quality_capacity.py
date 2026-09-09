from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.error_handlers import install_error_handlers
from app.core.exceptions import QueueBusyError
from app.rag.loader import RagAssetReport
from app.rag.retriever import ShadowRetriever, preferred_card_id
from app.schemas.consult import GeneratedConsultation, VetRecommendation
from app.core.constants import AnswerMode, RiskLevel, VetUrgency
from app.services.medical_safety_service import MedicalSafetyService
from types import SimpleNamespace


def _generated(**updates):
    payload = {
        "summary": "当前可以先进行基础护理。",
        "what_to_do_now": [],
        "what_to_monitor": [],
        "answer_mode": AnswerMode.NORMAL,
        "risk_level": RiskLevel.LOW,
        "vet_recommendation": VetRecommendation(
            recommended=False, urgency=VetUrgency.NONE
        ),
    }
    payload.update(updates)
    return GeneratedConsultation(**payload)


def test_non_eye_case_removes_eye_template_leak():
    generated = _generated(
        what_to_do_now=["每天轻轻擦拭眼周附近，并观察绝育切口。", "按医嘱防舔。"]
    )
    cleaned, changed = MedicalSafetyService.clean_owner_facing_language(
        generated,
        is_eye_case=False,
        user_text="猫绝育后伤口怎么护理",
        rag_categories=("preventive_care",),
    )
    assert changed
    assert cleaned.what_to_do_now == ["观察绝育切口。", "按医嘱防舔。"]


def test_eye_case_keeps_relevant_eye_care():
    generated = _generated(what_to_do_now=["可以用干净纱布轻轻清洁眼周。"])
    cleaned, _ = MedicalSafetyService.clean_owner_facing_language(
        generated,
        is_eye_case=True,
        user_text="猫眼睛有分泌物",
        rag_categories=("eye",),
    )
    assert cleaned.what_to_do_now == ["可以用干净纱布轻轻清洁眼周。"]


def test_oral_topic_removes_bathing_advice_but_not_when_user_asks_about_bathing():
    generated = _generated(
        what_to_do_now=["调整洗澡频率。", "预约口腔检查。"]
    )
    cleaned, _ = MedicalSafetyService.clean_owner_facing_language(
        generated,
        user_text="狗狗口臭怎么办",
        rag_categories=("oral",),
    )
    assert cleaned.what_to_do_now == ["预约口腔检查。"]

    bathing, _ = MedicalSafetyService.clean_owner_facing_language(
        generated,
        user_text="狗口臭期间能不能洗澡",
        rag_categories=("oral",),
    )
    assert "调整洗澡频率。" in bathing.what_to_do_now


def test_fixed_fasting_replacement_is_field_aware_and_deduplicated():
    generated = _generated(
        summary="建议禁食4小时后观察。",
        answer_text="旧正文仍包含重复禁食建议",
        what_to_do_now=["先禁食4小时。", "暂停喂食4小时。"],
        what_to_monitor=["先禁食4小时。"],
    )
    cleaned, changed = MedicalSafetyService.clean_owner_facing_language(generated)
    assert changed
    assert cleaned.summary == "不建议宠物自行采用固定时长禁食。"
    assert cleaned.answer_text == ""
    combined = cleaned.what_to_do_now + cleaned.what_to_monitor
    assert len(combined) == 1
    assert "少量多次" in combined[0]


def test_diarrhea_summary_starts_with_case_acknowledgement():
    generated = _generated(summary="建议先记录排便次数。")
    cleaned, _ = MedicalSafetyService.clean_owner_facing_language(
        generated,
        user_text="狗拉稀怎么办",
        rag_categories=("gastrointestinal",),
    )
    assert cleaned.summary.startswith("狗狗目前出现了腹泻或拉稀的情况。")


def test_kitten_schedule_promotes_specific_card():
    cards = (
        {
            "id": "MVP-PAR-005",
            "species": ["cat", "dog"],
            "title": "驱虫频率需要个体化",
            "retrieval_text": "幼猫驱虫多久一次",
            "user_phrases": ["多久驱虫一次"],
            "facts": [],
        },
        {
            "id": "V17-CAT-PED-001",
            "species": ["cat"],
            "title": "小猫疫苗与驱虫日程",
            "retrieval_text": "幼猫 小猫 驱虫 疫苗 日程",
            "user_phrases": ["幼猫驱虫多久做一次"],
            "source_supported_simple_facts": [
                "幼猫疫苗需要完成基础免疫。",
                "完成基础免疫前避免接触健康状态不明的猫。",
                "幼猫驱虫通常从2-3周龄开始，之后按月龄和风险调整。",
            ],
            "facts": [],
        },
    )
    report = RagAssetReport(cards=cards, source_ids=frozenset(), index_version="test")
    result = ShadowRetriever(report).search("幼猫驱虫多久一次", species="cat")
    assert preferred_card_id("幼猫驱虫多久一次", "cat") == "V17-CAT-PED-001"
    assert result.hits[0].card_id == "V17-CAT-PED-001"
    evidence = ShadowRetriever(report).build_grounded_evidence(result)
    assert "驱虫" in evidence[0]["supported_facts"][0]


def test_v72_capacity_defaults_and_queue_error_contract():
    settings = Settings(app_env="test")
    assert settings.consult_worker_count == 10
    assert settings.queue_max_pending == 12
    assert settings.vision_max_tokens == 512
    assert settings.consult_db_pool_size == 20
    assert QueueBusyError.http_status == 503
    assert QueueBusyError.retryable is True


def test_queue_busy_is_real_http_503_with_retry_after():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/busy")
    async def busy():
        raise QueueBusyError("系统繁忙，请稍后重试")

    response = TestClient(app).get("/busy")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.json()["error"] == {
        "code": "QUEUE_BUSY",
        "message": "系统繁忙，请稍后重试",
        "retryable": True,
    }


def test_vision_cache_key_includes_hashed_text_hint():
    from app.services.image_service import ImageService
    from app.schemas.image import ProcessedImage

    settings = SimpleNamespace(
        consult_vision_model_name="Qwen3.5-4B",
        max_image_edge=1024,
        vision_max_tokens=320,
        redis_namespace="pet_consult:test",
    )
    service = ImageService(settings, SimpleNamespace(is_mock=False))
    image = ProcessedImage(
        image_id="img_1",
        filename="pet.jpg",
        format="JPEG",
        sha256="abc123",
    )
    first = service._cache_key(image, "看看伤口")
    second = service._cache_key(image, "看看眼睛")
    assert first != second
    assert "看看伤口" not in first
    assert "看看眼睛" not in second
