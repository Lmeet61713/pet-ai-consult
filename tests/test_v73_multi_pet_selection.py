from app.schemas.consult import ConsultCommand
from app.schemas.pet import PetInfo


def _pets() -> list[PetInfo]:
    return [
        PetInfo(name="咪咪", species="cat"),
        PetInfo(name="豆豆", species="dog"),
    ]


def test_explicit_pet_ref_has_highest_priority():
    selected = ConsultCommand._resolve_pet(_pets(), "咪咪", "狗狗拉肚子")
    assert selected.name == "咪咪"


def test_pet_name_is_inferred_when_pet_ref_is_missing():
    selected = ConsultCommand._resolve_pet(_pets(), None, "豆豆今天拉肚子")
    assert selected.name == "豆豆"


def test_dog_is_inferred_from_question_when_cat_is_first():
    selected = ConsultCommand._resolve_pet(_pets(), None, "狗狗拉肚子怎么办？")
    assert selected.name == "豆豆"
    assert selected.species == "dog"


def test_cat_is_inferred_from_question_when_cat_is_first():
    selected = ConsultCommand._resolve_pet(_pets(), None, "猫咪最近不爱吃饭")
    assert selected.name == "咪咪"
    assert selected.species == "cat"


def test_invalid_explicit_pet_ref_keeps_legacy_fallback():
    selected = ConsultCommand._resolve_pet(_pets(), "不存在", "狗狗拉肚子")
    assert selected.name == "咪咪"
