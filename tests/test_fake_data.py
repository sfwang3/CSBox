from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import HOME_DATA_SOURCES, FakeHomeDataSource


def test_fake_home_data_preserves_environment_and_marks_every_item_as_demo() -> None:
    environment = EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )

    snapshot = FakeHomeDataSource().get_home_snapshot(environment)

    assert snapshot.environment == environment
    assert len(snapshot.recent_experiments) >= 3
    assert all(experiment.demo for experiment in snapshot.recent_experiments)


def test_fake_source_is_explicitly_registered_without_discovery() -> None:
    assert isinstance(HOME_DATA_SOURCES.get("fake"), FakeHomeDataSource)
