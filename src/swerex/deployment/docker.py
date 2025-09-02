import io
import logging
import os
import shlex
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import Self

from swerex import PACKAGE_NAME, REMOTE_EXECUTABLE_NAME
from swerex.deployment.abstract import AbstractDeployment
from swerex.deployment.config import DockerDeploymentConfig
from swerex.deployment.hooks.abstract import CombinedDeploymentHook, DeploymentHook
from swerex.exceptions import DeploymentNotStartedError, DockerPullError
from swerex.runtime.abstract import IsAliveResponse
from swerex.runtime.config import RemoteRuntimeConfig
from swerex.runtime.remote import RemoteRuntime
from swerex.utils.free_port import find_free_port
from swerex.utils.log import get_logger
from swerex.utils.wait import _wait_until_alive

__all__ = ["DockerDeployment", "DockerDeploymentConfig"]

# Optional Docker SDK import (for SDK engine)
try:
    import docker  # type: ignore
    from docker.errors import DockerException  # type: ignore
except Exception:  # pragma: no cover - environment without docker-py
    docker = None
    DockerException = Exception  # type: ignore

# Precise types for SDK client/container without requiring docker at runtime
if TYPE_CHECKING:
    from docker import DockerClient as _DockerClient  # type: ignore
    from docker.models.containers import Container as _Container  # type: ignore
else:

    class _DockerClient:  # type: ignore[no-redef]
        ...

    class _Container:  # type: ignore[no-redef]
        ...


