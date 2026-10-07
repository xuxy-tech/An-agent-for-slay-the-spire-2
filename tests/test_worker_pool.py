from controller.search import combat_search


class FakeCli:
    instances = []

    def __init__(self, cfg):
        self.cfg = cfg
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.started and not self.stopped


def test_worker_pool_prewarms_reuses_and_closes(monkeypatch):
    FakeCli.instances = []
    monkeypatch.setattr(combat_search, 'Sts2CliAdapter', FakeCli)
    pool = combat_search.CombatWorkerPool(object())

    pool.prewarm(2)
    pool.prewarm(2)
    first = pool.acquire()
    second = pool.acquire()
    pool.release(first)
    pool.release(second)

    assert len(FakeCli.instances) == 2
    assert pool.stats() == {
        'started': 2,
        'acquires': 2,
        'warm_reuses': 2,
        'releases': 2,
        'discards': 0,
        'discard_reasons': {},
        'alive': 2,
        'free': 2,
        'borrowed': 0,
    }

    pool.close()
    assert all(cli.stopped for cli in FakeCli.instances)


def test_worker_pool_discards_suspect_workers(monkeypatch):
    FakeCli.instances = []
    monkeypatch.setattr(combat_search, 'Sts2CliAdapter', FakeCli)
    pool = combat_search.CombatWorkerPool(object())
    pool.prewarm(1)
    dead = pool.acquire()
    pool.discard(dead, reason='test_failure')
    replacement = pool.acquire()

    assert replacement is not dead
    assert dead.stopped
    assert pool.stats()['discards'] == 1
    assert pool.stats()['discard_reasons'] == {'test_failure': 1}
    assert pool.stats()['started'] == 2
    pool.close()
