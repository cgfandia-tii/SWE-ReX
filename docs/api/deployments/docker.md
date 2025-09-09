::: swerex.deployment.docker.DockerDeployment

# Running inside containers without docker CLI or dockerd

SWE‑ReX supports interacting with Docker even when the SWE‑ReX process runs inside a container that does not include the docker CLI and does not run a Docker daemon. This is achieved via a Python Docker SDK backend that talks to an external Docker Engine API over a unix socket, TCP, or SSH.

Two operation modes are available:

- SDK engine (recommended for containerized environments):
  - No docker CLI required in the SWE‑ReX container.
  - Connects to an external Docker daemon (host socket, DinD TCP service, or remote host over SSH).
- CLI engine (backward compatible):
  - Uses the docker or podman CLI via subprocess.
  - Requires the CLI in the SWE‑ReX runtime environment.

## Configuration

Extend your `DockerDeploymentConfig` with engine and connection fields:

- engine: "auto" | "sdk" | "cli" (default: "auto")
  - "sdk": force using the Python Docker SDK
  - "cli": force using the docker/podman CLI
  - "auto": prefer "sdk" when docker SDK is installed and container_runtime == "docker", otherwise fallback to "cli"
- docker_endpoint: Optional[str]
  - Examples: unix:///var/run/docker.sock, tcp://127.0.0.1:2375, tcp://dind:2375, ssh://user@host
- docker_tls: bool (default False)
- docker_cert_path: Optional[str] (directory containing ca.pem, cert.pem, key.pem)
- docker_use_ssh: bool (default False) — set True to allow SDK to use your local SSH client for ssh:// endpoints
- docker_env: dict[str, str] (CLI mode only; e.g., {"DOCKER_HOST": "tcp://dind:2375"})
- container_runtime: "docker" | "podman" (CLI mode supports both; SDK engine requires "docker")

New flexible container options and remote runtime support:

- runtime_host: str | "auto" (default "auto")
  - Host used by the SWE‑ReX client to reach the running container.
  - "auto" infers from docker_endpoint/DOCKER_HOST:
    - unix:///… → http://127.0.0.1
    - tcp://host:port → http://host
    - ssh://user@host → http://host
- runtime_container_port: int (default 8000)
  - Internal container port where SWE‑ReX listens.
- ports: list[PortBinding]
  - Flexible publishing rules. If empty, legacy mapping is used from `port` (host) to `runtime_container_port`.
  - PortBinding fields: container_port, host_port (None for ephemeral), protocol ("tcp"/"udp"), host_ip (bind address).
- env: dict[str,str]
- volumes: list[VolumeMount]  (source, target, mode="ro"/"rw")
- labels: dict[str,str]
- networks: list[str] and/or network_mode: str | None (e.g., "host")
- restart_policy: RestartPolicy (name and optional maximum_retry_count)
- healthcheck: Healthcheck (test, interval/timeout/start_period in seconds, retries)
- resources: Resources (cpus, memory, shm_size, ulimits)
- workdir: str | None
- user: str | int | None
- entrypoint: list[str] | None
- cmd: list[str] | None
- command_mode: "append" | "override" (default "append")
  - "append": run SWE‑ReX start command and then your cmd.
  - "override": run only your entrypoint/cmd.

Example (SDK mode with host socket):
```python
from swerex.deployment.config import DockerDeploymentConfig
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="unix:///var/run/docker.sock",
)
```

Example (SDK mode with DinD service on TCP):
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="tcp://dind:2375",
    docker_tls=False,
)
```

Example (CLI mode using host docker CLI with socket passthrough):
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="cli",
    docker_env={"DOCKER_HOST": "unix:///var/run/docker.sock"},
)
```

### Flexible ports and remote inference

