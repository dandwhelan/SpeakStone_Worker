"""Fix 7: SSW_GPU is honoured; several cards: the board reads card 0, the "good enough" checks the biggest."""
from speakstone_worker import stats

TWO = "40, 5, 1000, 8192, Small Card\n35, 0, 500, 24576, Big Card\n"


class Out:
    def __init__(self, text):
        self.stdout, self.returncode = text, 0


def fake(text, seen):
    def run(cmd, **kw):
        seen.append(cmd)
        return Out(text)
    return run


def test_ssw_gpu_pins_the_reading_to_that_card():
    seen = []
    g = stats.gpu_reading(fake("35, 0, 500, 24576, Big Card\n", seen), env={"SSW_GPU": "1"})
    assert seen[0][-2:] == ["-i", "1"] and g["name"] == "Big Card"
    seen.clear()
    stats.gpu_reading(fake(TWO, seen), env={"SSW_GPU": "junk"})
    assert "-i" not in seen[0]


def test_board_reads_card_zero_and_the_checks_read_the_biggest():
    seen = []
    assert stats.gpu_reading(fake(TWO, seen), env={})["mem_total_mib"] == 8192
    assert stats.gpu_total_mib(fake(TWO, seen), env={}) == 24576
    assert stats.gpu_reading(fake(TWO, seen), env={}, biggest=True)["name"] == "Big Card"


def test_pinned_card_wins_over_biggest_and_failures_stay_quiet():
    seen = []
    assert stats.gpu_total_mib(fake("40, 5, 1000, 8192, Small Card\n", seen), env={"SSW_GPU": "0"}) == 8192
    assert stats.gpu_total_mib(fake("", seen), env={}) is None
    assert stats.gpu_total_mib(fake("garbage\n40, 5, 1000, 8192, X\n", seen), env={}) == 8192

    def boom(cmd, **kw):
        raise OSError
    assert stats.gpu_reading(boom, env={}) is None
