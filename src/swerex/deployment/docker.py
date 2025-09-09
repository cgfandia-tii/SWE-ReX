import io
import json
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
from swerex.deployment.config import (
    DockerDeploymentConfig,
    Healthcheck,
    PortBinding,
    Resources,
    RestartPolicy,
)
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
        """Deployment to local or remote container image using Docker or Podman.

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
        """Returns a unique or configured container name."""
        if self._config.container_name:
            return self._config.container_name
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

    def _infer_runtime_host(self) -> str:
        """Infer the host that will be used to reach the container service."""
        if isinstance(self._config.runtime_host, str) and self._config.runtime_host != "auto":
            return self._config.runtime_host

        # SDK inference from docker_endpoint/env
        if self._engine == "sdk":
            base_url = self._config.docker_endpoint or os.environ.get("DOCKER_HOST") or ""
            if base_url.startswith("tcp://"):
                # tcp://host:port
                host = base_url[len("tcp://") :].split("/", 1)[0]
                host = host.split(":", 1)[0]
                return f"http://{host}"
            if base_url.startswith("ssh://"):
                # ssh://user@host
                host = base_url[len("ssh://") :].split("@")[-1]
                host = host.split("/", 1)[0]
                return f"http://{host}"
            # unix socket or unknown
            return "http://127.0.0.1"

        # CLI engine inference from DOCKER_HOST
        dh = self._get_cli_env().get("DOCKER_HOST", "")
        if dh.startswith("tcp://"):
            host = dh[len("tcp://") :].split("/", 1)[0]
            host = host.split(":", 1)[0]
            return f"http://{host}"
        if dh.startswith("ssh://"):
            host = dh[len("ssh://") :].split("@")[-1]
            host = host.split("/", 1)[0]
            return f"http://{host}"
        return "http://127.0.0.1"

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

    def _default_ports(self) -> list[PortBinding]:
        """Return default port bindings honoring legacy 'port' field."""
        # If explicit ports are provided, use them as-is
        if self._config.ports:
            return self._config.ports

        # Legacy behavior: single mapping from host port -> container runtime port (8000 default)
        container_port = self._config.runtime_container_port
        if self._config.port is None:
            # Preserve previous behavior of pre-selecting a host port when no explicit binding is provided
            self._config.port = find_free_port()
        return [PortBinding(container_port=container_port, host_port=self._config.port, protocol="tcp")]

    def _format_restart_policy_cli(self, rp: RestartPolicy | None) -> list[str]:
        if not rp or rp.name == "no":
            return []
        if rp.name == "on-failure" and rp.maximum_retry_count is not None:
            return ["--restart", f"{rp.name}:{rp.maximum_retry_count}"]
        return ["--restart", rp.name]

    def _format_healthcheck_cli(self, hc: Healthcheck | None) -> list[str]:
        if not hc:
            return []
        parts: list[str] = []
        if isinstance(hc.test, str) and hc.test == "NONE":
            return ["--no-healthcheck"]
        if isinstance(hc.test, list) and hc.test:
            # Use the command part if provided, otherwise join list
            cmd = " ".join(shlex.quote(s) for s in hc.test if s not in {"CMD", "CMD-SHELL"})
            parts += ["--health-cmd", cmd] if cmd else []
        if hc.interval_s is not None:
            parts += ["--health-interval", f"{hc.interval_s}s"]
        if hc.timeout_s is not None:
            parts += ["--health-timeout", f"{hc.timeout_s}s"]
        if hc.retries is not None:
            parts += ["--health-retries", str(hc.retries)]
        if hc.start_period_s is not None:
            parts += ["--health-start-period", f"{hc.start_period_s}s"]
        return parts

    def _healthcheck_sdk(self, hc: Healthcheck | None) -> dict[str, Any] | None:
        if not hc:
            return None
        if isinstance(hc.test, str) and hc.test == "NONE":
            return {"test": ["NONE"]}
        test = hc.test if isinstance(hc.test, list) else []

        def ns(x: float | None) -> int | None:
            return int(x * 1e9) if x is not None else None

        out: dict[str, Any] = {"test": test}
        if hc.interval_s is not None:
            out["interval"] = ns(hc.interval_s)
        if hc.timeout_s is not None:
            out["timeout"] = ns(hc.timeout_s)
        if hc.retries is not None:
            out["retries"] = hc.retries
        if hc.start_period_s is not None:
            out["start_period"] = ns(hc.start_period_s)
        return out

    def _resources_cli(self, res: Resources | None) -> list[str]:
        if not res:
            return []
        args: list[str] = []
        if res.cpus is not None:
            args += ["--cpus", str(res.cpus)]
        if res.memory is not None:
            args += ["--memory", str(res.memory)]
        if res.shm_size is not None:
            args += ["--shm-size", str(res.shm_size)]
        if res.ulimits:
            for k, v in res.ulimits.items():
                args += ["--ulimit", f"{k}={v}"]
        return args

    def _resources_sdk(self, res: Resources | None) -> dict[str, Any]:
        if not res:
            return {}
        out: dict[str, Any] = {}
        if res.cpus is not None:
            out["nano_cpus"] = int(res.cpus * 1_000_000_000)
        if res.memory is not None:
            out["mem_limit"] = res.memory
        if res.shm_size is not None:
            out["shm_size"] = res.shm_size
        if res.ulimits:
            # docker SDK expects list of ulimit specs; accept simple dict of strings "soft:hard"
            ulimits = []
            try:
                from docker.types import Ulimit  # type: ignore
            except Exception:  # pragma: no cover
                Ulimit = None  # type: ignore
            for name, spec in res.ulimits.items():
                soft, _, hard = str(spec).partition(":")
                try:
                    if Ulimit:
                        ulimits.append(Ulimit(name=name, soft=int(soft), hard=int(hard or soft)))  # type: ignore
                except Exception:
                    pass
            if ulimits:
                out["ulimits"] = ulimits
        return out

    def _discover_runtime_port_cli(self, container_port: int, proto: str = "tcp") -> int | None:
        """Inspect container and return published host port for given container port, or None."""
        try:
            assert self._container_name is not None
            out = subprocess.check_output(
                [self._config.container_runtime, "inspect", cast(str, self._container_name)],
                env=self._get_cli_env(),
            )
            j = json.loads(out)[0]
            ports = j.get("NetworkSettings", {}).get("Ports", {}) or {}
            lst = ports.get(f"{container_port}/{proto}", [])
            if lst:
                # pick first published
                hp = lst[0].get("HostPort")
                if hp:
                    return int(hp)
            # If network_mode=host, port is directly the container port
            net_mode = j.get("HostConfig", {}).get("NetworkMode")
            if net_mode == "host":
                return container_port
        except Exception:
            pass
        return None

    def _discover_runtime_port_sdk(self, container_port: int, proto: str = "tcp") -> int | None:
        if not self._sdk_container:
            return None
        try:
            self._sdk_container.reload()
            attrs = getattr(self._sdk_container, "attrs", {}) or {}
            ports = attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
            lst = ports.get(f"{container_port}/{proto}", [])
            if lst:
                hp = lst[0].get("HostPort")
                if hp:
                    return int(hp)
            net_mode = attrs.get("HostConfig", {}).get("NetworkMode")
            if net_mode == "host":
                return container_port
        except Exception:
            pass
        return None

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

        assert self._container_name is None
        self._container_name = self._get_container_name()
        token = self._get_token()

        # Resolve port bindings
        port_bindings: list[PortBinding] = self._default_ports()
        # Find runtime binding (by container port/proto)
        runtime_binding = next(
            (
                pb
                for pb in port_bindings
                if pb.container_port == self._config.runtime_container_port and pb.protocol == "tcp"
            ),
            None,
        )

        # Build command based on command_mode
        default_cmd = self._get_swerex_start_cmd(token)
        if self._config.command_mode == "append" and self._config.cmd:
            # Append user command to our shell '-c' string
            appended = " ".join(shlex.quote(s) for s in self._config.cmd)
            default_cmd = [default_cmd[0], default_cmd[1], f"{default_cmd[2]} && {appended}"]
        elif self._config.command_mode == "override":
            # Use only provided cmd (if any), otherwise no explicit command (image defaults)
            if self._config.cmd:
                default_cmd = self._config.cmd
            else:
                default_cmd = []

        # Decide access mode and whether to publish ports
        access_mode = self._config.runtime_access_mode
        if access_mode == "auto":
            if (not self._config.ports) and self._config.network_mode != "host" and self._config.networks:
                access_mode = "network"
            else:
                access_mode = "host"
        publish_ports = access_mode != "network" and self._config.network_mode != "host"

        if self._engine == "cli":
            platform_arg = []
            if self._config.platform is not None:
                platform_arg = ["--platform", self._config.platform]
            rm_arg = []
            if self._config.remove_container:
                rm_arg = ["--rm"]

            # Build -p flags (skip when network_mode=host)
            port_args: list[str] = []
            if publish_ports:
                for pb in port_bindings:
                    # Format: ip:hostPort:containerPort[/proto]; for ephemeral hostPort use ip::containerPort
                    ip_prefix = f"{pb.host_ip}:" if pb.host_ip else ""
                    if pb.host_port is None:
                        if pb.host_ip:
                            mapping = f"{pb.host_ip}::{pb.container_port}"
                        else:
                            mapping = f"{pb.container_port}"
                    else:
                        mapping = f"{ip_prefix}{pb.host_port}:{pb.container_port}"
                    if pb.protocol and pb.protocol != "tcp":
                        mapping = f"{mapping}/{pb.protocol}"
                    port_args += ["-p", mapping]

            # Env, volumes, labels, user, workdir, entrypoint, restart, health, resources
            env_args = [x for kv in self._config.env.items() for x in ("-e", f"{kv[0]}={kv[1]}")]
            vol_args: list[str] = []
            for vm in self._config.volumes:
                suffix = f":{vm.mode}" if vm.mode else ""
                vol_args += ["-v", f"{vm.source}:{vm.target}{suffix}"]
            label_args = [x for kv in self._config.labels.items() for x in ("--label", f"{kv[0]}={kv[1]}")]
            user_args = ["--user", str(self._config.user)] if self._config.user is not None else []
            workdir_args = ["--workdir", self._config.workdir] if self._config.workdir else []
            entrypoint_args = ["--entrypoint", " ".join(self._config.entrypoint)] if self._config.entrypoint else []
            restart_args = self._format_restart_policy_cli(self._config.restart_policy)
            health_args = self._format_healthcheck_cli(self._config.healthcheck)
            resource_args = self._resources_cli(self._config.resources)

            network_args: list[str] = []
            if self._config.network_mode:
                network_args = ["--network", self._config.network_mode]
            elif self._config.networks:
                # CLI supports only one at run time; others require post-run attach
                network_args = ["--network", self._config.networks[0]]

            alias_args: list[str] = []
            if self._config.networks:
                for a in self._config.network_aliases.get(self._config.networks[0], []):
                    alias_args += ["--network-alias", a]

            cmds = [
                self._config.container_runtime,
                "run",
                *rm_arg,
                *platform_arg,
                *port_args,
                *env_args,
                *vol_args,
                *label_args,
                *user_args,
                *workdir_args,
                *entrypoint_args,
                *restart_args,
                *health_args,
                *resource_args,
                *network_args,
                *alias_args,
                *self._config.docker_args,  # keep user overrides last
                "--name",
                self._container_name,
                image_ref,
                *default_cmd,
            ]
            cmd_str = shlex.join(cmds)
            self.logger.info(f"Starting container {self._container_name} with image {self._config.image}")
            self.logger.debug(f"Command: {cmd_str!r}")
            self._container_process = subprocess.Popen(
                cmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._get_cli_env()
            )
            # Connect to additional networks with aliases if requested (CLI)
            if not self._config.network_mode and len(self._config.networks) > 1:
                for net in self._config.networks[1:]:
                    alias_flags: list[str] = []
                    for a in self._config.network_aliases.get(net, []):
                        alias_flags += ["--alias", a]
                    try:
                        subprocess.check_call(
                            [
                                self._config.container_runtime,
                                "network",
                                "connect",
                                *alias_flags,
                                net,
                                self._container_name,
                            ],  # type: ignore[arg-type]
                            env=self._get_cli_env(),
                        )
                    except Exception:
                        self.logger.warning("Failed to connect container to network %s (CLI)", net, exc_info=False)
        else:
            # SDK engine
            client = self._ensure_sdk_client()
            assert client is not None
            self.logger.info(f"(SDK) Starting container {self._container_name} with image {image_ref}")
            # Build ports mapping
            ports_map: dict[str, Any] = {}
            publish_all = False
            if publish_ports:
                for pb in port_bindings:
                    key = f"{pb.container_port}/{pb.protocol or 'tcp'}"
                    if pb.host_port is None:
                        # Request ephemeral port; publish_all_ports will ensure binding
                        publish_all = True
                        ports_map[key] = None
                    else:
                        if pb.host_ip:
                            ports_map[key] = (pb.host_ip, pb.host_port)
                        else:
                            ports_map[key] = pb.host_port

            # Volumes mapping
            volumes_map: dict[str, dict[str, str]] = {}
            for vm in self._config.volumes:
                volumes_map[vm.source] = {"bind": vm.target, "mode": vm.mode or "rw"}

            run_kwargs: dict[str, Any] = {
                "name": self._container_name,
                "detach": True,
                "auto_remove": self._config.remove_container,
                "environment": self._config.env or None,
                "volumes": volumes_map or None,
                "labels": self._config.labels or None,
                "ports": ports_map or None,
                "publish_all_ports": publish_all,
                "command": default_cmd or None,
                "user": str(self._config.user) if self._config.user is not None else None,
                "working_dir": self._config.workdir or None,
                "entrypoint": self._config.entrypoint or None,
                "platform": self._config.platform or None,
                "restart_policy": (
                    {
                        "Name": self._config.restart_policy.name,
                        "MaximumRetryCount": self._config.restart_policy.maximum_retry_count,
                    }
                    if self._config.restart_policy and self._config.restart_policy.name != "no"
                    else None
                ),
                "healthcheck": self._healthcheck_sdk(self._config.healthcheck),
                "network_mode": self._config.network_mode or None,
                **self._resources_sdk(self._config.resources),
            }
            # Attach to first network by name (if provided and no explicit network_mode)
            if not self._config.network_mode and self._config.networks:
                run_kwargs["network"] = self._config.networks[0]

            try:
                self._sdk_container = cast("_Container", client.containers.run(image_ref, **run_kwargs))
                # Connect to networks and set aliases where requested (SDK)
                if not self._config.network_mode and self._config.networks:
                    for net in self._config.networks:
                        try:
                            client.networks.get(net).connect(
                                self._sdk_container, aliases=self._config.network_aliases.get(net)
                            )
                        except Exception:
                            # Already connected or alias update unsupported; ignore
                            pass
            except Exception as e:  # pragma: no cover - depends on env
                msg = f"Failed to start SDK container: {e}"
                raise RuntimeError(msg) from e

        # Determine final runtime host and port
        if access_mode == "network":
            # Build a network-based endpoint: scheme://dns_name:container_port
            # Prefer explicit runtime_network_host, else first alias of primary network, else container name
            dns_host = self._config.runtime_network_host
            if not dns_host:
                if self._config.networks:
                    aliases = self._config.network_aliases.get(self._config.networks[0], [])
                    if aliases:
                        dns_host = aliases[0]
            if not dns_host:
                dns_host = self._container_name
            runtime_host = f"{self._config.runtime_host_scheme}://{dns_host}"
            runtime_port = self._config.runtime_container_port
        else:
            runtime_host = self._infer_runtime_host()
            if runtime_binding and runtime_binding.host_port is not None:
                runtime_port = runtime_binding.host_port
            else:
                # Need to discover via inspect (or host network)
                if self._engine == "cli":
                    runtime_port = self._discover_runtime_port_cli(self._config.runtime_container_port)
                else:
                    runtime_port = self._discover_runtime_port_sdk(self._config.runtime_container_port)
                if runtime_port is None:
                    # Fallback to configured 'port' if set, else container port (host network)
                    runtime_port = self._config.port or self._config.runtime_container_port

        # Start runtime after we know the connection details
        self._hooks.on_custom_step("Starting runtime")
        self.logger.info(f"Starting runtime at {runtime_host}:{runtime_port}")
        self._runtime = RemoteRuntime.from_config(
            RemoteRuntimeConfig(host=runtime_host, port=runtime_port, timeout=self._runtime_timeout, auth_token=token)
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