Ephemeral host port on any daemon, with automatic runtime host/port discovery:
```python
from swerex.deployment.config import DockerDeploymentConfig, PortBinding

cfg = DockerDeploymentConfig(
    image="python:3.11",
    engine="sdk",
    docker_endpoint="tcp://10.0.0.8:2375",
    # Let Docker assign an ephemeral host port, publish tcp 8000 internally
    ports=[PortBinding(container_port=8000, host_port=None, protocol="tcp")],
    runtime_container_port=8000,
    runtime_host="auto",  # inferred as http://10.0.0.8
)
```
SWE‑ReX inspects the container after start and connects the client to http://10.0.0.8:<assigned_port>.

Multiple port publishes:
```python
cfg.ports = [
  PortBinding(container_port=8000, host_port=18880),
  PortBinding(container_port=8080, host_port=18080),
]
```

Network mode host (no -p flags needed):
```python
cfg.network_mode = "host"
# Client will connect to runtime_host : runtime_container_port
```

### Common container options

Environment and volumes:
```python
from swerex.deployment.config import VolumeMount
cfg.env = {"ENV": "prod", "TZ": "UTC"}
cfg.volumes = [VolumeMount(source="/host/data", target="/data", mode="rw")]
```

Restart policy and healthcheck:
```python
from swerex.deployment.config import RestartPolicy, Healthcheck
cfg.restart_policy = RestartPolicy(name="on-failure", maximum_retry_count=3)
cfg.healthcheck = Healthcheck(
    test=["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/is_alive || exit 1"],
    interval_s=5.0,
    timeout_s=2.0,
    retries=5,
)
```

Resources and user/workdir:
```python
from swerex.deployment.config import Resources
cfg.resources = Resources(cpus=1.5, memory="1g", shm_size=134217728, ulimits={"nofile": "262144:262144"})
cfg.user = "1000:1000"
cfg.workdir = "/workspace"
```

Entrypoint/cmd behavior:
```python
cfg.command_mode = "append"
cfg.cmd = ["echo", "Container started"]
# Will run SWE‑ReX startup then 'echo ...'
# Use command_mode="override" and entrypoint/cmd to fully replace the start command
```

## Patterns

### 1) Socket passthrough (recommended)
Run the SWE‑ReX container with the host’s Docker socket mounted:
```
docker run --rm -it \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -p 8880:8880 \
  your-swerex-image
```
Use `engine="sdk"` and `docker_endpoint="unix:///var/run/docker.sock"`. No docker CLI is needed in the SWE‑ReX container.

Security note: mounting the host docker.sock grants broad control over the host.

### 2) Docker-in-Docker (DinD) as a sibling service
Start a DinD daemon and connect to it over TCP:
```
docker network create rexnet
docker run --privileged --name dind --network rexnet -d \
  -e DOCKER_TLS_CERTDIR= \
  docker:24-dind --host=tcp://0.0.0.0:2375
docker run --rm -it --network rexnet \
  -p 8880:8880 \
  -e DOCKER_HOST=tcp://dind:2375 \
  your-swerex-image
```
Config:
```python
cfg = DockerDeploymentConfig(
    image="swe-rex-test:latest",
    engine="sdk",
    docker_endpoint="tcp://dind:2375",
)
```

### 3) Remote daemon over SSH or TCP with TLS
- SSH:
  ```
  cfg = DockerDeploymentConfig(
      image="swe-rex-test:latest",
      engine="sdk",
      docker_endpoint="ssh://user@remote-host",
      docker_use_ssh=True,
  )
  ```
- TCP with TLS:
  ```
  cfg = DockerDeploymentConfig(
      image="swe-rex-test:latest",
      engine="sdk",
      docker_endpoint="tcp://remote:2376",
      docker_tls=True,
      docker_cert_path="/path/to/certs",  # contains ca.pem, cert.pem, key.pem
  )
  ```

## docker-compose example (DinD)

