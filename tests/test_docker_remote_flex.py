import pytest

from swerex.deployment.config import DockerDeploymentConfig, PortBinding
from swerex.deployment.docker import DockerDeployment


def _sdk_module_available() -> bool:
    """Return True if docker SDK module is importable (daemon may not be reachable)."""
    try:
        import docker  # type: ignore

        _ = docker  # silence linter
        return True
    except Exception:
        return False


def test_infer_runtime_host_cli_tcp():
    """When using CLI engine and DOCKER_HOST=tcp://host:port, infer http://host."""
    cfg = DockerDeploymentConfig(
        image="python:3.11",
        engine="cli",
        docker_env={"DOCKER_HOST": "tcp://10.0.0.8:2375"},
    )
    d = DockerDeployment.from_config(cfg)
    assert d._infer_runtime_host() == "http://10.0.0.8"


def test_infer_runtime_host_cli_ssh():
    """When using CLI engine and DOCKER_HOST=ssh://user@host, infer http://host."""
    cfg = DockerDeploymentConfig(
        image="python:3.11",
        engine="cli",
        docker_env={"DOCKER_HOST": "ssh://user@remote-host"},
    )
    d = DockerDeployment.from_config(cfg)
    assert d._infer_runtime_host() == "http://remote-host"


@pytest.mark.skipif(not _sdk_module_available(), reason="docker SDK module not available")
def test_infer_runtime_host_sdk_tcp():
    """When using SDK engine and tcp endpoint, infer http://host."""
    cfg = DockerDeploymentConfig(
        image="python:3.11",
        engine="sdk",
        docker_endpoint="tcp://10.0.0.9:2375",
    )
    d = DockerDeployment.from_config(cfg)
    assert d._infer_runtime_host() == "http://10.0.0.9"


def test_default_ports_legacy_with_port():
    """Legacy behavior: when ports is empty and port is set, create single binding host:port -> 8000/tcp."""
    cfg = DockerDeploymentConfig(
        image="python:3.11",
        port=12345,
        runtime_container_port=8000,
    )
    d = DockerDeployment.from_config(cfg)
    pbs = d._default_ports()
    assert len(pbs) == 1
    assert pbs[0].container_port == 8000
    assert pbs[0].host_port == 12345
    assert pbs[0].protocol == "tcp"


def test_default_ports_respects_explicit_ports_list():
    """When ports is provided, _default_ports returns it as-is."""
    explicit = [PortBinding(container_port=8000, host_port=None, protocol="tcp")]
    cfg = DockerDeploymentConfig(
        image="python:3.11",
        ports=explicit,
    )
    d = DockerDeployment.from_config(cfg)
    pbs = d._default_ports()
    # Exactly the same object list semantics are not guaranteed, check values
    assert len(pbs) == 1
    assert pbs[0].container_port == 8000
    assert pbs[0].host_port is None
    assert pbs[0].protocol == "tcp"
