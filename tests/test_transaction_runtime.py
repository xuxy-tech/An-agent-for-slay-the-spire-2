import pytest

from controller.action_transaction import make_transaction
from controller.transaction_runtime import execute_transaction


def test_transaction_runtime_completes_client_before_advancing_shadow():
    order = []
    transaction = make_transaction(
        'choose_event_option', {'option_index': 4},
        shadow_action='choose_option', shadow_args={'option_index': 1},
    )

    class Live:
        def execute_transaction(self, value, **kwargs):
            order.append(('client', value.client.action, value.client.params))
            report['actions'].append({'status': 'completed'})
            return {'screen': 'EVENT'}

    def advance(action, params, log, report_value):
        order.append(('shadow', action, params))
        return {'decision': 'event_choice'}

    report = {'actions': []}
    result = execute_transaction(
        transaction,
        live=Live(),
        client_state={'screen': 'EVENT'},
        log=None,
        report=report,
        advance_shadow=advance,
        operation_label='event choice',
    )
    assert order == [
        ('client', 'choose_event_option', {'option_index': 4}),
        ('shadow', 'choose_option', {'option_index': 1}),
    ]
    assert result.shadow_state == {'decision': 'event_choice'}
    assert report['actions'][0]['transaction']['intent'] == 'event.choose_option'
    assert report['actions'][0]['shadow_action_applied'] is True
    assert report['actions'][0]['transaction_status'] == 'shadow_completed_awaiting_checkpoint'
    assert report['actions'][0]['boundary_verified'] is False
    timeline = report['actions'][0]['transaction_timeline']
    assert timeline['client_settled_ms'] >= 0.0
    assert timeline['shadow_started_ms'] >= timeline['client_settled_ms']
    assert timeline['shadow_finished_ms'] >= timeline['shadow_started_ms']
    assert timeline['shadow_elapsed_ms'] >= 0.0


def test_transaction_runtime_records_shadow_failure_after_client_completion():
    transaction = make_transaction(
        'end_turn', {}, shadow_action='end_turn', shadow_args={}
    )
    report = {'actions': []}

    class Live:
        def execute_transaction(self, value, **kwargs):
            report['actions'].append({'sequence': 7, 'status': 'completed'})
            return {'screen': 'COMBAT'}

    class Log:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

    log = Log()
    with pytest.raises(TimeoutError, match='shadow timeout'):
        execute_transaction(
            transaction,
            live=Live(),
            client_state={'screen': 'COMBAT'},
            log=log,
            report=report,
            advance_shadow=lambda *args: (_ for _ in ()).throw(
                TimeoutError('shadow timeout')
            ),
            operation_label='end turn',
        )
    row = report['actions'][0]
    assert row['shadow_action_applied'] is False
    assert row['transaction_status'] == 'shadow_failed'
    assert row['shadow_error'] == 'shadow timeout'
    assert log.events[-1]['event'] == 'shadow_failed'
    assert report['actions'][0]['transaction_timeline']['shadow_elapsed_ms'] >= 0.0


def test_transaction_runtime_records_structured_shadow_error():
    transaction = make_transaction(
        'end_turn', {}, shadow_action='end_turn', shadow_args={}
    )
    report = {'actions': []}

    class Live:
        def execute_transaction(self, value, **kwargs):
            report['actions'].append({'sequence': 8, 'status': 'completed'})
            return {'screen': 'COMBAT'}

    class Log:
        def __init__(self):
            self.events = []

        def write(self, event):
            self.events.append(event)

    log = Log()
    with pytest.raises(RuntimeError, match='Headless failed to end turn'):
        execute_transaction(
            transaction,
            live=Live(),
            client_state={'screen': 'COMBAT'},
            log=log,
            report=report,
            advance_shadow=lambda *args: {'type': 'error', 'message': 'rejected'},
            operation_label='end turn',
        )
    row = report['actions'][0]
    assert row['shadow_action_applied'] is False
    assert row['transaction_status'] == 'shadow_failed'
    assert 'rejected' in row['shadow_error']
    assert log.events[-1]['event'] == 'shadow_failed'
