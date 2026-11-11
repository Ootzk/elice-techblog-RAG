from threading import Barrier

from elice_knowledge.evaluation import parallel_records


def test_evaluation_records_run_concurrently_and_retain_input_positions():
    barrier = Barrier(4, timeout=5)
    records = ["a", "b", "c", "d"]

    def execute(record):
        barrier.wait()
        return record.upper()

    outcomes = list(parallel_records(records, execute))
    assert sorted(outcomes) == [(0, "A"), (1, "B"), (2, "C"), (3, "D")]
