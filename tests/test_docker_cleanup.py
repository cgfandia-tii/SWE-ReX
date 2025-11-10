"""Tests for DockerDeployment.cleanup_container() functionality."""

import os
from pathlib import Path

import pytest

from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.docker import DockerDeployment
from swerex.utils.free_port import find_free_port


async def test_cleanup_nonexistent_container_cli():
    """Test cleanup returns False for nonexistent container (CLI)."""
    config = DockerDeploymentConfig(image="alpine", engine="cli")
    result = await DockerDeployment.cleanup_container(
        "nonexistent-container-xyz-12345",
        config=config,
        remove=True,
    )
    assert result is False


async def test_cleanup_nonexistent_container_sdk():
    """Test cleanup returns False for nonexistent container (SDK)."""
    pytest.importorskip("docker")
    config = DockerDeploymentConfig(image="alpine", engine="sdk")
    result = await DockerDeployment.cleanup_container(
        "nonexistent-container-xyz-12345",
        config=config,
        remove=True,
    )
    assert result is False


async def test_cleanup_without_config():
    """Test cleanup works without config (uses defaults)."""
    result = await DockerDeployment.cleanup_container("nonexistent-container-xyz-12345")
    assert result is False


async def test_cleanup_container_cli():
    """Test cleanup of running container via CLI."""
    port = find_free_port()

    # Start a container
    deployment = DockerDeployment(image="swe-rex-test:latest", port=port, engine="cli")
    await deployment.start()
    container_name = deployment.container_name
    assert container_name is not None

    # Verify it's alive
    assert await deployment.is_alive()

    # Close runtime connection but don't stop container
    if deployment._runtime:
        await deployment._runtime.close()
    deployment._runtime = None
    deployment._container_process = None  # Simulate cross-process scenario

    # Now cleanup using class method
    config = DockerDeploymentConfig(image="swe-rex-test:latest", engine="cli")
    result = await DockerDeployment.cleanup_container(
        container_name,
        config=config,
        timeout=5,
        remove=True,
    )
    assert result is True

    # Verify container is gone
    import subprocess

    result_check = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
    )
    assert result_check.returncode != 0  # Should fail because container is removed


@pytest.mark.slow
async def test_cleanup_container_sdk():
    """Test cleanup of running container via SDK."""
    pytest.importorskip("docker")

    port = find_free_port()

    # Start a container
    deployment = DockerDeployment(image="swe-rex-test:latest", port=port, engine="sdk")
    await deployment.start()
    container_name = deployment.container_name
    assert container_name is not None

    # Verify it's alive
    assert await deployment.is_alive()

    # Close runtime connection but don't stop container
    if deployment._runtime:
        await deployment._runtime.close()
    deployment._runtime = None
    deployment._sdk_container = None  # Simulate cross-process scenario

    # Now cleanup using class method
    config = DockerDeploymentConfig(image="swe-rex-test:latest", engine="sdk")
    result = await DockerDeployment.cleanup_container(
        container_name,
        config=config,
        timeout=5,
        remove=True,
    )
    assert result is True

    # Verify container is gone by trying to cleanup again
    result = await DockerDeployment.cleanup_container(
        container_name,
        config=config,
        timeout=5,
        remove=True,
    )
    assert result is False  # Should be False because already removed


async def test_cleanup_container_without_remove_cli():
    """Test cleanup without removal (stop only) via CLI."""
    port = find_free_port()

    # Start a container
    deployment = DockerDeployment(image="swe-rex-test:latest", port=port, engine="cli", remove_container=False)
    await deployment.start()
    container_name = deployment.container_name
    assert container_name is not None

    # Verify it's alive
    assert await deployment.is_alive()

    # Close runtime connection
    if deployment._runtime:
        await deployment._runtime.close()
    deployment._runtime = None
    deployment._container_process = None

    # Cleanup without removal
    config = DockerDeploymentConfig(image="swe-rex-test:latest", engine="cli")
    result = await DockerDeployment.cleanup_container(
        container_name,
        config=config,
        timeout=5,
        remove=False,  # Don't remove
    )
    assert result is True

    # Verify container still exists but is stopped
    import subprocess

    result_check = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
        capture_output=True,
        text=True,
    )
    assert result_check.returncode == 0  # Should succeed because container exists
    assert "exited" in result_check.stdout.lower() or "created" in result_check.stdout.lower()

    # Clean up the stopped container
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


