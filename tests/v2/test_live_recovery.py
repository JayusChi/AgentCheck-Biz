"""D34 consent binding, fixed controls and business verdicts (no paid requests)."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from agentcheck_biz.live_recovery.plan import make_plan, validate, authorize
from agentcheck_biz.live_recovery.worker import fake_response
from agentcheck_biz.persistence.store import digest


class PreviewTests(unittest.TestCase):
    def test_preview_does_not_read_credentials_or_connect(self):
        with (patch('pipeline.bailian.bailian_connection',side_effect=AssertionError('credentials')),
              patch('socket.socket.connect',side_effect=AssertionError('network'))):
            plan=make_plan()
        self.assertEqual(len(plan['slots']),6)
        self.assertEqual(plan['maximum_model_requests'],30)
        self.assertEqual(plan['first_request']['max_tokens'],512)
        self.assertEqual(plan['limits'],dict(model_limit=5,tool_limit=6,wall_seconds=90))
        self.assertEqual(plan['parent_timeout_seconds'],180)

    def test_all_mutated_controls_rejected(self):
        plan=make_plan()
        for key,value in [('maximum_model_requests',31),('endpoint','https://example.com'),
                          ('recovery','new adapter'),('source_sha256','old')]:
            changed=deepcopy(plan); changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validate(changed)

    def test_abc_only_service_and_fault_change(self):
        plan=make_plan()
        for offset in (0,3):
            a,b,c=plan['slots'][offset:offset+3]
            self.assertEqual((a['app_version'],b['app_version'],c['app_version']),('unsafe','unsafe','fixed'))
            self.assertEqual((a['fault'],b['fault'],c['fault']),(False,True,True))

    def test_authorization_bound_to_payload_output_and_explicit_uncapped_fees(self):
        plan=make_plan()
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'new'
            record=dict(schema='d34-authorization/1',plan_sha256=digest(plan),output=str(output.resolve()),
                maximum_model_requests=30,accept_uncapped_input_and_fees=True,approved=True,user_authorization='explicit test consent')
            authorize(plan,record,output)
            for key in record:
                changed=dict(record); changed.pop(key)
                with self.subTest(key=key),self.assertRaises(ValueError):authorize(plan,changed,output)
            with self.assertRaises(ValueError):authorize(plan,record,Path(temp)/'other')
            output.mkdir()
            with self.assertRaises(ValueError):authorize(plan,record,output)

    def test_fixture_recovery_is_a_choice_not_forced_retry(self):
        import json
        messages=make_plan()['first_request']['messages']+[dict(role='tool',content=json.dumps(dict(status='outcome_unknown')))]
        self.assertEqual(fake_response(messages)['tool_calls'][0]['function']['name'],'query_tickets')
        self.assertEqual(fake_response(messages,True)['tool_calls'][0]['function']['name'],'create_ticket')

    def test_live_batch_rejects_missing_consent_before_resource_allocation(self):
        from agentcheck_biz.live_recovery.runner import batch
        with tempfile.TemporaryDirectory() as temp,patch('agentcheck_biz.live_recovery.runner.PostgresRuntime') as pg:
            with self.assertRaises(ValueError):batch(Path(temp)/'new',make_plan(),'live',model_key='test-only')
            pg.assert_not_called()
            self.assertFalse((Path(temp)/'new').exists())

    def test_fail_is_retained_error_stops_and_slots_are_not_replaced(self):
        from agentcheck_biz.live_recovery.runner import batch
        class PG:
            dsn='offline'; receipts=[]
            def __init__(self,*args):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
        with tempfile.TemporaryDirectory() as temp,patch('agentcheck_biz.live_recovery.runner.PostgresRuntime',PG),\
             patch('agentcheck_biz.live_recovery.runner.LiveBudgets'),\
             patch('agentcheck_biz.live_recovery.runner.execute_slot',side_effect=[dict(status='FAIL'),dict(status='ERROR')]) as execute:
            result=batch(Path(temp)/'new',make_plan(),'offline-test')
        self.assertEqual(execute.call_count,2)
        self.assertEqual([r['status'] for r in result['slots']],['FAIL','ERROR']+['NOT_STARTED']*4)
        self.assertEqual(result['reserved_requests'],10)


if __name__=='__main__':unittest.main()