def _is_image_available(image: str, runtime: str = "docker", env: dict[str, str] | None = None) -> bool:
    try:
        subprocess.check_call(
            [runtime, "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _pull_image(image: str, runtime: str = "docker", env: dict[str, str] | None = None) -> bytes:
    try:
        return subprocess.check_output([runtime, "pull", image], stderr=subprocess.PIPE, env=env)
    except subprocess.CalledProcessError as e:
        # e.stderr contains the error message as bytes
        raise subprocess.CalledProcessError(e.returncode, e.cmd, e.output, e.stderr) from None


def _remove_image(image: str, runtime: str = "docker", env: dict[str, str] | None = None) -> bytes:
    return subprocess.check_output([runtime, "rmi", image], timeout=30, env=env)


class DockerDeployment(AbstractDeployment):
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        **kwargs: Any,
    ):
        """Deployment to local container image using Docker or Podman.

        Supports two engines:
        - CLI: subprocess calls to docker/podman CLIs (backward compatible default)
        - SDK: Python Docker SDK talking to an external daemon (no docker CLI needed in this container)
        """
        self._config = DockerDeploymentConfig(**kwargs)
        self._runtime: RemoteRuntime | None = None

        # CLI engine state
        self._container_process: subprocess.Popen | None = None

        # SDK engine state
        self._sdk_client: _DockerClient | None = None
        self._sdk_container: _Container | None = None

        self._container_name: str | None = None
        self.logger = logger or get_logger("rex-deploy")
        self._runtime_timeout = 0.15
        self._hooks = CombinedDeploymentHook()

        self._engine = self._resolve_engine()

    def add_hook(self, hook: DeploymentHook):
        self._hooks.add_hook(hook)

    @classmethod
    def from_config(cls, config: DockerDeploymentConfig) -> Self:
        return cls(**config.model_dump())

    def _resolve_engine(self) -> str:
        """Return either 'cli' or 'sdk' based on config and environment."""
        engine = self._config.engine
        if engine == "cli":
            return "cli"
        if engine == "sdk":
            if docker is None:
                msg = (
                    "Docker SDK engine requested but python 'docker' package is not installed.\n"
                    "Install with: pip install 'docker' or pip install 'swe-rex[docker]'"
                )
                raise RuntimeError(msg)
            if self._config.container_runtime != "docker":
                # SDK targets the Docker Engine API; for podman keep CLI path
                msg = "Docker SDK engine requires container_runtime='docker'."
                raise RuntimeError(msg)
            return "sdk"
        # auto
        if self._config.container_runtime != "docker":
            return "cli"
        return "sdk" if docker is not None else "cli"

    def _get_container_name(self) -> str:
        """Returns a unique container name based on the image name."""
        image_name_sanitized = "".join(c for c in self._config.image if c.isalnum() or c in "-_.")
        return f"{image_name_sanitized}-{uuid.uuid4()}"

    @property
    def container_name(self) -> str | None:
        return self._container_name

    def _get_cli_env(self) -> dict[str, str]:
        """Environment for docker/podman CLI subprocesses."""
        env = os.environ.copy()
        # allow overriding/adding env for CLI usage (e.g., DOCKER_HOST)
        env.update(self._config.docker_env)
        return env

    def _ensure_sdk_client(self) -> _DockerClient | None:
        if self._engine != "sdk":
            return None
        if self._sdk_client is not None:
            return self._sdk_client

        base_url = self._config.docker_endpoint
        # If not explicitly set, auto-detect from env or default socket
        if base_url is None:
            # Respect DOCKER_HOST if present
            base_url = os.environ.get("DOCKER_HOST")
        if base_url is None:
            # Default to unix socket if present
            if Path("/var/run/docker.sock").exists():
                base_url = "unix:///var/run/docker.sock"
            else:
                base_url = "unix:///var/run/docker.sock"  # default; will fail if not present/reachable

        tls_config = None
        if self._config.docker_tls:
            # docker SDK will pick up certs from path if provided; otherwise env
            cert_path = self._config.docker_cert_path or os.environ.get("DOCKER_CERT_PATH")
            # docker.tls.TLSConfig is optional; do not import directly to avoid type errors if docker is None
            tls_config = docker.tls.TLSConfig(  # type: ignore[attr-defined]
                client_cert=(str(Path(cert_path) / "cert.pem"), str(Path(cert_path) / "key.pem"))
                if cert_path
                else None,
                ca_cert=str(Path(cert_path) / "ca.pem") if cert_path else None,
                verify=True,
            )

        params: dict[str, Any] = {"base_url": base_url}
        if tls_config is not None:
            params["tls"] = tls_config
        if base_url.startswith("ssh://"):
            # Use local ssh client, if requested
            if getattr(self._config, "docker_use_ssh", False):
                params["use_ssh_client"] = True

        try:
            self._sdk_client = cast("_DockerClient", docker.DockerClient(**params))  # type: ignore[attr-defined]
            # preflight ping
            self._sdk_client.ping()
        except Exception as e:  # pragma: no cover - depends on env
            msg = "Failed to connect to Docker daemon via SDK."
            msg += f" base_url={base_url!r}."
            if isinstance(e, DockerException):
                msg += f" Docker error: {e}."
            else:
                msg += f" Error: {e}."
            msg += (
                "\nEnsure one of the following:\n"
                "- Mount /var/run/docker.sock and use unix:///var/run/docker.sock\n"
                "- Set docker_endpoint to a reachable tcp://host:2375 (with TLS as needed)\n"
                "- Or run a docker:dind sibling and set docker_endpoint to tcp://dind:2375"
            )
            raise RuntimeError(msg) from e
        return self._sdk_client

    async def is_alive(self, *, timeout: float | None = None) -> IsAliveResponse:
        """Checks if the runtime is alive. The return value can be
        tested with bool().

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            msg = "Runtime not started"
            raise RuntimeError(msg)

        if self._engine == "cli":
            if self._container_process is None:
                msg = "Container process not started"
                raise RuntimeError(msg)
            if self._container_process.poll() is not None:
                msg = "Container process terminated."
                output = "stdout:\n" + self._container_process.stdout.read().decode()  # type: ignore
                output += "\nstderr:\n" + self._container_process.stderr.read().decode()  # type: ignore
                msg += "\n" + output
                raise RuntimeError(msg)
            return await self._runtime.is_alive(timeout=timeout)

        # SDK engine
        if self._sdk_container is None:
            msg = "SDK container not started"
            raise RuntimeError(msg)
        try:
            self._sdk_container.reload()
            status = getattr(self._sdk_container, "status", "")
            if status in {"exited", "dead"}:
                logs = ""
                try:
                    logs = self._sdk_container.logs(tail=2000).decode("utf-8", errors="replace")
                except Exception:
                    pass
                msg = f"Container terminated (status={status}). Logs:\n{logs}"
                raise RuntimeError(msg)
        except Exception as e:  # pragma: no cover
            msg = f"Failed to check SDK container state: {e}"
            raise RuntimeError(msg) from e
        return await self._runtime.is_alive(timeout=timeout)

    async def _wait_until_alive(self, timeout: float = 10.0):
        try:
            return await _wait_until_alive(self.is_alive, timeout=timeout, function_timeout=self._runtime_timeout)
        except TimeoutError as e:
            self.logger.error("Runtime did not start within timeout. Here's the output from the container.")
            if self._engine == "cli":
                try:
                    self.logger.error(self._container_process.stdout.read().decode())  # type: ignore
                    self.logger.error(self._container_process.stderr.read().decode())  # type: ignore
                except Exception:
                    pass
            else:
                try:
                    assert self._sdk_container is not None
                    logs = self._sdk_container.logs(tail=2000).decode("utf-8", errors="replace")
                    self.logger.error(logs)
                except Exception:
                    pass
            assert self._container_name is not None
            await self.stop()
            raise e

    def _get_token(self) -> str:
        return str(uuid.uuid4())

    def _get_swerex_start_cmd(self, token: str) -> list[str]:
        rex_args = f"--auth-token {token}"
        pipx_install = "python3 -m pip install pipx && python3 -m pipx ensurepath"
        if self._config.python_standalone_dir:
            cmd = f"{self._config.python_standalone_dir}/python3.11/bin/{REMOTE_EXECUTABLE_NAME} {rex_args}"
        else:
            cmd = f"{REMOTE_EXECUTABLE_NAME} {rex_args} || ({pipx_install} && pipx run {PACKAGE_NAME} {rex_args})"
        # Need to wrap with /bin/sh -c to avoid having '&&' interpreted by the parent shell
        return [
            "/bin/sh",
            "-c",
            cmd,
        ]

    def _pull_image_cli(self) -> None:
        if self._config.pull == "never":
            return
        if self._config.pull == "missing" and _is_image_available(
            self._config.image, self._config.container_runtime, env=self._get_cli_env()
        ):
            return
        self.logger.info(f"Pulling image {self._config.image!r}")
        self._hooks.on_custom_step("Pulling container image")
        try:
            _pull_image(self._config.image, self._config.container_runtime, env=self._get_cli_env())
        except subprocess.CalledProcessError as e:
            msg = f"Failed to pull image {self._config.image}. "
            msg += f"Error: {e.stderr.decode()}"
            msg += f"Output: {e.output.decode()}"
            raise DockerPullError(msg) from e

    def _pull_image_sdk(self) -> None:
        if self._config.pull == "never":
            return
        client = self._ensure_sdk_client()
        assert client is not None
        try:
            # images.pull returns an Image object; for "missing", check presence first
            if self._config.pull == "missing":
                try:
                    client.images.get(self._config.image)
                    return
                except Exception:
                    pass
            self.logger.info(f"Pulling image {self._config.image!r} via SDK")
            self._hooks.on_custom_step("Pulling container image")
            client.images.pull(self._config.image)
        except Exception as e:  # pragma: no cover - network dependent
            msg = f"Failed to pull image {self._config.image}: {e}"
            raise DockerPullError(msg) from e

    @property
    def glibc_dockerfile(self) -> str:
        # will only work with glibc-based systems
        if self._config.platform:
            platform_arg = f"--platform={self._config.platform}"
        else:
            platform_arg = ""
        return (
            "ARG BASE_IMAGE\n\n"
            # Build stage for standalone Python
            f"FROM {platform_arg} python:3.11.9-slim-bookworm AS builder\n"
            # Install build dependencies
            "RUN apt-get update && apt-get install -y \\\n"
            "    wget \\\n"
            "    gcc \\\n"
            "    make \\\n"
            "    zlib1g-dev \\\n"
            "    libssl-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            # Download and compile Python as standalone
            "WORKDIR /build\n"
            "RUN wget https://www.python.org/ftp/python/3.11.8/Python-3.11.8.tgz \\\n"
            "    && tar xzf Python-3.11.8.tgz\n"
            "WORKDIR /build/Python-3.11.8\n"
            "RUN ./configure \\\n"
            "    --prefix=/root/python3.11 \\\n"
            "    --enable-shared \\\n"
            "    LDFLAGS='-Wl,-rpath=/root/python3.11/lib' && \\\n"
            "    make -j$(nproc) && \\\n"
            "    make install && \\\n"
            "    ldconfig\n\n"
            # Production stage
            f"FROM {platform_arg} $BASE_IMAGE\n"
            # Ensure we have the required runtime libraries
            "RUN apt-get update && apt-get install -y \\\n"
            "    libc6 \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            # Copy the standalone Python installation
            f"COPY --from=builder /root/python3.11 {self._config.python_standalone_dir}/python3.11\n"
            f"ENV LD_LIBRARY_PATH={self._config.python_standalone_dir}/python3.11/lib:${{LD_LIBRARY_PATH:-}}\n"
            # Verify installation
            f"RUN {self._config.python_standalone_dir}/python3.11/bin/python3 --version\n"
            # Install swe-rex using the standalone Python
            f"RUN /root/python3.11/bin/pip3 install --no-cache-dir {PACKAGE_NAME}\n\n"
            f"RUN ln -s /root/python3.11/bin/{REMOTE_EXECUTABLE_NAME} /usr/local/bin/{REMOTE_EXECUTABLE_NAME}\n\n"
            f"RUN {REMOTE_EXECUTABLE_NAME} --version\n"
        )

    def _build_image_cli(self) -> str:
        """Builds image with CLI, returns image ID."""
        self.logger.info(
            f"Building image {self._config.image} to install a standalone python to {self._config.python_standalone_dir}. "
            "This might take a while (but you only have to do it once). To skip this step, set `python_standalone_dir` to None."
        )
        dockerfile = self.glibc_dockerfile
        platform_arg = []
        if self._config.platform:
            platform_arg = ["--platform", self._config.platform]
        build_cmd = [
            self._config.container_runtime,
            "build",
            "-q",
            *platform_arg,
            "--build-arg",
            f"BASE_IMAGE={self._config.image}",
            "-",
        ]
        image_id = (
            subprocess.check_output(
                build_cmd,
                input=dockerfile.encode(),
                env=self._get_cli_env(),
            )
            .decode()
            .strip()
        )
        if not image_id.startswith("sha256:"):
            msg = f"Failed to build image. Image ID is not a SHA256: {image_id}"
            raise RuntimeError(msg)
        return image_id

    def _build_image_sdk(self) -> str:
        """Builds image with SDK. Returns a tag to use for running."""
        assert self._engine == "sdk"
        client = self._ensure_sdk_client()
        assert client is not None

        # Create an in-memory tar with only the Dockerfile
        dockerfile_content = self.glibc_dockerfile.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("Dockerfile")
            info.size = len(dockerfile_content)
            tar.addfile(info, io.BytesIO(dockerfile_content))
        buf.seek(0)

        tag = f"swe-rex-standalone:{uuid.uuid4()}"
        self.logger.info(
            f"(SDK) Building image {tag} from base {self._config.image} with standalone python at {self._config.python_standalone_dir}"
        )
        self._hooks.on_custom_step("Building container image")
        # Use low-level API to pass the tar stream
        try:
            build_kwargs: dict[str, Any] = {
                "fileobj": buf,
                "custom_context": True,
                "tag": tag,
                "buildargs": {"BASE_IMAGE": self._config.image},
                "decode": True,
                "rm": True,
            }
            if self._config.platform:
                build_kwargs["platform"] = self._config.platform
            # Pull strategy
            if self._config.pull == "always":
                build_kwargs["pull"] = True
            elif self._config.pull == "never":
                build_kwargs["pull"] = False

            # Stream logs for visibility
            for chunk in client.api.build(**build_kwargs):
                # Typical keys: 'stream', 'aux', 'error', 'status'
                if chunk.get("stream"):
                    s = str(chunk["stream"]).strip()
                    if s:
                        self.logger.debug(f"[build] {s}")
                if chunk.get("error"):
                    msg = f"Build error: {chunk['error']}"
                    raise RuntimeError(msg)
                # No need to parse 'aux' if using a tag
            # Ensure the image exists and return the tag
            client.images.get(tag)
            return tag
        except Exception as e:  # pragma: no cover - depends on docker daemon
            msg = f"SDK build failed: {e}"
            raise RuntimeError(msg) from e

    async def start(self):
        """Starts the runtime."""
        # Pull as needed
        if self._engine == "cli":
            self._pull_image_cli()
        else:
            self._pull_image_sdk()

        # Build if requested (standalone python)
        if self._config.python_standalone_dir:
            if self._engine == "cli":
                image_ref = self._build_image_cli()
            else:
                image_ref = self._build_image_sdk()
        else:
            image_ref = self._config.image

        # Resolve port
        if self._config.port is None:
            self._config.port = find_free_port()

        assert self._container_name is None
        self._container_name = self._get_container_name()
        token = self._get_token()

        if self._engine == "cli":
            platform_arg = []
            if self._config.platform is not None:
                platform_arg = ["--platform", self._config.platform]
            rm_arg = []
            if self._config.remove_container:
                rm_arg = ["--rm"]
            cmds = [
                self._config.container_runtime,
                "run",
                *rm_arg,
                "-p",
                f"{self._config.port}:8000",
                *platform_arg,
                *self._config.docker_args,
                "--name",
                self._container_name,
                image_ref,
                *self._get_swerex_start_cmd(token),
            ]
            cmd_str = shlex.join(cmds)
            self.logger.info(
                f"Starting container {self._container_name} with image {self._config.image} serving on port {self._config.port}"
            )
            self.logger.debug(f"Command: {cmd_str!r}")
            self._container_process = subprocess.Popen(
                cmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._get_cli_env()
            )
        else:
            # SDK engine
            client = self._ensure_sdk_client()
            assert client is not None
            self.logger.info(
                f"(SDK) Starting container {self._container_name} with image {image_ref} serving on port {self._config.port}"
            )
            ports = {"8000/tcp": self._config.port}
            run_kwargs: dict[str, Any] = {
                "name": self._container_name,
                "detach": True,
                "auto_remove": self._config.remove_container,
                "ports": ports,
                "command": self._get_swerex_start_cmd(token),
            }
            if self._config.platform:
                run_kwargs["platform"] = self._config.platform
            try:
                self._sdk_container = cast("_Container", client.containers.run(image_ref, **run_kwargs))
            except Exception as e:  # pragma: no cover - depends on env
                msg = f"Failed to start SDK container: {e}"
                raise RuntimeError(msg) from e

        self._hooks.on_custom_step("Starting runtime")
        self.logger.info(f"Starting runtime at {self._config.port}")
        self._runtime = RemoteRuntime.from_config(
            RemoteRuntimeConfig(port=self._config.port, timeout=self._runtime_timeout, auth_token=token)
        )
        t0 = time.time()
        await self._wait_until_alive(timeout=self._config.startup_timeout)
        self.logger.info(f"Runtime started in {time.time() - t0:.2f}s")

    async def stop(self):
        """Stops the runtime."""
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None

        if self._engine == "cli":
            if self._container_process is not None:
                try:
                    subprocess.check_call(
                        [self._config.container_runtime, "kill", self._container_name],  # type: ignore
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        env=self._get_cli_env(),
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                    self.logger.warning(
                        f"Failed to kill container {self._container_name}: {e}. Will try harder.",
                        exc_info=False,
                    )
                for _ in range(3):
                    try:
                        self._container_process.kill()
                    except Exception:
                        pass
                    try:
                        self._container_process.wait(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                else:
                    self.logger.warning(f"Failed to kill container {self._container_name} with SIGKILL")

                self._container_process = None
                self._container_name = None
        else:
            # SDK engine
            if self._sdk_container is not None:
                try:
                    # Try graceful stop first, then force kill
                    try:
                        self._sdk_container.stop(timeout=10)
                    except Exception:
                        self._sdk_container.kill()
                    # Remove container if not auto-removed
                    if not self._config.remove_container:
                        try:
                            self._sdk_container.remove(force=True)
                        except Exception:
                            pass
                except Exception as e:  # pragma: no cover - depends on env
                    self.logger.warning(f"Failed to stop SDK container: {e}", exc_info=False)
                finally:
                    self._sdk_container = None
                    self._container_name = None

        if self._config.remove_images:
            # Preserve existing behavior: remove the base image only
            if self._engine == "cli":
                if _is_image_available(self._config.image, self._config.container_runtime, env=self._get_cli_env()):
                    self.logger.info(f"Removing image {self._config.image}")
                    try:
                        _remove_image(self._config.image, self._config.container_runtime, env=self._get_cli_env())
                    except subprocess.CalledProcessError:
                        self.logger.error(f"Failed to remove image {self._config.image}", exc_info=True)
            else:
                client = self._ensure_sdk_client()
                if client is not None:
                    try:
                        client.images.remove(self._config.image)
                    except Exception:
                        self.logger.error(f"Failed to remove image {self._config.image}", exc_info=True)

    @property
    def runtime(self) -> RemoteRuntime:
        """Returns the runtime if running.

        Raises:
            DeploymentNotStartedError: If the deployment was not started.
        """
        if self._runtime is None:
            raise DeploymentNotStartedError()
        return self._runtime
