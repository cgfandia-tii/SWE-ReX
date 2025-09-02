import os
from pathlib import Path

import pytest

from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.docker import DockerDeployment
from swerex.utils.free_port import find_free_port


async def test_docker_deployment():
    port = find_free_port()
    print(f"Using port {port} for the docker deployment")
    d = DockerDeployment(image="swe-rex-test:latest", port=port)
    with pytest.raises(RuntimeError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()


@pytest.mark.slow
async def test_docker_deployment_with_python_standalone():
    port = find_free_port()
    print(f"Using port {port} for the docker deployment")
    d = DockerDeployment(image="ubuntu:latest", port=port, python_standalone_dir="/root")
    with pytest.raises(RuntimeError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()


@pytest.mark.slow
def test_docker_deployment_config_platform():
    config = DockerDeploymentConfig(docker_args=["--platform", "linux/amd64", "--other-arg"])
    assert config.platform == "linux/amd64"

    config = DockerDeploymentConfig(docker_args=["--platform=linux/amd64", "--other-arg"])
    assert config.platform == "linux/amd64"

    config = DockerDeploymentConfig(docker_args=["--other-arg"])
    assert config.platform is None

    with pytest.raises(ValueError):
        config = DockerDeploymentConfig(platform="linux/amd64", docker_args=["--platform", "linux/amd64"])
    with pytest.raises(ValueError):
        config = DockerDeploymentConfig(platform="linux/amd64", docker_args=["--platform=linux/amd64"])


def test_docker_deployment_config_container_runtime():
    # Test default container runtime is docker
    config = DockerDeploymentConfig(image="test")
    assert config.container_runtime == "docker"

    # Test setting container runtime to podman
    config = DockerDeploymentConfig(image="test", container_runtime="podman")
    assert config.container_runtime == "podman"


async def test_podman_deployment():
    """Test deployment with Podman container runtime"""
    port = find_free_port()
    print(f"Using port {port} for the podman deployment")
    d = DockerDeployment(image="swe-rex-test:latest", port=port, container_runtime="podman")
    assert d._config.container_runtime == "podman"
    # Note: This test will only pass if podman is installed and the test image exists
    # In CI/CD environments without podman, this test may be skipped


def test_podman_deployment_config():
    """Test that DockerDeployment works with podman configuration"""
    config = DockerDeploymentConfig(image="test:latest", container_runtime="podman", port=8080, pull="never")
    deployment = DockerDeployment.from_config(config)
    assert deployment._config.container_runtime == "podman"
    assert deployment._config.image == "test:latest"
    assert deployment._config.port == 8080


# ----------------------------- SDK engine tests -----------------------------


def _sdk_daemon_available() -> bool:
    """Return True if a Docker daemon is reachable via SDK (no CLI required)."""
    try:
        import docker  # type: ignore
    except Exception:
        return False
    try:
        base_url = os.environ.get("DOCKER_HOST")
        if base_url is None and Path("/var/run/docker.sock").exists():
            base_url = "unix:///var/run/docker.sock"
        if base_url is None:
            # Fallback commonly used in CI with DinD service
            base_url = "tcp://127.0.0.1:2375"
        client = docker.DockerClient(base_url=base_url)  # type: ignore
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _sdk_daemon_available(), reason="No reachable Docker daemon for SDK engine")
async def test_docker_deployment_sdk_engine_no_cli():
    """Validate SDK mode works without requiring docker CLI in the SWE‑ReX container."""
    port = find_free_port()
    d = DockerDeployment(image="python:3.11", port=port, engine="sdk")
    with pytest.raises(RuntimeError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()


@pytest.mark.slow
@pytest.mark.skipif(not _sdk_daemon_available(), reason="No reachable Docker daemon for SDK engine")
async def test_docker_deployment_sdk_with_python_standalone():
    """Validate SDK mode can build the dynamic Dockerfile when python_standalone_dir is set."""
    port = find_free_port()
    d = DockerDeployment(image="ubuntu:latest", port=port, python_standalone_dir="/root", engine="sdk")
    with pytest.raises(RuntimeError):
        await d.is_alive()
    await d.start()
    assert await d.is_alive()
    await d.stop()