```yaml
services:
  swerex:
    image: your-swerex-image
    environment:
      - DOCKER_HOST=tcp://dind:2375    # not needed if using unix socket
    ports:
      - "8880:8880"
    depends_on:
      - dind
    # For socket passthrough, alternatively:
    # volumes:
    #   - /var/run/docker.sock:/var/run/docker.sock

  dind:
    image: docker:24-dind
    privileged: true
    environment:
      DOCKER_TLS_CERTDIR: ""
    ports:
      - "2375:2375"
```

## Notes on build and run behavior

- If `python_standalone_dir` is set, SWE‑ReX generates a temporary Dockerfile that layers a standalone Python on top of your base image and installs the SWE‑ReX remote binary.
  - SDK engine performs an in-memory tar build via the Docker API.
  - CLI engine uses `docker build - < Dockerfile` style (stdin) as before.
- Run semantics:
  - Ports are controlled via `ports` (with support for ephemeral host ports) or `network_mode="host"`.
  - Container name is auto-generated or can be provided via `container_name`.
  - Command starts the SWE‑ReX remote server and performs a pipx fallback if not present in the image, unless `command_mode="override"`.
  - `remove_container=True` sets auto-remove where supported.
- Remote runtime connection:
  - The host used by the client defaults to `runtime_host="auto"` and is inferred from the Docker endpoint/DOCKER_HOST.
  - When using ephemeral ports (host_port=None), SWE‑ReX inspects the container to discover the assigned host port before connecting.

## Troubleshooting

- SDK engine cannot connect:
  - Ensure a reachable daemon: mount `/var/run/docker.sock`, use a DinD service on TCP, or a remote daemon.
  - Verify `docker_endpoint` (e.g., `unix:///var/run/docker.sock`, `tcp://dind:2375`).
- Permission denied on `/var/run/docker.sock`:
  - Adjust container permissions and group mappings, or use a TCP endpoint (DinD/remote).
- CLI engine not found:
  - Install docker/podman CLI in the image or switch to `engine="sdk"`.
- Platform mismatch:
  - Use `platform="linux/amd64"` or provide `--platform` in `docker_args` (CLI mode).

## Installation of SDK extra

The SDK engine requires the `docker` Python package. Install via:
```
pip install "swe-rex[docker]"
```

## Accessing the runtime over Docker networks (no host ports)

If your SWE‑ReX client runs as a container on the same Docker daemon, you can avoid publishing host ports and connect by container DNS name on a user-defined network.

New fields in `DockerDeploymentConfig`:
- runtime_access_mode: "auto" | "host" | "network"
  - "network": do not publish ports; connect with Docker DNS
  - "host": publish ports (or use host network)
  - "auto": defaults to "host" unless no ports and a network is provided
- runtime_network_host: Optional[str] — the DNS name to use when in "network" mode
- runtime_host_scheme: "http" | "https" (default "http")
- network_aliases: dict[str, list[str]] — per-network aliases

Example (client and runtime on the same user network, no host ports):
```python
from swerex.deployment.config import DockerDeploymentConfig

cfg = DockerDeploymentConfig(
    image="python:3.11",
    networks=["rexnet"],
    network_aliases={"rexnet": ["rex"]},
    container_name="rex-runtime",
    runtime_container_port=8000,
    runtime_access_mode="network",
    runtime_network_host="rex",       # resolve to alias within rexnet
    ports=[],                         # no -p published
)
# The client container must also join the same "rexnet" to resolve "rex".
```

Notes:
- CLI engine: first network is attached via `--network`. Additional networks are connected after start with `docker network connect`. Aliases are applied via `--network-alias` (primary) and `docker network connect --alias` (others).
- SDK engine: first network is passed via `network=...` at run and additional networks are connected via `client.networks.get(net).connect(container, aliases=[...])`.
- In network mode, the runtime endpoint becomes: `<runtime_host_scheme>://<runtime_network_host or alias or container_name>:<runtime_container_port>`.
- Ensure the SWE‑ReX client is attached to the same network to resolve the DNS name.
```
