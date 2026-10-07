from controller.search.worker_scaling import (
    AdaptiveWorkerController,
    recommended_hardware_workers,
    recommended_initial_workers,
)


def test_recommended_workers_reserve_visible_client_capacity():
    assert recommended_hardware_workers(4) == 3
    assert recommended_hardware_workers(8) == 6
    assert recommended_hardware_workers(32) == 16
    assert recommended_initial_workers(32) == 8


def test_adaptive_workers_require_sustained_underload_before_growth():
    controller = AdaptiveWorkerController(hardware_max=8, current_workers=3)
    first = controller.observe(60, 65, queued_root_work=True)
    second = controller.observe(62, 68, queued_root_work=True)
    assert first['after'] == 3
    assert second == {'before': 3, 'after': 4, 'reason': 'sustained_underload'}


def test_adaptive_workers_reduce_immediately_at_safety_threshold():
    controller = AdaptiveWorkerController(hardware_max=8, current_workers=6)
    result = controller.observe(84, 96, queued_root_work=True)
    assert result == {'before': 6, 'after': 4, 'reason': 'cpu_peak_safety'}


def test_severe_deadline_overrun_does_not_reduce_idle_workers():
    controller = AdaptiveWorkerController(hardware_max=16, current_workers=12)
    result = controller.observe(
        30, 50, queued_root_work=True,
        deadline_overrun=True, deadline_overrun_ratio=3.0,
    )
    assert result == {
        'before': 12, 'after': 12, 'reason': 'severe_deadline_overrun_held'
    }


def test_deadline_overrun_scales_up_when_cpu_is_idle_and_work_is_queued():
    controller = AdaptiveWorkerController(hardware_max=16, current_workers=4)
    result = controller.observe(
        25, 40, queued_root_work=True,
        deadline_overrun=True, deadline_overrun_ratio=1.4,
    )
    assert result == {
        'before': 4, 'after': 6, 'reason': 'deadline_coverage_scale_up'
    }


def test_runtime_pressure_still_reduces_workers_when_cpu_is_saturated():
    controller = AdaptiveWorkerController(hardware_max=8, current_workers=4)
    result = controller.observe(
        92, 93, queued_root_work=True,
        deadline_overrun=True, deadline_overrun_ratio=2.5,
    )
    assert result == {'before': 4, 'after': 3, 'reason': 'runtime_pressure'}


def test_deadline_overrun_recovers_from_serial_underutilization():
    controller = AdaptiveWorkerController(hardware_max=8, current_workers=1)
    result = controller.observe(
        12, 20, queued_root_work=True,
        deadline_overrun=True, deadline_overrun_ratio=1.4,
    )
    assert result == {
        'before': 1, 'after': 2, 'reason': 'deadline_coverage_scale_up'
    }


def test_fixed_worker_mode_never_changes():
    controller = AdaptiveWorkerController(hardware_max=8, mode='fixed', current_workers=5)
    assert controller.observe(99, 100, queued_root_work=True)['after'] == 5
