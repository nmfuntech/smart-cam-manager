import threading

from blackframe.agent.residency import (
    ModelResidencyManager,
    _parse_macos_vm_stat,
    choose_keep_alive,
)


def test_resident_mode_always_keeps_model_loaded() -> None:
    assert choose_keep_alive("resident", "5m", 100, 2048, 1024) == "-1"


def test_idle_and_off_modes_preserve_configured_duration() -> None:
    assert choose_keep_alive("idle", "20m", 9000, 2048, 1024) == "20m"
    assert choose_keep_alive("off", "20m", 9000, 2048, 1024) == "20m"


def test_auto_mode_uses_resident_only_when_ram_budget_allows() -> None:
    assert choose_keep_alive("auto", "30m", 8192, 2048, 1536) == "-1"
    assert choose_keep_alive("auto", "30m", 3000, 2048, 1536) == "30m"


def test_invalid_mode_falls_back_to_idle() -> None:
    assert choose_keep_alive("wat", "7m", 9000, 1, 1) == "7m"


def test_macos_vm_stat_parser_counts_reclaimable_pages() -> None:
    output = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000.
Pages inactive:                           500.
Pages speculative:                        100.
Pages purgeable:                           50.
"""

    assert _parse_macos_vm_stat(output) == 25.78125


def test_preload_is_deduplicated_while_in_progress() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def warmup(base_url, model, *, keep_alive):
        calls.append((base_url, model, keep_alive))
        entered.set()
        release.wait(timeout=2)

    manager = ModelResidencyManager(
        "http://127.0.0.1:11434",
        "small-model",
        mode="resident",
        warmup=warmup,
    )

    assert manager.preload_async() is True
    assert entered.wait(timeout=1)
    assert manager.preload_async() is False
    release.set()
    manager.wait(timeout=2)

    assert calls == [("http://127.0.0.1:11434", "small-model", "-1")]
