"""Positive complete observation confirms; uncertain absence never proves no write."""
from agentcheck_biz.persistence.store import BindingError


class NeedsVerification(RuntimeError):
    pass


def classify(observation, expected):
    if not isinstance(observation, dict) or observation.get('complete') is not True:
        return 'unknown', None
    rows = observation.get('items')
    if not isinstance(rows, list):
        return 'unknown', None
    if not rows:
        return 'absent', None
    if len(rows) != 1 or not isinstance(rows[0], dict) or any(rows[0].get(k) != v for k, v in expected.items()):
        return 'conflict', None
    return 'confirmed', rows[0]


class Recovery:
    def __init__(self, store, manifest, lease, target, *, gate=lambda window: None, retry_unknown=False):
        self.store, self.manifest, self.lease, self.target = store, manifest, lease, target
        self.gate, self.retry_unknown = gate, retry_unknown

    def apply(self):
        row = self.store.read(self.manifest)
        if row is None:
            raise BindingError('Missing durable operation; never recreate it on recovery')
        if row['state'] == 'confirmed':
            return row['result']
        if row['state'] == 'conflict':
            raise NeedsVerification('Conflicting operation requires manual verification')
        if row['state'] == 'prepared':
            self.gate('before_call')
            self.send()
        elif self.retry_unknown:
            if not self.target.atomic_idempotency:
                raise BindingError('Replay requires the verified atomic Ticket contract')
            self.send()
        observation = self.target.observe()
        state, value = classify(observation, self.target.expected)
        if state == 'absent' and self.target.atomic_idempotency:
            # Same frozen payload and business key; the service serializes even
            # an older still-in-flight create in its atomic idempotency transaction.
            self.send()
            observation = self.target.observe()
            state, value = classify(observation, self.target.expected)
        if state == 'confirmed':
            self.store.transition(self.manifest, self.lease, 'confirmed', observation, value)
            return value
        self.store.transition(self.manifest, self.lease, 'conflict' if state == 'conflict' else 'sent_unknown', observation)
        raise NeedsVerification('Business result ' + state + '; manual verification required')

    def send(self):
        # Commit unknown BEFORE issuing HTTP. A kill at any later point must
        # reconcile or rely on actual service idempotency, never a local lock.
        self.store.transition(self.manifest, self.lease, 'sent_unknown', dict(reason='before_http_send'))
        self.gate('sent_unknown')
        try:
            self.target.create()
        except Exception as exc:
            self.store.transition(self.manifest, self.lease, 'sent_unknown', dict(error_type=type(exc).__name__))
            raise NeedsVerification('Create response uncertain; manual verification required') from exc
        self.gate('after_commit')
