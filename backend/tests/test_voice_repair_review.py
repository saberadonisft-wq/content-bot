import json
import threading

import pytest
from test_voice_repair_text import payload

from app.services.voiceover.repair_budget import RepairBudget
from app.services.voiceover.repair_review import propose_reviewed_wording


@pytest.mark.parametrize('decision', ['pass', 'lost_meaning', 'unclear_source', 'wrong_id'])
def test_separate_review_blocks_generator_claims_before_tts(tmp_path, decision):
    inputs = [{'id': 'one', 'spoken_text': 'Tôi không thể nào trả cô 2 đồng.', 'max_syllables': 8,
               'source_text': 'I cannot pay you 2 dong.'}]
    budget = RepairBudget(tmp_path / 'budget.json', input_binding='a' * 64, cluster_ids=['one'])
    calls = []

    def request(service, ledger, ids, prompt, schema, *, cancel):
        receipt = ledger.reserve('gemini', ids, cancel=cancel)
        ledger.settle(receipt, succeeded=True)
        calls.append(prompt)
        if len(calls) == 1:
            return payload('Tôi không thể trả cô 2 đồng.')
        assert 'meaning_preserved' not in prompt  # No generator's self-assessment.
        rows = json.loads(prompt.split('DỮ LIỆU:\n', 1)[1])
        assert rows[0]['input']['source_text'] == inputs[0]['source_text']
        review = dict(candidate_id=rows[0]['candidate_id'], reason='fixture',
                      **dict.fromkeys(['meaning_preserved', 'polarity_preserved', 'quantities_preserved',
                                       'addressing_preserved', 'tone_preserved', 'source_is_clear'], True))
        if decision == 'lost_meaning':
            review['meaning_preserved'] = False
        elif decision == 'unclear_source':
            review['source_is_clear'] = False
        elif decision == 'wrong_id':
            review['candidate_id'] = 'b' * 64
        return json.dumps({'reviews': [review]})

    if decision == 'wrong_id':
        with pytest.raises(ValueError, match='ID'):
            propose_reviewed_wording(None, budget, inputs, cancel=threading.Event(), request=request)
    else:
        result = propose_reviewed_wording(None, budget, inputs, cancel=threading.Event(), request=request)
        assert bool(result['one']) == (decision == 'pass')
    assert budget.snapshot()['gemini_used'] == 2 and budget.snapshot()['tts_used'] == 0
