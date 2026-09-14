import json

import pytest

from app.services.voiceover.repair_text import validate_alternatives


def payload(text):
    return json.dumps({'rows': [{'id': 'one', 'alternatives': [{'spoken_text': text,
        **dict.fromkeys(['meaning_preserved', 'polarity_preserved', 'quantities_preserved',
                         'addressing_preserved', 'tone_preserved'], True)}]}]})


@pytest.mark.parametrize('text', ['Tôi trả cô 2 đồng.', 'Tôi không trả cô 3 đồng.', 'Ta không trả cô 2 đồng.',
                                 'Tôi không trả cô hai đồng.'])
def test_candidates_cannot_drop_negation_change_addressing_or_quantity(text):
    inputs = [{'id': 'one', 'spoken_text': 'Tôi không thể nào trả cô 2 đồng.', 'max_syllables': 8}]
    assert validate_alternatives(payload(text), inputs) == {'one': []}


def test_shorter_wording_remains_only_a_candidate_and_wrong_mapping_is_rejected():
    inputs = [{'id': 'one', 'spoken_text': 'Tôi không thể nào trả cô 2 đồng.', 'max_syllables': 8}]
    assert validate_alternatives(payload('Tôi không thể trả cô 2 đồng.'), inputs) == {'one': ['Tôi không thể trả cô 2 đồng.']}
    with pytest.raises(ValueError):
        validate_alternatives('{"rows": []}', inputs)


def test_quantity_units_and_conditional_meaning_are_not_removed():
    inputs = [{'id': 'one', 'spoken_text': 'Nếu tôi có đủ 2 triệu đồng, tôi trả cô.', 'max_syllables': 12}]
    for text in ['Nếu tôi có 2 đồng, tôi trả cô.', 'Tôi trả cô 2 triệu đồng.']:
        assert validate_alternatives(payload(text), inputs) == {'one': []}