async def test_cleanup_container_config_serialization():
    """Test that DockerDeploymentConfig can be serialized for cross-process use."""
    config = DockerDeploymentConfig(
        image="ubuntu:latest",
        docker_endpoint="tcp://docker-host:2375",
        docker_env={"DOCKER_TLS_VERIFY": "0"},
        container_runtime="docker",
        engine="cli",
    )

    # Serialize and deserialize
    json_str = config.model_dump_json()
    restored = DockerDeploymentConfig.model_validate_json(json_str)

    assert restored.image == config.image
    assert restored.docker_endpoint == config.docker_endpoint
    assert restored.docker_env == config.docker_env
    assert restored.container_runtime == config.container_runtime
    assert restored.engine == config.engine


def _sdk_daemon_available() -> bool:
    """Return True if a Docker daemon is reachable via SDK."""
    try:
        import docker  # type: ignore
    except Exception:
        return False
    try:
        base_url = os.environ.get("DOCKER_HOST")
        if base_url is None and Path("/var/run/docker.sock").exists():
            base_url = "unix:///var/run/docker.sock"
        if base_url is None:
            base_url = "tcp://127.0.0.1:2375"
        client = docker.DockerClient(base_url=base_url)  # type: ignore
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _sdk_daemon_available(), reason="No reachable Docker daemon for SDK engine")
async def test_cleanup_with_custom_docker_endpoint():
    """Test cleanup respects custom docker endpoint configuration."""
    pytest.importorskip("docker")

    # Use environment DOCKER_HOST if available
    docker_host = os.environ.get("DOCKER_HOST")
    if not docker_host and Path("/var/run/docker.sock").exists():
        docker_host = "unix:///var/run/docker.sock"

    if not docker_host:
        pytest.skip("No DOCKER_HOST configured for testing")

    config = DockerDeploymentConfig(
        image="alpine",
        docker_endpoint=docker_host,
        engine="sdk",
    )

    # Should not raise even if container doesn't exist
    result = await DockerDeployment.cleanup_container(
        "nonexistent-container-xyz-12345",
        config=config,
    )
    assert result is False


async def test_cleanup_container_podman():
    """Test cleanup with Podman container runtime."""
    import subprocess

    # Check if podman is available
    try:
        subprocess.run(["podman", "version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Podman not available")

    config = DockerDeploymentConfig(
        image="alpine",
        container_runtime="podman",
        engine="cli",
    )

    # Should not raise even if container doesn't exist
    result = await DockerDeployment.cleanup_container(
        "nonexistent-container-xyz-12345",
        config=config,
    )
    assert result is False


async def test_cleanup_container_with_custom_logger():
    """Test cleanup with custom logger."""
    import logging

    custom_logger = logging.getLogger("test_cleanup")
    custom_logger.setLevel(logging.DEBUG)

    config = DockerDeploymentConfig(image="alpine", engine="cli")
    result = await DockerDeployment.cleanup_container(
        "nonexistent-container-xyz-12345",
        config=config,
        logger=custom_logger,
    )
    assert result is False


async def test_cleanup_container_error_handling_sdk():
    """Test cleanup error handling when SDK client initialization fails."""
    pytest.importorskip("docker")

    # Create config with invalid endpoint
    config = DockerDeploymentConfig(
        image="alpine",
        docker_endpoint="tcp://nonexistent-host-xyz:9999",
        engine="sdk",
    )

    # Should raise RuntimeError due to connection failure
    with pytest.raises(RuntimeError, match="Failed to connect to Docker daemon"):
        await DockerDeployment.cleanup_container(
            "some-container",
            config=config,
            timeout=1,
        )


async def test_cleanup_container_cli_not_found():
    """Test cleanup error handling when CLI is not found."""
    from unittest.mock import patch

    config = DockerDeploymentConfig(
        image="alpine",
        container_runtime="docker",
        engine="cli",
    )

    # Mock subprocess.run to raise FileNotFoundError
    with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
        # Should raise RuntimeError due to CLI not found
        with pytest.raises(RuntimeError, match="CLI not found in PATH"):
            await DockerDeployment.cleanup_container(
                "some-container",
                config=config,
            )
